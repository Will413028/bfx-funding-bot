from contextlib import AbstractAsyncContextManager

from aiolimiter import AsyncLimiter


class FundingRateLimiter:
    """Wraps aiolimiter.AsyncLimiter with funding-endpoint defaults.

    Bitfinex public funding endpoints are rate-limited at ~30 req/min per IP
    (conservative estimate from parent doc; actual ceiling is ~90).
    """

    def __init__(self, max_rate: float = 30, time_period: float = 60.0) -> None:
        self._limiter = AsyncLimiter(max_rate=max_rate, time_period=time_period)

    def acquire(self) -> AbstractAsyncContextManager[None]:
        return self._limiter
