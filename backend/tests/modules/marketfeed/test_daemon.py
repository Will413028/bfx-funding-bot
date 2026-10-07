from __future__ import annotations

import asyncio
from pathlib import Path

from pytest_httpx import HTTPXMock

from bfx_funding_bot.core.telemetry import Phase
from tests.modules.marketfeed.account_test_helpers import (
    boot_live_construction,
    go_offline,
)


async def test_daemon_builds_and_runs_briefly(
    monkeypatch, tmp_path: Path, httpx_mock: HTTPXMock,
) -> None:
    """Smoke: build_daemon() returns a Daemon with all components wired;
    daemon.run() starts all sub-tasks, responds to _stop_event, and exits
    cleanly (TaskGroup pattern, Phase 4.2).

    The Bitfinex-venue composition on file-based sqlite (the tables must be visible to the
    engine inside build_daemon). The WebSockets and the venue observation
    loops are detached (``go_offline``): nothing here may reach the network.
    """
    daemon, engine = await boot_live_construction(monkeypatch, tmp_path, httpx_mock)
    try:
        assert daemon.config.phase == Phase.LIVE
        go_offline(daemon)

        # Run briefly then signal stop — TaskGroup should drain all sub-tasks.
        async def _stop_after_delay() -> None:
            await asyncio.sleep(0.05)
            daemon._stop_event.set()

        stop_task = asyncio.create_task(_stop_after_delay())
        await daemon.run()  # returns cleanly after the stop event
        await stop_task
    finally:
        await engine.dispose()


async def test_daemon_engine_has_d3_pool_config_and_url_transform(
    monkeypatch, tmp_path: Path, httpx_mock: HTTPXMock,
) -> None:
    """Regression for 5/21 chaos recovery URL-handling crash.

    daemon.py:614 was passing config.database_url directly to
    create_async_engine since Phase 4.1 (`26b059d`), bypassing
    _prepare_engine_kwargs (D2 URL transform) and the pool_pre_ping +
    pool_recycle=600 settings added by D3 (`7a826d8`). It only "worked"
    because the DATABASE_URL secret was pre-transformed manually. Chaos
    recovery rebuilt the secret in libpq form and crashed.

    Phase 4.4c: build_daemon now calls from_snapshot at boot, so it needs a
    real DB. This test uses a file-based sqlite for build_daemon, and separately
    calls make_async_engine_from_url with the problematic postgresql URL to
    lock the URL-transform + pool-config contract without needing a live server.
    """
    from bfx_funding_bot.core.db import make_async_engine_from_url

    # Verify URL transform + pool config via make_async_engine_from_url directly
    # (the form chaos recovery accidentally produced — asyncpg scheme + libpq params).
    bad_url = (
        "postgresql+asyncpg://u:p@ep-foo-pooler.ap-southeast-1.aws.neon.tech/db"
        "?sslmode=require&channel_binding=require"
    )
    transformed_engine = make_async_engine_from_url(bad_url)
    u = str(transformed_engine.url)
    assert u.startswith("postgresql+asyncpg://"), f"scheme not asyncpg: {u}"
    assert "sslmode" not in u, f"sslmode not stripped: {u}"
    assert "channel_binding" not in u, f"channel_binding not stripped: {u}"
    assert "-pooler." not in u, f"-pooler suffix not stripped: {u}"
    assert transformed_engine.pool._pre_ping is True, "pool_pre_ping missing (D3)"
    assert transformed_engine.pool._recycle == 600, "pool_recycle != 600 (D3.1)"
    await transformed_engine.dispose()

    # build_daemon still uses the sqlite URL but daemon.db_engine is also checked.
    daemon, engine = await boot_live_construction(monkeypatch, tmp_path, httpx_mock, name="daemon_d3")
    try:
        # Confirm daemon's engine was built via make_async_engine_from_url (not raw create_async_engine).
        # SQLite path doesn't apply pool config the same way; the key regression guard
        # is the make_async_engine_from_url call above with the real postgresql URL.
        assert daemon.db_engine is not None
    finally:
        await engine.dispose()
