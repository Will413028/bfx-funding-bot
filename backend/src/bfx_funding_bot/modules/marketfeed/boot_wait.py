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
import errno
import json
import logging
import socket
import ssl
from collections.abc import Callable

import asyncpg
import httpx
from sqlalchemy import exc as sa_exc
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from bfx_funding_bot.core.errors import FatalError, TransientError
from bfx_funding_bot.external.bitfinex.errors import (
    TRANSIENT_VENUE_ERROR_CODES,
    BitfinexAPIError,
)
from bfx_funding_bot.external.bitfinex.submit_wire import venue_error
from bfx_funding_bot.modules.observability import alerts

log = logging.getLogger(__name__)

BOOT_RETRY_MAX_DELAY_S = 60.0

# The asyncpg pair is a server still starting up or a connection dropped mid-use.
_UNREACHABLE = (
    httpx.TransportError,
    TransientError,
    asyncpg.CannotConnectNowError,
    asyncpg.ConnectionDoesNotExistError,
)
# Connection-level OSErrors only: refused/reset (ConnectionError), DNS
# (socket.gaierror), timeouts. Other OSError subclasses are answers about this
# process's own setup (ssl.SSLCertVerificationError, PermissionError, ...) and
# refuse the boot.
_CONNECTION_OSERRORS = (ConnectionError, socket.gaierror, TimeoutError)
_NETWORK_ERRNOS = frozenset({
    errno.ENETUNREACH, errno.ENETDOWN, errno.EHOSTUNREACH, errno.EHOSTDOWN,
    errno.ECONNREFUSED, errno.ECONNRESET, errno.ECONNABORTED, errno.ETIMEDOUT,
})


def boot_retry_delay_s(attempt: int) -> float:
    """1, 2, 4, ... seconds after the 1st, 2nd, 3rd failure, capped at 60."""
    return float(min(BOOT_RETRY_MAX_DELAY_S, 2 ** max(attempt - 1, 0)))


def is_transient_dependency_error(exc: BaseException) -> bool:
    """True only for "the venue or the database did not answer" (or said "not now").

    A definite refusal anywhere in the chain wins: walking both ``__cause__`` and
    the implicit ``__context__`` -- even one suppressed with ``from None``, as
    httpcore does when it turns ``ssl.SSLCertVerificationError`` into its
    ``ConnectError`` -- any ``FatalError``, TLS certificate verification failure,
    permission error or venue answer makes the failure non-transient. A wrapper that only says "no
    answer" (``BitfinexAPIError`` status 0, ``httpx.ConnectError``) therefore cannot
    hide the TLS failure underneath it. Otherwise the failure is transient only if
    a link of the explicit ``raise ... from`` chain is a reachability fault: an
    unrelated error raised while handling a network error stays a refusal.
    """
    for link in _links(exc, follow_context=True):
        if _is_refusal(link):
            return False
    return any(_is_unreachable(link) for link in _links(exc, follow_context=False))


def _links(exc: BaseException, *, follow_context: bool) -> list[BaseException]:
    links: list[BaseException] = []
    pending: list[BaseException] = [exc]
    seen: set[int] = set()
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        links.append(current)
        if current.__cause__ is not None:
            pending.append(current.__cause__)
        if follow_context and current.__context__ is not None:
            pending.append(current.__context__)
    return links


def _is_refusal(exc: BaseException) -> bool:
    """A definite answer about this process or its request: retrying cannot change it."""
    if isinstance(exc, FatalError | ssl.SSLCertVerificationError | PermissionError):
        return True
    if isinstance(exc, BitfinexAPIError) and exc.status_code != 0:
        return not _venue_did_not_answer(exc)
    return False


def _is_unreachable(exc: BaseException) -> bool:
    if isinstance(exc, BitfinexAPIError):
        return _venue_did_not_answer(exc)
    if isinstance(exc, sa_exc.DBAPIError):
        return bool(exc.connection_invalidated)
    if isinstance(exc, OSError):
        return _is_connection_failure(exc)
    return isinstance(exc, _UNREACHABLE)


def _venue_did_not_answer(exc: BitfinexAPIError) -> bool:
    """Status 0 is the client's transport error or deadline. A body shaped
    ``["error", CODE, MESSAGE]`` is the venue answering -- Bitfinex sends its
    refusals (``apikey: invalid``, ``nonce: small``) with HTTP 500 -- so only the
    rate-limit and maintenance codes are "not now". Without such a body, 429 and
    5xx (a gateway page, an empty body) are the venue declining for now; other
    4xx are answers."""
    if exc.status_code == 0:
        return True
    answer = _parsed_venue_error(exc.raw)
    if answer is not None:
        return answer[0] in TRANSIENT_VENUE_ERROR_CODES
    return exc.status_code == 429 or exc.status_code >= 500


def _parsed_venue_error(raw: str | None) -> tuple[int, str] | None:
    if not raw:
        return None
    try:
        return venue_error(json.loads(raw))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None


def _is_connection_failure(exc: OSError) -> bool:
    if isinstance(exc, _CONNECTION_OSERRORS):
        return True
    # asyncio raises a plain OSError for "network unreachable" and, when every
    # address of a host failed with different messages, a plain OSError with no
    # errno ("Multiple exceptions"). Subclasses (SSL, permission, files) are not this.
    return type(exc) is OSError and (exc.errno is None or exc.errno in _NETWORK_ERRNOS)


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
