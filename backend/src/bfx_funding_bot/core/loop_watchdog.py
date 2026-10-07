"""Out-of-loop watchdog: a wedged asyncio event loop ends the process.

Every in-process health check (``/healthz``, ``HealthMonitor.scan_staleness``)
is a task on the same event loop, so a loop blocked by synchronous code or a
deadlock silences all of them, and nothing outside restarts a container that
is alive but stuck (compose ``restart:`` reacts only to an exit). The timer
here lives outside the loop: ``faulthandler.dump_traceback_later`` runs on a C
thread that needs neither the loop nor the GIL. A task on the loop re-arms it
every ``timeout_s / 4``; when the loop stops iterating for ``timeout_s`` the
timer dumps every thread's traceback to stderr and ``_exit(1)``s, and the
container restart policy starts a fresh process.

The timer is cancelled when the stop event is set, so a graceful drain is never
killed however long it takes. ``faulthandler`` keeps a single process-wide
timer, so one ``LoopWatchdog`` runs per process (the bot's ``_run``).
"""
from __future__ import annotations

import asyncio
import faulthandler
import logging
import math
import sys
from collections.abc import Callable, Mapping
from typing import Protocol

from bfx_funding_bot.core.errors import ConfigurationError

log = logging.getLogger(__name__)

LOOP_WATCHDOG_ENV = "BFX_LOOP_WATCHDOG_S"
DEFAULT_LOOP_WATCHDOG_S = 120.0
# Re-arm four times per timeout, so a healthy loop always re-arms well before expiry.
_REARMS_PER_TIMEOUT = 4


class _Arm(Protocol):
    def __call__(self, timeout: float, *, exit: bool) -> None: ...


def _faulthandler_arm(timeout: float, *, exit: bool) -> None:
    faulthandler.dump_traceback_later(timeout, repeat=False, file=sys.stderr, exit=exit)


def loop_watchdog_timeout_s(environ: Mapping[str, str]) -> float:
    """``BFX_LOOP_WATCHDOG_S`` (seconds, > 0); unset or empty means the default."""
    raw = environ.get(LOOP_WATCHDOG_ENV, "").strip()
    if not raw:
        return DEFAULT_LOOP_WATCHDOG_S
    try:
        value = float(raw)
    except ValueError as exc:
        raise ConfigurationError(f"{LOOP_WATCHDOG_ENV} must be a number, got {raw!r}") from exc
    if not math.isfinite(value) or value <= 0:
        raise ConfigurationError(f"{LOOP_WATCHDOG_ENV} must be > 0, got {raw!r}")
    return value


class LoopWatchdog:
    """Re-arms the out-of-loop timer while the event loop iterates."""

    def __init__(
        self,
        *,
        timeout_s: float,
        arm: _Arm = _faulthandler_arm,
        disarm: Callable[[], None] = faulthandler.cancel_dump_traceback_later,
    ) -> None:
        if not math.isfinite(timeout_s) or timeout_s <= 0:
            raise ConfigurationError(f"loop watchdog timeout must be > 0, got {timeout_s!r}")
        self.timeout_s = timeout_s
        self._arm = arm
        self._disarm = disarm
        # Set by the bot once its metrics exist: seconds the loop overran a re-arm wait.
        self.on_lag: Callable[[float], None] | None = None

    async def run(self, stop: asyncio.Event) -> None:
        """Arm now, re-arm every ``timeout_s / 4`` until ``stop`` is set, then disarm.

        Disarming on stop (and on cancellation) is what keeps a long graceful
        drain alive: after this returns, nothing can kill the process.
        """
        loop = asyncio.get_running_loop()
        interval = self.timeout_s / _REARMS_PER_TIMEOUT
        self._arm(self.timeout_s, exit=True)
        log.info("loop_watchdog_armed timeout_s=%s", self.timeout_s)
        try:
            while True:
                started = loop.time()
                try:
                    await asyncio.wait_for(stop.wait(), timeout=interval)
                    return
                except TimeoutError:
                    pass
                self._arm(self.timeout_s, exit=True)
                self._observe_lag(loop.time() - started - interval)
        finally:
            self._disarm()
            log.info("loop_watchdog_disarmed")

    def _observe_lag(self, lag_s: float) -> None:
        if self.on_lag is None:
            return
        try:
            self.on_lag(max(lag_s, 0.0))
        except Exception:
            log.debug("loop_watchdog_lag_observe_failed", exc_info=True)
