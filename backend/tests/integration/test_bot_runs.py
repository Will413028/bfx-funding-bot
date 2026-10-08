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

import contextlib
import logging
import os
import subprocess
import sys
import textwrap
import threading
import time
from uuid import UUID, uuid4

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from bfx_funding_bot.modules.observability.bot_runs import BotRunRecord
from tests.pg_templates import LAST_REVERSIBLE_REVISION, alembic

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
        # bot_runs is LAST_REVERSIBLE_REVISION itself; its downgrade below starts there. The
        # drift check runs at head, after the round trip.
        alembic(url, "upgrade", LAST_REVERSIBLE_REVISION)

        def assert_the_bot_inserts_and_updates_only_the_end() -> None:
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

        assert_the_bot_inserts_and_updates_only_the_end()
        alembic(url, "downgrade", "a6c7e8f9b0d1")
        with engine.connect() as conn:
            assert conn.scalar(text("SELECT to_regclass('public.bot_runs')")) is None
        alembic(url, "upgrade", "head")
        alembic(url, "check")
        assert_the_bot_inserts_and_updates_only_the_end()
    finally:
        with engine.begin() as conn:
            conn.exec_driver_sql("ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE ALL ON TABLES FROM bfx_bot")
        engine.dispose()


# ── a database that stops answering without closing the socket ─────────────────


@pytest.fixture
def frozen_pg(tmp_path):  # type: ignore[no-untyped-def]
    """A throwaway PG18 of this test's own, migrated to head with the account seeded.

    Yields ``(async_url, freeze)``; ``freeze()`` SIGSTOPs the postmaster and every backend
    (the reviewer's reproduction of an unresponsive database) and thaws them again after
    15s, so an unbounded wait fails the elapsed-time assertion instead of hanging the
    test. SIGCONT and an immediate stop always run on teardown."""
    import shutil
    import signal
    import socket

    from tests import pg_local
    from tests.pg_templates import stamp_realm

    bin_dir = pg_local.find_pg_bin()
    assert bin_dir is not None, pg_local.missing_bin_message()
    data = tmp_path / "pgdata"
    sockets = pg_local.short_socket_directory()
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    subprocess.run([str(bin_dir / "initdb"), "-D", str(data), "-U", "postgres", "-A", "trust",
                    *pg_local.INITDB_LOCALE_OPTIONS], check=True, capture_output=True)
    subprocess.run([str(bin_dir / "pg_ctl"), "-D", str(data), "-w", "-l", str(tmp_path / "pg.log"),
                    "-o", f"-p {port} -k {sockets} -c listen_addresses=127.0.0.1", "start"],
                   check=True, capture_output=True)
    stopped: list[int] = []

    def thaw() -> None:
        for pid in stopped:
            with contextlib.suppress(ProcessLookupError):
                os.kill(pid, signal.SIGCONT)

    def freeze() -> None:
        postmaster = int((data / "postmaster.pid").read_text().split()[0])
        children = subprocess.run(["pgrep", "-P", str(postmaster)], capture_output=True,
                                  text=True, check=False).stdout.split()
        for pid in [*map(int, children), postmaster]:
            os.kill(pid, signal.SIGSTOP)
            stopped.append(pid)
        timer = threading.Timer(15.0, thaw)
        timer.daemon = True
        timer.start()

    try:
        sync_url = f"postgresql+psycopg://postgres@127.0.0.1:{port}/postgres"
        alembic(sync_url, "upgrade", "head")
        stamp_realm(sync_url, "ci")
        engine = create_engine(sync_url)
        with engine.begin() as conn:
            conn.execute(text("INSERT INTO exchange_accounts (id, venue, label, lifecycle_status) "
                              "VALUES (:id, 'bitfinex', 'frozen', 'active')"), {"id": ACCOUNT})
        engine.dispose()
        yield sync_url.replace("+psycopg", "+asyncpg"), freeze
    finally:
        thaw()
        subprocess.run([str(bin_dir / "pg_ctl"), "-D", str(data), "-m", "immediate", "stop"],
                       capture_output=True, check=False)
        shutil.rmtree(sockets, ignore_errors=True)


async def test_the_end_of_a_run_is_bounded_when_the_database_stops_answering(frozen_pg) -> None:
    url, freeze = frozen_pg
    engine = create_async_engine(url)
    record = BotRunRecord(async_sessionmaker(engine, expire_on_commit=False),
                          exchange_account_id=ACCOUNT, deployment_environment="ci")
    await record.start()
    freeze()
    started = time.monotonic()
    await record.finish("clean_stop", timeout_s=2.0)
    elapsed = time.monotonic() - started
    assert elapsed < 2.0 + 1.0, f"finish took {elapsed:.1f}s against a frozen database"


async def test_the_writer_lock_release_is_bounded_when_the_database_stops_answering(
    frozen_pg,
) -> None:
    from bfx_funding_bot.core.bounded import run_bounded
    from bfx_funding_bot.core.writer_lock import WriterLock

    url, freeze = frozen_pg
    lock = WriterLock(database_url=url.replace("+asyncpg", ""), key=42)
    await lock.acquire()
    freeze()
    started = time.monotonic()
    released = await run_bounded(lock.release(), timeout_s=2.0, what="test_release")
    elapsed = time.monotonic() - started
    assert released is False
    assert elapsed < 2.0 + 1.0, f"release took {elapsed:.1f}s against a frozen database"
