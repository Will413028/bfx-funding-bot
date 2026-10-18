"""``CredentialRotationWorker`` on SQLite with fake Bitfinex clients.

The CLI side is the real service (``operator_commands.request_credential_rotation``); the
worker is the real ``OperatorRequestWorker`` driver. PostgreSQL roles, triggers and the
operator CLI as ``bfx_operator`` are ``tests/integration/test_credential_rotation.py``.

Mutation checks (one at a time; revert after each):

* ``apply`` calls ``rotate_active_credential`` on a ``Refused`` verdict:
  ``test_every_refusal_is_rejected_and_keeps_the_old_credential`` fails.
* ``committed`` calls ``restart`` on a rejection: same test, and the failed one, fail.
* the user id comparison is removed: the ``credential_account_mismatch`` case fails.
* ``_verify`` compares with anything but the replaced row's stored id (or goes on when it has
  none): ``test_a_stored_user_id_of_another_account_is_a_mismatch`` or
  ``test_a_replaced_credential_without_a_stored_id_is_refused_before_bitfinex_is_asked`` fails.
"""
from __future__ import annotations

import importlib
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from bfx_funding_bot.core.crypto import encrypt_secret_with_aad
from bfx_funding_bot.core.db import Base
from bfx_funding_bot.external.bitfinex.auth_rest import KeyPermissions
from bfx_funding_bot.external.bitfinex.errors import BitfinexAPIError
from bfx_funding_bot.modules.accounts.rotation_tables import CredentialRotationRequestRow
from bfx_funding_bot.modules.accounts.tables import ExchangeAccount, ExchangeAccountCredential
from bfx_funding_bot.modules.accounts.vault import load_account_credentials
from bfx_funding_bot.modules.deployments.tables import DeploymentRow
from bfx_funding_bot.modules.execution import credential_rotation as rotation
from bfx_funding_bot.modules.execution import operator_commands as commands
from bfx_funding_bot.modules.execution.credential_rotation import (
    BOT_SETTLE_WINDOW,
    DEPLOYMENT_IN_PROGRESS_WINDOW,
    CredentialRotationWorker,
    PreparedRotation,
    Verified,
    _Secret,
)
from bfx_funding_bot.modules.execution.operator_commands import OperatorContext
from bfx_funding_bot.modules.execution.protocols import AccountContext
from bfx_funding_bot.modules.observability.tables import BotRunRow

ACCOUNT = UUID("aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee")
ENV = "ci"
OPERATOR = "operator-1"
KEK = bytes(range(32))
NOW_MS = 1_800_000_000_000
NOW = datetime.fromtimestamp(NOW_MS / 1000, UTC)
# This run is past BOT_SETTLE_WINDOW unless a test says otherwise.
RUN_STARTED_MS = NOW_MS - 3_600_000
OLD_KEY, OLD_SECRET = "FAKE-OLD-KEY-000000", "FAKE-OLD-SECRET-not-real-000000"
NEW_KEY, NEW_SECRET = "FAKE-NEW-KEY-111111", "FAKE-NEW-SECRET-not-real-111111"
USER_ID = 4242
FUNDING_ONLY = KeyPermissions(scopes={"funding": (True, True), "account": (True, False),
                                      "withdraw": (False, False)})
# Every table on Base.metadata, foreign-key targets included.
importlib.import_module("bfx_funding_bot.apps.schema")
CONTEXT = OperatorContext(exchange_account_id=ACCOUNT, user_id=OPERATOR, role="owner",
                          lifecycle_status="active", deployment_environment=ENV)


@dataclass
class FakeClient:
    """A Bitfinex auth client: answers or raises, and counts what it was asked."""

    perms: KeyPermissions | BaseException = FUNDING_ONLY
    user_id: int | BaseException = USER_ID
    calls: list[str] = field(default_factory=list)
    keys: list[str] = field(default_factory=list)

    async def get_key_permissions(self, *, ctx: AccountContext) -> KeyPermissions:
        self.calls.append("permissions")
        self.keys.append(ctx.credentials.api_key)
        if isinstance(self.perms, BaseException):
            raise self.perms
        return self.perms

    async def get_user_id(self, *, ctx: AccountContext) -> int:
        self.calls.append("user_id")
        self.keys.append(ctx.credentials.api_key)
        if isinstance(self.user_id, BaseException):
            raise self.user_id
        return self.user_id


@dataclass
class Harness:
    factory: async_sessionmaker[AsyncSession]
    worker: CredentialRotationWorker
    new_client: FakeClient
    restarts: list[int]
    alerts: list[tuple[str, dict[str, Any]]]
    old_credential: UUID
    authorized: set[str]


@pytest.fixture
async def harness(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Harness]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:",
                                 connect_args={"check_same_thread": False},
                                 poolclass=StaticPool)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    run_id = uuid4()
    envelope = encrypt_secret_with_aad(OLD_SECRET, aad=str(ACCOUNT), kek=KEK)
    async with factory.begin() as session:
        session.add(ExchangeAccount(id=ACCOUNT, venue="bitfinex", label="test",
                                    lifecycle_status="active"))
        await session.flush()
        old = ExchangeAccountCredential(
            exchange_account_id=ACCOUNT, venue="bitfinex", label="old", api_key=OLD_KEY,
            secret_ciphertext=envelope.secret_ciphertext, secret_nonce=envelope.secret_nonce,
            wrapped_dek=envelope.wrapped_dek, dek_nonce=envelope.dek_nonce,
            key_version=envelope.key_version, lifecycle_status="active",
            verified_at=NOW - timedelta(days=30), bitfinex_user_id=USER_ID)
        session.add(old)
        session.add(BotRunRow(run_id=run_id, exchange_account_id=ACCOUNT,
                              deployment_environment=ENV, started_at_ms=RUN_STARTED_MS,
                              host_name="test", pid=1))
        await session.flush()
        old_id = old.id
    recorded: list[tuple[str, dict[str, Any]]] = []

    def emit(event: str, *, level: str | None = None, **fields: Any) -> None:
        recorded.append((event, {"level": level, **fields}))

    monkeypatch.setattr(rotation.alerts, "emit", emit)
    authorized = {OPERATOR}

    async def authority(session: AsyncSession, *, account_id: UUID, user: str) -> bool:
        return user in authorized

    new_client, restarts = FakeClient(), []
    worker = CredentialRotationWorker(
        session_factory=factory, account_id=ACCOUNT, environment=ENV, authority=authority,
        client_factory=lambda: new_client,
        kek_loader=lambda: KEK, restart=lambda: restarts.append(1), clock=lambda: NOW_MS)
    worker.bind_run(run_id, RUN_STARTED_MS)
    await worker.idle()
    try:
        yield Harness(factory, worker, new_client, restarts, recorded, old_id, authorized)
    finally:
        await engine.dispose()


async def _queue(h: Harness, *, key: str = NEW_KEY, secret: str = NEW_SECRET) -> UUID:
    async with h.factory.begin() as session:
        return await commands.request_credential_rotation(
            session, CONTEXT, api_key=key, api_secret=secret, label="rotated",
            reason="scheduled rotation", now_ms=NOW_MS)


async def _settle(h: Harness, request_id: UUID) -> CredentialRotationRequestRow:
    assert await h.worker.tick() is True
    async with h.factory() as session:
        row = await session.get(CredentialRotationRequestRow, request_id)
        assert row is not None
        return row


async def _credentials(h: Harness) -> list[tuple[str, str]]:
    async with h.factory() as session:
        rows = (await session.execute(select(
            ExchangeAccountCredential.api_key, ExchangeAccountCredential.lifecycle_status,
        ).order_by(ExchangeAccountCredential.created_at, ExchangeAccountCredential.api_key))).all()
    return sorted((r.api_key, r.lifecycle_status) for r in rows)


async def _deployment(h: Harness, outcome: str, started_at: datetime, attempt: UUID) -> None:
    async with h.factory.begin() as session:
        session.add(DeploymentRow(
            attempt_id=attempt, started_at=started_at,
            finished_at=None if outcome == "started" else started_at + timedelta(minutes=2),
            source_revision="0" * 40, backend_digest="sha256:" + "0" * 64,
            frontend_digest="sha256:" + "0" * 64, migrations_applied=False, outcome=outcome,
            detail=""))


@pytest.mark.asyncio
async def test_an_applied_rotation_swaps_the_active_credential_and_restarts(harness) -> None:
    h = harness
    request_id = await _queue(h)
    row = await _settle(h, request_id)
    assert await _credentials(h) == [(NEW_KEY, "active"), (OLD_KEY, "retired")]
    # What the next boot loads.
    async with h.factory() as session:
        loaded = await load_account_credentials(session, exchange_account_id=ACCOUNT, kek=KEK)
        new = (await session.execute(select(
            ExchangeAccountCredential.id, ExchangeAccountCredential.operator_request_id).where(
            ExchangeAccountCredential.api_key == NEW_KEY))).one()
        old_request = await session.scalar(select(ExchangeAccountCredential.operator_request_id)
                                           .where(ExchangeAccountCredential.api_key == OLD_KEY))
    assert (loaded.api_key, loaded.api_secret) == (NEW_KEY, NEW_SECRET)
    assert (row.state, row.outcome_reason) == ("applied", f"rotated: credential {new.id}")
    # The effect names the request; the replaced credential keeps what it had.
    assert (new.operator_request_id, old_request) == (request_id, None)
    assert h.restarts == [1]
    (event, fields), = h.alerts
    assert event == "credential_rotation_applied" and fields["level"] == "warning"
    assert set(fields) == {"level", "request_id", "replaces_credential_id", "credential_id",
                           "reason"}
    assert fields["replaces_credential_id"] == str(h.old_credential)
    assert fields["credential_id"] == str(new.id)
    # The new key on its own client, compared with the replaced row's stored user id.
    assert h.new_client.calls == ["permissions", "user_id"]
    assert set(h.new_client.keys) == {NEW_KEY}
    # The new row stores the Bitfinex user its key belongs to.
    assert await _stored_user_id(h, NEW_KEY) == USER_ID


@pytest.mark.asyncio
async def test_the_restart_happens_even_when_the_alert_fails(harness, monkeypatch) -> None:
    h = harness

    def broken(event: str, **fields: Any) -> None:
        raise RuntimeError("alert transport down")

    monkeypatch.setattr(rotation.alerts, "emit", broken)
    row = await _settle(h, await _queue(h))
    assert row.state == "applied" and h.restarts == [1]


@pytest.mark.asyncio
async def test_an_unauthorized_operator_is_rejected_before_anything_is_opened(harness) -> None:
    h = harness
    request_id = await _queue(h)
    h.authorized.clear()
    row = await _settle(h, request_id)
    assert (row.state, row.outcome_reason) == ("rejected", "operator_not_authorized")
    assert h.new_client.calls == []
    assert await _credentials(h) == [(OLD_KEY, "active")]
    assert h.restarts == []


async def _make_stale(h: Harness) -> None:
    # The run that published the recipient restarted: this process has a new key pair.
    h.worker._recipient = None
    await h.worker.idle()


async def _corrupt(h: Harness) -> None:
    async with h.factory.begin() as session:
        await session.execute(text(
            "UPDATE credential_rotation_requests SET sealed_credential = :b"), {"b": b"\x01" * 90})


@pytest.mark.parametrize(("case", "code"), [
    ("stale", "recipient_key_stale"),
    ("corrupt", "sealed_payload_invalid"),
    ("reused", "credential_reused"),
    ("unauthenticated", "invalid_credentials"),
    ("no_funding_write", "funding_write_required"),
    ("withdraw", "withdraw_must_be_disabled"),
    ("orders_write", "unexpected_write_scope:orders"),
    ("identity_unavailable", "account_identity_unavailable"),
    ("other_account", "credential_account_mismatch"),
    ("deploying", "deployment_in_progress"),
])
@pytest.mark.asyncio
async def test_every_refusal_is_rejected_and_keeps_the_old_credential(harness, case, code) -> None:
    """Rejected, not failed: each verdict is decided in ``prepare`` and raised in ``apply``."""
    h = harness
    key = OLD_KEY if case == "reused" else NEW_KEY
    request_id = await _queue(h, key=key)
    if case == "stale":
        await _make_stale(h)
    elif case == "corrupt":
        await _corrupt(h)
    elif case == "unauthenticated":
        h.new_client.perms = BitfinexAPIError(401, "apikey: invalid")
    elif case == "no_funding_write":
        h.new_client.perms = KeyPermissions(scopes={"funding": (True, False)})
    elif case == "withdraw":
        h.new_client.perms = KeyPermissions(scopes={"funding": (True, True),
                                                    "withdraw": (True, True)})
    elif case == "orders_write":
        h.new_client.perms = KeyPermissions(scopes={"funding": (True, True),
                                                    "orders": (True, True)})
    elif case == "identity_unavailable":
        h.new_client.user_id = BitfinexAPIError(403, "permission: invalid")
    elif case == "other_account":
        h.new_client.user_id = USER_ID + 1
    elif case == "deploying":
        await _deployment(h, "started", NOW - timedelta(minutes=1), uuid4())
    row = await _settle(h, request_id)
    assert (row.state, row.outcome_reason) == ("rejected", code)
    assert await _credentials(h) == [(OLD_KEY, "active")]
    assert h.restarts == []
    (event, fields), = h.alerts
    assert (event, fields["level"], fields["reason"]) == (
        "credential_rotation_rejected", "warning", code)


@pytest.mark.parametrize("status", [0, 429, 500, 503])
@pytest.mark.asyncio
async def test_a_transient_venue_error_fails_the_request_without_a_restart(harness, status) -> None:
    h = harness
    h.new_client.perms = BitfinexAPIError(status, "unavailable")
    row = await _settle(h, await _queue(h))
    assert (row.state, row.outcome_reason) == ("failed", "prepare_failed:BitfinexAPIError")
    assert await _credentials(h) == [(OLD_KEY, "active")]
    assert h.restarts == []
    (event, fields), = h.alerts
    assert (event, fields["level"]) == ("credential_rotation_failed", "critical")


@pytest.mark.asyncio
async def test_a_changed_active_credential_is_rejected(harness) -> None:
    h = harness
    request_id = await _queue(h)
    # Someone else's rotation landed first: the request's credential is no longer active.
    async with h.factory.begin() as session:
        await session.execute(text(
            "UPDATE exchange_account_credentials SET lifecycle_status = 'retired'"))
    row = await _settle(h, request_id)
    assert (row.state, row.outcome_reason) == ("rejected", "active_credential_changed")
    assert h.restarts == []


@pytest.mark.parametrize(("deployments", "expected"), [
    # The newest row is a started one inside the window: settling now would restart the bot.
    ([("started", timedelta(minutes=1))], "rejected"),
    ([("started", DEPLOYMENT_IN_PROGRESS_WINDOW - timedelta(seconds=1))], "rejected"),
    # An interrupted deploy (no terminal row) older than the window no longer blocks.
    ([("started", DEPLOYMENT_IN_PROGRESS_WINDOW + timedelta(seconds=1))], "applied"),
    # A finished deploy keeps its started row; the newest row is the terminal one.
    ([("started", timedelta(minutes=3)), ("deployed", timedelta(minutes=3))], "applied"),
    ([], "applied"),
])
@pytest.mark.asyncio
async def test_a_deploy_in_progress_is_the_newest_started_row_inside_the_window(
        harness, deployments, expected) -> None:
    h = harness
    attempt = uuid4()
    for outcome, age in deployments:
        await _deployment(h, outcome, NOW - age, attempt)
    row = await _settle(h, await _queue(h))
    assert row.state == expected
    if expected == "rejected":
        assert row.outcome_reason == "deployment_in_progress"


@pytest.mark.asyncio
async def test_no_queue_without_a_running_bot_or_an_active_credential(harness) -> None:
    h = harness
    async with h.factory.begin() as session:
        await session.execute(text("UPDATE bot_runs SET end_reason = 'clean_stop', "
                                   "end_recorded_at_ms = 1"))
    with pytest.raises(commands.OperatorCommandError) as refused:
        await _queue(h)
    assert refused.value.code == "bot_not_running"
    async with h.factory.begin() as session:
        await session.execute(text("UPDATE bot_runs SET end_reason = NULL, "
                                   "end_recorded_at_ms = NULL"))
        await session.execute(text(
            "UPDATE exchange_account_credentials SET lifecycle_status = 'retired'"))
    with pytest.raises(commands.OperatorCommandError) as refused:
        await _queue(h)
    assert refused.value.code == "no_active_credential"
    async with h.factory() as session:
        assert (await session.execute(select(CredentialRotationRequestRow.request_id))).all() == []


@pytest.mark.asyncio
async def test_one_pending_rotation_at_a_time(harness) -> None:
    h = harness
    await _queue(h)
    with pytest.raises(commands.OperatorCommandError) as refused:
        await _queue(h)
    assert refused.value.code == "request_pending"


@pytest.mark.asyncio
async def test_the_recipient_is_published_once_and_a_failure_is_retried(harness, caplog) -> None:
    h = harness
    first = h.worker.recipient_id
    await h.worker.idle()
    assert h.worker.recipient_id == first is not None

    class Broken:
        def begin(self) -> Any:
            raise RuntimeError("database down")

    worker = CredentialRotationWorker(
        session_factory=Broken(), account_id=ACCOUNT, environment=ENV,
        authority=h.worker.authority, client_factory=lambda: h.new_client,
        kek_loader=lambda: KEK, restart=lambda: None, clock=lambda: NOW_MS)
    await worker.idle()  # no run bound yet: nothing to publish
    worker.bind_run(uuid4(), RUN_STARTED_MS)
    with caplog.at_level(logging.WARNING):
        await worker.idle()
    assert worker.recipient_id is None
    assert "credential_rotation_recipient_unpublished reason=RuntimeError" in caplog.text


@pytest.mark.asyncio
async def test_the_secret_is_in_no_repr_log_or_alert(harness, caplog) -> None:
    h = harness
    with caplog.at_level(logging.DEBUG):
        request_id = await _queue(h)
        prepared = await h.worker.prepare(request_id)
        assert isinstance(prepared, PreparedRotation) and isinstance(prepared.verdict, Verified)
        for rendered in (repr(prepared), str(prepared), repr(prepared.verdict),
                         f"{prepared.verdict.secret}"):
            assert NEW_SECRET not in rendered and NEW_KEY not in rendered
        await _settle(h, request_id)
    assert NEW_SECRET not in caplog.text and OLD_SECRET not in caplog.text
    assert NEW_SECRET not in repr(h.alerts)
    assert repr(_Secret(NEW_SECRET)) == "_Secret(***)"
    async with h.factory() as session:
        sealed = (await session.execute(select(
            CredentialRotationRequestRow.sealed_credential))).scalar_one()
    assert NEW_SECRET.encode() not in sealed and NEW_KEY.encode() not in sealed


# --- a bot that just started (cr-1) ----------------------------------------------------------

@pytest.mark.parametrize(("age", "expected"), [
    (timedelta(seconds=30), "rejected"),
    (BOT_SETTLE_WINDOW - timedelta(seconds=1), "rejected"),
    (BOT_SETTLE_WINDOW + timedelta(seconds=1), "applied"),
])
@pytest.mark.asyncio
async def test_a_bot_still_settling_after_its_start_refuses_to_restart(harness, age, expected) -> None:
    """A deploy's health check and settle window watch the bot that just started."""
    h = harness
    run_id = h.worker._run_id
    assert run_id is not None
    h.worker.bind_run(run_id, NOW_MS - int(age.total_seconds() * 1000))
    row = await _settle(h, await _queue(h))
    assert row.state == expected
    if expected == "rejected":
        assert row.outcome_reason == "bot_recently_started"
        assert h.new_client.calls == [] and h.restarts == []


# --- the venue's answer with HTTP 500 (cr-2) -------------------------------------------------

def _venue(status: int, raw: str | None) -> BitfinexAPIError:
    return BitfinexAPIError(status, "venue", raw=raw)


@pytest.mark.parametrize(("where", "error", "expected"), [
    ("permissions", _venue(500, '["error",10100,"apikey: digest invalid"]'),
     ("rejected", "invalid_credentials")),
    ("new_user", _venue(500, '["error",10100,"apikey: digest invalid"]'),
     ("rejected", "account_identity_unavailable")),
    ("permissions", _venue(500, '["error",11010,"ratelimit: error"]'),
     ("failed", "prepare_failed:BitfinexAPIError")),
    ("permissions", _venue(500, '["error",11000,"ready: invalid"]'),
     ("failed", "prepare_failed:BitfinexAPIError")),
    ("permissions", _venue(500, None), ("failed", "prepare_failed:BitfinexAPIError")),
    ("permissions", _venue(429, None), ("failed", "prepare_failed:BitfinexAPIError")),
    ("new_user", _venue(503, "<html>bad gateway</html>"),
     ("failed", "prepare_failed:BitfinexAPIError")),
])
@pytest.mark.asyncio
async def test_the_venue_refusing_with_http_500_is_an_answer(harness, where, error, expected) -> None:
    h = harness
    if where == "permissions":
        h.new_client.perms = error
    else:
        h.new_client.user_id = error
    row = await _settle(h, await _queue(h))
    assert (row.state, row.outcome_reason) == expected
    assert await _credentials(h) == [(OLD_KEY, "active")]
    assert h.restarts == []


# --- database errors carry no key (cr-5) -----------------------------------------------------

class UniqueViolationError(Exception):
    pass


def _db_error() -> IntegrityError:
    return IntegrityError("INSERT INTO exchange_account_credentials (api_key) VALUES ($1)",
                          {"api_key": NEW_KEY, "secret": NEW_SECRET}, UniqueViolationError("dup"))


@pytest.mark.asyncio
async def test_a_database_error_while_rotating_names_only_its_type(
        harness, monkeypatch, caplog) -> None:
    h = harness

    async def broken(*_: Any, **__: Any) -> Any:
        raise _db_error()

    monkeypatch.setattr(rotation, "rotate_active_credential", broken)
    # The level the bot logs at (aiosqlite's DEBUG lines echo every parameter).
    with caplog.at_level(logging.INFO):
        row = await _settle(h, await _queue(h))
    assert (row.state, row.outcome_reason) == ("failed", "credential_rotation_failed:UniqueViolationError")
    assert NEW_KEY not in caplog.text and NEW_SECRET not in caplog.text
    assert NEW_KEY not in repr(h.alerts)
    assert await _credentials(h) == [(OLD_KEY, "active")]


@pytest.mark.asyncio
async def test_a_database_error_while_preparing_names_only_its_type(
        harness, monkeypatch, caplog) -> None:
    h = harness

    async def broken(*_: Any, **__: Any) -> Any:
        raise _db_error()

    monkeypatch.setattr(h.worker, "_judge_locally", broken)
    # The level the bot logs at (aiosqlite's DEBUG lines echo every parameter).
    with caplog.at_level(logging.INFO):
        row = await _settle(h, await _queue(h))
    assert (row.state, row.outcome_reason) == ("failed", "prepare_failed:CredentialStoreError")
    assert NEW_KEY not in caplog.text and NEW_SECRET not in caplog.text
    assert "UniqueViolationError" in caplog.text


# --- a rotation whose restart was lost (cr-6) ------------------------------------------------

def _process_after(h: Harness, run_started_ms: int) -> CredentialRotationWorker:
    """Another worker of the same account, as a process whose run started then."""
    restarts = h.restarts
    worker = CredentialRotationWorker(
        session_factory=h.factory, account_id=ACCOUNT, environment=ENV,
        authority=h.worker.authority, client_factory=lambda: h.new_client,
        kek_loader=lambda: KEK, restart=lambda: restarts.append(2), clock=lambda: NOW_MS)
    worker.bind_run(uuid4(), run_started_ms)
    return worker


@pytest.mark.parametrize(("run_started_ms", "restarted"), [
    # The commit landed during this run but its restart never ran (lost acknowledgement).
    (RUN_STARTED_MS, True),
    # The rotation that caused this boot was applied before it started.
    (NOW_MS + 1, False),
])
@pytest.mark.asyncio
async def test_idle_catches_up_on_a_rotation_applied_during_this_run(
        harness, run_started_ms, restarted) -> None:
    h = harness
    assert (await _settle(h, await _queue(h))).state == "applied"
    assert h.restarts == [1]
    worker = _process_after(h, run_started_ms)
    await worker.idle()
    await worker.idle()  # once
    assert h.restarts == ([1, 2] if restarted else [1])


@pytest.mark.asyncio
async def test_the_restart_is_requested_once_per_process(harness) -> None:
    h = harness
    assert (await _settle(h, await _queue(h))).state == "applied"
    await h.worker.idle()
    assert h.restarts == [1]


# --- the stored Bitfinex user id (9c-2a; the only identity compared since 9c-2b) ------------

async def _store_user_id(h: Harness, user_id: int | None, *, key: str = OLD_KEY) -> None:
    async with h.factory.begin() as session:
        await session.execute(text(
            "UPDATE exchange_account_credentials SET bitfinex_user_id = :id WHERE api_key = :key"),
            {"id": user_id, "key": key})


async def _stored_user_id(h: Harness, key: str) -> int | None:
    async with h.factory() as session:
        return await session.scalar(select(ExchangeAccountCredential.bitfinex_user_id).where(
            ExchangeAccountCredential.api_key == key))


@pytest.mark.asyncio
async def test_a_stored_user_id_of_another_account_is_a_mismatch(harness) -> None:
    h = harness
    await _store_user_id(h, USER_ID + 1)
    row = await _settle(h, await _queue(h))
    assert (row.state, row.outcome_reason) == ("rejected", "credential_account_mismatch")
    assert await _credentials(h) == [(OLD_KEY, "active")]
    assert h.restarts == []


@pytest.mark.asyncio
async def test_a_replaced_credential_without_a_stored_id_is_refused_before_bitfinex_is_asked(
        harness) -> None:
    """Only a credential retired before the id was stored can lack one (an active row always
    has it): the identity is unavailable and no key, new or in use, is asked."""
    h = harness
    request_id = await _queue(h)
    async with h.factory.begin() as session:
        await session.execute(text("UPDATE exchange_account_credentials "
                                   "SET lifecycle_status = 'retired', bitfinex_user_id = NULL"))
    row = await _settle(h, request_id)
    assert (row.state, row.outcome_reason) == ("rejected", "account_identity_unavailable")
    assert h.new_client.calls == []
    assert h.restarts == []


@pytest.mark.asyncio
async def test_a_bot_in_recovery_mode_still_waits_out_its_settle_window(harness) -> None:
    """Recovery mode keeps BOT_SETTLE_WINDOW: a deploy settling the bot that just started
    must not see it restart."""
    h = harness
    run_id = h.worker._run_id
    assert run_id is not None
    h.worker.bind_run(run_id, NOW_MS - 60_000)
    row = await _settle(h, await _queue(h))
    assert (row.state, row.outcome_reason) == ("rejected", "bot_recently_started")
    assert h.new_client.calls == [] and h.restarts == []
