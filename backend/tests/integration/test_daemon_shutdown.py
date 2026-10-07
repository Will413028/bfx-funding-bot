"""D5 — daemon.run() lifecycle: happy-path SIGTERM + no-hang + fatal escalation.

The daemon is the Bitfinex-venue composition on migrated-schema PostgreSQL (so the single-writer
advisory lock is really acquired). Nothing reaches the network: public REST is served by
``httpx_mock`` and ``go_offline`` detaches the WebSocket tasks and the venue observation loops.
"""
import asyncio
import contextlib
import os
import re

import pytest

from bfx_funding_bot.apps.bot import build_daemon
from tests.modules.marketfeed.account_test_helpers import (
    go_offline,
    live_construction_env,
    seed_exchange_account,
)

pytestmark = pytest.mark.integration

_CELLS_YAML = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "configs", "cells.yaml")
)


async def _set_daemon_env(monkeypatch, pg_engine, httpx_mock) -> None:
    """Set required env vars for build_daemon using testcontainer DB."""
    # render_as_string(hide_password=False): str(URL) masks the password as
    # "***", so the daemon's create_async_engine connects with the wrong
    # password (InvalidPasswordError). The daemon needs the real password.
    live_construction_env(
        monkeypatch, pg_engine.url.render_as_string(hide_password=False),
        BFX_CELLS_YAML=_CELLS_YAML,
    )
    await seed_exchange_account(pg_engine)
    httpx_mock.add_response(
        url=re.compile(r"https://api-pub\.bitfinex\.com/.*"),
        method="GET", status_code=200, json=[], is_reusable=True, is_optional=True,
    )


@contextlib.asynccontextmanager
async def _daemon():
    daemon = await build_daemon(skip_ws=True)
    go_offline(daemon)
    try:
        yield daemon
    finally:
        # The writer lock is per account and realm: free it for the next test.
        if daemon.writer_lock is not None:
            await daemon.writer_lock.release()
        await daemon.bitfinex_http.aclose()
        await daemon.db_engine.dispose()


async def test_daemon_shutdown_happy_path(monkeypatch, pg_session_factory, pg_engine, httpx_mock):
    """SIGTERM-equivalent stop_event.set() → daemon.run() returns within 10s.

    Verify the TaskGroup cancel chain completes cleanly (no hung tasks).
    Does NOT assert heartbeat timing because health_check only fires after 30s.
    """
    await _set_daemon_env(monkeypatch, pg_engine, httpx_mock)

    async with _daemon() as daemon:
        run_task = asyncio.create_task(daemon.run())
        await asyncio.sleep(0.5)
        daemon._stop_event.set()

        try:
            await asyncio.wait_for(run_task, timeout=10.0)
        except TimeoutError:
            pytest.fail("daemon.run() did not return within 10s after stop_event")
        except BaseExceptionGroup as eg:
            # CancelledError group is acceptable on shutdown
            for e in eg.exceptions:
                if not isinstance(e, asyncio.CancelledError):
                    raise


async def test_daemon_shutdown_does_not_hang_on_normal_stop(
    monkeypatch, pg_engine, pg_session_factory, httpx_mock,
):
    """SIGTERM-equivalent stop_event.set() → daemon.run() returns within 10s.

    Weak assertion: our TaskGroup design relies on (1) each sub-task self-exits
    on stop_event, (2) TaskGroup waits for siblings to drain, (3) Docker's
    stop grace period (`stop_grace_period: 30s` in
    deploy/vm/docker-compose.app.yml) force-kills if anything hangs. No
    daemon-level wait_for enforcement — that's delegated to the container stop.

    The test verifies happy-path no-hang under normal stop. Hung sub-tasks are
    covered by the container's SIGKILL after the grace period (not tested here).
    """
    await _set_daemon_env(monkeypatch, pg_engine, httpx_mock)

    async with _daemon() as daemon:
        run_task = asyncio.create_task(daemon.run())
        await asyncio.sleep(0.5)
        daemon._stop_event.set()

        try:
            await asyncio.wait_for(run_task, timeout=10.0)
        except TimeoutError:
            pytest.fail("daemon.run() did not return within 10s after stop_event")
        except BaseExceptionGroup:
            pass


async def test_daemon_subtask_fatal_escalates(
    monkeypatch, pg_engine, pg_session_factory, httpx_mock,
):
    """Injected FatalError in one sub-task → TaskGroup cancels all + raises."""
    from bfx_funding_bot.core.errors import FatalError

    await _set_daemon_env(monkeypatch, pg_engine, httpx_mock)

    async with _daemon() as daemon:
        # Force candle_writer to raise FatalError immediately on run
        async def boom_run():
            await asyncio.sleep(0.1)
            raise FatalError("synthetic candle_writer fatal")

        daemon.writer.run = boom_run  # type: ignore[method-assign]

        with pytest.raises(BaseExceptionGroup) as exc_info:
            await daemon.run()

        # Flatten group, extract FatalError or chained
        def _flatten(eg):
            for e in eg.exceptions:
                if isinstance(e, BaseExceptionGroup):
                    yield from _flatten(e)
                else:
                    yield e

        excs = list(_flatten(exc_info.value))
        assert any(isinstance(e, FatalError) for e in excs), \
            f"expected FatalError in group, got {[type(e).__name__ for e in excs]}"
