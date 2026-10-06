"""7d2a9c4e6b13 on real PostgreSQL roles: the web API queues a toggle, the bot applies it.

Production's default privileges hand every new table to the runtime roles, so
the fixture does the same and the migration must take back what it does not
mean to grant. Owner-only fixtures would pass whether or not the web API could
write policy, or the bot could rewrite more than ``enabled``.
"""
from __future__ import annotations

import asyncio
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from sqlalchemy import create_engine, event, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from bfx_funding_bot.modules.execution.capital_policy_control import CapitalPolicyRequestWorker
from bfx_funding_bot.modules.execution.capital_tables import CapitalPolicyRequestRow
from bfx_funding_bot.modules.execution.operator_requests import insert_request
from bfx_funding_bot.modules.ledger import Scope
from bfx_funding_bot.modules.ledger.wiring import build_policy_store, build_scope_lock
from bfx_funding_bot.modules.trading import CapitalPolicy, OfferEnvelope
from tests.pg_templates import stamp_realm

from .test_ledger_schema_roles import pre_switch_url
from .test_trading_state_migration import _alembic, _alembic_cli, _reset

pytestmark = pytest.mark.integration

_BEFORE = "5b1e7c9d2a40"
_A = UUID("00000000-0000-0000-0000-0000000000a1")
_SCOPE = Scope(_A, "ci")
_POLICY = CapitalPolicy(
    enabled=True, max_offer_amount=Decimal("200"),
    envelope=OfferEnvelope(min_period_days=2, max_period_days=2, max_open_offers=6,
                           rate_floor_ratio=Decimal("0.5"), min_rate_apr=Decimal("0.01")))


def _build_migrated(url: str) -> None:
    engine = create_engine(url)
    _reset(engine)
    engine.dispose()
    _alembic(url, "upgrade", "head")
    _alembic(url, "check")
    stamp_realm(url, "ci")


@pytest.fixture
def migrated(pg_templates, pg_clone):
    """A fresh copy of the upgraded database; the upgrade runs once per session."""
    url = pg_clone(pg_templates.template("capital_policy_roles_migrated", _build_migrated))
    engine = create_engine(url)
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO exchange_accounts(id, venue, label) VALUES (:a, 'bitfinex', 'x')"),
                     {"a": _A})
    try:
        yield url, engine
    finally:
        engine.dispose()


def _async(url: str, role: str | None = None):
    engine = create_async_engine(url.replace("+psycopg", "+asyncpg"))
    if role is not None:
        @event.listens_for(engine.sync_engine, "connect")
        def _set_role(dbapi_connection, _record) -> None:
            cursor = dbapi_connection.cursor()
            cursor.execute(f"SET ROLE {role}")
            cursor.close()
    return engine, async_sessionmaker(engine, expire_on_commit=False)


def _has(conn, sql: str, **params) -> bool:
    return bool(conn.scalar(text(sql), params))


def _request_sql(request_id: str = "00000000-0000-0000-0000-0000000000e1", action: str = "disable",
                 extra: tuple[str, str] = ("", ""), by: str = "operator") -> str:
    return (f"INSERT INTO capital_policy_requests (request_id, exchange_account_id, "
            f"deployment_environment, symbol, action, reason, requested_by, created_at_ms{extra[0]}) "
            f"VALUES ('{request_id}', '{_A}', 'ci', 'fUST', '{action}', 'x', '{by}', 1{extra[1]})")


def _operator(engine) -> None:
    """``operator``: an enrolled admin owning the account (public.operator_authorized)."""
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO exchange_account_memberships(exchange_account_id,user_id,role) "
                          "VALUES (:a,'operator','owner')"), {"a": _A})
        conn.exec_driver_sql('''INSERT INTO auth."user" (id,name,email,"emailVerified","createdAt",
            "updatedAt",role,banned,"twoFactorEnabled") VALUES ('operator','op','op@test.invalid',false,
            now(),now(),'admin',false,true)''')


async def _seed(url: str) -> None:
    engine, owner = _async(url)
    try:
        async with owner.begin() as session:
            await build_scope_lock().lock(session, _SCOPE)
            await build_policy_store(_SCOPE).apply_policy(
                session, symbol="fUST", policy=_POLICY, expected_revision=0, source={"test": True})
    finally:
        await engine.dispose()


def test_grants_are_exactly_the_outbox_split_and_the_toggle(migrated):
    _, engine = migrated
    table = "capital_policy_requests"
    with engine.connect() as conn:
        for column in CapitalPolicyRequestRow.REQUEST_COLUMNS:
            assert _has(conn, "SELECT has_column_privilege('bfx_webapi', :t, :c, 'INSERT')",
                        t=table, c=column)
            assert not _has(conn, "SELECT has_column_privilege('bfx_bot', :t, :c, 'UPDATE')",
                            t=table, c=column)
        for column in CapitalPolicyRequestRow.WORKER_COLUMNS:
            assert _has(conn, "SELECT has_column_privilege('bfx_bot', :t, :c, 'UPDATE')",
                        t=table, c=column)
            assert not _has(conn, "SELECT has_column_privilege('bfx_webapi', :t, :c, 'INSERT')",
                            t=table, c=column)
        for role in ("bfx_bot", "bfx_webapi"):
            assert _has(conn, "SELECT has_table_privilege(:r, :t, 'SELECT')", r=role, t=table)
            for privilege in ("DELETE", "TRUNCATE"):
                assert not _has(conn, "SELECT has_table_privilege(:r, :t, :p)", r=role, t=table,
                                p=privilege)
        assert not _has(conn, "SELECT has_any_column_privilege('bfx_bot', :t, 'INSERT')", t=table)
        assert not _has(conn, "SELECT has_any_column_privilege('bfx_webapi', :t, 'UPDATE')", t=table)
        # The policy itself: the web API only reads it; the bot appends a
        # revision and moves the pointer, nothing else.
        for policy_table in ("capital_policy_revisions", "capital_policy_heads"):
            assert _has(conn, "SELECT has_table_privilege('bfx_webapi', :t, 'SELECT')", t=policy_table)
            assert not _has(conn, "SELECT has_any_column_privilege('bfx_webapi', :t, 'INSERT')",
                            t=policy_table)
            assert not _has(conn, "SELECT has_any_column_privilege('bfx_webapi', :t, 'UPDATE')",
                            t=policy_table)
            assert not _has(conn, "SELECT has_table_privilege('bfx_bot', :t, 'DELETE')", t=policy_table)
        assert _has(conn, "SELECT has_table_privilege('bfx_bot', 'capital_policy_revisions', 'INSERT')")
        assert not _has(conn, "SELECT has_any_column_privilege('bfx_bot', 'capital_policy_revisions', "
                        "'UPDATE')")
        assert not _has(conn, "SELECT has_any_column_privilege('bfx_bot', 'capital_policy_heads', "
                        "'INSERT')")
        for column, granted in (("revision_id", True), ("revision", True), ("symbol", False),
                                ("exchange_account_id", False), ("deployment_environment", False)):
            assert _has(conn, "SELECT has_column_privilege('bfx_bot', 'capital_policy_heads', :c, "
                        "'UPDATE')", c=column) is granted, column


def test_the_web_api_queues_only_request_columns_and_the_rules_hold(migrated):
    _, engine = migrated
    with engine.begin() as conn, pytest.raises(Exception, match="permission denied"):
        conn.exec_driver_sql("SET LOCAL ROLE bfx_webapi")
        conn.exec_driver_sql(_request_sql(extra=(", state", ", 'applied'")))
    with engine.begin() as conn:
        conn.exec_driver_sql("SET LOCAL ROLE bfx_webapi")
        conn.exec_driver_sql(_request_sql())
        # Enable has its own slot beside the waiting disable.
        conn.exec_driver_sql(_request_sql("00000000-0000-0000-0000-0000000000e2", "enable"))
    for sql, message in (
            (_request_sql("00000000-0000-0000-0000-0000000000e3"), "uq_capital_policy_requests_pending"),
            (_request_sql("00000000-0000-0000-0000-0000000000e4", "resume"),
             "ck_capital_policy_requests_action")):
        with engine.begin() as conn, pytest.raises(Exception, match=message):
            conn.exec_driver_sql("SET LOCAL ROLE bfx_webapi")
            conn.exec_driver_sql(sql)
    with engine.begin() as conn, pytest.raises(Exception, match="permission denied"):
        conn.exec_driver_sql("SET LOCAL ROLE bfx_webapi")
        conn.exec_driver_sql("UPDATE capital_policy_revisions SET source = '{}'")
    with engine.begin() as conn, pytest.raises(Exception, match="permission denied"):
        conn.exec_driver_sql("SET LOCAL ROLE bfx_bot")
        conn.exec_driver_sql(_request_sql("00000000-0000-0000-0000-0000000000e5"))
    with engine.begin() as conn, pytest.raises(Exception, match="permission denied"):
        conn.exec_driver_sql("SET LOCAL ROLE bfx_bot")
        conn.exec_driver_sql("UPDATE capital_policy_requests SET reason = 'rewritten'")
    with engine.begin() as conn, pytest.raises(Exception, match="applied"):
        # An applied outcome must name the revision in force.
        conn.exec_driver_sql("SET LOCAL ROLE bfx_bot")
        conn.exec_driver_sql("UPDATE capital_policy_requests SET state='applied', processed_at_ms=2")
    with engine.begin() as conn:
        conn.exec_driver_sql("SET LOCAL ROLE bfx_bot")
        conn.exec_driver_sql("UPDATE capital_policy_requests SET state='rejected', processed_at_ms=2, "
                             "outcome_reason='x'")
    with engine.begin() as conn, pytest.raises(Exception, match="invalid capital policy request transition"):
        conn.exec_driver_sql("UPDATE capital_policy_requests SET state='failed', outcome_reason='y'")
    with engine.begin() as conn, pytest.raises(Exception, match="immutable capital policy request"):
        conn.exec_driver_sql("DELETE FROM capital_policy_requests")


def test_the_bot_applies_a_web_api_request_as_a_new_revision(migrated):
    url, engine = migrated
    asyncio.run(_seed(url))
    _operator(engine)

    async def scenario() -> tuple[str, str | None, UUID | None]:
        web_engine, web = _async(url, "bfx_webapi")
        bot_engine, bot = _async(url, "bfx_bot")
        try:
            request_id = uuid4()
            async with web.begin() as session:
                assert await insert_request(session, CapitalPolicyRequestRow, {
                    "request_id": request_id, "exchange_account_id": _A,
                    "deployment_environment": "ci", "symbol": "fUST", "action": "disable",
                    "reason": "maintenance", "requested_by": "operator", "created_at_ms": 5})

            async def allow(session, *, account_id, user) -> bool:
                return user == "operator"

            worker = CapitalPolicyRequestWorker(
                session_factory=bot, account_id=_A, environment="ci", authority=allow,
                policy_store=build_policy_store(_SCOPE), scope_lock=build_scope_lock(),
                clock=lambda: 6)
            assert await worker.tick() is True
            async with bot() as session:
                row = await session.get(CapitalPolicyRequestRow, request_id)
                return row.state, row.outcome_reason, row.policy_revision_id
        finally:
            await web_engine.dispose()
            await bot_engine.dispose()

    state, reason, revision_id = asyncio.run(scenario())
    assert (state, reason) == ("applied", "disabled (revision 2)")
    with engine.connect() as conn:
        head = conn.execute(text("SELECT h.revision, r.id, r.policy->>'enabled', r.policy - 'enabled' "
                                 "= p.policy - 'enabled' FROM capital_policy_heads h "
                                 "JOIN capital_policy_revisions r ON r.id = h.revision_id "
                                 "JOIN capital_policy_revisions p ON p.symbol = r.symbol "
                                 "AND p.revision = 1")).one()
    assert tuple(head) == (2, revision_id, "false", True)


def test_the_runtime_role_may_toggle_enabled_and_nothing_else(migrated):
    url, engine = migrated
    asyncio.run(_seed(url))
    _operator(engine)
    waiting = "00000000-0000-0000-0000-0000000000e1"
    with engine.begin() as conn:
        conn.exec_driver_sql(_request_sql(waiting, "disable"))
        conn.exec_driver_sql(_request_sql("00000000-0000-0000-0000-0000000000e2", "enable", by="nobody"))
        first = conn.execute(text("SELECT id, policy::text, digest FROM capital_policy_revisions")).one()

    def revision(policy_sql: str, *, number: int = 2, request: str | None = waiting) -> str:
        source = "{}" if request is None else f'{{"request_id": "{request}"}}'
        return (f"INSERT INTO capital_policy_revisions (id, exchange_account_id, deployment_environment, "
                f"symbol, revision, schema_version, policy, digest, source) VALUES (gen_random_uuid(), "
                f"'{_A}', 'ci', 'fUST', {number}, 3, {policy_sql}, 'd', '{source}'::jsonb)")

    base = f"'{first.policy}'::jsonb"
    disabled = f"jsonb_set({base}, '{{enabled}}', 'false')"
    for sql, message in (
            (revision(f"jsonb_set({base}, '{{max_offer_amount}}', '\"9999\"')"), "may only toggle"),
            (revision(f"{base} - 'envelope'"), "may only toggle"),
            (revision(f"jsonb_set({base}, '{{enabled}}', '\"yes\"')"), "may only toggle"),
            (revision(disabled, number=5), "may only toggle"),          # skips a revision
            (revision(disabled, request=None), "may only toggle"),      # names no request
            # A forged request id, a request for the opposite change, or one by
            # someone who is not the operator: the bot cannot widen trading alone.
            (revision(disabled, request=str(uuid4())), "must apply a waiting request"),
            (revision(base), "must apply a waiting request"),
            (revision(base, request="00000000-0000-0000-0000-0000000000e2"),
             "must apply a waiting request"),
    ):
        with engine.begin() as conn, pytest.raises(Exception, match=message):
            conn.exec_driver_sql("SET LOCAL ROLE bfx_bot")
            conn.exec_driver_sql(sql)
    with engine.begin() as conn:
        conn.exec_driver_sql("SET LOCAL ROLE bfx_bot")
        conn.exec_driver_sql(revision(disabled))
        new_id = conn.scalar(text("SELECT id FROM capital_policy_revisions WHERE revision = 2"))
        for update, ok in ((f"revision_id = '{first.id}', revision = 1", False),     # rewind
                           (f"revision_id = '{first.id}', revision = 2", False),     # wrong row
                           (f"revision_id = '{new_id}', revision = 2", True)):
            if ok:
                conn.exec_driver_sql(f"UPDATE capital_policy_heads SET {update}")
                continue
            with pytest.raises(Exception, match="may only advance"), conn.begin_nested():
                conn.exec_driver_sql(f"UPDATE capital_policy_heads SET {update}")
    # The owner (the amendment script) is not narrowed.
    with engine.begin() as conn:
        conn.exec_driver_sql(revision(f"jsonb_set({base}, '{{max_offer_amount}}', '\"300\"')",
                                      number=3, request=None))


def test_downgrade_round_trip_and_refusal(migrated):
    url, engine = migrated
    pre_switch_url(url)  # a switched database refuses a downgrade through f6a7b8c9d0e1
    _alembic(url, "downgrade", _BEFORE)
    with engine.connect() as conn:
        assert conn.scalar(text("SELECT to_regclass('public.capital_policy_requests')")) is None
        assert not _has(conn, "SELECT has_table_privilege('bfx_bot', 'capital_policy_revisions', 'INSERT')")
        assert not _has(conn, "SELECT has_any_column_privilege('bfx_bot', 'capital_policy_heads', "
                        "'UPDATE')")
        assert conn.scalar(text("SELECT count(*) FROM pg_trigger WHERE tgname LIKE 'runtime_policy_%'")) == 0
    _alembic(url, "upgrade", "head")
    _alembic(url, "check")
    stamp_realm(url, "ci")  # the downgrade dropped the stamp; the tables hold no rows to derive it from
    with engine.begin() as conn:
        conn.exec_driver_sql(_request_sql())
    pre_switch_url(url)  # a switched database refuses a downgrade through f6a7b8c9d0e1
    result = _alembic_cli(url, "downgrade", _BEFORE)
    assert result.returncode != 0
    assert "refuse downgrade of recorded capital policy requests" in result.stdout + result.stderr
