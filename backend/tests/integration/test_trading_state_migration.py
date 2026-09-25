"""Run Alembic to the trading-state revision on a disposable PostgreSQL.

Covers what SQLite fixtures cannot: the carried-over halt rows, the insert
trigger's transition rules and id ordering, the append-only triggers, and the
grants as the restricted runtime roles actually see them.
"""
import asyncio
import os
import subprocess
from pathlib import Path
from uuid import UUID

import pytest
from sqlalchemy import create_engine, text

pytestmark = pytest.mark.integration

_BACKEND = Path(__file__).resolve().parents[2]
_PREVIOUS = "a7f3c1d9e204"
_A = "00000000-0000-0000-0000-0000000000a1"
_B = "00000000-0000-0000-0000-0000000000b1"
_C = "00000000-0000-0000-0000-0000000000c1"
_PRODUCTION_REASON = "L4 gate: candle distortion — keep halted until probation lands (halt 11)"


def _alembic(url: str, *args: str) -> None:
    result = subprocess.run(["uv", "run", "alembic", *args], cwd=_BACKEND,
                            env=dict(os.environ, DATABASE_URL=url), capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr


def _reset(engine) -> None:
    with engine.begin() as conn:
        conn.exec_driver_sql("DROP SCHEMA IF EXISTS projection_audit CASCADE")
        conn.exec_driver_sql("DROP SCHEMA IF EXISTS auth CASCADE")
        conn.exec_driver_sql("DROP SCHEMA public CASCADE")
        conn.exec_driver_sql("CREATE SCHEMA public")
        # Production's default privileges hand new tables to the runtime roles;
        # the migration must take back what it does not mean to grant.
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


@pytest.fixture
def migrated(pg_container):
    url = pg_container.get_connection_url().replace("+psycopg2", "+psycopg")
    engine = create_engine(url)
    _reset(engine)
    _alembic(url, "upgrade", _PREVIOUS)
    ids: dict[str, int] = {}
    with engine.begin() as conn:
        for account in (_A, _B, _C):
            conn.execute(text("INSERT INTO exchange_accounts(id, venue, label) VALUES (:a, 'bitfinex', 'fixture')"),
                         {"a": account})
        # Production today: an operator's safety halt, never cleared.
        _halt(conn, _A, "prod", True, "maintenance", "earlier pause", "admin-api", 100)
        _halt(conn, _A, "prod", False, "maintenance", "pause over", "admin-api", 200)
        ids["prod"] = _halt(conn, _A, "prod", True, "safety", _PRODUCTION_REASON, "worker", 300)
        ids["pause"] = _halt(conn, _A, "ci", True, "maintenance", "pg 18.6 upgrade", "will", 400)
        _halt(conn, _B, "prod", True, "release", "release_blocked", "worker", 500)
        ids["resumed"] = _halt(conn, _B, "prod", False, "safety", "release_promoted:x", "will", 600)
        ids["release"] = _halt(conn, _C, "prod", True, "release", "release_command_terminal", "worker", 700)
    _alembic(url, "upgrade", "head")
    _alembic(url, "check")
    try:
        yield url, engine, ids
    finally:
        engine.dispose()


def test_current_halt_rows_carry_over_verbatim(migrated):
    _, engine, ids = migrated
    with engine.begin() as conn:
        rows = conn.execute(text("""SELECT exchange_account_id::text, deployment_environment, state, cause,
            actor, reason, created_at_ms, legacy_halt_id, probation_multiplier
            FROM trading_state ORDER BY id""")).all()
    assert [tuple(r) for r in rows] == [
        (_A, "prod", "HALTED", "operator", "worker", _PRODUCTION_REASON, 300, ids["prod"], None),
        (_A, "ci", "REDUCING", "operator", "will", "pg 18.6 upgrade", 400, ids["pause"], None),
        (_B, "prod", "ACTIVE", "operator", "will", "release_promoted:x", 600, ids["resumed"], None),
        (_C, "prod", "HALTED", "operator", "worker", "release_command_terminal", 700, ids["release"], None),
    ]


def test_history_is_append_only(migrated):
    _, engine, _ = migrated
    for sql, message in (("UPDATE trading_state SET state='ACTIVE'", "immutable trading state"),
                         ("DELETE FROM trading_state", "immutable trading state"),
                         ("TRUNCATE trading_state", "immutable trading state")):
        with engine.begin() as conn, pytest.raises(Exception, match=message):
            conn.exec_driver_sql(sql)


@pytest.mark.parametrize(("account", "state", "cause", "message"), [
    (_A, "REDUCING", "operator", "HALTED -> REDUCING"),
    (_A, "ACTIVE", "auto", "HALTED -> ACTIVE by auto"),
    (_B, "HALTED", "material_deploy", "ck_trading_state_cause"),
    (_B, "REDUCING", "kill_switch", "ck_trading_state_cause"),
    (_B, "PAUSED", "operator", "ck_trading_state_(state|cause)"),
])
def test_trigger_and_checks_reject_illegal_transitions(migrated, account, state, cause, message):
    _, engine, _ = migrated
    with engine.begin() as conn, pytest.raises(Exception, match=message):
        _insert(conn, account, state, cause)


def test_material_deploy_cannot_be_relabelled_as_an_operator_pause(migrated):
    _, engine, _ = migrated
    with engine.begin() as conn:
        _insert(conn, _B, "REDUCING", "material_deploy")
    with engine.begin() as conn, pytest.raises(Exception, match="cannot be relabelled"):
        _insert(conn, _B, "REDUCING", "operator")
    with engine.begin() as conn:
        _insert(conn, _B, "ACTIVE", "operator")


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
        assert conn.scalar(text("SELECT count(*) FROM trading_state")) == 5
    with engine.begin() as conn, pytest.raises(Exception, match="permission denied"):
        conn.exec_driver_sql("SET LOCAL ROLE bfx_webapi")
        _insert(conn, _C, "HALTED", "operator")
    with engine.begin() as conn:
        conn.exec_driver_sql("SET LOCAL ROLE bfx_webapi")
        assert conn.scalar(text("SELECT count(*) FROM trading_state")) == 5


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
            with pytest.raises(IllegalTradingTransition, match="HALTED -> REDUCING"):
                await repo(first).transition("REDUCING", cause="operator", actor="t", reason="t")
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


def test_downgrade_keeps_decisions_made_after_the_migration(migrated):
    url, engine, _ = migrated
    with engine.begin() as conn:
        _insert(conn, _B, "HALTED", "kill_switch")
    result = subprocess.run(["uv", "run", "alembic", "downgrade", _PREVIOUS], cwd=_BACKEND,
                            env=dict(os.environ, DATABASE_URL=url), capture_output=True, text=True)
    assert result.returncode != 0
    assert "refuse downgrade of recorded trading state decisions" in result.stdout + result.stderr
    with engine.begin() as conn:
        assert conn.scalar(text("SELECT count(*) FROM trading_state")) == 5
