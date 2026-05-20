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
