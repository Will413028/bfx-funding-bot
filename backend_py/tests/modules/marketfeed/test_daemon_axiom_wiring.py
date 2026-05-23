"""build_daemon wires the SAME deployment_env into emit + replay paths.

Invariant: AxiomClient._resource.deployment_environment ==
AxiomReplayQueryAdapter._deployment_environment. If these drift, the daemon
emits to one env but queries another → silent replay miss.
"""
from __future__ import annotations

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
    monkeypatch.setenv("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
    monkeypatch.setenv("BFX_HEALTHZ_PORT", "0")
    monkeypatch.setenv("BFX_ACCOUNT_ID", "default")
    monkeypatch.setenv("BFX_API_KEY", "test_key")
    monkeypatch.setenv("BFX_API_SECRET", "test_secret")
    monkeypatch.setenv("BFX_ALLOCATION_CAP_USDT", "500")
    monkeypatch.delenv("BFX_EXECUTOR", raising=False)
    monkeypatch.delenv("BFX_FILL_TRACKER_ENABLED", raising=False)

    httpx_mock.add_response(
        url="https://api.axiom.co/v1/datasets/x/ingest",
        method="POST", status_code=200, json={"ingested": 1},
        is_reusable=True, is_optional=True,
    )
    httpx_mock.add_response(
        url="https://api.axiom.co/v1/datasets/_apl?format=tabular",
        method="POST", status_code=200, json={"tables": []},
        is_reusable=True, is_optional=True,
    )

    from bfx_funding_bot.modules.marketfeed.daemon import build_daemon
    daemon = await build_daemon(
        cells_yaml_path=_write_cells_yaml(tmp_path), skip_ws=True,
    )

    client_env = daemon.axiom._resource.deployment_environment
    adapter_env = daemon.axiom_query._deployment_environment
    assert client_env is adapter_env
    assert client_env.value == env_value
