from __future__ import annotations

import asyncio
from pathlib import Path

from pytest_httpx import HTTPXMock

from bfx_funding_bot.modules.marketfeed.daemon import build_daemon
from bfx_funding_bot.modules.marketfeed.schemas import Phase


async def test_daemon_starts_and_shuts_down_cleanly(
    monkeypatch, tmp_path: Path, httpx_mock: HTTPXMock,
) -> None:
    """Smoke: build_daemon() returns a Daemon with all components wired;
    daemon.startup() and daemon.shutdown() complete without exception."""
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
    # Axiom flush during shutdown — accept any POST to ingest endpoint.
    httpx_mock.add_response(
        url="https://api.axiom.co/v1/datasets/x/ingest",
        method="POST", status_code=200, json={"ingested": 1},
        is_reusable=True, is_optional=True,
    )

    daemon = await build_daemon(cells_yaml_path=yaml_path, skip_ws=True)
    assert daemon.config.phase == Phase.PAPER

    await daemon.startup()
    await asyncio.sleep(0.05)
    await daemon.shutdown()
