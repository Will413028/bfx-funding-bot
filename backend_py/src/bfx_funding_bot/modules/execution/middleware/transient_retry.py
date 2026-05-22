"""TransientRetryMiddleware — wrap inner.submit with transient_retry decorator.

Retries on ExecutorTransientError (network / 5xx) per tenacity config in
retry.py (1/2/4s backoff, stop_after_attempt cap). Fatal/Auth errors
propagate first attempt.
"""
from __future__ import annotations

from bfx_funding_bot.modules.execution.protocols import (
    AccountContext,
    ExecutorPort,
    SubmittedOrder,
)
from bfx_funding_bot.modules.execution.retry import transient_retry
from bfx_funding_bot.modules.marketfeed.schemas import DecisionPayload


class TransientRetryMiddleware:
    """ExecutorPort wrapper. Innermost middleware in the chain."""

    def __init__(self, inner: ExecutorPort) -> None:
        self._inner = inner
        self._submit_retried = transient_retry(inner.submit)

    async def submit(
        self, decision: DecisionPayload, ctx: AccountContext,
    ) -> SubmittedOrder:
        return await self._submit_retried(decision, ctx)
