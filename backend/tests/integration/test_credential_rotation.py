"""Credential rotation end to end on PostgreSQL: the CLI as ``bfx_operator``, the worker as
``bfx_bot``.

Production's host setup: ``bfx_bot`` holds the owner's default DML on new tables, the CLI's
role has no default privileges and no schema grant before the migrations. So every read and
write below is one a migration granted (or, for the worker on the credentials, accounts,
runs and deployments, the existing grants ``1db83367918c`` relies on). Bitfinex is a fake.
"""
from __future__ import annotations

import asyncio
import io
import json
import logging
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from bfx_funding_bot.apps import operator_cli
from bfx_funding_bot.core.crypto import encrypt_secret_with_aad
from bfx_funding_bot.external.bitfinex.auth_rest import KeyPermissions
from bfx_funding_bot.external.bitfinex.errors import BitfinexAPIError
from bfx_funding_bot.modules.accounts.vault import load_account_credentials
from bfx_funding_bot.modules.execution.credential_rotation import CredentialRotationWorker
from bfx_funding_bot.modules.execution.protocols import AccountContext, Credentials
from tests.pg_templates import alembic, stamp_realm

from .test_ledger_schema_roles import _prepare

pytestmark = pytest.mark.integration

ROLE = "bfx_operator"
PASSWORD = "operator-rotation-test"
OPERATOR = "operator-1"
TAILNET = "4182486213003443"
ACCOUNT = UUID("aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee")
KEK = bytes(range(32))
OLD_KEY, OLD_SECRET = "FAKE-OLD-KEY-000000", "FAKE-OLD-SECRET-not-real-000000"
NEW_KEY, NEW_SECRET = "FAKE-NEW-KEY-111111", "FAKE-NEW-SECRET-not-real-111111"
NOW_MS = 1_800_000_000_000
# The run started long before the worker's clock: past BOT_SETTLE_WINDOW.
RUN_STARTED_MS = 1
FUNDING_ONLY = KeyPermissions(scopes={"funding": (True, True), "withdraw": (False, False)})


def _build(url: str) -> None:
    _prepare(url)
    engine = create_engine(url)
    with engine.begin() as conn:
        conn.exec_driver_sql(f"DO $$ BEGIN IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname='{ROLE}') "
                             f"THEN CREATE ROLE {ROLE}; END IF; END $$")
        conn.exec_driver_sql(f"ALTER ROLE {ROLE} LOGIN PASSWORD '{PASSWORD}'")
        conn.exec_driver_sql(f"REVOKE ALL ON SCHEMA public FROM {ROLE}")
    engine.dispose()
    alembic(url, "upgrade", "head")
    stamp_realm(url, "ci")


@dataclass
class Db:
    url: str
    owner: Any
    old_credential: UUID
    run_id: UUID


@pytest.fixture
def db(pg_templates, pg_clone):
    url = pg_clone(pg_templates.template("credential_rotation_roles", _build))
    owner = create_engine(url)
    envelope = encrypt_secret_with_aad(OLD_SECRET, aad=str(ACCOUNT), kek=KEK)
    run_id = uuid4()
    with owner.begin() as conn:
        conn.execute(text("INSERT INTO exchange_accounts(id, venue, label) VALUES (:a, 'bitfinex', 'x')"),
                     {"a": ACCOUNT})
        conn.execute(text("INSERT INTO exchange_account_memberships(exchange_account_id, user_id, role) "
                          "VALUES (:a, :u, 'owner')"), {"a": ACCOUNT, "u": OPERATOR})
        old = conn.scalar(text(
            "INSERT INTO exchange_account_credentials (exchange_account_id, venue, label, api_key, "
            "secret_ciphertext, secret_nonce, wrapped_dek, dek_nonce, key_version, "
            "lifecycle_status, verified_at) VALUES (:a, 'bitfinex', 'old', :k, :c, :n, :w, :d, "
            ":v, 'active', now()) RETURNING id"),
            {"a": ACCOUNT, "k": OLD_KEY, "c": envelope.secret_ciphertext,
             "n": envelope.secret_nonce, "w": envelope.wrapped_dek, "d": envelope.dek_nonce,
             "v": envelope.key_version})
        conn.execute(text(
            "INSERT INTO bot_runs (run_id, exchange_account_id, deployment_environment, "
            "started_at_ms, host_name, pid) VALUES (:r, :a, 'ci', 1, 'test', 1)"),
            {"r": run_id, "a": ACCOUNT})
    try:
        yield Db(url, owner, old, run_id)
    finally:
        owner.dispose()


@pytest.fixture
def operator_env(db, monkeypatch) -> None:
    url = make_url(db.url).set(drivername="postgresql", username=ROLE, password=PASSWORD)
    for key, value in {
        "DATABASE_URL": url.render_as_string(hide_password=False),
        "BFX_EXCHANGE_ACCOUNT_ID": str(ACCOUNT),
        "BFX_OPERATOR_USER_ID": OPERATOR,
        "BFX_OPERATOR_TAILNET_USER_ID": TAILNET,
        "BFX_OPERATOR_TAILNET_IDENTITY": TAILNET,
        "BFX_DEPLOYMENT_ENV": "ci",
    }.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("BFX_PHASE", raising=False)


class Stdin(io.StringIO):
    def isatty(self) -> bool:
        return False


async def cli(*argv: str, stdin: str = "") -> tuple[int, dict[str, Any], str]:
    out, log = io.StringIO(), io.StringIO()
    code = await asyncio.to_thread(operator_cli.main, list(argv), out=out, log=log,
                                   stdin=Stdin(stdin))
    return code, json.loads(out.getvalue()), log.getvalue()


async def rotate(key: str = NEW_KEY, secret: str = NEW_SECRET) -> tuple[int, dict[str, Any], str]:
    return await cli("rotate-credential", "--reason", "scheduled rotation", "--label", "new",
                     "--no-wait", stdin=f"{key}\n{secret}\n")


@dataclass
class FakeClient:
    perms: KeyPermissions | BaseException = FUNDING_ONLY
    user_id: int = 4242
    calls: list[str] = field(default_factory=list)

    async def get_key_permissions(self, *, ctx: AccountContext) -> KeyPermissions:
        self.calls.append("permissions")
        if isinstance(self.perms, BaseException):
            raise self.perms
        return self.perms

    async def get_user_id(self, *, ctx: AccountContext) -> int:
        self.calls.append("user_id")
        return self.user_id


@dataclass
class Bot:
    worker: CredentialRotationWorker
    client: FakeClient
    restarts: list[int]
    authorized: set[str]
    engine: Any
    factory: Any


def _as(url: str, role: str) -> tuple[Any, Any]:
    engine = create_async_engine(url.replace("+psycopg", "+asyncpg"))

    @event.listens_for(engine.sync_engine, "connect")
    def _set_role(dbapi_connection, _record) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute(f"SET ROLE {role}")
        cursor.close()

    return engine, async_sessionmaker(engine, expire_on_commit=False)


@pytest.fixture
async def bot(db):
    engine, factory = _as(db.url, "bfx_bot")
    client, restarts, authorized = FakeClient(), [], {OPERATOR}

    async def authority(session, *, account_id, user) -> bool:
        return user in authorized

    worker = CredentialRotationWorker(
        session_factory=factory, account_id=ACCOUNT, environment="ci", authority=authority,
        client_factory=lambda: client, current_client=FakeClient(),
        current_ctx=AccountContext(account_id=str(ACCOUNT),
                                   credentials=Credentials(OLD_KEY, OLD_SECRET)),
        kek_loader=lambda: KEK, restart=lambda: restarts.append(1), clock=lambda: NOW_MS)
    worker.bind_run(db.run_id, RUN_STARTED_MS)
    # As bfx_bot: the recipient insert is this role's own grant.
    await worker.idle()
    assert worker.recipient_id is not None
    try:
        yield Bot(worker, client, restarts, authorized, engine, factory)
    finally:
        await engine.dispose()


def _credentials(db: Db) -> list[tuple[str, str, UUID | None]]:
    with db.owner.connect() as conn:
        return sorted(tuple(r) for r in conn.execute(text(
            "SELECT api_key, lifecycle_status, operator_request_id FROM exchange_account_credentials")))


def _plaintext_anywhere(db: Db, needle: str) -> list[str]:
    """Every text and bytea column of every public table that holds ``needle``."""
    found = []
    with db.owner.connect() as conn:
        columns = conn.execute(text(
            "SELECT table_name, column_name, data_type FROM information_schema.columns "
            "WHERE table_schema = 'public' AND data_type IN ('text', 'bytea', 'jsonb', 'json', "
            "'character varying')")).all()
        for table, column, kind in columns:
            value = (f"position(convert_to(:n, 'UTF8') in \"{column}\") > 0" if kind == "bytea"
                     else f"\"{column}\"::text LIKE '%' || :n || '%'")
            if conn.scalar(text(f'SELECT count(*) FROM public."{table}" WHERE {value}'),
                           {"n": needle}):
                found.append(f"{table}.{column}")
    return found


@pytest.mark.asyncio
async def test_an_applied_rotation_is_what_the_next_boot_loads(db, operator_env, bot, caplog) -> None:
    with caplog.at_level(logging.DEBUG):
        code, queued, log = await rotate()
        assert (code, queued["state"], queued["replaces_credential_id"]) == (
            0, "requested", str(db.old_credential))
        assert "sealed_credential" not in queued
        # Queued: only the sealed form is in the outbox.
        assert _plaintext_anywhere(db, NEW_SECRET) == []
        assert log == (f"operator_request request_id={queued['request_id']} "
                       f"action=rotate_credential tailnet_identity={TAILNET}\n")
        # One pending rotation at a time, refused before the outbox.
        assert (await rotate())[:2] == (3, {"error": "request_pending", "kind": "conflict"})
        assert await bot.worker.tick() is True
        code, applied, _ = await cli("request", "credential", queued["request_id"])
    request_id = UUID(queued["request_id"])
    assert (code, applied["state"]) == (0, "applied")
    (new_key, new_status, new_request), (old_key, old_status, old_request) = _credentials(db)
    assert (new_key, new_status, new_request) == (NEW_KEY, "active", request_id)
    assert (old_key, old_status, old_request) == (OLD_KEY, "retired", None)
    assert bot.restarts == [1]
    # The boot path, as the bot's role.
    async with bot.factory() as session:
        loaded = await load_account_credentials(session, exchange_account_id=ACCOUNT, kek=KEK)
    assert (loaded.api_key, loaded.api_secret) == (NEW_KEY, NEW_SECRET)
    code, view, _ = await cli("credential")
    assert code == 0
    assert view["credential"]["label"] == "new"
    assert view["credential"]["operator_request_id"] == str(request_id)
    assert applied["outcome_reason"] == f"rotated: credential {view['credential']['id']}"
    assert view["latest_request"]["request_id"] == str(request_id)
    # The secret is nowhere in the database (the vault holds it encrypted), the log or output.
    for needle in (NEW_SECRET, OLD_SECRET):
        assert _plaintext_anywhere(db, needle) == []
        assert needle not in caplog.text
        assert needle not in json.dumps([queued, applied, view]) + log


@pytest.mark.asyncio
async def test_a_refused_key_keeps_the_old_credential(db, operator_env, bot) -> None:
    bot.client.perms = BitfinexAPIError(401, "apikey: invalid")
    code, queued, _ = await rotate()
    assert code == 0
    assert await bot.worker.tick() is True
    code, view, _ = await cli("request", "credential", queued["request_id"])
    assert (view["state"], view["outcome_reason"]) == ("rejected", "invalid_credentials")
    assert _credentials(db) == [(OLD_KEY, "active", None)]
    assert bot.restarts == []


@pytest.mark.asyncio
async def test_a_changed_active_credential_is_rejected(db, operator_env, bot) -> None:
    _, queued, _ = await rotate()
    with db.owner.begin() as conn:
        conn.exec_driver_sql("UPDATE exchange_account_credentials SET lifecycle_status = 'retired'")
    assert await bot.worker.tick() is True
    view = (await cli("request", "credential", queued["request_id"]))[1]
    assert (view["state"], view["outcome_reason"]) == ("rejected", "active_credential_changed")
    assert bot.restarts == []


@pytest.mark.asyncio
async def test_an_unauthorized_operator_reaches_no_venue(db, operator_env, bot) -> None:
    _, queued, _ = await rotate()
    bot.authorized.clear()
    assert await bot.worker.tick() is True
    view = (await cli("request", "credential", queued["request_id"]))[1]
    assert (view["state"], view["outcome_reason"]) == ("rejected", "operator_not_authorized")
    assert bot.client.calls == []
    assert _credentials(db) == [(OLD_KEY, "active", None)]


@pytest.mark.asyncio
async def test_without_a_running_bot_nothing_is_queued(db, operator_env, bot) -> None:
    with db.owner.begin() as conn:
        conn.exec_driver_sql("UPDATE bot_runs SET end_reason = 'clean_stop', end_recorded_at_ms = 2")
    assert (await rotate())[:2] == (3, {"error": "bot_not_running", "kind": "unavailable"})
    with db.owner.connect() as conn:
        assert conn.scalar(text("SELECT count(*) FROM credential_rotation_requests")) == 0
    # A newer run that never published a key is not running for the CLI either.
    with db.owner.begin() as conn:
        conn.execute(text(
            "INSERT INTO bot_runs (run_id, exchange_account_id, deployment_environment, "
            "started_at_ms, host_name, pid) VALUES (:r, :a, 'ci', 5, 'test', 2)"),
            {"r": uuid4(), "a": ACCOUNT})
    assert (await rotate())[:2] == (3, {"error": "bot_not_running", "kind": "unavailable"})


@pytest.mark.asyncio
async def test_history_rows_cannot_be_rewritten(db, operator_env, bot) -> None:
    assert (await rotate())[0] == 0
    for statement, message in (
            ("UPDATE credential_rotation_requests SET reason = 'rewritten'",
             "immutable credential rotation request"),
            ("UPDATE credential_rotation_requests SET sealed_credential = '\\x00'",
             "immutable credential rotation request"),
            ("DELETE FROM credential_rotation_requests", "immutable credential rotation history"),
            ("UPDATE credential_rotation_recipients SET public_key = '\\x00'",
             "immutable credential rotation history"),
            ("DELETE FROM credential_rotation_recipients", "immutable credential rotation history"),
            ("TRUNCATE credential_rotation_recipients CASCADE",
             "immutable credential rotation history"),
    ):
        # The owner: the triggers bind every role.
        with db.owner.begin() as conn, pytest.raises(Exception, match=message):
            conn.exec_driver_sql(statement)
    assert await bot.worker.tick() is True
    with db.owner.begin() as conn, pytest.raises(Exception, match="invalid credential rotation"):
        conn.exec_driver_sql("UPDATE credential_rotation_requests SET state = 'failed', "
                             "outcome_reason = 'again'")


@pytest.mark.asyncio
async def test_a_request_sealed_to_a_previous_boot_is_stale(db, operator_env, bot) -> None:
    _, queued, _ = await rotate()
    # The bot restarted: the new run has its own key pair, the old private key is gone.
    restarted = CredentialRotationWorker(
        session_factory=bot.factory, account_id=ACCOUNT, environment="ci",
        authority=bot.worker.authority, client_factory=lambda: bot.client,
        current_client=FakeClient(), current_ctx=bot.worker.current_ctx,
        kek_loader=lambda: KEK, restart=lambda: None, clock=lambda: NOW_MS)
    restarted.bind_run(db.run_id, RUN_STARTED_MS)
    await restarted.idle()
    assert await restarted.tick() is True
    view = (await cli("request", "credential", queued["request_id"]))[1]
    assert (view["state"], view["outcome_reason"]) == ("rejected", "recipient_key_stale")
    assert bot.client.calls == []

