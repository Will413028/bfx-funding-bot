"""``bot_runs``: the next boot reports, once, a run that ended without recording its end.

The end-to-end tests run the real ``apps.bot._run`` in a child interpreter (the loop
watchdog's ``_exit`` would end the pytest worker) with only ``build_daemon`` replaced by a
daemon whose ``run`` either returns (a clean stop) or wedges the loop past the watchdog
(``BFX_LOOP_WATCHDOG_S=2``, loop blocked 30s); the run row is written to this test's
database by the real ``BotRunRecord``. The only time bound is the watchdog's own.

Mutation checks (one at a time; revert after each):

* skip closing the open runs in ``BotRunRecord._start``:
  ``test_an_open_previous_run_alerts_once_and_is_closed`` (the second boot re-alerts).
* call ``run_record.finish`` before ``daemon.run()`` in ``apps.bot._run_daemon``:
  ``test_a_run_ended_by_the_loop_watchdog_is_reported_by_the_next_boot``.
"""
from __future__ import annotations

import logging
import os
import subprocess
import sys
import textwrap
from uuid import UUID, uuid4

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from bfx_funding_bot.modules.observability.bot_runs import BotRunRecord
from tests.pg_templates import alembic

pytestmark = pytest.mark.integration

ACCOUNT = UUID("6a1c0d3e-1111-4222-8333-944455556666")
_ALERT_TITLE = "previous bot run ended uncleanly"


@pytest.fixture
def runs_url(pg_head_url: str) -> str:
    engine = create_engine(pg_head_url)
    try:
        with engine.begin() as conn:
            conn.execute(text(
                "INSERT INTO exchange_accounts (id, venue, label, lifecycle_status) "
                "VALUES (:id, 'bitfinex', 'runs', 'active')"), {"id": ACCOUNT})
    finally:
        engine.dispose()
    return pg_head_url.replace("+psycopg", "+asyncpg")


async def _boot(url: str, *, then_stop: bool = True) -> list:  # type: ignore[type-arg]
    """One boot's ``start``; by default the run then stops cleanly, so a later boot
    reports only what this one left behind."""
    engine = create_async_engine(url)
    try:
        record = BotRunRecord(async_sessionmaker(engine, expire_on_commit=False),
                              exchange_account_id=ACCOUNT, deployment_environment="ci")
        closed = await record.start()
        if then_stop:
            await record.finish("clean_stop")
        return closed
    finally:
        await engine.dispose()


def _rows(url: str) -> list[tuple[str | None, int]]:
    engine = create_engine(url.replace("+asyncpg", "+psycopg"))
    try:
        with engine.connect() as conn:
            return [(row.end_reason, row.pid) for row in conn.execute(text(
                "SELECT end_reason, pid FROM bot_runs ORDER BY started_at_ms, run_id"))]
    finally:
        engine.dispose()


async def test_a_clean_finish_is_not_reported(runs_url: str, caplog) -> None:
    engine = create_async_engine(runs_url)
    try:
        first = BotRunRecord(async_sessionmaker(engine, expire_on_commit=False),
                             exchange_account_id=ACCOUNT, deployment_environment="ci")
        assert await first.start() == []
        await first.finish("clean_stop")
    finally:
        await engine.dispose()
    with caplog.at_level(logging.WARNING):
        assert await _boot(runs_url) == []
    assert _ALERT_TITLE not in caplog.text
    assert [reason for reason, _ in _rows(runs_url)] == ["clean_stop", "clean_stop"]


async def test_an_open_previous_run_alerts_once_and_is_closed(runs_url: str, caplog) -> None:
    engine = create_async_engine(runs_url)
    try:
        crashed = BotRunRecord(async_sessionmaker(engine, expire_on_commit=False),
                               exchange_account_id=ACCOUNT, deployment_environment="ci")
        await crashed.start()  # and never finishes
    finally:
        await engine.dispose()
    with caplog.at_level(logging.WARNING):
        reported = await _boot(runs_url)
        again = await _boot(runs_url)
    assert [run.run_id for run in reported] == [crashed.run_id]
    assert again == []  # the second boot closed nothing: the first marked it
    assert caplog.text.count(_ALERT_TITLE) == 1
    assert [reason for reason, _ in _rows(runs_url)] == ["unclean", "clean_stop", "clean_stop"]


async def test_runs_of_another_scope_are_not_reported(runs_url: str) -> None:
    other = uuid4()
    sync = create_engine(runs_url.replace("+asyncpg", "+psycopg"))
    with sync.begin() as conn:
        conn.execute(text("INSERT INTO exchange_accounts (id, venue, label, lifecycle_status) "
                          "VALUES (:id, 'bitfinex', 'other', 'active')"), {"id": other})
    sync.dispose()
    engine = create_async_engine(runs_url)
    try:
        foreign = BotRunRecord(async_sessionmaker(engine, expire_on_commit=False),
                               exchange_account_id=other, deployment_environment="ci")
        await foreign.start()
    finally:
        await engine.dispose()
    assert await _boot(runs_url) == []


_CHILD = """
import asyncio
import os
import sys
import time
from types import SimpleNamespace
from uuid import UUID

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from bfx_funding_bot.apps import bot

MODE = sys.argv[1]


async def build_daemon(*, stop_event):
    engine = create_async_engine(os.environ["BOT_RUNS_URL"])

    async def run():
        if MODE == "wedge":
            time.sleep(30)  # the loop is stuck; the watchdog ends the process

    async def nothing():
        return None

    async def dispose():
        await engine.dispose()

    return SimpleNamespace(
        run=run, metrics=None, booted=True, writer_lock=None, tracing=None,
        bitfinex_http=SimpleNamespace(aclose=nothing), venue_aclose=dispose,
        config=SimpleNamespace(phase="live", cells=[], run_duration_hours=None),
        session_factory=async_sessionmaker(engine, expire_on_commit=False),
        account_bootstrap=SimpleNamespace(
            exchange_account_id=UUID(os.environ["BOT_RUNS_ACCOUNT"]),
            deployment_environment="ci"),
    )


bot.build_daemon = build_daemon
asyncio.run(bot._run())
print("exited")
"""


def _child(url: str, mode: str) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "BOT_RUNS_URL": url, "BOT_RUNS_ACCOUNT": str(ACCOUNT),
           "BFX_LOOP_WATCHDOG_S": "2", "TELEGRAM_BOT_TOKEN": "", "TELEGRAM_CHAT_ID": ""}
    return subprocess.run([sys.executable, "-c", textwrap.dedent(_CHILD), mode],
                          capture_output=True, text=True, timeout=120, env=env, check=False)


async def test_a_run_ended_by_the_loop_watchdog_is_reported_by_the_next_boot(
    runs_url: str, caplog,
) -> None:
    child = _child(runs_url, "wedge")
    assert child.returncode != 0
    assert "Timeout" in child.stderr and "exited" not in child.stdout
    assert [reason for reason, _ in _rows(runs_url)] == [None]  # it never said goodbye

    with caplog.at_level(logging.WARNING):
        reported = await _boot(runs_url)
    assert len(reported) == 1
    assert caplog.text.count(_ALERT_TITLE) == 1
    assert [reason for reason, _ in _rows(runs_url)] == ["unclean", "clean_stop"]


async def test_a_run_that_stops_cleanly_is_not_reported(runs_url: str, caplog) -> None:
    child = _child(runs_url, "clean")
    assert child.returncode == 0, child.stderr
    assert [reason for reason, _ in _rows(runs_url)] == ["clean_stop"]
    with caplog.at_level(logging.WARNING):
        assert await _boot(runs_url) == []
    assert _ALERT_TITLE not in caplog.text


def test_the_runtime_role_inserts_runs_and_updates_only_their_end(pg_templates, pg_clone) -> None:
    url = pg_clone(pg_templates.template("empty_database", lambda _url: None))
    engine = create_engine(url)
    try:
        with engine.begin() as conn:
            conn.exec_driver_sql("DO $$ BEGIN IF NOT EXISTS (SELECT FROM pg_roles WHERE "
                                 "rolname='bfx_bot') THEN CREATE ROLE bfx_bot; END IF; END $$")
            conn.exec_driver_sql("ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON TABLES TO bfx_bot")
        alembic(url, "upgrade", "head")
        alembic(url, "check")
        with engine.connect() as conn:
            def table(privilege: str) -> bool:
                return bool(conn.scalar(text(
                    "SELECT has_table_privilege('bfx_bot', 'bot_runs', :p)"), {"p": privilege}))

            def column(name: str) -> bool:
                return bool(conn.scalar(text(
                    "SELECT has_column_privilege('bfx_bot', 'bot_runs', :c, 'UPDATE')"),
                    {"c": name}))

            assert table("SELECT") and table("INSERT")
            assert not table("UPDATE") and not table("DELETE") and not table("TRUNCATE")
            assert column("end_reason") and column("end_recorded_at_ms")
            assert not column("started_at_ms") and not column("run_id")
        alembic(url, "downgrade", "-1")
        with engine.connect() as conn:
            assert conn.scalar(text("SELECT to_regclass('public.bot_runs')")) is None
        alembic(url, "upgrade", "head")
        alembic(url, "check")
    finally:
        with engine.begin() as conn:
            conn.exec_driver_sql("ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE ALL ON TABLES FROM bfx_bot")
        engine.dispose()
