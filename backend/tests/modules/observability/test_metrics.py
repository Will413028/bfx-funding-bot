"""DaemonMetrics — Four Golden Signals Prometheus metrics (observe-only).

Contract under test:
1. Every observe_* method is FAIL-OPEN: a broken metric backend or malformed
   event never raises into the observed path.
2. Wrappers (MetricsSubmitMiddleware / TimedReconcileRecovery) are transparent:
   results and exceptions pass through byte-identical; only counters/histograms
   move on the side.
3. Per-instance CollectorRegistry — no cross-test / cross-daemon collisions.
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest

from bfx_funding_bot.core.health import HealthProbe
from bfx_funding_bot.core.telemetry import HealthStatus, HealthTarget
from bfx_funding_bot.modules.execution.contracts import (
    BlockReason,
    ExecutionPolicy,
    GuardResult,
    ReadyToSubmit,
    ReservationRef,
)
from bfx_funding_bot.modules.execution.events import CancelAcknowledged, CancelRequested
from bfx_funding_bot.modules.execution.protocols import (
    AccountContext,
    Credentials,
    SubmittedOrder,
)
from bfx_funding_bot.modules.execution.submit_outcomes import (
    SubmitAcknowledged,
    SubmitOutcomeUnknown,
)
from bfx_funding_bot.modules.ledger import CycleResult, Scope
from bfx_funding_bot.modules.observability.metrics import (
    DaemonMetrics,
    LogMetricsHandler,
    MetricsSubmitMiddleware,
    TimedReconcileRecovery,
    attach_httpx_metrics,
    install_log_metrics_handler,
)
from bfx_funding_bot.modules.strategy import DecisionOutcome, DecisionPayload


def _ctx() -> AccountContext:
    return AccountContext(
        account_id="default",
        credentials=Credentials(api_key="k", api_secret="s"),
        allocation_cap_usdt=Decimal("500"),
    )


def _decision() -> DecisionPayload:
    return DecisionPayload(
        decision_outcome=DecisionOutcome.POST,
        signal_correlation_id=uuid4(),
        offer_rate=0.0005,
        offer_amount_usdt=150.0,
        offer_duration_days=2,
        symbol="fUST",
    )


def _ready() -> ReadyToSubmit:
    return ReadyToSubmit(
        decision=_decision(),
        decision_id="d-metrics",
        policy=ExecutionPolicy.BOOK_GUARDED,
        market_snapshot_id="snapshot-metrics",
        model_version=None,
        evidence={},
        safety=GuardResult(allowed=True, guard_name="test"),
    )


# ── operational / diagnostic event counters ──────────────────────────────────


def test_operational_event_counter_increments() -> None:
    m = DaemonMetrics()
    m.observe_operational_event({"event_type": "signal", "level": "info"})
    m.observe_operational_event({"event_type": "signal", "level": "info"})
    m.observe_operational_event({"event_type": "health_check", "level": "warn"})
    assert m.registry.get_sample_value(
        "bfx_operational_events_total", {"event_type": "signal", "level": "info"},
    ) == 2.0
    assert m.registry.get_sample_value(
        "bfx_operational_events_total", {"event_type": "health_check", "level": "warn"},
    ) == 1.0


def test_execution_metric_labels_are_bounded() -> None:
    m = DaemonMetrics()

    m.observe_execution_decision(
        outcome="blocked",
        reason=BlockReason.BOOK_STALE.value,
        policy=ExecutionPolicy.BOOK_GUARDED.value,
    )

    assert m.registry.get_sample_value(
        "bfx_execution_decisions_total",
        {
            "outcome": "blocked",
            "reason": "book_stale",
            "policy": "book_guarded",
        },
    ) == 1.0


def test_execution_metrics_fail_open_and_normalize_unknown_labels() -> None:
    m = DaemonMetrics()

    m.observe_execution_decision(outcome="unexpected", reason="raw-error", policy="other")
    m.observe_audit_persist_failure()
    m.observe_book_snapshot(result="unexpected")
    m.observe_book_snapshot_age(age_seconds=12.5)
    m.observe_execution_gate_duration(seconds=0.2)
    m.set_trading_ready(True)

    assert m.registry.get_sample_value(
        "bfx_execution_decisions_total",
        {"outcome": "other", "reason": "other", "policy": "other"},
    ) == 1.0
    assert m.registry.get_sample_value("bfx_execution_audit_persist_failures_total") == 1.0
    assert m.registry.get_sample_value(
        "bfx_funding_book_snapshots_total", {"result": "other"},
    ) == 1.0
    assert m.registry.get_sample_value("bfx_funding_book_snapshot_age_seconds") == 12.5
    assert m.registry.get_sample_value("bfx_execution_gate_duration_seconds_count") == 1.0
    assert m.registry.get_sample_value("bfx_trading_ready") == 1.0


def test_diagnostic_event_counter_increments() -> None:
    m = DaemonMetrics()
    m.observe_diagnostic_event({"event_type": "safety_trigger", "level": "critical"})
    assert m.registry.get_sample_value(
        "bfx_diagnostic_events_total",
        {"event_type": "safety_trigger", "level": "critical"},
    ) == 1.0


def test_operational_event_fail_open_on_malformed_event() -> None:
    m = DaemonMetrics()
    m.observe_operational_event({})          # no keys → "unknown" labels, no raise
    m.observe_operational_event({"event_type": None, "level": 7})  # junk types
    assert m.registry.get_sample_value(
        "bfx_operational_events_total", {"event_type": "unknown", "level": "unknown"},
    ) == 2.0


def test_observe_methods_fail_open_when_backend_broken() -> None:
    """Broken internals must never propagate into the observed path."""
    m = DaemonMetrics()

    class _Boom:
        def labels(self, **kwargs: Any) -> Any:
            raise RuntimeError("broken metric")

        def observe(self, *a: Any) -> None:
            raise RuntimeError("broken metric")

    m.operational_events = _Boom()  # type: ignore[assignment]
    m.diagnostic_events = _Boom()  # type: ignore[assignment]
    m.domain_events = _Boom()  # type: ignore[assignment]
    m.executor_submits = _Boom()  # type: ignore[assignment]
    m.executor_submit_duration = _Boom()  # type: ignore[assignment]
    m.reconcile_ticks = _Boom()  # type: ignore[assignment]
    m.reconcile_tick_duration = _Boom()  # type: ignore[assignment]
    m.log_messages = _Boom()  # type: ignore[assignment]
    # None of these may raise:
    m.observe_operational_event({"event_type": "signal", "level": "info"})
    m.observe_diagnostic_event({"event_type": "safety_trigger", "level": "warn"})
    m.observe_submit(status="filled", duration_s=0.1)
    m.observe_reconcile_tick(result="ok", duration_s=0.1)
    m.observe_log_record(level="warning", logger="x")


# ── domain event bus handler ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_domain_event_handler_counts_by_class() -> None:
    m = DaemonMetrics()
    handler = m.domain_event_handler()
    await handler(CancelRequested(
        venue_offer_id="1", requested_at_ms=1000, signal_correlation_id=uuid4(),
        account_id="default",
    ))
    for venue_offer_id in ("1", "2"):
        await handler(CancelAcknowledged(
            venue_offer_id=venue_offer_id, acknowledged_at_ms=1001,
            signal_correlation_id=uuid4(), account_id="default", rest_status="success",
        ))
    assert m.registry.get_sample_value(
        "bfx_domain_events_total", {"event_type": "CancelRequested"},
    ) == 1.0
    assert m.registry.get_sample_value(
        "bfx_domain_events_total", {"event_type": "CancelAcknowledged"},
    ) == 2.0


# ── executor submit middleware ───────────────────────────────────────────────


class _StubExecutor:
    def __init__(self, order: SubmittedOrder | None = None,
                 exc: Exception | None = None) -> None:
        self._order = order
        self._exc = exc
        self.calls: list[object | None] = []
        self.readies: list[ReadyToSubmit] = []

    async def submit(
        self, ready: ReadyToSubmit, ctx: AccountContext, *,
        reservation_ref: object | None = None,
    ) -> SubmittedOrder:
        self.calls.append(reservation_ref)
        self.readies.append(ready)
        if self._exc is not None:
            raise self._exc
        assert self._order is not None
        return self._order


@pytest.mark.asyncio
async def test_submit_middleware_passes_result_and_records() -> None:
    m = DaemonMetrics()
    order = SubmittedOrder(outcome=SubmitAcknowledged("paper_x"))
    inner = _StubExecutor(order=order)
    mw = MetricsSubmitMiddleware(inner, metrics=m)
    ready = _ready()
    ref = ReservationRef(
        execution_decision_id=ready.decision_id,
        signal_correlation_id=ready.decision.signal_correlation_id,
    )
    got = await mw.submit(ready, _ctx(), reservation_ref=ref)
    assert got is order                       # byte-identical passthrough
    assert inner.calls == [ref]               # reference threaded down unchanged
    assert inner.readies == [ready]            # immutable boundary object is not rebuilt
    assert m.registry.get_sample_value(
        "bfx_executor_submits_total", {"status": "submitted"},
    ) == 1.0
    assert m.registry.get_sample_value("bfx_executor_submit_duration_seconds_count") == 1.0


@pytest.mark.asyncio
async def test_submit_middleware_reraises_and_counts_exception() -> None:
    m = DaemonMetrics()
    boom = ValueError("venue said no")
    mw = MetricsSubmitMiddleware(_StubExecutor(exc=boom), metrics=m)
    with pytest.raises(ValueError) as ei:
        await mw.submit(_ready(), _ctx())
    assert ei.value is boom                   # exception object unchanged
    assert m.registry.get_sample_value(
        "bfx_executor_submits_total", {"status": "exception"},
    ) == 1.0


@pytest.mark.asyncio
async def test_observe_submit_bounds_an_unrecognized_status_to_other() -> None:
    m = DaemonMetrics()
    m.observe_submit(status="weird_venue_string", duration_s=0.0)
    assert m.registry.get_sample_value(
        "bfx_executor_submits_total", {"status": "other"},
    ) == 1.0


@pytest.mark.asyncio
async def test_submit_middleware_keeps_unknown_as_first_class_metric() -> None:
    m = DaemonMetrics()
    order = SubmittedOrder(
        venue_offer_id=None,
        outcome=SubmitOutcomeUnknown("timeout", True),
        raw_response=None,
    )
    mw = MetricsSubmitMiddleware(_StubExecutor(order=order), metrics=m)

    await mw.submit(_ready(), _ctx())

    assert m.registry.get_sample_value(
        "bfx_executor_submits_total", {"status": "unknown"},
    ) == 1.0


@pytest.mark.asyncio
async def test_submit_middleware_fail_open_when_metrics_broken() -> None:
    m = DaemonMetrics()

    def _boom(**kwargs: Any) -> None:
        raise RuntimeError("metrics down")

    m.observe_submit = _boom  # type: ignore[method-assign]
    order = SubmittedOrder(outcome=SubmitAcknowledged("x"))
    mw = MetricsSubmitMiddleware(_StubExecutor(order=order), metrics=m)
    got = await mw.submit(_ready(), _ctx())   # must NOT raise
    assert got is order


# ── reconcile tick timing wrapper ────────────────────────────────────────────


_SCOPE = Scope(UUID("00000000-0000-0000-0000-00000000a001"), "ci")


def _reconcile_result() -> CycleResult:
    return CycleResult("accepted")


class _StubRecovery:
    def __init__(self, result: CycleResult | None = None,
                 exc: Exception | None = None) -> None:
        self._result = result
        self._exc = exc

    async def run(self, scope: Scope) -> CycleResult:
        assert scope == _SCOPE
        if self._exc is not None:
            raise self._exc
        assert self._result is not None
        return self._result


@pytest.mark.asyncio
async def test_timed_recovery_passes_result_through() -> None:
    m = DaemonMetrics()
    result = _reconcile_result()
    wrapped = TimedReconcileRecovery(_StubRecovery(result=result), metrics=m)
    got = await wrapped.run(_SCOPE)
    assert got is result
    assert m.registry.get_sample_value(
        "bfx_reconcile_ticks_total", {"result": "ok"},
    ) == 1.0
    assert m.registry.get_sample_value("bfx_reconcile_tick_duration_seconds_count") == 1.0


@pytest.mark.asyncio
async def test_timed_recovery_reraises_and_counts_error() -> None:
    m = DaemonMetrics()
    boom = ConnectionError("venue unreachable")
    wrapped = TimedReconcileRecovery(_StubRecovery(exc=boom), metrics=m)
    with pytest.raises(ConnectionError) as ei:
        await wrapped.run(_SCOPE)
    assert ei.value is boom
    assert m.registry.get_sample_value(
        "bfx_reconcile_ticks_total", {"result": "error"},
    ) == 1.0


# ── probe collector: heartbeat age / thresholds / health status ──────────────


def test_probe_collector_exports_heartbeat_age_and_thresholds() -> None:
    m = DaemonMetrics()
    probe = HealthProbe()
    now = datetime.now(UTC)
    probe.last_active_ts["ws"] = now - timedelta(seconds=30)
    probe.last_active_ts["executor"] = now - timedelta(seconds=600)
    m.register_probe(probe)

    age_ws = m.registry.get_sample_value(
        "bfx_subtask_heartbeat_age_seconds", {"sub_task": "ws"},
    )
    assert age_ws is not None and 29.0 <= age_ws <= 35.0
    age_exec = m.registry.get_sample_value(
        "bfx_subtask_heartbeat_age_seconds", {"sub_task": "executor"},
    )
    assert age_exec is not None and age_exec >= 599.0
    # thresholds exported for both classes → alert expr `age > threshold`
    assert m.registry.get_sample_value(
        "bfx_subtask_heartbeat_threshold_seconds",
        {"sub_task": "ws", "task_class": "liveness"},
    ) == 90.0
    assert m.registry.get_sample_value(
        "bfx_subtask_heartbeat_threshold_seconds",
        {"sub_task": "executor", "task_class": "activity"},
    ) == 360.0


def test_probe_collector_exports_health_status() -> None:
    m = DaemonMetrics()
    probe = HealthProbe()
    probe.update(HealthTarget.BITFINEX_REST, HealthStatus.HEALTHY)
    probe.update(HealthTarget.RECONCILE, HealthStatus.DEGRADED)
    probe.update(HealthTarget.EXECUTOR, HealthStatus.DOWN)
    m.register_probe(probe)
    assert m.registry.get_sample_value(
        "bfx_health_status", {"target": "bitfinex_rest"},
    ) == 0.0
    assert m.registry.get_sample_value(
        "bfx_health_status", {"target": "reconcile"},
    ) == 1.0
    assert m.registry.get_sample_value(
        "bfx_health_status", {"target": "executor"},
    ) == 2.0


def test_probe_collector_fail_open_on_broken_probe() -> None:
    m = DaemonMetrics()

    class _BrokenProbe:
        @property
        def last_active_ts(self) -> dict[str, datetime]:
            raise RuntimeError("probe exploded")

        def snapshot(self) -> dict[Any, Any]:
            raise RuntimeError("probe exploded")

    m.register_probe(_BrokenProbe())  # type: ignore[arg-type]
    # scrape must not raise, just omit the families
    out = m.render().decode()
    assert "bfx_subtask_heartbeat_age_seconds{" not in out


# ── log-derived error counter ────────────────────────────────────────────────


def test_log_handler_counts_warning_and_above() -> None:
    m = DaemonMetrics()
    handler = install_log_metrics_handler(m, logger_name="bfx_metrics_test")
    log = logging.getLogger("bfx_metrics_test.sub.module")
    try:
        log.info("not counted")
        log.warning("counted w")
        log.error("counted e")
        log.critical("counted c")
    finally:
        logging.getLogger("bfx_metrics_test").removeHandler(handler)
    assert m.registry.get_sample_value(
        "bfx_log_messages_total",
        {"level": "warning", "logger": "bfx_metrics_test.sub.module"},
    ) == 1.0
    assert m.registry.get_sample_value(
        "bfx_log_messages_total",
        {"level": "error", "logger": "bfx_metrics_test.sub.module"},
    ) == 1.0
    assert m.registry.get_sample_value(
        "bfx_log_messages_total",
        {"level": "critical", "logger": "bfx_metrics_test.sub.module"},
    ) == 1.0
    assert m.registry.get_sample_value(
        "bfx_log_messages_total",
        {"level": "info", "logger": "bfx_metrics_test.sub.module"},
    ) is None


def test_install_log_metrics_handler_is_idempotent() -> None:
    m = DaemonMetrics()
    target = logging.getLogger("bfx_metrics_test_idem")
    h1 = install_log_metrics_handler(m, logger_name="bfx_metrics_test_idem")
    h2 = install_log_metrics_handler(m, logger_name="bfx_metrics_test_idem")
    try:
        ours = [h for h in target.handlers if isinstance(h, LogMetricsHandler)]
        assert ours == [h2]
        assert h1 not in target.handlers
    finally:
        target.removeHandler(h2)


# ── venue REST via httpx event hooks ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_httpx_hooks_count_requests_latency_and_errors() -> None:
    m = DaemonMetrics()

    def _respond(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/boom":
            return httpx.Response(500)
        if request.url.path == "/nope":
            return httpx.Response(404)
        return httpx.Response(200, json=[])

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(_respond), base_url="https://api.test",
    ) as client:
        attach_httpx_metrics(client, m)
        await client.get("/ok")
        await client.get("/ok")
        await client.get("/boom")
        await client.post("/nope")

    assert m.registry.get_sample_value(
        "bfx_venue_rest_requests_total", {"method": "GET", "status_class": "2xx"},
    ) == 2.0
    assert m.registry.get_sample_value(
        "bfx_venue_rest_requests_total", {"method": "GET", "status_class": "5xx"},
    ) == 1.0
    assert m.registry.get_sample_value(
        "bfx_venue_rest_requests_total", {"method": "POST", "status_class": "4xx"},
    ) == 1.0
    assert m.registry.get_sample_value(
        "bfx_venue_rest_errors_total", {"kind": "http_5xx"},
    ) == 1.0
    assert m.registry.get_sample_value(
        "bfx_venue_rest_errors_total", {"kind": "http_4xx"},
    ) == 1.0
    assert m.registry.get_sample_value(
        "bfx_venue_rest_request_duration_seconds_count",
    ) == 4.0


@pytest.mark.asyncio
async def test_a_429_is_counted_on_its_own_and_as_a_4xx() -> None:
    m = DaemonMetrics()

    def _respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429 if request.url.path == "/limited" else 404)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(_respond), base_url="https://api.test",
    ) as client:
        attach_httpx_metrics(client, m)
        await client.get("/other")
        assert m.registry.get_sample_value("bfx_venue_rest_rate_limited_total") == 0.0
        await client.get("/limited")
        await client.post("/limited")

    assert m.registry.get_sample_value("bfx_venue_rest_rate_limited_total") == 2.0
    assert m.registry.get_sample_value(
        "bfx_venue_rest_errors_total", {"kind": "http_4xx"}) == 3.0


def test_sim_feed_stream_events_are_counted_and_bounded() -> None:
    m = DaemonMetrics()
    observer = m.sim_venue_observer()
    observer.feed_stream("connected")
    observer.feed_stream("disconnected")
    observer.feed_stream("disconnected")
    observer.feed_stream("anything else")  # unbounded labels are dropped
    sample = m.registry.get_sample_value
    assert sample("bfx_sim_venue_feed_stream_total", {"event": "connected"}) == 1.0
    assert sample("bfx_sim_venue_feed_stream_total", {"event": "disconnected"}) == 2.0
    assert sample("bfx_sim_venue_feed_stream_total", {"event": "anything else"}) is None


def test_sim_watermark_lag_is_read_at_scrape_time_and_unknown_has_no_sample() -> None:
    m = DaemonMetrics()
    lag: dict[str, float | None] = {"fUST": 3.0, "fUSD": None}
    m.bind_sim_watermark_lag(lambda: lag)
    sample = m.registry.get_sample_value
    assert sample("bfx_sim_venue_feed_watermark_lag_seconds", {"symbol": "fUST"}) == 3.0
    assert sample("bfx_sim_venue_feed_watermark_lag_seconds", {"symbol": "fUSD"}) is None
    lag["fUST"] = 90.0
    assert sample("bfx_sim_venue_feed_watermark_lag_seconds", {"symbol": "fUST"}) == 90.0


@pytest.mark.asyncio
async def test_httpx_hooks_do_not_break_request_when_metrics_broken() -> None:
    m = DaemonMetrics()

    def _boom(**kwargs: Any) -> None:
        raise RuntimeError("metrics down")

    m.observe_venue_rest_response = _boom  # type: ignore[method-assign]
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200)),
        base_url="https://api.test",
    ) as client:
        attach_httpx_metrics(client, m)
        resp = await client.get("/ok")     # must NOT raise
    assert resp.status_code == 200


# ── ws dispatcher queue saturation gauges ────────────────────────────────────


def test_ws_queue_gauges_scrape_time_callback() -> None:
    m = DaemonMetrics()
    depth = 3
    m.bind_ws_dispatcher_queue(depth_fn=lambda: depth, capacity=1000)
    assert m.registry.get_sample_value("bfx_ws_dispatcher_queue_depth") == 3.0
    assert m.registry.get_sample_value("bfx_ws_dispatcher_queue_capacity") == 1000.0
    depth = 42   # live read at scrape time — no stale 30s snapshot
    assert m.registry.get_sample_value("bfx_ws_dispatcher_queue_depth") == 42.0


def test_ws_queue_gauge_fail_open_when_depth_fn_raises() -> None:
    m = DaemonMetrics()

    def _boom() -> int:
        raise RuntimeError("dispatcher gone")

    m.bind_ws_dispatcher_queue(depth_fn=_boom, capacity=1000)
    assert m.registry.get_sample_value("bfx_ws_dispatcher_queue_depth") == 0.0
    m.render()   # full scrape must not raise either


# ── info + registry isolation ────────────────────────────────────────────────


def test_daemon_info_and_render() -> None:
    m = DaemonMetrics()
    m.set_daemon_info(
        service_version="abc1234", deployment_environment="prod", phase="live",
    )
    assert m.registry.get_sample_value(
        "bfx_daemon_info",
        {"service_version": "abc1234", "deployment_environment": "prod", "phase": "live"},
    ) == 1.0
    out = m.render().decode()
    assert "bfx_daemon_info" in out
    assert m.content_type.startswith("text/plain")


def test_two_instances_are_isolated() -> None:
    a = DaemonMetrics()
    b = DaemonMetrics()   # second instance must not raise duplicate-timeseries
    a.observe_operational_event({"event_type": "signal", "level": "info"})
    assert b.registry.get_sample_value(
        "bfx_operational_events_total", {"event_type": "signal", "level": "info"},
    ) is None


@pytest.mark.asyncio
async def test_cycle_wrapper_passes_scope_and_result_identity():
    from unittest.mock import AsyncMock

    from bfx_funding_bot.modules.ledger import CycleResult, Scope

    scope = Scope(uuid4(), "ci")
    result = CycleResult("fenced")
    sink = AsyncMock()
    sink.run.return_value = result
    metrics = DaemonMetrics()
    wrapper = TimedReconcileRecovery(sink, metrics=metrics)
    assert await wrapper.run(scope) is result
    sink.run.assert_awaited_once_with(scope)


def test_the_simulated_venue_observer_feeds_the_soak_counters() -> None:
    """Mutation: the observer stops incrementing (the soak report would read zero)."""
    from prometheus_client import generate_latest

    metrics = DaemonMetrics()
    observer = metrics.sim_venue_observer()
    observer.internal_failure("no_market_data")
    observer.internal_failure("no_market_data")
    observer.unexpected_request()
    observer.feed_failure("trades")
    text = generate_latest(metrics.registry).decode()
    assert 'bfx_sim_venue_internal_failures_total{kind="no_market_data"} 2.0' in text
    assert "bfx_sim_venue_unexpected_requests_total 1.0" in text
    assert 'bfx_sim_venue_feed_failures_total{source="trades"} 1.0' in text
