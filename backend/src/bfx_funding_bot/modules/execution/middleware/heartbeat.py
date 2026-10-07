"""HeartbeatMiddleware — outermost middleware, records executor heartbeat.

I1 invariant: record_heartbeat("executor") fires regardless of inner outcome.
Probe failure swallowed + logged (heartbeat infra issue should not kill
submit path).
"""
from __future__ import annotations

import logging

from bfx_funding_bot.core.health import HealthProbe
from bfx_funding_bot.modules.execution.contracts import ReadyToSubmit
from bfx_funding_bot.modules.execution.protocols import (
    AccountContext,
    ExecutorPort,
    SubmittedOrder,
)

log = logging.getLogger(__name__)


class HeartbeatMiddleware:
    """Outermost ExecutorPort wrapper. try/finally to keep I1 invariant."""

    def __init__(self, inner: ExecutorPort, *, probe: HealthProbe) -> None:
        self._inner = inner
        self._probe = probe

    async def submit(
        self, ready: ReadyToSubmit, ctx: AccountContext,
    ) -> SubmittedOrder:
        try:
            return await self._inner.submit(ready, ctx)
        finally:
            try:
                self._probe.record_heartbeat("executor")
            except Exception as exc:
                log.warning("heartbeat_record_failed err=%r", exc)
