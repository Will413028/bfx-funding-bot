"""Boot waits in-process for an unreachable dependency instead of exiting.

An exit while the venue or the database is unreachable buys nothing: the
container restarts, the next boot meets the same outage, and the process
crash-loops (one boot-refused alert per round). So the two boot steps that need
a dependency — the first database contact (``wait_for_database``) and the boot
observation (``Daemon._boot_until_observed``) — retry with a capped backoff
while the failure is a transient reachability fault. Nothing trades while they
wait: no trading task exists before boot completes. Every other failure (a
refusal, a credential rejection, an invariant) still ends the boot.
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable

import asyncpg
import httpx
from sqlalchemy import exc as sa_exc
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from bfx_funding_bot.core.errors import FatalError, TransientError
from bfx_funding_bot.external.bitfinex.errors import BitfinexAPIError
from bfx_funding_bot.modules.observability import alerts

log = logging.getLogger(__name__)

BOOT_RETRY_MAX_DELAY_S = 60.0

# OSError covers refused connections, DNS failures and timeouts at connect; the asyncpg
# pair is a server still starting up or a connection dropped mid-use.
_UNREACHABLE = (
    OSError,
    httpx.TransportError,
    TransientError,
    asyncpg.CannotConnectNowError,
    asyncpg.ConnectionDoesNotExistError,
)


def boot_retry_delay_s(attempt: int) -> float:
    """1, 2, 4, ... seconds after the 1st, 2nd, 3rd failure, capped at 60."""
    return float(min(BOOT_RETRY_MAX_DELAY_S, 2 ** max(attempt - 1, 0)))


def is_transient_dependency_error(exc: BaseException) -> bool:
    """True only for "the venue or the database did not answer".

    Walks the explicit ``raise ... from`` chain (never the implicit context, so a
    refusal raised while handling a network error stays a refusal). Any
    ``FatalError`` in the chain (boot invariant, credential rejection, writer
    lock contention) makes it non-transient.
    """
    seen: set[int] = set()
    found = False
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, FatalError):
            return False
        if _is_unreachable(current):
            found = True
        current = current.__cause__
    return found


def _is_unreachable(exc: BaseException) -> bool:
    if isinstance(exc, BitfinexAPIError):
        # Status 0 is the client's transport error or deadline; 429/5xx are the
        # venue declining for now. Other 4xx are answers (credentials, request).
        return exc.status_code == 0 or exc.status_code == 429 or exc.status_code >= 500
    if isinstance(exc, sa_exc.DBAPIError):
        return bool(exc.connection_invalidated)
    return isinstance(exc, _UNREACHABLE)


class BootWait:
    """Logging, one operator alert per wait, and the backoff for one boot step."""

    def __init__(self, step: str, *, delay_s: Callable[[int], float] = boot_retry_delay_s) -> None:
        self.step = step
        self._delay_s = delay_s
        self.failures = 0

    def failed(self, exc: BaseException) -> float:
        """Record one transient failure; returns how long to wait before the next try."""
        self.failures += 1
        delay = self._delay_s(self.failures)
        log.warning(
            "boot_waiting_for_dependency step=%s attempt=%d retry_in_s=%.1f error=%r",
            self.step, self.failures, delay, exc,
        )
        if self.failures == 1:
            alerts.emit(alerts.BOOT_WAITING, level=alerts.WARNING, step=self.step,
                        error=f"{type(exc).__name__}: {exc}"[:300])
        return delay

    def succeeded(self) -> None:
        if self.failures:
            log.info("boot_dependency_reachable step=%s after_failures=%d",
                     self.step, self.failures)


async def wait_for_database(
    engine: AsyncEngine, *, delay_s: Callable[[int], float] = boot_retry_delay_s,
) -> None:
    """Return once ``SELECT 1`` succeeds; retry while the database is unreachable.

    Runs before the boot's first real query, so a database that is restarting or
    not up yet delays the boot instead of failing it. A non-transient error (bad
    credentials, unknown database) propagates and refuses the boot as before.
    """
    wait = BootWait("database", delay_s=delay_s)
    while True:
        try:
            async with engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
        except Exception as exc:
            if not is_transient_dependency_error(exc):
                raise
            await asyncio.sleep(wait.failed(exc))
            continue
        wait.succeeded()
        return
