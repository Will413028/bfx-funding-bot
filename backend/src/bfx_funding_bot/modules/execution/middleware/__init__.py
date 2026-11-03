"""Executor middleware chain — composable ExecutorPort decorators.

NOTE: there is no retry middleware around submit. A funding-offer submit is a
once-only financial write (Bitfinex funding offers have no client cid dedup), so
transient failures are recovered by the periodic reconcile, never by re-submit.
"""
from bfx_funding_bot.modules.execution.middleware.heartbeat import (
    HeartbeatMiddleware,
)
from bfx_funding_bot.modules.execution.middleware.metrics import (
    MetricsSubmitMiddleware,
    TimedReconcileRecovery,
)
from bfx_funding_bot.modules.execution.middleware.reservation_emitting import (
    ReservationEmittingMiddleware,
)
from bfx_funding_bot.modules.execution.middleware.tracing import (
    TracedReconcileRecovery,
    TracingSubmitMiddleware,
    instrument_ws_dispatcher,
)

__all__ = [
    "HeartbeatMiddleware",
    "MetricsSubmitMiddleware",
    "ReservationEmittingMiddleware",
    "TimedReconcileRecovery",
    "TracedReconcileRecovery",
    "TracingSubmitMiddleware",
    "instrument_ws_dispatcher",
]
