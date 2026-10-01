"""DaemonTracing — OpenTelemetry OTLP trace spans on the daemon's key paths.

Coarse-grained span coverage (wired in build_daemon, observe-only):
- executor.submit        (TracingSubmitMiddleware — outermost executor wrapper;
                          attrs: bfx.symbol / bfx.cid / bfx.submit.status)
- reconcile.tick         (TracedReconcileRecovery — around the reconcile
                          backbone's recovery.run(); attrs: bfx.reconcile.result)
- ws_dispatcher.process  (instrument_ws_dispatcher — per WS event translate +
                          persist + publish; attrs: bfx.ws.event_type)

Resource attrs: service.name=bfx-bot, service.version (BFX_SERVICE_VERSION via
EventResource), deployment.environment (+ host.name when known).

Invariants (observe-only contract, mirrors metrics.py):
- DEFAULT OFF: BFX_OTEL_ENABLED unset/false ⇒ zero SDK initialization — no
  opentelemetry import ever executes (all SDK imports live inside
  _build_provider) and span() is a pure no-op passthrough.
- FAIL-OPEN: SDK init failure, span-start failure, attribute failure — all are
  debug/warning-logged and swallowed. A tracing fault must never raise into a
  traced path — least of all the submit / reconcile money paths.
- Wrappers are transparent: results and exceptions pass through unchanged; no
  behavior branches added. Exceptions are recorded on the span (status=ERROR)
  and re-raised byte-identical.
- Export: OTLP gRPC to BFX_OTEL_EXPORTER_ENDPOINT (default http://tempo:4317)
  via BatchSpanProcessor (background thread, never blocks the hot path). Tests
  inject an exporter ⇒ synchronous SimpleSpanProcessor, no network.
"""
from __future__ import annotations

import contextlib
import logging
import sys
from collections.abc import Iterator, Mapping
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from bfx_funding_bot.modules.execution.protocols import (
        AccountContext,
        ExecutorPort,
        SubmittedOrder,
    )
    from bfx_funding_bot.modules.observability.resource import EventResource

from bfx_funding_bot.modules.execution.contracts import ReadyToSubmit, ReservationRef

log = logging.getLogger(__name__)

DEFAULT_OTLP_ENDPOINT = "http://tempo:4317"

# service.name matches the compose service (bfx-bot) so Tempo/Grafana show the
# same identity as container logs / Prometheus. StdoutEventSink envelopes keep
# their own service_name (bfx-funding-bot) — separate, unchanged surface.
_SERVICE_NAME = "bfx-bot"

_TRUTHY = frozenset({"1", "true", "yes", "on"})


class _SpanHandle:
    """Fail-open facade over an OTel span: set_attribute never raises.

    The no-op instance (no underlying span) is what traced bodies see on the
    disabled path and whenever span start failed — same API, zero effect.
    """

    __slots__ = ("_span",)

    def __init__(self, span: Any = None) -> None:
        self._span = span

    def set_attribute(self, key: str, value: Any) -> None:
        if self._span is None:
            return
        try:
            self._span.set_attribute(key, value)
        except Exception:
            log.debug("tracing_set_attribute_failed key=%s", key, exc_info=True)


_NOOP_SPAN = _SpanHandle()


class DaemonTracing:
    """OTel tracer behind a fail-open, default-off facade.

    Disabled (the default): `_build_provider` is never called, so no
    opentelemetry module is imported and span() yields a shared no-op handle.
    Enabled: per-instance TracerProvider (never the global one) so tests and
    repeated build_daemon() calls cannot collide.
    """

    def __init__(
        self,
        *,
        enabled: bool,
        endpoint: str = DEFAULT_OTLP_ENDPOINT,
        event_resource: EventResource | None = None,
        exporter: Any | None = None,
    ) -> None:
        self.endpoint = endpoint
        self._event_resource = event_resource
        self._exporter_override = exporter
        self._provider: Any | None = None
        self._tracer: Any | None = None
        if not enabled:
            return
        try:
            self._build_provider()
        except Exception:
            log.warning(
                "tracing_init_failed endpoint=%s — continuing without traces (fail-open)",
                self.endpoint,
                exc_info=True,
            )
            self._provider = None
            self._tracer = None

    @property
    def enabled(self) -> bool:
        """True only when the SDK initialized successfully (fail-open on init)."""
        return self._tracer is not None

    def _build_provider(self) -> None:
        """All SDK imports live here — never executed on the disabled path."""
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import (
            BatchSpanProcessor,
            SimpleSpanProcessor,
        )

        attrs: dict[str, Any] = {"service.name": _SERVICE_NAME}
        er = self._event_resource
        if er is not None:
            attrs["service.version"] = er.service_version
            attrs["deployment.environment"] = er.deployment_environment.value
            if er.host_name is not None:
                attrs["host.name"] = er.host_name
        provider = TracerProvider(resource=Resource.create(attrs))
        if self._exporter_override is not None:
            # Tests: injected exporter (e.g. InMemorySpanExporter) exports
            # synchronously — no OTLP endpoint, no background thread.
            provider.add_span_processor(SimpleSpanProcessor(self._exporter_override))
        else:
            from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (
                OTLPSpanExporter,
            )

            provider.add_span_processor(
                BatchSpanProcessor(OTLPSpanExporter(endpoint=self.endpoint, insecure=True))
            )
        self._provider = provider
        self._tracer = provider.get_tracer("bfx_funding_bot")

    @contextlib.contextmanager
    def span(
        self, name: str, *, attributes: Mapping[str, Any] | None = None,
    ) -> Iterator[_SpanHandle]:
        """Fail-open span context manager, transparent to the wrapped body.

        The body's result/exception always passes through unchanged; on
        exception the span records it and gets status=ERROR before re-raise.
        A broken tracer degrades to the no-op handle, never to a raise.
        """
        tracer = self._tracer
        if tracer is None:
            yield _NOOP_SPAN
            return
        cm: Any | None = None
        handle = _NOOP_SPAN
        try:
            cm = tracer.start_as_current_span(
                name,
                attributes=dict(attributes) if attributes else None,
                record_exception=True,
                set_status_on_exception=True,
            )
            handle = _SpanHandle(cm.__enter__())
        except Exception:
            log.debug("tracing_span_start_failed name=%s", name, exc_info=True)
            cm = None
        try:
            yield handle
        except BaseException:
            if cm is not None:
                with contextlib.suppress(Exception):
                    cm.__exit__(*sys.exc_info())
            raise
        else:
            if cm is not None:
                with contextlib.suppress(Exception):
                    cm.__exit__(None, None, None)

    def shutdown(self) -> None:
        """Flush + shutdown the provider (daemon exit). Fail-open, idempotent."""
        provider = self._provider
        self._provider = None
        self._tracer = None
        if provider is None:
            return
        try:
            provider.shutdown()
        except Exception:
            log.debug("tracing_shutdown_failed", exc_info=True)


def tracing_from_env(
    environ: Mapping[str, str],
    *,
    event_resource: EventResource | None = None,
    exporter: Any | None = None,
) -> DaemonTracing:
    """Build DaemonTracing from env: BFX_OTEL_ENABLED (default false) +
    BFX_OTEL_EXPORTER_ENDPOINT (default http://tempo:4317, OTLP gRPC)."""
    enabled = environ.get("BFX_OTEL_ENABLED", "").strip().lower() in _TRUTHY
    endpoint = (
        environ.get("BFX_OTEL_EXPORTER_ENDPOINT", "").strip() or DEFAULT_OTLP_ENDPOINT
    )
    return DaemonTracing(
        enabled=enabled,
        endpoint=endpoint,
        event_resource=event_resource,
        exporter=exporter,
    )


class TracingSubmitMiddleware:
    """Outermost ExecutorPort wrapper — span "executor.submit", observe-only.

    Wired OUTSIDE MetricsSubmitMiddleware (and only when tracing is enabled)
    so the span covers the full chain: intent persist + venue POST + ack.
    Transparent by construction: the inner result/exception passes through
    unchanged; span recording is fail-open end to end.
    """

    def __init__(self, inner: ExecutorPort, *, tracing: DaemonTracing) -> None:
        self._inner = inner
        self._tracing = tracing

    async def submit(
        self, ready: ReadyToSubmit, ctx: AccountContext, *, cid: int | None = None,
        reservation_ref: ReservationRef | None = None,
    ) -> SubmittedOrder:
        attrs: dict[str, Any] = {}
        # Defense-in-depth on the money path: even attr harvesting must not raise.
        with contextlib.suppress(Exception):
            attrs["bfx.symbol"] = ready.decision.symbol
            attrs["bfx.execution_decision_id"] = ready.decision_id
            if cid is not None:
                attrs["bfx.cid"] = cid
        with self._tracing.span("executor.submit", attributes=attrs) as span:
            order = await self._inner.submit(
                ready, ctx, cid=cid, reservation_ref=reservation_ref,
            )
            span.set_attribute("bfx.submit.status", order.status)
            return order


class _RecoveryRunner[**P, R](Protocol):
    async def run(self, *args: P.args, **kwargs: P.kwargs) -> R: ...


class TracedReconcileRecovery[**P, R]:
    """Transparent span wrapper around the reconcile backbone's recovery.run().

    Stacked outside TimedReconcileRecovery at wiring time (enabled only) so
    the "reconcile.tick" span covers the same window the duration histogram
    measures. Result and exceptions pass through unchanged.
    """

    def __init__(self, inner: _RecoveryRunner[P, R], *, tracing: DaemonTracing) -> None:
        self._inner = inner
        self._tracing = tracing

    async def run(self, *args: P.args, **kwargs: P.kwargs) -> R:
        with self._tracing.span("reconcile.tick") as span:
            result = await self._inner.run(*args, **kwargs)
            span.set_attribute("bfx.reconcile.result", "ok")
            return result


def instrument_ws_dispatcher(dispatcher: Any, *, tracing: DaemonTracing) -> None:
    """Wrap dispatcher._process with a "ws_dispatcher.process" span in place.

    Bound-method wrapping at wiring time keeps ws_dispatcher.py untouched
    (same seam philosophy as bind_ws_dispatcher_queue). Fail-open: a
    dispatcher without _process is warn-logged and skipped; the wrapped call
    itself is transparent — the inner coroutine's exceptions propagate
    unchanged (recorded on the span first).
    """
    inner = getattr(dispatcher, "_process", None)
    if inner is None or not callable(inner):
        log.warning("tracing_ws_dispatcher_missing_process — skipping instrumentation")
        return

    async def _traced_process(bfx_event: Any) -> None:
        attrs: dict[str, Any] = {}
        with contextlib.suppress(Exception):
            attrs["bfx.ws.event_type"] = type(bfx_event).__name__
        with tracing.span("ws_dispatcher.process", attributes=attrs):
            await inner(bfx_event)

    dispatcher._process = _traced_process
