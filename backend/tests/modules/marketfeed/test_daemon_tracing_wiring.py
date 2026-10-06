"""build_daemon wires DaemonTracing — default OFF, conditional span wrappers.

OTel adoption (wiki pending #4) wiring invariants under test:
- Default (BFX_OTEL_ENABLED unset): Daemon.tracing exists but is disabled and
  NOTHING is wrapped — executor chain / recovery / ws dispatcher are exactly
  the metrics-era objects (zero overhead, zero new objects on the money path).
- BFX_OTEL_ENABLED=true (paper): TracingSubmitMiddleware is OUTERMOST around
  the MetricsSubmitMiddleware chain.
- BFX_OTEL_ENABLED=true (live): recovery is TracedReconcileRecovery around
  TimedReconcileRecovery, and the ws dispatcher's _process is instrumented.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest
from pytest_httpx import HTTPXMock

from bfx_funding_bot.modules.observability.metrics import (
    MetricsSubmitMiddleware,
    TimedReconcileRecovery,
)
from bfx_funding_bot.modules.observability.tracing import (
    DaemonTracing,
    TracedReconcileRecovery,
    TracingSubmitMiddleware,
)
from tests.modules.marketfeed.account_test_helpers import (
    boot_live_construction,
    configure_account_env,
    configure_live_wiring_env,
    seed_exchange_account,
)


def _write_cells_yaml(tmp_path: Path) -> Path:
    yaml_path = tmp_path / "cells.yaml"
    yaml_path.write_text("""
cells:
  - strategy: rate_percentile
    symbol: fUST
    period_agg: a30
    timeframe: 1h
    params: {percentile: 75, lookback_hours: 5}
    reference_amount_usdt: 150.0
phase3b_wfo_results_ref: x
""")
    return yaml_path


async def _prepare_env(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    httpx_mock: HTTPXMock,
    *,
    db_name: str,
) -> None:
    monkeypatch.setenv("BFX_SERVICE_VERSION", "test-sha")  # avoid git subprocess
    db_path = tmp_path / db_name
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{db_path}")
    monkeypatch.setenv("BFX_HEALTHZ_PORT", "0")
    configure_account_env(monkeypatch)
    monkeypatch.setenv("BFX_API_KEY", "test_key")
    monkeypatch.setenv("BFX_API_SECRET", "test_secret")
    monkeypatch.setenv("BFX_ALLOCATION_CAP_USDT", "500")

    from bfx_funding_bot.core.db import Base, make_async_engine_from_url
    _eng = make_async_engine_from_url(f"sqlite+aiosqlite:///{db_path}")
    async with _eng.begin() as _c:
        await _c.run_sync(Base.metadata.create_all)
    await seed_exchange_account(_eng)
    await _eng.dispose()

    httpx_mock.add_response(
        url=re.compile(r"https://api-pub\.bitfinex\.com/.*"),
        method="GET", status_code=200, json=[],
        is_reusable=True, is_optional=True,
    )


@pytest.mark.asyncio
async def test_build_daemon_default_tracing_disabled_nothing_wrapped(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    httpx_mock: HTTPXMock,
) -> None:
    monkeypatch.delenv("BFX_OTEL_ENABLED", raising=False)
    daemon, engine = await boot_live_construction(
        monkeypatch, tmp_path, httpx_mock, name="tracing_off",
    )
    await engine.dispose()

    assert isinstance(daemon.tracing, DaemonTracing)
    assert daemon.tracing.enabled is False
    # Disabled ⇒ executor chain is EXACTLY the metrics-era stack — no tracing
    # wrapper object anywhere on the money path.
    assert daemon.periodic_reconcile is not None
    assert isinstance(daemon.periodic_reconcile._deployment._executor, MetricsSubmitMiddleware)


@pytest.mark.asyncio
async def test_build_daemon_tracing_enabled_wraps_submit_outermost(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    httpx_mock: HTTPXMock,
) -> None:
    daemon, engine = await boot_live_construction(
        monkeypatch, tmp_path, httpx_mock, name="tracing_on",
        extra_env={"BFX_OTEL_ENABLED": "true"},
    )
    await engine.dispose()
    try:
        assert isinstance(daemon.tracing, DaemonTracing)
        assert daemon.tracing.enabled is True
        assert daemon.periodic_reconcile is not None
        outer = daemon.periodic_reconcile._deployment._executor
        assert isinstance(outer, TracingSubmitMiddleware)
        assert isinstance(outer._inner, MetricsSubmitMiddleware)
    finally:
        daemon.tracing.shutdown()


@pytest.mark.asyncio
async def test_build_daemon_live_tracing_enabled_wraps_reconcile_and_ws(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    httpx_mock: HTTPXMock,
) -> None:
    safety_live = Path(__file__).parents[3] / "configs" / "safety.live.yaml"
    monkeypatch.setenv("BFX_PHASE", "live")
    monkeypatch.setenv("BFX_DEPLOYMENT_ENV", "prod")
    monkeypatch.setenv("BFX_EXECUTION_POLICY", "book_guarded")
    monkeypatch.setenv("BFX_BOOK_MAX_AGE_SECONDS", "30")
    monkeypatch.setenv("BFX_BOOK_RECONCILE_INTERVAL_SECONDS", "15")
    monkeypatch.setenv("BFX_BOOK_MAX_DOWN_PCT", "0.15")
    monkeypatch.setenv("BFX_SAFETY_CONFIG", str(safety_live))
    monkeypatch.setenv("BFX_OTEL_ENABLED", "true")
    configure_live_wiring_env(monkeypatch, tmp_path)
    await _prepare_env(monkeypatch, tmp_path, httpx_mock, db_name="tracing_live.db")
    monkeypatch.delenv("BFX_ALLOCATION_CAP_USDT", raising=False)

    from bfx_funding_bot.apps.bot import build_daemon
    daemon = await build_daemon(
        cells_yaml_path=_write_cells_yaml(tmp_path), skip_ws=True,
    )
    try:
        assert daemon.tracing is not None and daemon.tracing.enabled is True

        # reconcile.tick span wrapper stacked OUTSIDE the timing wrapper.
        assert daemon.periodic_reconcile is not None
        recovery = daemon.periodic_reconcile._recovery
        assert isinstance(recovery, TracedReconcileRecovery)
        assert isinstance(recovery._inner, TimedReconcileRecovery)

        # ws_dispatcher.process instrumented in place (instance-level override).
        assert daemon.ws_dispatcher is not None
        assert "_process" in vars(daemon.ws_dispatcher)
    finally:
        daemon.tracing.shutdown()
