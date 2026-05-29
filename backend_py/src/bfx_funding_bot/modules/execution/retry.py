"""Transient-only retry decorator for executor calls.

Used ONLY for the cancel path (BitfinexLiveExecutor.cancel), which is idempotent
(already-filled / already-cancelled offers return "not found"/"not active" and are
treated as success). submit is deliberately NOT retried: a funding-offer submit is
a once-only financial write and Bitfinex funding offers have no client cid dedup
(only trading orders do), so re-submitting would risk a real duplicate live offer.
Transient submit failures are recovered by the periodic reconcile instead.

Policy (spec section "Error Handling Policy"):
- Retry: ExecutorTransientError (httpx.NetworkError / TimeoutException / 5xx)
- DO NOT retry: ExecutorFatalError / ExecutorAuthError (4xx / auth)
- Backoff: 1s / 2s / 4s; total cap ~7s
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

import httpx
from tenacity import (
    AsyncRetrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from bfx_funding_bot.core.errors import (
    ExecutorAuthError,
    ExecutorFatalError,
    ExecutorTransientError,
)

RETRY_ATTEMPTS = 3
RETRY_BACKOFF_MULTIPLIER = 1.0  # 1s, 2s, 4s
RETRY_BACKOFF_MAX = 4.0


def classify_httpx_response(resp: httpx.Response) -> Exception:
    """Return an ExecutorAuth/Fatal/TransientError matching the status code.

    The caller raises the returned exception themselves so type narrowing works.
    """
    status = resp.status_code
    if status in (401, 403):
        return ExecutorAuthError(f"auth_failed: HTTP {status}")
    if 400 <= status < 500:
        return ExecutorFatalError(f"venue_rejected: HTTP {status}")
    if status >= 500:
        return ExecutorTransientError(f"venue_5xx: HTTP {status}")
    return ExecutorFatalError(f"unexpected status: HTTP {status}")


def classify_httpx_exception(exc: BaseException) -> Exception:
    """Map httpx-level exceptions to executor taxonomy."""
    if isinstance(exc, httpx.NetworkError | httpx.TimeoutException):
        return ExecutorTransientError(f"network_error: {exc!r}")
    return ExecutorFatalError(f"unexpected_httpx: {exc!r}")


def transient_retry[T](
    fn: Callable[..., Awaitable[T]],
) -> Callable[..., Awaitable[T]]:
    """Decorate an async fn to retry on ExecutorTransientError only."""

    async def wrapper(*args: Any, **kwargs: Any) -> T:
        async for attempt in AsyncRetrying(
            retry=retry_if_exception_type(ExecutorTransientError),
            stop=stop_after_attempt(RETRY_ATTEMPTS),
            wait=wait_exponential(
                multiplier=RETRY_BACKOFF_MULTIPLIER,
                max=RETRY_BACKOFF_MAX,
            ),
            reraise=True,
        ):
            with attempt:
                return await fn(*args, **kwargs)
        raise AssertionError("unreachable: tenacity raised already")  # for mypy

    return wrapper
