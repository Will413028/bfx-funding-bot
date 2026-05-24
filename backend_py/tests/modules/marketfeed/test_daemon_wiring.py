"""build_daemon wires the SAME deployment_env into the emit + PG-store paths.

Phase 4.4c: AxiomReplayQueryAdapter removed from boot path (replaced by PG
from_snapshot). The emit-env invariant is now checked via StdoutEventSink:
  StdoutEventSink._resource.deployment_environment.value == BFX_DEPLOYMENT_ENV

Phase 3c T10: AxiomClient removed; invariant migrated to stdout_sink path,
accessed via daemon.monitor._events._resource (StdoutEventSink injected into
HealthMonitor which is a Daemon field).
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

    # warmup_cell fetches Bitfinex candles with file-based sqlite.
    httpx_mock.add_response(
        url=re.compile(r"https://api-pub\.bitfinex\.com/.*"),
        method="GET", status_code=200, json=[],
        is_reusable=True, is_optional=True,
    )

    from bfx_funding_bot.modules.marketfeed.daemon import build_daemon
    daemon = await build_daemon(
        cells_yaml_path=_write_cells_yaml(tmp_path), skip_ws=True,
    )

    # Invariant: StdoutEventSink (the live emit path) must be wired with the
    # EventResource that carries BFX_DEPLOYMENT_ENV. Accessed via the
    # HealthMonitor's injected event_sink (both monitor + signal_engine share
    # the same stdout_sink instance, so one check suffices).
    # Phase 4.4c: PG-store env correctness covered by unit tests on build_daemon.
    from bfx_funding_bot.modules.observability.stdout_sink import StdoutEventSink
    sink = daemon.monitor._events
    assert isinstance(sink, StdoutEventSink)
    assert sink._resource.deployment_environment.value == env_value
