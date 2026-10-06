"""``apps.ledger_seed --switch`` and ``--check`` against the real schema (PR-3, switch pre-flight §F).

The legacy runtime that built a closure to switch over is gone (S1-8); the switch itself ran in
prod on 2026-10-05 and its scaffolding goes in S1-8 PR-D. What is left needs no legacy history:
``--switch`` refuses without a readable cells file before it touches the database, and
``--check`` is one READ ONLY transaction without locks that reports its refusals (here the
genesis ``ledger`` epoch of a database migrated to head) and writes nothing.

Mutations (apply one at a time, run this file, revert; the test that fails is named):

* ``check`` takes the writer locks (``quiesce``): ``test_check_writes_nothing_and_takes_no_lock``.
"""
from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import AsyncEngine

from bfx_funding_bot.apps import ledger_seed as seed_app
from bfx_funding_bot.modules.ledger.tables import LEDGER_TABLES, CapitalAuthorityEpochRow

from .bot_e2e import (
    T0,
    BotEnv,
    bot_env,  # noqa: F401 - fixture
    ledger_db,  # noqa: F401 - fixture dependency
)
from .seed_e2e import run_seed, seed_command, table_count

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

SEED_AT = T0 + 100_000


def switch_command(env: BotEnv, tmp_path: Any, url: Any, **kwargs: Any) -> list[str]:
    return [*seed_command(env, tmp_path, url=url, **kwargs), "--switch", "--cells",
            str(env.cells_path)]


def check_command(env: BotEnv, tmp_path: Any, url: Any, **kwargs: Any) -> list[str]:
    argv = seed_command(env, tmp_path, url=url, authorize=False, **kwargs)
    return [*argv, "--check", "--cells", str(env.cells_path)]


async def epochs(env: BotEnv) -> list[CapitalAuthorityEpochRow]:
    async with env.factory() as session:
        return list(await session.scalars(
            select(CapitalAuthorityEpochRow).order_by(CapitalAuthorityEpochRow.epoch_seq)))


async def assert_nothing_committed(env: BotEnv) -> None:
    for table in LEDGER_TABLES:
        assert await table_count(env, table.name) == 0, table.name
    # The genesis epochs of a database migrated to head, and nothing appended.
    assert [row.authority for row in await epochs(env)] == ["legacy", "ledger"]


async def test_switch_requires_the_cells_file(
    bot_env: BotEnv, ledger_db: Any, tmp_path: Any,  # noqa: F811
) -> None:
    env = bot_env
    argv = [*seed_command(env, tmp_path, url=ledger_db.url), "--switch"]
    code, lines = await run_seed(argv, now_ms=SEED_AT)
    assert (code, lines[-1]["reason"]) == (seed_app.EXIT_REFUSED, "cells_required")
    missing = [*seed_command(env, tmp_path, url=ledger_db.url), "--switch", "--cells",
               str(tmp_path / "absent.yaml")]
    code, lines = await run_seed(missing, now_ms=SEED_AT)
    assert (code, lines[-1]["reason"]) == (seed_app.EXIT_REFUSED, "cells_unreadable")
    await assert_nothing_committed(env)


class Statements:
    """Every SQL statement a ``--check`` run sends, captured on its engine."""

    def __init__(self) -> None:
        self.sql: list[str] = []

    def connector(self, plan: Any) -> AsyncEngine:
        engine = seed_app.connect(plan)

        @event.listens_for(engine.sync_engine, "before_cursor_execute")
        def capture(conn: Any, cursor: Any, statement: str, *args: Any) -> None:
            self.sql.append(" ".join(statement.split()))

        return engine


READ_PREFIXES = ("SELECT", "WITH", "SET TRANSACTION READ ONLY", "SET LOCAL search_path",
                 "BEGIN", "COMMIT", "ROLLBACK", "SHOW")


async def test_check_writes_nothing_and_takes_no_lock(
    bot_env: BotEnv, ledger_db: Any, tmp_path: Any,  # noqa: F811
) -> None:
    """The check reports the database's refusal without a lock or a write."""
    env = bot_env
    statements = Statements()
    code, lines = await run_seed(check_command(env, tmp_path, ledger_db.url), now_ms=SEED_AT,
                                 connector=statements.connector)
    assert code == seed_app.EXIT_REFUSED, lines
    assert [r["reason"] for r in lines[-1]["refusals"] if r["scope"] is None] == [
        "epoch_not_legacy"]
    assert (lines[-1]["mode"], lines[-1]["committed"]) == ("check", False)
    assert statements.sql
    offending = [sql for sql in statements.sql if not sql.upper().startswith(
        tuple(p.upper() for p in READ_PREFIXES))]
    assert offending == []
    assert not [sql for sql in statements.sql if "advisory" in sql or "FOR UPDATE" in sql.upper()
                or "FOR SHARE" in sql.upper()]
    assert any(sql == "SET TRANSACTION READ ONLY" for sql in statements.sql)
    await assert_nothing_committed(env)
