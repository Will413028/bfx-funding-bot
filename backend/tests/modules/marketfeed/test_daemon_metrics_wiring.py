"""build_daemon wires DaemonMetrics through every observation point.

Four Golden Signals L2 upgrade — the wiring invariants under test:
- Daemon.metrics exists and is served by the healthz app (`/metrics`).
- StdoutEventSink + DiagnosticsSink carry the SAME DaemonMetrics instance.
- HealthProbe is registered (heartbeat age / threshold / status families).
- bfx_funding_bot logger tree has exactly one LogMetricsHandler → error-rate.
- The shared Bitfinex httpx client has the request/response metric hooks.
- DomainEventBus counts events (behavioral: publish → counter moves).
- Live path: recovery is wrapped in TimedReconcileRecovery (tick latency) and
  the ws dispatcher queue gauges are bound; the executor chain is
  MetricsSubmitMiddleware-outermost.
"""
from __future__ import annotations

import logging
import re
from pathlib import Path
from uuid import uuid4

import pytest
from pytest_httpx import HTTPXMock

from bfx_funding_bot.modules.execution.events import CancelRequested
from bfx_funding_bot.modules.execution.middleware import (
    HeartbeatMiddleware,
    ReservationEmittingMiddleware,
)
from bfx_funding_bot.modules.observability.metrics import (
    DaemonMetrics,
    LogMetricsHandler,
    MetricsSubmitMiddleware,
    TimedReconcileRecovery,
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
async def test_build_daemon_wires_metrics_everywhere(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    httpx_mock: HTTPXMock,
) -> None:
    monkeypatch.setenv("BFX_SERVICE_VERSION", "test-sha")  # avoid git subprocess
    daemon, engine = await boot_live_construction(
        monkeypatch, tmp_path, httpx_mock, name="metrics_live_ci",
        extra_env={"BFX_SERVICE_VERSION": "test-sha"},
    )
    await engine.dispose()

    assert isinstance(daemon.metrics, DaemonMetrics)

    # One instance shared by both sinks.
    assert daemon.monitor._events._metrics is daemon.metrics
    assert daemon.diagnostics._metrics is daemon.metrics

    # Probe collector + info gauge visible in the exposition.
    out = daemon.metrics.render().decode()
    assert "bfx_subtask_heartbeat_threshold_seconds" in out
    assert (
        'bfx_daemon_info{deployment_environment="ci",phase="live",'
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
    await daemon.bus.publish(CancelRequested(
        venue_offer_id="99", requested_at_ms=1000, signal_correlation_id=uuid4(),
        account_id="default",
    ))
    assert daemon.metrics.registry.get_sample_value(
        "bfx_domain_events_total", {"event_type": "CancelRequested"},
    ) == 1.0


@pytest.mark.asyncio
async def test_build_daemon_live_wires_reconcile_timing_and_queue_gauges(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    httpx_mock: HTTPXMock,
) -> None:
    safety_live = Path(__file__).parents[3] / "configs" / "safety.live.yaml"
    monkeypatch.setenv("BFX_PHASE", "live")
    monkeypatch.setenv("BFX_DEPLOYMENT_ENV", "prod")
    monkeypatch.setenv("BFX_SAFETY_CONFIG", str(safety_live))
    monkeypatch.setenv("BFX_EXECUTION_POLICY", "book_guarded")
    monkeypatch.setenv("BFX_BOOK_MAX_AGE_SECONDS", "30")
    monkeypatch.setenv("BFX_BOOK_RECONCILE_INTERVAL_SECONDS", "15")
    monkeypatch.setenv("BFX_BOOK_MAX_DOWN_PCT", "0.15")
    configure_live_wiring_env(monkeypatch, tmp_path)
    await _prepare_env(monkeypatch, tmp_path, httpx_mock, db_name="metrics_live.db")
    monkeypatch.delenv("BFX_ALLOCATION_CAP_USDT", raising=False)

    from bfx_funding_bot.apps.bot import build_daemon
    daemon = await build_daemon(
        cells_yaml_path=_write_cells_yaml(tmp_path), skip_ws=True,
    )

    assert isinstance(daemon.metrics, DaemonMetrics)
    assert daemon.metrics.registry.get_sample_value("bfx_trading_ready") == 0.0

    # Reconcile backbone timing: PeriodicReconcile's recovery is the timing
    # wrapper; PeriodicReconcile itself is untouched.
    assert daemon.periodic_reconcile is not None
    assert isinstance(daemon.periodic_reconcile._recovery, TimedReconcileRecovery)
    deployment = daemon.periodic_reconcile._deployment
    assert deployment is not None
    metrics_executor = deployment._executor
    assert isinstance(metrics_executor, MetricsSubmitMiddleware)
    heartbeat = metrics_executor._inner
    assert isinstance(heartbeat, HeartbeatMiddleware)
    reservation = heartbeat._inner
    assert isinstance(reservation, ReservationEmittingMiddleware)
    assert reservation._command_gate is not None
    assert reservation._command_gate._safety_evaluator is daemon.safety_chain
    assert reservation._command_gate is daemon.command_gate

    # WS dispatcher queue saturation gauges bound to the live dispatcher.
    assert daemon.ws_dispatcher is not None
    assert daemon.metrics.registry.get_sample_value(
        "bfx_ws_dispatcher_queue_depth",
    ) == 0.0
    assert daemon.metrics.registry.get_sample_value(
        "bfx_ws_dispatcher_queue_capacity",
    ) == float(daemon.ws_dispatcher.queue_capacity)
