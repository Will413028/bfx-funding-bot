"""build_daemon wires DaemonMetrics through every observation point.

Four Golden Signals L2 upgrade — the wiring invariants under test:
- Daemon.metrics exists and is served by the healthz app (`/metrics`).
- StdoutEventSink + DiagnosticsSink carry the SAME DaemonMetrics instance.
- HealthProbe is registered (heartbeat age / threshold / status families).
- bfx_funding_bot logger tree has exactly one LogMetricsHandler → error-rate.
- The shared Bitfinex httpx client has the request/response metric hooks.
- DomainEventBus counts events (behavioral: publish → counter moves).
- Live path: recovery is wrapped in TimedReconcileRecovery (tick latency) and
  the ws dispatcher queue gauges are bound; smoke path executor chain is
  MetricsSubmitMiddleware-outermost (paper).
"""
from __future__ import annotations

import logging
import re
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest
from pytest_httpx import HTTPXMock

from bfx_funding_bot.modules.execution.events import ReservationClaimed
from bfx_funding_bot.modules.observability.metrics import (
    DaemonMetrics,
    LogMetricsHandler,
    MetricsSubmitMiddleware,
    TimedReconcileRecovery,
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
    monkeypatch.setenv("BFX_ACCOUNT_ID", "default")
    monkeypatch.setenv("BFX_API_KEY", "test_key")
    monkeypatch.setenv("BFX_API_SECRET", "test_secret")
    monkeypatch.setenv("BFX_ALLOCATION_CAP_USDT", "500")
    monkeypatch.delenv("BFX_FILL_TRACKER_ENABLED", raising=False)

    import bfx_funding_bot.modules.execution.event_store.tables  # noqa: F401
    from bfx_funding_bot.core.db import Base, make_async_engine_from_url
    _eng = make_async_engine_from_url(f"sqlite+aiosqlite:///{db_path}")
    async with _eng.begin() as _c:
        await _c.run_sync(Base.metadata.create_all)
    await _eng.dispose()

    httpx_mock.add_response(
        url=re.compile(r"https://api-pub\.bitfinex\.com/.*"),
        method="GET", status_code=200, json=[],
        is_reusable=True, is_optional=True,
    )


@pytest.mark.asyncio
async def test_build_daemon_paper_wires_metrics_everywhere(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    httpx_mock: HTTPXMock,
) -> None:
    monkeypatch.setenv("BFX_PHASE", "paper")
    monkeypatch.setenv("BFX_DEPLOYMENT_ENV", "ci")
    monkeypatch.delenv("BFX_EXECUTOR", raising=False)
    await _prepare_env(monkeypatch, tmp_path, httpx_mock, db_name="metrics_paper.db")

    from bfx_funding_bot.modules.marketfeed.daemon import build_daemon
    daemon = await build_daemon(
        cells_yaml_path=_write_cells_yaml(tmp_path), skip_ws=True,
    )

    assert isinstance(daemon.metrics, DaemonMetrics)

    # One instance shared by both sinks.
    assert daemon.monitor._events._metrics is daemon.metrics
    assert daemon.diagnostics._metrics is daemon.metrics

    # Probe collector + info gauge visible in the exposition.
    out = daemon.metrics.render().decode()
    assert "bfx_subtask_heartbeat_threshold_seconds" in out
    assert (
        'bfx_daemon_info{deployment_environment="ci",phase="paper",'
        'service_version="test-sha"} 1.0'
    ) in out

    # Log-derived error counter installed exactly once on the bfx tree.
    ours = [
        h for h in logging.getLogger("bfx_funding_bot").handlers
        if isinstance(h, LogMetricsHandler)
    ]
    assert len(ours) == 1 and ours[0]._metrics is daemon.metrics

    # Shared Bitfinex httpx client carries the metric hooks.
    hooks = daemon.bitfinex_http.event_hooks
    assert len(hooks["request"]) >= 1 and len(hooks["response"]) >= 1

    # Bus traffic counter — behavioral: publish moves the counter.
    await daemon.bus.publish(ReservationClaimed(
        symbol="fUST", cid=99, venue_offer_id="99",
        signal_correlation_id=uuid4(), account_id="default",
        is_simulated=True, amount=Decimal("100"),
    ))
    assert daemon.metrics.registry.get_sample_value(
        "bfx_domain_events_total", {"event_type": "ReservationClaimed"},
    ) == 1.0

    # Paper smoke runner submits through the metrics-outermost chain.
    assert daemon.smoke_runner is not None
    assert isinstance(daemon.smoke_runner._executor, MetricsSubmitMiddleware)


@pytest.mark.asyncio
async def test_build_daemon_live_wires_reconcile_timing_and_queue_gauges(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    httpx_mock: HTTPXMock,
) -> None:
    safety_canary = Path(__file__).parents[3] / "configs" / "safety.canary.yaml"
    monkeypatch.setenv("BFX_PHASE", "canary")
    monkeypatch.setenv("BFX_DEPLOYMENT_ENV", "prod")
    monkeypatch.setenv("BFX_SAFETY_CONFIG", str(safety_canary))
    monkeypatch.setenv("BFX_EXECUTOR", "bitfinex_live")
    monkeypatch.setenv("BFX_WS_CLIENT_ENABLED", "true")
    await _prepare_env(monkeypatch, tmp_path, httpx_mock, db_name="metrics_live.db")

    from bfx_funding_bot.modules.marketfeed.daemon import build_daemon
    daemon = await build_daemon(
        cells_yaml_path=_write_cells_yaml(tmp_path), skip_ws=True,
    )

    assert isinstance(daemon.metrics, DaemonMetrics)

    # Reconcile backbone timing: PeriodicReconcile's recovery is the timing
    # wrapper; PeriodicReconcile itself is untouched.
    assert daemon.periodic_reconcile is not None
    assert isinstance(daemon.periodic_reconcile._recovery, TimedReconcileRecovery)

    # WS dispatcher queue saturation gauges bound to the live dispatcher.
    assert daemon.ws_dispatcher is not None
    assert daemon.metrics.registry.get_sample_value(
        "bfx_ws_dispatcher_queue_depth",
    ) == 0.0
    assert daemon.metrics.registry.get_sample_value(
        "bfx_ws_dispatcher_queue_capacity",
    ) == float(daemon.ws_dispatcher.queue_capacity)
