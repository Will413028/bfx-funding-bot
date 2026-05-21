from __future__ import annotations

import asyncio
from pathlib import Path

from pytest_httpx import HTTPXMock

from bfx_funding_bot.modules.marketfeed.daemon import build_daemon
from bfx_funding_bot.modules.marketfeed.schemas import Phase


async def test_daemon_builds_and_runs_briefly(
    monkeypatch, tmp_path: Path, httpx_mock: HTTPXMock,
) -> None:
    """Smoke: build_daemon() returns a Daemon with all components wired;
    daemon.run() starts all sub-tasks, responds to _stop_event, and exits
    cleanly (TaskGroup pattern, Phase 4.2)."""
    yaml_path = tmp_path / "cells.yaml"
    yaml_path.write_text("""
cells:
  - strategy: rate_percentile
    symbol: fUSD
    period_agg: a30
    timeframe: 1h
    params: {percentile: 75, lookback_hours: 5}
    reference_amount_usdt: 150.0
phase3b_wfo_results_ref: x
""")
    monkeypatch.setenv("BFX_PHASE", "paper")
    monkeypatch.setenv("AXIOM_API_KEY", "x")
    monkeypatch.setenv("AXIOM_DATASET", "x")
    monkeypatch.setenv("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
    # OS-assigned port to avoid 8080 conflicts during parallel runs / dev boxes.
    monkeypatch.setenv("BFX_HEALTHZ_PORT", "0")
    # Phase 4.2 Task 20 execution wiring requires these env vars.
    monkeypatch.setenv("BFX_ACCOUNT_ID", "default")
    monkeypatch.setenv("BFX_API_KEY", "test_key")
    monkeypatch.setenv("BFX_API_SECRET", "test_secret")
    monkeypatch.setenv("BFX_ALLOCATION_CAP_USDT", "500")
    # Axiom flush during shutdown — accept any POST to ingest endpoint.
    httpx_mock.add_response(
        url="https://api.axiom.co/v1/datasets/x/ingest",
        method="POST", status_code=200, json={"ingested": 1},
        is_reusable=True, is_optional=True,
    )

    daemon = await build_daemon(cells_yaml_path=yaml_path, skip_ws=True)
    assert daemon.config.phase == Phase.PAPER

    # Run briefly then signal stop — TaskGroup should drain all sub-tasks.
    async def _stop_after_delay() -> None:
        await asyncio.sleep(0.05)
        daemon._stop_event.set()

    asyncio.create_task(_stop_after_delay())
    await daemon.run()  # should return cleanly after _stop_event is set


async def test_daemon_engine_has_d3_pool_config_and_url_transform(
    monkeypatch, tmp_path: Path, httpx_mock: HTTPXMock,
) -> None:
    """Regression for 5/21 chaos recovery URL-handling crash.

    daemon.py:614 was passing config.database_url directly to
    create_async_engine since Phase 4.1 (`26b059d`), bypassing
    _prepare_engine_kwargs (D2 URL transform) and the pool_pre_ping +
    pool_recycle=600 settings added by D3 (`7a826d8`). It only "worked"
    because Koyeb DATABASE_URL secret was pre-transformed manually. Chaos
    recovery rebuilt the secret from Neon dashboard libpq form and crashed.

    Locks contract: daemon's db_engine must apply D3 pool config and the
    URL must be transformed (no sslmode/channel_binding in query, asyncpg
    scheme, -pooler suffix stripped).
    """
    yaml_path = tmp_path / "cells.yaml"
    yaml_path.write_text("""
cells:
  - strategy: rate_percentile
    symbol: fUSD
    period_agg: a30
    timeframe: 1h
    params: {percentile: 75, lookback_hours: 5}
    reference_amount_usdt: 150.0
""")
    monkeypatch.setenv("BFX_PHASE", "paper")
    monkeypatch.setenv("AXIOM_API_KEY", "x")
    monkeypatch.setenv("AXIOM_DATASET", "x")
    # Asyncpg-scheme URL that still carries libpq query params — the form
    # chaos recovery accidentally produced. Engine creation would currently
    # succeed but connect() would crash with TypeError(sslmode).
    monkeypatch.setenv(
        "DATABASE_URL",
        "postgresql+asyncpg://u:p@ep-foo-pooler.ap-southeast-1.aws.neon.tech/db"
        "?sslmode=require&channel_binding=require",
    )
    monkeypatch.setenv("BFX_HEALTHZ_PORT", "0")
    monkeypatch.setenv("BFX_ACCOUNT_ID", "default")
    monkeypatch.setenv("BFX_API_KEY", "test_key")
    monkeypatch.setenv("BFX_API_SECRET", "test_secret")
    monkeypatch.setenv("BFX_ALLOCATION_CAP_USDT", "500")
    httpx_mock.add_response(
        url="https://api.axiom.co/v1/datasets/x/ingest",
        method="POST", status_code=200, json={"ingested": 1},
        is_reusable=True, is_optional=True,
    )

    daemon = await build_daemon(cells_yaml_path=yaml_path, skip_ws=True)

    u = str(daemon.db_engine.url)
    assert u.startswith("postgresql+asyncpg://"), f"scheme not asyncpg: {u}"
    assert "sslmode" not in u, f"sslmode not stripped: {u}"
    assert "channel_binding" not in u, f"channel_binding not stripped: {u}"
    assert "-pooler." not in u, f"-pooler suffix not stripped: {u}"

    assert daemon.db_engine.pool._pre_ping is True, "pool_pre_ping missing (D3)"
    assert daemon.db_engine.pool._recycle == 600, "pool_recycle != 600 (D3.1)"
