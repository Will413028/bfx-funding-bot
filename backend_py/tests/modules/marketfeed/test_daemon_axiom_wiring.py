"""build_daemon wires the SAME deployment_env into emit + PG-store paths.

Phase 4.4c: AxiomReplayQueryAdapter removed from boot path (replaced by
PG from_snapshot). The emit-env invariant is now:
  AxiomClient._resource.deployment_environment.value == BFX_DEPLOYMENT_ENV

The old adapter_env == client_env assertion is removed; the PG-store env is
thread-checked via PostgresEventStore at unit level instead.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest
from pytest_httpx import HTTPXMock


def _write_cells_yaml(tmp_path: Path) -> Path:
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
    return yaml_path


@pytest.mark.parametrize("env_value", ["prod", "shadow", "ci"])
@pytest.mark.asyncio
async def test_build_daemon_emit_and_query_env_symmetric(
    env_value: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    httpx_mock: HTTPXMock,
) -> None:
    monkeypatch.setenv("BFX_PHASE", "paper")
    monkeypatch.setenv("AXIOM_API_KEY", "x")
    monkeypatch.setenv("AXIOM_DATASET", "x")
    monkeypatch.setenv("BFX_DEPLOYMENT_ENV", env_value)
    monkeypatch.setenv("BFX_SERVICE_VERSION", "test-sha")  # avoid git subprocess
    # Phase 4.4c: file-based sqlite so event-store tables created below are
    # visible to build_daemon's engine (from_snapshot uses them at boot).
    db_path = tmp_path / "daemon_wiring.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{db_path}")
    monkeypatch.setenv("BFX_HEALTHZ_PORT", "0")
    monkeypatch.setenv("BFX_ACCOUNT_ID", "default")
    monkeypatch.setenv("BFX_API_KEY", "test_key")
    monkeypatch.setenv("BFX_API_SECRET", "test_secret")
    monkeypatch.setenv("BFX_ALLOCATION_CAP_USDT", "500")
    monkeypatch.delenv("BFX_EXECUTOR", raising=False)
    monkeypatch.delenv("BFX_FILL_TRACKER_ENABLED", raising=False)

    import bfx_funding_bot.modules.execution.event_store.tables  # noqa: F401
    from bfx_funding_bot.core.db import Base, make_async_engine_from_url
    _eng = make_async_engine_from_url(f"sqlite+aiosqlite:///{db_path}")
    async with _eng.begin() as _c:
        await _c.run_sync(Base.metadata.create_all)
    await _eng.dispose()

    httpx_mock.add_response(
        url="https://api.axiom.co/v1/datasets/x/ingest",
        method="POST", status_code=200, json={"ingested": 1},
        is_reusable=True, is_optional=True,
    )
    # warmup_cell fetches Bitfinex candles with file-based sqlite.
    httpx_mock.add_response(
        url=re.compile(r"https://api-pub\.bitfinex\.com/.*"),
        method="GET", status_code=200, json=[],
        is_reusable=True, is_optional=True,
    )
    # Phase 4.4c: _apl mock removed (AxiomReplayQueryAdapter no longer used at boot).

    from bfx_funding_bot.modules.marketfeed.daemon import build_daemon
    daemon = await build_daemon(
        cells_yaml_path=_write_cells_yaml(tmp_path), skip_ws=True,
    )

    # Invariant: AxiomClient emit env must match BFX_DEPLOYMENT_ENV.
    # Phase 4.4c: adapter_env assertion removed (AxiomReplayQueryAdapter no longer
    # in the boot path; PG-store env correctness covered by unit tests on build_daemon).
    client_env = daemon.axiom._resource.deployment_environment
    assert client_env.value == env_value
