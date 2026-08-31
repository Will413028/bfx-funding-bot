"""D5 — daemon.run() lifecycle: happy-path SIGTERM + no-hang + fatal escalation."""
import asyncio
import os

import pytest

from bfx_funding_bot.modules.marketfeed.daemon import build_daemon

pytestmark = pytest.mark.integration

_CELLS_YAML = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "configs", "cells.yaml")
)


def _set_daemon_env(monkeypatch, pg_engine) -> None:
    """Set required env vars for build_daemon using testcontainer DB."""
    monkeypatch.setenv("BFX_PHASE", "paper")
    monkeypatch.setenv("BFX_DEPLOYMENT_ENV", "ci")
    monkeypatch.setenv("BFX_EXECUTION_POLICY", "paper")
    monkeypatch.setenv("BFX_CELLS_YAML", _CELLS_YAML)
    monkeypatch.setenv("BFX_API_KEY", "test_key")
    monkeypatch.setenv("BFX_API_SECRET", "test_secret")
    # Port 0 → kernel-assigned random port; avoids "address already in use"
    # when a real bfx daemon (or a sibling test) holds the default 8080.
    monkeypatch.setenv("BFX_HEALTHZ_PORT", "0")
    # render_as_string(hide_password=False): str(URL) masks the password as
    # "***", so the daemon's create_async_engine connects with the wrong
    # password (InvalidPasswordError). The daemon needs the real password.
    monkeypatch.setenv(
        "DATABASE_URL", pg_engine.url.render_as_string(hide_password=False)
    )


async def test_daemon_shutdown_happy_path(monkeypatch, pg_session_factory, pg_engine):
    """SIGTERM-equivalent stop_event.set() → daemon.run() returns within 10s.

    Verify the TaskGroup cancel chain completes cleanly (no hung tasks).
    Does NOT assert heartbeat timing because health_check only fires after 30s.
    """
    _set_daemon_env(monkeypatch, pg_engine)

    daemon = await build_daemon(skip_ws=True)
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
    monkeypatch, pg_engine, pg_session_factory,
):
    """SIGTERM-equivalent stop_event.set() → daemon.run() returns within 10s.

    Weak assertion: our TaskGroup design relies on (1) each sub-task self-exits
    on stop_event, (2) TaskGroup waits for siblings to drain, (3) Koyeb container
    grace force-exits if anything hangs. No daemon-level wait_for enforcement
    (no "55s timeout" wrapper) — that's delegated to Koyeb.

    The test verifies happy-path no-hang under normal stop. Hung sub-tasks are
    covered by Koyeb-grade SIGKILL (not unit-tested here).
    """
    _set_daemon_env(monkeypatch, pg_engine)

    daemon = await build_daemon(skip_ws=True)
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
    monkeypatch, pg_engine, pg_session_factory,
):
    """Injected FatalError in one sub-task → TaskGroup cancels all + raises."""
    from bfx_funding_bot.core.errors import FatalError

    _set_daemon_env(monkeypatch, pg_engine)

    daemon = await build_daemon(skip_ws=True)

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
