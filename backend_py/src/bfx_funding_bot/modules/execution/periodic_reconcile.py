"""PeriodicReconcile — runtime correctness backbone (spec 2026-05-27).

Runs the venue snapshot reconcile (BootRecovery.run) on an interval. The WS
stream is a latency optimization; this loop is what GUARANTEES the ledger
converges to venue truth, so the bot can never get permanently stuck at the
allocation cap when the stream silently breaks.

- Divergence: a release on a periodic (non-boot) run means the stream missed an
  event -> BITFINEX_REST DEGRADED + WARN so the silent breakage surfaces.
- Fail-safe: consecutive venue-fetch failures -> EXECUTOR DOWN so AuthHealthGuard
  blocks new offers (never trade on a stale ledger). Cleared on recovery.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Protocol

from bfx_funding_bot.modules.execution.boot_recovery import ReconcileResult
from bfx_funding_bot.modules.marketfeed.schemas import HealthStatus, HealthTarget

log = logging.getLogger(__name__)


class _Recovery(Protocol):
    async def run(self) -> ReconcileResult: ...


class _Probe(Protocol):
    def record_heartbeat(self, sub_task: str) -> None: ...
    def update(self, target: HealthTarget, status: HealthStatus, **fields: object) -> None: ...


class PeriodicReconcile:
    SUB_TASK = "periodic_reconcile"

    def __init__(
        self,
        *,
        recovery: _Recovery,
        probe: _Probe,
        interval_s: float,
        max_consecutive_failures: int = 3,
    ) -> None:
        self._recovery = recovery
        self._probe = probe
        self._interval_s = interval_s
        self._max_failures = max_consecutive_failures
        self._consecutive_failures = 0
        self._tripped_down = False  # this loop owns the EXECUTOR DOWN it sets

    async def run_loop(self, stop_event: asyncio.Event) -> None:
        while not stop_event.is_set():
            await self._tick()
            self._probe.record_heartbeat(self.SUB_TASK)
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=self._interval_s)
            except TimeoutError:
                continue

    async def _tick(self) -> None:
        try:
            result = await self._recovery.run()
        except Exception as exc:  # never let the backbone crash the daemon
            self._consecutive_failures += 1
            log.warning(
                "periodic_reconcile_failed attempt=%d/%d err=%r",
                self._consecutive_failures, self._max_failures, exc,
            )
            if self._consecutive_failures >= self._max_failures and not self._tripped_down:
                self._tripped_down = True
                self._probe.update(
                    HealthTarget.EXECUTOR, HealthStatus.DOWN,
                    error_message=f"venue reconcile unreachable: {exc!r}",
                )
            return

        self._consecutive_failures = 0
        if self._tripped_down:
            self._tripped_down = False
            self._probe.update(
                HealthTarget.EXECUTOR, HealthStatus.HEALTHY,
                error_message="venue reconcile recovered",
            )
        if result.n_released > 0 or result.n_claimed > 0:
            log.warning(
                "periodic_reconcile_divergence released=%d claimed=%d failed=%d "
                "— WS lifecycle path missed events",
                result.n_released, result.n_claimed, result.n_failed,
            )
            self._probe.update(
                HealthTarget.BITFINEX_REST, HealthStatus.DEGRADED,
                error_message=(
                    f"reconcile drift released={result.n_released} claimed={result.n_claimed}"
                ),
            )
