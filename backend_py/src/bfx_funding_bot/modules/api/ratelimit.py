"""SP5 per-user rate limiting — in-memory token bucket + FastAPI dependency.

Scope: (user_id, read|write) — GET/HEAD spend the read bucket, everything else
the write bucket. Limits via BFX_RATE_LIMIT_READ_PER_MIN (default 120) /
BFX_RATE_LIMIT_WRITE_PER_MIN (default 30). Over limit → 429 + Retry-After.

LIMITATION: state is per-process. webapi runs as a single uvicorn instance;
scaling to multiple workers/replicas requires moving the bucket to Redis —
revisit at SP6 multi-tenant.
"""
from __future__ import annotations

import math
import os
import time
from collections.abc import Awaitable, Callable
from typing import Literal

from fastapi import Depends, HTTPException, Request, Response, status

from bfx_funding_bot.core.auth import Principal, require_operator

Scope = Literal["read", "write"]


class TokenBucketLimiter:
    """Classic token bucket per (user, scope). Injected clock for tests.

    acquire() returns None when a token was spent, else the seconds to wait
    until one token is available (ceil'd by the caller for Retry-After).
    """

    def __init__(
        self,
        *,
        read_per_min: int,
        write_per_min: int,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._rates = {"read": read_per_min, "write": write_per_min}
        self._clock = clock or time.monotonic
        # (user_id, scope) -> (tokens, last_refill_ts)
        self._buckets: dict[tuple[str, Scope], tuple[float, float]] = {}

    def acquire(self, user_id: str, scope: Scope) -> float | None:
        capacity = float(self._rates[scope])
        refill_per_s = capacity / 60.0
        now = self._clock()
        tokens, last = self._buckets.get((user_id, scope), (capacity, now))
        tokens = min(capacity, tokens + (now - last) * refill_per_s)
        if tokens >= 1.0:
            self._buckets[(user_id, scope)] = (tokens - 1.0, now)
            return None
        self._buckets[(user_id, scope)] = (tokens, now)
        return (1.0 - tokens) / refill_per_s


def _limiter_from_env() -> TokenBucketLimiter:
    return TokenBucketLimiter(
        read_per_min=int(os.environ.get("BFX_RATE_LIMIT_READ_PER_MIN", "120")),
        write_per_min=int(os.environ.get("BFX_RATE_LIMIT_WRITE_PER_MIN", "30")),
    )


_shared_limiter: TokenBucketLimiter | None = None
_shared_dependency: Callable[..., Awaitable[None]] | None = None


def shared_rate_limit_dependency() -> Callable[..., Awaitable[None]]:
    """One process-wide dependency (and thus ONE bucket set) shared by every
    router — per-router instances would multiply the effective limit."""
    global _shared_dependency, _shared_limiter
    if _shared_dependency is None:
        _shared_limiter = _limiter_from_env()
        _shared_dependency = build_rate_limit_dependency(_shared_limiter)
    return _shared_dependency


def reset_shared_rate_limits() -> None:
    """Test hook: drop all buckets so suites with a shared fake user don't
    bleed limit state across tests. No-op in production paths."""
    if _shared_limiter is not None:
        _shared_limiter._buckets.clear()


def build_rate_limit_dependency(
    limiter: TokenBucketLimiter | None = None,
) -> Callable[..., Awaitable[None]]:
    """Router-level dependency: resolve the Principal, spend a token, 429 on empty."""
    _limiter = limiter or _limiter_from_env()

    async def enforce_rate_limit(
        request: Request,
        response: Response,
        user: Principal = Depends(require_operator),  # noqa: B008
    ) -> None:
        scope: Scope = "read" if request.method in ("GET", "HEAD") else "write"
        retry_in = _limiter.acquire(user.user_id, scope)
        if retry_in is not None:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="rate_limited",
                headers={"Retry-After": str(max(1, math.ceil(retry_in)))},
            )

    return enforce_rate_limit
