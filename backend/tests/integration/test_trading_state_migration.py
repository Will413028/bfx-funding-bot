"""Run Alembic to the trading-state revision on a disposable PostgreSQL.

Covers what SQLite fixtures cannot: the carried-over halt rows, the insert
trigger's transition rules and id ordering, the append-only triggers, and the
grants as the restricted runtime roles actually see them.
"""
import asyncio
import os
import subprocess
import sys
from pathlib import Path
from uuid import UUID

import pytest
from sqlalchemy import create_engine, text

from tests.pg_templates import alembic

pytestmark = pytest.mark.integration

_BACKEND = Path(__file__).resolve().parents[2]
_PREVIOUS = "a7f3c1d9e204"
_A = "00000000-0000-0000-0000-0000000000a1"
_B = "00000000-0000-0000-0000-0000000000b1"
_C = "00000000-0000-0000-0000-0000000000c1"
# A fourth account carries the maintenance pause: one database holds one realm, and the
# realm stamp the head migration derives refuses a database whose halts span realms.
_D = "00000000-0000-0000-0000-0000000000d1"
_E = "00000000-0000-0000-0000-0000000000e1"  # no halt history: a scope of its own
_PRODUCTION_REASON = "L4 gate: candle distortion — keep halted until probation lands (halt 11)"


def _alembic(url: str, name: str, *args: str) -> None:
    alembic(url, name, *args)


def _alembic_cli(url: str, *args: str) -> subprocess.CompletedProcess[str]:
    """The operator's `alembic` CLI: a refused migration must exit non-zero, saying why."""
    return subprocess.run([sys.executable, "-m", "alembic", *args], cwd=_BACKEND,
                          env=dict(os.environ, DATABASE_URL=url), capture_output=True, text=True)


def _reset(engine) -> None:
    with engine.begin() as conn:
        conn.exec_driver_sql("DROP SCHEMA IF EXISTS projection_audit CASCADE")
        conn.exec_driver_sql("DROP SCHEMA IF EXISTS auth CASCADE")
        conn.exec_driver_sql("DROP SCHEMA IF EXISTS release_archive CASCADE")
        conn.exec_driver_sql("DROP SCHEMA public CASCADE")
        conn.exec_driver_sql("CREATE SCHEMA public")
        # Worst case, deliberately broader than production: default privileges hand every new
        # table and sequence to both runtime roles, so each migration must take back what it
        # does not mean to grant. Production has them only for bfx_bot (fresh-host-setup §1a);
        # bfx_webapi has none there. At head that difference is gone: d0e1f2a3b4c6 revokes
        # everything the web API holds in public and grants back its exact allowlist
        # (test_webapi_privilege_allowlist.py checks both builds equal it).
        for role in ("bfx_bot", "bfx_webapi"):
            conn.exec_driver_sql(f"DO $$ BEGIN IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname='{role}') "
                                 f"THEN CREATE ROLE {role}; END IF; END $$")
            conn.exec_driver_sql(f"GRANT USAGE ON SCHEMA public TO {role}")
            conn.exec_driver_sql(f"ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON TABLES TO {role}")
            conn.exec_driver_sql(f"ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON SEQUENCES TO {role}")


def _halt(conn, account: str, env: str, halted: bool, kind: str, reason: str, actor: str,
          at: int) -> int:
    return conn.scalar(text("""INSERT INTO trading_halt
        (account_id, exchange_account_id, deployment_environment, halted, kind, reason, actor, created_at_ms)
        VALUES (:s, :a, :env, :halted, :kind, :reason, :actor, :at) RETURNING id"""),
        {"s": account, "a": UUID(account), "env": env, "halted": halted, "kind": kind, "reason": reason,
         "actor": actor, "at": at})


def _insert(conn, account: str, state: str, cause: str, *, env: str = "prod",
            explicit_id: int | None = None) -> int:
    columns = "exchange_account_id, deployment_environment, state, cause, actor, reason, created_at_ms"
    values = ":a, :env, :state, :cause, 'test', 'test', 9000"
    if explicit_id is not None:
        columns, values = "id, " + columns, ":id, " + values
    return conn.scalar(text(f"INSERT INTO trading_state ({columns}) VALUES ({values}) RETURNING id"),
                       {"a": account, "env": env, "state": state, "cause": cause, "id": explicit_id})


def _build_migrated(url: str) -> dict[str, int]:
    """Production's halt rows at _PREVIOUS, then the upgrade to head under test."""
    engine = create_engine(url)
    _reset(engine)
    _alembic(url, "upgrade", _PREVIOUS)
    ids: dict[str, int] = {}
    with engine.begin() as conn:
        for account in (_A, _B, _C, _D, _E):
            conn.execute(text("INSERT INTO exchange_accounts(id, venue, label) VALUES (:a, 'bitfinex', 'fixture')"),
                         {"a": account})
        # Production today: an operator's safety halt, never cleared.
        _halt(conn, _A, "prod", True, "maintenance", "earlier pause", "admin-api", 100)
        _halt(conn, _A, "prod", False, "maintenance", "pause over", "admin-api", 200)
        ids["prod"] = _halt(conn, _A, "prod", True, "safety", _PRODUCTION_REASON, "worker", 300)
        ids["pause"] = _halt(conn, _D, "prod", True, "maintenance", "pg 18.6 upgrade", "will", 400)
        _halt(conn, _B, "prod", True, "release", "release_blocked", "worker", 500)
        ids["resumed"] = _halt(conn, _B, "prod", False, "safety", "release_promoted:x", "will", 600)
        ids["release"] = _halt(conn, _C, "prod", True, "release", "release_command_terminal", "worker", 700)
    engine.dispose()
    _alembic(url, "upgrade", "head")
    _alembic(url, "check")
    return ids


@pytest.fixture
def migrated(pg_templates, pg_clone):
    """A fresh copy of the upgraded database; the upgrade itself runs once per session."""
    template = pg_templates.template("trading_state_migrated", _build_migrated)
    url = pg_clone(template)
    engine = create_engine(url)
    try:
        yield url, engine, dict(pg_templates.built(template))
    finally:
        engine.dispose()


def test_current_halt_rows_carry_over_verbatim(migrated):
    _, engine, ids = migrated
    with engine.begin() as conn:
        rows = conn.execute(text("""SELECT exchange_account_id::text, deployment_environment, state, cause,
            actor, reason, legacy_halt_id FROM trading_state ORDER BY id""")).all()
        pause_id = conn.scalar(text("SELECT id FROM trading_state WHERE legacy_halt_id = :h"),
                               {"h": ids["pause"]})
    assert [tuple(r) for r in rows] == [
        (_A, "prod", "HALTED", "operator", "worker", _PRODUCTION_REASON, ids["prod"]),
        (_D, "prod", "REDUCING", "operator", "will", "pg 18.6 upgrade", ids["pause"]),
        (_B, "prod", "ACTIVE", "operator", "will", "release_promoted:x", ids["resumed"]),
        (_C, "prod", "HALTED", "operator", "worker", "release_command_terminal", ids["release"]),
        # 5b1e7c9d2a40: the carried pause becomes the stricter stop.
        (_D, "prod", "HALTED", "operator", "migration 5b1e7c9d2a40",
         f"REDUCING retired (lending envelope ADR); carried over from trading_state {pause_id}: "
         "pg 18.6 upgrade", None),
    ]


def test_history_is_append_only(migrated):
    _, engine, _ = migrated
    # CASCADE gets past the audit table's foreign key, so the truncate trigger
    # itself is what refuses.
    for sql, message in (("UPDATE trading_state SET state='ACTIVE'", "immutable trading state"),
                         ("DELETE FROM trading_state", "immutable trading state"),
                         ("TRUNCATE trading_state CASCADE",
                          "immutable (trading state history|funding cancel-all audit)")):
        with engine.begin() as conn, pytest.raises(Exception, match=message):
            conn.exec_driver_sql(sql)


@pytest.mark.parametrize(("account", "state", "cause", "message"), [
    # Never ends an operator's halt or a halt younger than 15 min (8e4b2f6a1c37).
    (_A, "ACTIVE", "auto", "-> ACTIVE by auto|minimum halt duration"),
    (_B, "REDUCING", "operator", "ck_trading_state_state"),           # retired state
    (_B, "HALTED", "material_deploy", "ck_trading_state_cause"),      # retired causes
    (_B, "HALTED", "kill_switch", "ck_trading_state_cause"),
    (_B, "PAUSED", "operator", "ck_trading_state_(state|cause)"),
])
def test_trigger_and_checks_reject_illegal_transitions(migrated, account, state, cause, message):
    _, engine, _ = migrated
    with engine.begin() as conn, pytest.raises(Exception, match=message):
        _insert(conn, account, state, cause)


def test_trigger_orders_rows_by_decision_not_by_supplied_id(migrated):
    """A writer that brings its own low id still becomes the latest decision."""
    _, engine, _ = migrated
    with engine.begin() as conn:
        latest = conn.scalar(text("SELECT max(id) FROM trading_state"))
        assigned = _insert(conn, _A, "ACTIVE", "operator", explicit_id=1)
        assert assigned > latest
        assert conn.scalar(text(
            "SELECT state FROM trading_state WHERE exchange_account_id=:a AND deployment_environment='prod' "
            "ORDER BY id DESC LIMIT 1"), {"a": _A}) == "ACTIVE"


def test_runtime_roles_bot_appends_and_webapi_only_reads(migrated):
    _, engine, _ = migrated
    with engine.begin() as conn:
        for privilege, bot, webapi in (("SELECT", True, True), ("INSERT", True, False),
                                       ("UPDATE", False, False), ("DELETE", False, False),
                                       ("TRUNCATE", False, False)):
            assert conn.scalar(text("SELECT has_table_privilege('bfx_bot', 'trading_state', :p)"),
                               {"p": privilege}) is bot, privilege
            assert conn.scalar(text("SELECT has_table_privilege('bfx_webapi', 'trading_state', :p)"),
                               {"p": privilege}) is webapi, privilege
        assert not conn.scalar(text(
            "SELECT has_sequence_privilege('bfx_webapi', 'trading_state_id_seq', 'USAGE')"))
    with engine.begin() as conn:
        conn.exec_driver_sql("SET LOCAL ROLE bfx_bot")
        _insert(conn, _C, "ACTIVE", "operator")
        assert conn.scalar(text("SELECT count(*) FROM trading_state")) == 6
    with engine.begin() as conn, pytest.raises(Exception, match="permission denied"):
        conn.exec_driver_sql("SET LOCAL ROLE bfx_webapi")
        _insert(conn, _C, "HALTED", "operator")
    with engine.begin() as conn:
        conn.exec_driver_sql("SET LOCAL ROLE bfx_webapi")
        assert conn.scalar(text("SELECT count(*) FROM trading_state")) == 6


def test_repository_on_postgres_survives_restart_and_serialises_reassertion(migrated):
    url, _, _ = migrated

    async def scenario():
        from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

        from bfx_funding_bot.modules.execution.safety.trading_state import (
            IllegalTradingTransition,
            TradingStateRepository,
        )

        def repo(engine):
            return TradingStateRepository(async_sessionmaker(engine, expire_on_commit=False),
                                          account_id=UUID(_B), deployment_environment="prod")

        first = create_async_engine(url)
        try:
            results = await asyncio.gather(*(repo(first).transition(
                "HALTED", cause="auto", actor=f"worker-{i}", reason="concurrent") for i in range(8)))
            assert sum(result.changed for result in results) == 1
            with pytest.raises(IllegalTradingTransition, match="minimum halt duration"):
                await repo(first).transition("ACTIVE", cause="auto", actor="t", reason="t")
            written = await repo(first).current()
        finally:
            await first.dispose()
        second = create_async_engine(url)
        try:
            assert await repo(second).current() == written
            assert written is not None and written.state == "HALTED" and written.cause == "auto"
        finally:
            await second.dispose()

    asyncio.run(scenario())


def test_the_database_clock_decides_an_automatic_resume(migrated):
    """8e4b2f6a1c37: automatic rows are stamped by the database, and its refusals
    map to typed errors even when the writer's own clock would have allowed them."""
    url, _, _ = migrated

    async def scenario():
        import time

        from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

        from bfx_funding_bot.modules.execution.safety.trading_state import (
            AutoResumeTooSoon,
            TradingStateRepository,
        )

        engine = create_async_engine(url)
        repo = TradingStateRepository(async_sessionmaker(engine, expire_on_commit=False),
                                      account_id=UUID(_E), deployment_environment="prod")
        try:
            now = int(time.time() * 1000)
            await repo.transition("ACTIVE", cause="operator", actor="will", reason="start")
            # A writer claiming its halt is a day old: the row carries the database's time.
            halted = await repo.transition("HALTED", cause="auto", actor="auto:x", reason="x",
                                           now_ms=now - 24 * 60 * 60 * 1000)
            assert abs(halted.state.created_at_ms - now) < 60_000
            assert abs((await repo.current()).created_at_ms - now) < 60_000
            # The writer's clock says 15 minutes passed; the database's does not.
            with pytest.raises(AutoResumeTooSoon):
                await repo.transition("ACTIVE", cause="auto", actor="auto-resume", reason="ok",
                                      now_ms=now + 24 * 60 * 60 * 1000)
            assert (await repo.current()).state == "HALTED"
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_downgrade_keeps_decisions_made_after_the_migration(migrated):
    url, engine, _ = migrated
    with engine.begin() as conn:
        _insert(conn, _B, "HALTED", "operator")
    from .test_ledger_schema_roles import pre_switch_url  # circular at module level
    pre_switch_url(url)  # a switched database refuses a downgrade through f6a7b8c9d0e1
    result = _alembic_cli(url, "downgrade", _PREVIOUS)
    assert result.returncode != 0
    assert "refuse downgrade of recorded trading state decisions" in result.stdout + result.stderr
    with engine.begin() as conn:
        assert conn.scalar(text("SELECT count(*) FROM trading_state")) == 6


def _audit(conn, attempt: str, phase: str, *, detail: str | None = None) -> None:
    state_id = conn.scalar(text("SELECT max(id) FROM trading_state WHERE exchange_account_id=:a"), {"a": _A})
    conn.execute(text("""INSERT INTO funding_cancel_all_audit
        (exchange_account_id, deployment_environment, trading_state_id, attempt_id, currency, phase,
         detail, actor, occurred_at_ms)
        VALUES (:a, 'prod', :s, :attempt, 'UST', :phase, :detail, 'test', 1)"""),
        {"a": _A, "s": state_id, "attempt": attempt, "phase": phase, "detail": detail})


def test_cancel_all_audit_is_append_only_one_outcome_per_attempt(migrated):
    _, engine, _ = migrated
    attempt = "00000000-0000-0000-0000-0000000000f1"
    with engine.begin() as conn:
        _audit(conn, attempt, "requested")
        _audit(conn, attempt, "failed", detail="ExecutorTransientError")
    for sql in ("UPDATE funding_cancel_all_audit SET phase='acknowledged'",
                "DELETE FROM funding_cancel_all_audit", "TRUNCATE funding_cancel_all_audit"):
        with engine.begin() as conn, pytest.raises(Exception, match="immutable funding cancel-all audit"):
            conn.exec_driver_sql(sql)
    for phase, detail, message in (("acknowledged", None, "uq_funding_cancel_all_audit_outcome"),
                                   ("requested", None, "uq_funding_cancel_all_audit_request"),
                                   ("requested", "x", "ck_funding_cancel_all_audit_request_shape")):
        with engine.begin() as conn, pytest.raises(Exception, match=message):
            _audit(conn, attempt if message != "ck_funding_cancel_all_audit_request_shape"
                   else "00000000-0000-0000-0000-0000000000f2", phase, detail=detail)


def test_cancel_all_audit_grants(migrated):
    _, engine, _ = migrated
    with engine.begin() as conn:
        for privilege, bot, webapi in (("SELECT", True, True), ("INSERT", True, False),
                                       ("UPDATE", False, False), ("DELETE", False, False)):
            assert conn.scalar(text("SELECT has_table_privilege('bfx_bot', 'funding_cancel_all_audit', :p)"),
                               {"p": privilege}) is bot, privilege
            assert conn.scalar(text("SELECT has_table_privilege('bfx_webapi', 'funding_cancel_all_audit', :p)"),
                               {"p": privilege}) is webapi, privilege
    with engine.begin() as conn:
        conn.exec_driver_sql("SET LOCAL ROLE bfx_bot")
        _audit(conn, "00000000-0000-0000-0000-0000000000f3", "skipped", detail="writer_lock_not_held")


_REQ = "00000000-0000-0000-0000-0000000000d1"


def _insert_request(conn, request_id=_REQ, account=_A, action="resume") -> None:
    conn.execute(text("""INSERT INTO trading_control_requests
        (request_id, exchange_account_id, deployment_environment, action, reason,
         requested_by, created_at_ms) VALUES (:r, :a, 'prod', :act, 'x', 'operator', 1)"""),
        {"r": request_id, "a": account, "act": action})


def test_web_api_can_only_queue_a_request(migrated):
    _, engine, _ = migrated
    with engine.begin() as conn:
        conn.exec_driver_sql("SET LOCAL ROLE bfx_webapi")
        _insert_request(conn)
        assert conn.scalar(text("SELECT count(*) FROM trading_control_requests")) == 1
    for sql in (
        "UPDATE trading_control_requests SET state='applied', processed_at_ms=2",
        "INSERT INTO trading_control_requests (request_id, exchange_account_id, deployment_environment,"
        " action, reason, requested_by, created_at_ms, state, processed_at_ms)"
        f" VALUES ('00000000-0000-0000-0000-0000000000d2', '{_A}', 'prod', 'resume', 'x',"
        " 'operator', 1, 'applied', 2)",
        f"INSERT INTO trading_state (exchange_account_id, deployment_environment, state, cause, actor,"
        f" reason, created_at_ms) VALUES ('{_A}', 'prod', 'ACTIVE', 'operator', 'x', 'x', 1)",
        "INSERT INTO funding_cancel_all_audit (exchange_account_id, deployment_environment,"
        " trading_state_id, attempt_id, currency, phase, actor, occurred_at_ms) VALUES"
        f" ('{_A}', 'prod', 1, '00000000-0000-0000-0000-0000000000f9', 'UST', 'requested', 'x', 1)",
    ):
        with engine.begin() as conn, pytest.raises(Exception, match="permission denied"):
            conn.exec_driver_sql("SET LOCAL ROLE bfx_webapi")
            conn.exec_driver_sql(sql)


def test_the_web_api_queues_a_resume_and_a_kill_has_its_own_lane(migrated):
    """A waiting resume never makes a kill wait; retired actions are refused."""
    _, engine, _ = migrated
    with engine.begin() as conn:
        conn.exec_driver_sql("SET LOCAL ROLE bfx_webapi")
        _insert_request(conn, "00000000-0000-0000-0000-0000000000e1", action="resume")
        _insert_request(conn, "00000000-0000-0000-0000-0000000000e2", action="kill")
    for action, account, message in (
            ("kill", _A, "uq_trading_control_requests_pending_kill"),
            ("resume", _A, "uq_trading_control_requests_pending"),
            ("approve", _B, "ck_trading_control_requests_action"),
            ("pause", _B, "ck_trading_control_requests_action")):
        with engine.begin() as conn, pytest.raises(Exception, match=message):
            conn.exec_driver_sql("SET LOCAL ROLE bfx_webapi")
            _insert_request(conn, "00000000-0000-0000-0000-0000000000e3", account, action)


def test_bot_records_one_outcome(migrated):
    _, engine, _ = migrated
    with engine.begin() as conn:
        _insert_request(conn)
    with engine.begin() as conn, pytest.raises(Exception, match="permission denied"):
        conn.exec_driver_sql("SET LOCAL ROLE bfx_bot")
        _insert_request(conn, "00000000-0000-0000-0000-0000000000d3")
    with engine.begin() as conn, pytest.raises(Exception, match="permission denied"):
        conn.exec_driver_sql("SET LOCAL ROLE bfx_bot")
        conn.exec_driver_sql("UPDATE trading_control_requests SET reason='rewritten'")
    with engine.begin() as conn:
        conn.exec_driver_sql("SET LOCAL ROLE bfx_bot")
        conn.exec_driver_sql("UPDATE trading_control_requests SET state='applied', processed_at_ms=2")
    with engine.begin() as conn, pytest.raises(Exception, match="invalid trading control request transition"):
        conn.exec_driver_sql("UPDATE trading_control_requests SET state='rejected', outcome_reason='x'")
    with engine.begin() as conn, pytest.raises(Exception, match="immutable trading control"):
        conn.exec_driver_sql("DELETE FROM trading_control_requests")


def test_the_operator_check_runs_for_the_bot(migrated):
    _, engine, _ = migrated
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO exchange_account_memberships(exchange_account_id,user_id,role) "
                          "VALUES (:a,'operator','owner')"), {"a": _A})
        conn.exec_driver_sql('''INSERT INTO auth."user" (id,name,email,"emailVerified","createdAt","updatedAt",
            role,banned,"twoFactorEnabled") VALUES ('operator','op','op@test.invalid',false,now(),now(),
            'admin',false,true)''')
        conn.exec_driver_sql("SET LOCAL ROLE bfx_bot")
        assert conn.scalar(text("SELECT public.operator_authorized(:a, 'operator')"), {"a": _A})
        assert not conn.scalar(text("SELECT public.operator_authorized(:a, 'nobody')"), {"a": _A})


# ------------------------------------------------------------ release archive


_ARCHIVED = (("trading_halt", "id"), ("canary_command_permits", "permit_id"),
             ("release_sessions", "id"), ("release_session_audit", "id"))


def _content(conn, schema: str, table: str, key: str) -> tuple[int, str]:
    return tuple(conn.execute(text(
        f"SELECT count(*), encode(sha256(convert_to(coalesce(string_agg(to_jsonb(t)::text, "
        f"E'\\n' ORDER BY t.{key}), ''), 'UTF8')), 'hex') FROM {schema}.{table} t")).one())


def test_the_release_ceremony_is_archived_whole_frozen_and_verifiable(migrated):
    """Real-money canary history leaves the application but is kept, unchanged."""
    _, engine, ids = migrated
    with engine.begin() as conn:
        for table, _ in _ARCHIVED:
            assert conn.scalar(text(f"SELECT to_regclass('public.{table}')")) is None
        halts = conn.execute(text("SELECT id, reason FROM release_archive.trading_halt ORDER BY id")).all()
        assert [row.id for row in halts][-1] == ids["release"] and len(halts) == 7
        # Anyone can recompute the manifest and see nothing changed since.
        manifest = {row.table_name: (row.row_count, row.content_sha256) for row in conn.execute(text(
            "SELECT table_name, row_count, content_sha256 FROM release_archive.manifest"
            " WHERE archived_by_revision = 'c74d45a54e46'"))}
        assert manifest == {table: _content(conn, "release_archive", table, key)
                            for table, key in _ARCHIVED}
        # The foreign keys still bind the archive to the public rows it names.
        assert conn.scalar(text("""SELECT count(*) FROM pg_constraint c
            JOIN pg_namespace n ON n.oid = c.connamespace
            WHERE n.nspname = 'release_archive' AND c.contype = 'f'
              AND c.confrelid = 'public.exchange_accounts'::regclass""")) >= 2
    for sql in ("UPDATE release_archive.trading_halt SET reason = 'rewritten'",
                "DELETE FROM release_archive.release_sessions",
                "INSERT INTO release_archive.manifest (table_name, row_count, content_sha256, "
                "archived_by_revision) VALUES ('x', 0, repeat('0', 64), 'x')",
                "TRUNCATE release_archive.release_session_audit"):
        with engine.begin() as conn, pytest.raises(Exception, match="release_archive is frozen"):
            conn.exec_driver_sql(sql)
    for role in ("bfx_bot", "bfx_webapi"):
        with engine.begin() as conn, pytest.raises(Exception, match="permission denied for schema"):
            conn.exec_driver_sql(f"SET LOCAL ROLE {role}")
            conn.exec_driver_sql("SELECT count(*) FROM release_archive.trading_halt")


def test_archiving_the_release_ceremony_is_lossless_both_ways(migrated):
    url, engine, _ = migrated
    with engine.begin() as conn:
        archived = {table: _content(conn, "release_archive", table, key) for table, key in _ARCHIVED}
    # Back to before the archive (5b1e7c9d2a40's own round trip has its own test).
    from .test_ledger_schema_roles import pre_switch_url  # circular at module level
    pre_switch_url(url)  # a switched database refuses a downgrade through f6a7b8c9d0e1
    _alembic(url, "downgrade", "1c435a35dcb4")
    with engine.begin() as conn:
        assert conn.scalar(text("SELECT to_regnamespace('release_archive')")) is None
        assert {table: _content(conn, "public", table, key) for table, key in _ARCHIVED} == archived
        assert conn.scalar(text("SELECT version_num FROM alembic_version")) == "1c435a35dcb4"
        assert conn.scalar(text("SELECT to_regclass('public.deployments')")) is None
    _alembic(url, "upgrade", "head")
    _alembic(url, "check")
    with engine.begin() as conn:
        assert {table: _content(conn, "release_archive", table, key) for table, key in _ARCHIVED} == archived


def test_the_web_api_baseline_is_granted_by_migration_not_by_hand(pg_templates, pg_clone):
    """No default privileges, no runbook: the migration alone gives the web API
    what it reads (and its own setup writes), and still no execution write."""
    # A database of its own: the container's default one exists only after a pg_engine test.
    url = pg_clone(pg_templates.template("empty_database", lambda _url: None))
    engine = create_engine(url)
    try:
        _reset(engine)
        with engine.begin() as conn:
            for role in ("bfx_bot", "bfx_webapi"):
                conn.exec_driver_sql(f"ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE ALL ON TABLES FROM {role}")
                conn.exec_driver_sql(f"ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE ALL ON SEQUENCES FROM {role}")
                conn.exec_driver_sql(f"REVOKE ALL ON SCHEMA public FROM {role}")
        _alembic(url, "upgrade", "head")
        expected = {
            # d0e1f2a3b4c6 owns what the psql runbook steps used to grant.
            "user_profiles": {"SELECT", "INSERT", "UPDATE"},
            "api_keys": {"SELECT", "INSERT", "UPDATE", "DELETE"},
            "user_configs": {"SELECT", "INSERT", "UPDATE", "DELETE"},
            "exchange_accounts": {"SELECT"}, "exchange_account_memberships": {"SELECT"},
            "position_state": {"SELECT"}, "offer_claims": {"SELECT"}, "event_log": {"SELECT"},
            "execution_uncertainties": {"SELECT"}, "submission_attempts": {"SELECT"},
            "attribution_weekly": {"SELECT"}, "funding_candles": {"SELECT"},
            "exchange_account_credentials": {"SELECT", "INSERT", "UPDATE"},
            "account_config_drafts": {"SELECT", "INSERT", "UPDATE", "DELETE"},
            "trading_state": {"SELECT"}, "funding_cancel_all_audit": {"SELECT"},
            "deployments": {"SELECT"},
            # 7d2a9c4e6b13: the currency toggle outbox and the policy it lists.
            "capital_policy_requests": {"SELECT"}, "capital_policy_heads": {"SELECT"},
            "capital_policy_revisions": {"SELECT"},
        }
        with engine.connect() as conn:
            assert conn.scalar(text("SELECT has_schema_privilege('bfx_webapi', 'public', 'USAGE')"))
            assert conn.scalar(text(
                "SELECT has_column_privilege('bfx_webapi', 'alembic_version', 'version_num', 'SELECT')"))
            for table, granted in expected.items():
                for privilege in ("SELECT", "INSERT", "UPDATE", "DELETE"):
                    has = conn.scalar(text("SELECT has_table_privilege('bfx_webapi', :t, :p)"),
                                      {"t": f"public.{table}", "p": privilege})
                    assert has is (privilege in granted), (table, privilege)
    finally:
        engine.dispose()
