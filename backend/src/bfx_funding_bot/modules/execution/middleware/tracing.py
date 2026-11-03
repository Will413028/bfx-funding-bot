"""OpenTelemetry span decorators for the submit chain, reconcile and WS dispatch.

Observe-only and transparent: results and exceptions pass through unchanged
(exceptions are recorded on the span first); span recording is fail-open.
"""
from __future__ import annotations

import contextlib
import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from bfx_funding_bot.modules.execution.contracts import ReadyToSubmit
    from bfx_funding_bot.modules.execution.protocols import (
        AccountContext,
        ExecutorPort,
        SubmittedOrder,
    )
    from bfx_funding_bot.modules.ledger import CycleResult, ObservationSink, Scope
    from bfx_funding_bot.modules.observability.tracing import DaemonTracing

log = logging.getLogger(__name__)


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
        self, ready: ReadyToSubmit, ctx: AccountContext,
    ) -> SubmittedOrder:
        attrs: dict[str, Any] = {}
        # Defense-in-depth on the money path: even attr harvesting must not raise.
        with contextlib.suppress(Exception):
            attrs["bfx.symbol"] = ready.decision.symbol
            attrs["bfx.execution_decision_id"] = ready.decision_id
        with self._tracing.span("executor.submit", attributes=attrs) as span:
            order = await self._inner.submit(ready, ctx)
            span.set_attribute("bfx.submit.status", order.status)
            return order


class TracedReconcileRecovery:
    """An ``ObservationSink`` that spans the reconcile backbone's ``inner.run()``.

    Stacked outside TimedReconcileRecovery at wiring time (enabled only) so
    the "reconcile.tick" span covers the same window the duration histogram
    measures. Result and exceptions pass through unchanged.
    """

    def __init__(self, inner: ObservationSink, *, tracing: DaemonTracing) -> None:
        self._inner = inner
        self._tracing = tracing

    async def run(self, scope: Scope) -> CycleResult:
        with self._tracing.span("reconcile.tick") as span:
            result = await self._inner.run(scope)
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
