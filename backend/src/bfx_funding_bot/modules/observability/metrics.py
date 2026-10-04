"""DaemonMetrics — Google Four Golden Signals as Prometheus metrics (L2 upgrade).

Replaces heartbeat-guessing with explicit, scrapeable signals, exposed at
`GET /metrics` on the existing healthz HTTP server (port 8080, Prometheus
text exposition via prometheus_client).

Signal → metric map:
- Traffic:    bfx_operational_events_total{event_type,level} (SIGNAL / DECISION /
              ORDER_SUBMIT / HEALTH_CHECK rates via StdoutEventSink hook),
              bfx_domain_events_total{event_type} (bus: submit/fill/release/
              reconcile), bfx_executor_submits_total{status},
              bfx_venue_rest_requests_total{method,status_class}
- Latency:    bfx_executor_submit_duration_seconds (submit→ack incl. persist),
              bfx_reconcile_tick_duration_seconds,
              bfx_venue_rest_request_duration_seconds (p95 via histogram_quantile)
- Saturation: bfx_ws_dispatcher_queue_depth / _capacity (scrape-time callback),
              bfx_subtask_heartbeat_age_seconds{sub_task} +
              bfx_subtask_heartbeat_threshold_seconds{sub_task,task_class}
              (alert expr: age > threshold — the explicit form of the old
              scan_staleness inference)
- Errors:     bfx_venue_rest_errors_total{kind},
              bfx_diagnostic_events_total{event_type,level} (safety_trigger trips),
              bfx_log_messages_total{level,logger} (WARNING+ incl.
              ws_dispatcher_persist_failed / bus_handler_failed),
              bfx_health_status{target} (0 healthy / 1 degraded / 2 down)

Invariants (observe-only contract):
- FAIL-OPEN: every observe_* method and every collector swallows its own
  exceptions (debug-log only). A metrics fault must never raise into an
  observed path — least of all the submit / reconcile money paths.
- Wrappers (MetricsSubmitMiddleware / TimedReconcileRecovery) are transparent:
  results and exceptions pass through unchanged; no behavior branches added.
- Per-instance CollectorRegistry (never the global REGISTRY) so tests and
  multiple build_daemon() calls cannot collide on duplicated timeseries.
"""
from __future__ import annotations

import contextlib
import logging
import time
from collections.abc import Awaitable, Callable, Iterable, Mapping
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Protocol

import httpx
from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)
from prometheus_client.core import GaugeMetricFamily, Metric
from prometheus_client.registry import Collector

from bfx_funding_bot.core.health import ACTIVITY_THRESHOLDS, LIVENESS_THRESHOLDS, HealthProbe
from bfx_funding_bot.core.telemetry import HealthStatus

if TYPE_CHECKING:
    from bfx_funding_bot.modules.execution.protocols import (
        AccountContext,
        ExecutorPort,
        SubmittedOrder,
    )

from bfx_funding_bot.modules.execution.contracts import (
    BlockReason,
    ReadyToSubmit,
    ReservationRef,
)

log = logging.getLogger(__name__)

# Bounded label values for bfx_executor_submits_total — SubmittedOrder.status is
# a venue-fed string; anything outside the known set becomes "other" so a venue
# quirk can never explode timeseries cardinality.  UNKNOWN/NOT_SENT remain
# first-class labels because collapsing either into FAILED hides safety state.
_KNOWN_SUBMIT_STATUSES = frozenset({"submitted", "filled", "failed", "unknown", "not_sent"})
_KNOWN_EXECUTION_OUTCOMES = frozenset({"ready", "blocked", "no_recommendation"})
_KNOWN_EXECUTION_REASONS = frozenset({"none", *(reason.value for reason in BlockReason)})
_KNOWN_EXECUTION_POLICIES = frozenset({
    "book_guarded", "optimizer_shadow", "optimizer_live",
})
_KNOWN_BOOK_SNAPSHOT_RESULTS = frozenset({"valid", "invalid", "unavailable", "error"})

_HEALTH_STATUS_CODE: dict[HealthStatus, float] = {
    HealthStatus.HEALTHY: 0.0,
    HealthStatus.DEGRADED: 1.0,
    HealthStatus.DOWN: 2.0,
}

# perf_counter stash key on httpx request.extensions (namespaced; additive).
_HTTPX_START_KEY = "bfx_metrics_start_pc"

_SUBMIT_BUCKETS = (0.05, 0.1, 0.25, 0.5, 1.0, 2.0, 5.0, 10.0, 30.0)
_RECONCILE_BUCKETS = (0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0)
_REST_BUCKETS = (0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0)


def _label_str(value: Any) -> str:
    """Coerce an event-dict field into a bounded label string ("unknown" fallback)."""
    if isinstance(value, str) and value:
        return value
    return "unknown"


class _ProbeCollector(Collector):
    """Scrape-time view of HealthProbe — heartbeat ages, thresholds, target status.

    Runs on the event loop (the /metrics route is async), so reading the probe
    dicts is race-free with the daemon's single-threaded mutations. Fail-open:
    a broken probe yields no families, never a scrape error.
    """

    def __init__(self, probe: HealthProbe) -> None:
        self._probe = probe

    def collect(self) -> Iterable[Metric]:
        try:
            now = datetime.now(UTC)
            age = GaugeMetricFamily(
                "bfx_subtask_heartbeat_age_seconds",
                "Seconds since each sub-task's last recorded heartbeat "
                "(alert: age > bfx_subtask_heartbeat_threshold_seconds).",
                labels=["sub_task"],
            )
            for sub_task, last_ts in list(self._probe.last_active_ts.items()):
                age.add_metric([sub_task], max((now - last_ts).total_seconds(), 0.0))
            threshold = GaugeMetricFamily(
                "bfx_subtask_heartbeat_threshold_seconds",
                "Staleness threshold per sub-task; task_class=liveness drives "
                "/healthz 503, task_class=activity is observe-only (idle != dead).",
                labels=["sub_task", "task_class"],
            )
            for sub_task, thr in LIVENESS_THRESHOLDS.items():
                threshold.add_metric([sub_task, "liveness"], float(thr))
            for sub_task, thr in ACTIVITY_THRESHOLDS.items():
                threshold.add_metric([sub_task, "activity"], float(thr))
            health = GaugeMetricFamily(
                "bfx_health_status",
                "Last known status per health target: 0=healthy 1=degraded 2=down.",
                labels=["target"],
            )
            for target, state in list(self._probe.snapshot().items()):
                health.add_metric(
                    [target.value], _HEALTH_STATUS_CODE.get(state.status, -1.0),
                )
            return [age, threshold, health]
        except Exception:
            log.debug("probe_collector_failed", exc_info=True)
            return []


class DaemonMetrics:
    """All daemon Prometheus metrics behind fail-open observe methods."""

    def __init__(self) -> None:
        self.registry = CollectorRegistry()
        self.content_type = CONTENT_TYPE_LATEST

        # ── Traffic ──────────────────────────────────────────────────────────
        self.operational_events = Counter(
            "bfx_operational_events",
            "Operational telemetry events through StdoutEventSink "
            "(signal / decision / order_submit / health_check / ...).",
            ["event_type", "level"],
            registry=self.registry,
        )
        self.domain_events = Counter(
            "bfx_domain_events",
            "Execution domain events published on DomainEventBus, by event class.",
            ["event_type"],
            registry=self.registry,
        )
        self.executor_submits = Counter(
            "bfx_executor_submits",
            "Executor submit outcomes through the full middleware chain.",
            ["status"],
            registry=self.registry,
        )
        self.venue_rest_requests = Counter(
            "bfx_venue_rest_requests",
            "Bitfinex REST responses on the shared httpx client, by status class.",
            ["method", "status_class"],
            registry=self.registry,
        )

        # ── Latency ──────────────────────────────────────────────────────────
        self.executor_submit_duration = Histogram(
            "bfx_executor_submit_duration_seconds",
            "End-to-end submit latency (intent persist + venue POST + ack).",
            registry=self.registry,
            buckets=_SUBMIT_BUCKETS,
        )
        self.reconcile_tick_duration = Histogram(
            "bfx_reconcile_tick_duration_seconds",
            "Duration of one periodic venue reconcile tick (BootRecovery.run).",
            registry=self.registry,
            buckets=_RECONCILE_BUCKETS,
        )
        self.venue_rest_duration = Histogram(
            "bfx_venue_rest_request_duration_seconds",
            "Bitfinex REST request latency (send → response headers).",
            registry=self.registry,
            buckets=_REST_BUCKETS,
        )

        # ── Saturation ───────────────────────────────────────────────────────
        self.ws_queue_depth = Gauge(
            "bfx_ws_dispatcher_queue_depth",
            "Auth-WS dispatcher queue depth, read live at scrape time.",
            registry=self.registry,
        )
        self.ws_queue_capacity = Gauge(
            "bfx_ws_dispatcher_queue_capacity",
            "Auth-WS dispatcher queue max size.",
            registry=self.registry,
        )

        # ── Errors ───────────────────────────────────────────────────────────
        self.reconcile_ticks = Counter(
            "bfx_reconcile_ticks",
            "Periodic reconcile tick outcomes (result=ok|error).",
            ["result"],
            registry=self.registry,
        )
        self.venue_rest_errors = Counter(
            "bfx_venue_rest_errors",
            "Bitfinex REST error responses (kind=http_4xx|http_5xx).",
            ["kind"],
            registry=self.registry,
        )
        self.diagnostic_events = Counter(
            "bfx_diagnostic_events",
            "Forensic events through DiagnosticsSink (decision / safety_trigger).",
            ["event_type", "level"],
            registry=self.registry,
        )
        self.log_messages = Counter(
            "bfx_log_messages",
            "WARNING+ log records from the bfx_funding_bot logger tree "
            "(includes persist/publish failures and transport errors).",
            ["level", "logger"],
            registry=self.registry,
        )
        self.execution_decisions = Counter(
            "bfx_execution_decisions",
            "Execution eligibility outcomes with bounded decision labels.",
            ["outcome", "reason", "policy"],
            registry=self.registry,
        )
        self.execution_audit_persist_failures = Counter(
            "bfx_execution_audit_persist_failures",
            "Execution audit persistence failures before a venue submit.",
            registry=self.registry,
        )
        self.alerts = Counter(
            "bfx_alerts",
            "Operator alerts by event and outcome (sent / failed / deduplicated / "
            "rate_limited / queue_full / log_only); drops never block trading",
            ["event", "outcome"],
            registry=self.registry,
        )
        self.funding_book_snapshots = Counter(
            "bfx_funding_book_snapshots",
            "Funding book snapshot validation outcomes.",
            ["result"],
            registry=self.registry,
        )

        # ── Info ─────────────────────────────────────────────────────────────
        self.daemon_info = Gauge(
            "bfx_daemon_info",
            "Static daemon build/deployment labels; value is always 1.",
            ["service_version", "deployment_environment", "phase"],
            registry=self.registry,
        )
        self.funding_book_snapshot_age = Gauge(
            "bfx_funding_book_snapshot_age_seconds",
            "Age of the current funding-book snapshot.",
            registry=self.registry,
        )
        self.execution_gate_duration = Histogram(
            "bfx_execution_gate_duration_seconds",
            "Execution eligibility and pre-trade audit duration.",
            registry=self.registry,
            buckets=_SUBMIT_BUCKETS,
        )
        self.trading_ready = Gauge(
            "bfx_trading_ready",
            "Whether trading business dependencies are currently ready.",
            registry=self.registry,
        )

    # ── fail-open observe methods ────────────────────────────────────────────

    def observe_operational_event(self, event: Mapping[str, Any]) -> None:
        try:
            self.operational_events.labels(
                event_type=_label_str(event.get("event_type")),
                level=_label_str(event.get("level")),
            ).inc()
        except Exception:
            log.debug("metrics_observe_failed metric=operational_events", exc_info=True)

    def observe_diagnostic_event(self, event: Mapping[str, Any]) -> None:
        try:
            self.diagnostic_events.labels(
                event_type=_label_str(event.get("event_type")),
                level=_label_str(event.get("level")),
            ).inc()
        except Exception:
            log.debug("metrics_observe_failed metric=diagnostic_events", exc_info=True)

    def domain_event_handler(self) -> Callable[[Any], Awaitable[None]]:
        """Async DomainEventBus handler counting events by dataclass name.

        One handler instance may subscribe to many event types (the bus rejects
        duplicates only per (type, handler) pair).
        """

        async def _count(event: Any) -> None:
            try:
                self.domain_events.labels(event_type=type(event).__name__).inc()
            except Exception:
                log.debug("metrics_observe_failed metric=domain_events", exc_info=True)

        return _count

    def observe_submit(self, *, status: str, duration_s: float) -> None:
        try:
            if status not in _KNOWN_SUBMIT_STATUSES and status != "exception":
                status = "other"
            self.executor_submits.labels(status=status).inc()
            self.executor_submit_duration.observe(duration_s)
        except Exception:
            log.debug("metrics_observe_failed metric=executor_submits", exc_info=True)

    def observe_reconcile_tick(self, *, result: str, duration_s: float) -> None:
        try:
            self.reconcile_ticks.labels(result=result).inc()
            self.reconcile_tick_duration.observe(duration_s)
        except Exception:
            log.debug("metrics_observe_failed metric=reconcile_ticks", exc_info=True)

    def observe_venue_rest_response(
        self, *, method: str, status_code: int, duration_s: float | None,
    ) -> None:
        try:
            status_class = f"{status_code // 100}xx"
            self.venue_rest_requests.labels(
                method=method, status_class=status_class,
            ).inc()
            if duration_s is not None:
                self.venue_rest_duration.observe(duration_s)
            if status_code >= 500:
                self.venue_rest_errors.labels(kind="http_5xx").inc()
            elif status_code >= 400:
                self.venue_rest_errors.labels(kind="http_4xx").inc()
        except Exception:
            log.debug("metrics_observe_failed metric=venue_rest", exc_info=True)

    def observe_log_record(self, *, level: str, logger: str) -> None:
        try:
            self.log_messages.labels(level=level, logger=logger).inc()
        except Exception:
            log.debug("metrics_observe_failed metric=log_messages", exc_info=True)

    def observe_execution_decision(self, *, outcome: str, reason: str, policy: str) -> None:
        try:
            self.execution_decisions.labels(
                outcome=outcome if outcome in _KNOWN_EXECUTION_OUTCOMES else "other",
                reason=reason if reason in _KNOWN_EXECUTION_REASONS else "other",
                policy=policy if policy in _KNOWN_EXECUTION_POLICIES else "other",
            ).inc()
        except Exception:
            log.debug("metrics_observe_failed metric=execution_decisions", exc_info=True)

    def observe_alert(self, event: str, outcome: str) -> None:
        with contextlib.suppress(Exception):
            self.alerts.labels(event=event, outcome=outcome).inc()

    def observe_audit_persist_failure(self) -> None:
        try:
            self.execution_audit_persist_failures.inc()
        except Exception:
            log.debug("metrics_observe_failed metric=execution_audit_persist_failures", exc_info=True)

    def observe_book_snapshot(self, *, result: str) -> None:
        try:
            self.funding_book_snapshots.labels(
                result=result if result in _KNOWN_BOOK_SNAPSHOT_RESULTS else "other",
            ).inc()
        except Exception:
            log.debug("metrics_observe_failed metric=funding_book_snapshots", exc_info=True)

    def observe_book_snapshot_age(self, *, age_seconds: float) -> None:
        try:
            self.funding_book_snapshot_age.set(max(age_seconds, 0.0))
        except Exception:
            log.debug("metrics_observe_failed metric=funding_book_snapshot_age", exc_info=True)

    def observe_execution_gate_duration(self, *, seconds: float) -> None:
        try:
            self.execution_gate_duration.observe(max(seconds, 0.0))
        except Exception:
            log.debug("metrics_observe_failed metric=execution_gate_duration", exc_info=True)

    def set_trading_ready(self, value: bool) -> None:
        try:
            self.trading_ready.set(1.0 if value else 0.0)
        except Exception:
            log.debug("metrics_observe_failed metric=trading_ready", exc_info=True)

    # ── binding / registration ───────────────────────────────────────────────

    def register_probe(self, probe: HealthProbe) -> None:
        """Export heartbeat ages + thresholds + target statuses at scrape time."""
        try:
            self.registry.register(_ProbeCollector(probe))
        except Exception:
            log.warning("metrics_register_probe_failed", exc_info=True)

    def bind_ws_dispatcher_queue(
        self, *, depth_fn: Callable[[], int], capacity: int,
    ) -> None:
        """Read queue depth live at each scrape (no stale 30s snapshot)."""

        def _safe_depth() -> float:
            try:
                return float(depth_fn())
            except Exception:
                log.debug("metrics_ws_queue_depth_read_failed", exc_info=True)
                return 0.0

        try:
            self.ws_queue_depth.set_function(_safe_depth)
            self.ws_queue_capacity.set(float(capacity))
        except Exception:
            log.warning("metrics_bind_ws_queue_failed", exc_info=True)

    def set_daemon_info(
        self, *, service_version: str, deployment_environment: str, phase: str,
    ) -> None:
        try:
            self.daemon_info.labels(
                service_version=service_version,
                deployment_environment=deployment_environment,
                phase=phase,
            ).set(1.0)
        except Exception:
            log.debug("metrics_observe_failed metric=daemon_info", exc_info=True)

    def render(self) -> bytes:
        """Prometheus text exposition of this daemon's registry."""
        return generate_latest(self.registry)


class MetricsSubmitMiddleware:
    """Outermost ExecutorPort wrapper — pure observation of the submit path.

    Sits OUTSIDE HeartbeatMiddleware so the histogram covers the full chain
    (intent persist + venue POST + ack). Transparent by construction: the
    inner result/exception passes through unchanged and metric recording is
    fail-open (observe_submit swallows its own errors).
    """

    def __init__(self, inner: ExecutorPort, *, metrics: DaemonMetrics) -> None:
        self._inner = inner
        self._metrics = metrics

    def _safe_observe(self, status: str, start: float) -> None:
        # Defense-in-depth on the money path: observe_submit is already
        # fail-open, but even a fully broken metrics object must not raise here.
        try:
            self._metrics.observe_submit(
                status=status, duration_s=time.perf_counter() - start,
            )
        except Exception:
            log.debug("metrics_submit_observe_failed", exc_info=True)

    async def submit(
        self, ready: ReadyToSubmit, ctx: AccountContext, *, cid: int | None = None,
        reservation_ref: ReservationRef | None = None,
    ) -> SubmittedOrder:
        start = time.perf_counter()
        try:
            order = await self._inner.submit(
                ready, ctx, cid=cid, reservation_ref=reservation_ref,
            )
        except BaseException:
            self._safe_observe("exception", start)
            raise
        self._safe_observe(order.status, start)
        return order


class _RecoveryRunner[**P, R](Protocol):
    async def run(self, *args: P.args, **kwargs: P.kwargs) -> R: ...


class TimedReconcileRecovery[**P, R]:
    """Transparent timing wrapper around the reconcile backbone's recovery.run().

    Injected between PeriodicReconcile and BootRecovery at wiring time so the
    reconcile module itself stays untouched. Result and exceptions pass through
    unchanged; PeriodicReconcile's own failure handling sees exactly what the
    raw recovery would have produced.
    """

    def __init__(self, inner: _RecoveryRunner[P, R], *, metrics: DaemonMetrics) -> None:
        self._inner = inner
        self._metrics = metrics

    def _safe_observe(self, result: str, start: float) -> None:
        try:
            self._metrics.observe_reconcile_tick(
                result=result, duration_s=time.perf_counter() - start,
            )
        except Exception:
            log.debug("metrics_reconcile_observe_failed", exc_info=True)

    async def run(self, *args: P.args, **kwargs: P.kwargs) -> R:
        start = time.perf_counter()
        try:
            result = await self._inner.run(*args, **kwargs)
        except BaseException:
            self._safe_observe("error", start)
            raise
        self._safe_observe("ok", start)
        return result


class LogMetricsHandler(logging.Handler):
    """Counts WARNING+ log records — log-derived error-rate signal.

    Catches failures that only surface as logs (ws_dispatcher persist/publish
    CRITICALs, bus_handler_failed WARNINGs, venue transport errors) without
    touching those code paths. Metrics module logs its own failures at DEBUG,
    below this handler's level → no recursion.
    """

    def __init__(self, metrics: DaemonMetrics) -> None:
        super().__init__(level=logging.WARNING)
        self._metrics = metrics

    def emit(self, record: logging.LogRecord) -> None:
        # Never let metrics break logging (observe_log_record is itself
        # fail-open; this is defense-in-depth for a fully broken metrics obj).
        with contextlib.suppress(Exception):
            self._metrics.observe_log_record(
                level=record.levelname.lower(), logger=record.name,
            )


def install_log_metrics_handler(
    metrics: DaemonMetrics, *, logger_name: str = "bfx_funding_bot",
) -> LogMetricsHandler:
    """Attach a LogMetricsHandler to the given logger tree, idempotently.

    Prior LogMetricsHandler instances on the same logger are removed first so
    repeated build_daemon() calls (tests) never double-count.
    """
    target = logging.getLogger(logger_name)
    for existing in list(target.handlers):
        if isinstance(existing, LogMetricsHandler):
            target.removeHandler(existing)
    handler = LogMetricsHandler(metrics)
    target.addHandler(handler)
    return handler


def attach_httpx_metrics(client: httpx.AsyncClient, metrics: DaemonMetrics) -> None:
    """Append request/response event hooks to a shared httpx.AsyncClient.

    Purely additive (existing hooks are preserved) and fail-open on both ends.
    Transport-level failures never reach the response hook — they surface via
    bfx_log_messages_total from the caller's error logging instead.
    """

    async def _on_request(request: httpx.Request) -> None:
        try:
            request.extensions[_HTTPX_START_KEY] = time.perf_counter()
        except Exception:
            log.debug("metrics_httpx_request_hook_failed", exc_info=True)

    async def _on_response(response: httpx.Response) -> None:
        try:
            start = response.request.extensions.get(_HTTPX_START_KEY)
            duration_s = (
                time.perf_counter() - start if isinstance(start, float) else None
            )
            metrics.observe_venue_rest_response(
                method=response.request.method,
                status_code=response.status_code,
                duration_s=duration_s,
            )
        except Exception:
            log.debug("metrics_httpx_response_hook_failed", exc_info=True)

    hooks = client.event_hooks
    hooks["request"].append(_on_request)
    hooks["response"].append(_on_response)
    client.event_hooks = hooks
