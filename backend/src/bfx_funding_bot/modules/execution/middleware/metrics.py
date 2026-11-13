"""Prometheus timing decorators for the submit chain and the reconcile backbone.

Observe-only and transparent: results and exceptions pass through unchanged and
metric recording is fail-open, so the money paths never see a metrics fault.
"""
from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from bfx_funding_bot.modules.execution.contracts import ReadyToSubmit
    from bfx_funding_bot.modules.execution.protocols import (
        AccountContext,
        ExecutorPort,
        SubmittedOrder,
    )
    from bfx_funding_bot.modules.ledger import CycleResult, ObservationSink, Scope
    from bfx_funding_bot.modules.observability.metrics import DaemonMetrics

log = logging.getLogger(__name__)


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
        self, ready: ReadyToSubmit, ctx: AccountContext,
    ) -> SubmittedOrder:
        start = time.perf_counter()
        try:
            order = await self._inner.submit(ready, ctx)
        except BaseException:
            self._safe_observe("exception", start)
            raise
        self._safe_observe(order.status, start)
        return order


class TimedReconcileRecovery:
    """An ``ObservationSink`` that times the reconcile backbone's ``inner.run()``.

    Injected between PeriodicReconcile and the observation sink at wiring time so the
    reconcile module itself stays untouched. Result and exceptions pass through
    unchanged; PeriodicReconcile's own failure handling sees exactly what the
    raw recovery would have produced.
    """

    def __init__(self, inner: ObservationSink, *, metrics: DaemonMetrics) -> None:
        self._inner = inner
        self._metrics = metrics

    def _safe_observe(self, result: str, start: float) -> None:
        try:
            self._metrics.observe_reconcile_tick(
                result=result, duration_s=time.perf_counter() - start,
            )
        except Exception:
            log.debug("metrics_reconcile_observe_failed", exc_info=True)

    async def run(self, scope: Scope) -> CycleResult:
        start = time.perf_counter()
        try:
            result = await self._inner.run(scope)
        except BaseException:
            self._safe_observe("error", start)
            raise
        self._safe_observe("ok", start)
        return result
