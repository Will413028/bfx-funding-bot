"""PeriodicReconcile — runtime correctness backbone (spec 2026-05-27).

Runs the ledger's observation cycle on an interval. The WS stream is a latency
optimization; this loop is what GUARANTEES the ledger converges to venue truth, so
the bot can never get permanently stuck at the allocation cap when the stream
silently breaks.

- Non-accepted streak: a cycle the ledger does not accept marks RECONCILE DEGRADED
  (cleared by the next accepted cycle) and alerts once the streak reaches the
  failure bound. This loop FULLY OWNS HealthTarget.RECONCILE and does NOT touch
  HealthTarget.BITFINEX_REST (owned by the daemon REST poller).
- Fail-safe: consecutive venue-fetch failures -> EXECUTOR DOWN so AuthHealthGuard
  blocks new offers (never trade on a stale ledger). Cleared on recovery.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Callable
from typing import Protocol

from bfx_funding_bot.core.telemetry import HealthStatus, HealthTarget
from bfx_funding_bot.external.bitfinex.auth_rest import ActiveFundingOffer
from bfx_funding_bot.modules.execution.deployment_input import DeploymentInput
from bfx_funding_bot.modules.execution.resync_channel import ResyncChannel
from bfx_funding_bot.modules.ledger import CycleResult, ObservationSink, Scope
from bfx_funding_bot.modules.observability import alerts

log = logging.getLogger(__name__)

class _Probe(Protocol):
    def record_heartbeat(self, sub_task: str) -> None: ...
    def update(self, target: HealthTarget, status: HealthStatus, **fields: object) -> None: ...


class _Deployment(Protocol):
    async def deploy(
        self, *, venue_offers: tuple[ActiveFundingOffer, ...] = (),
    ) -> None: ...


class PeriodicReconcile:
    SUB_TASK = "periodic_reconcile"

    def __init__(
        self,
        *,
        recovery: ObservationSink,
        scope: Scope,
        resync: ResyncChannel,
        probe: _Probe,
        interval_s: float,
        max_consecutive_failures: int = 3,
        min_resync_interval_s: float = 10.0,
        monotonic: Callable[[], float] | None = None,
        deployment: _Deployment | None = None,
        deployment_input: DeploymentInput | None = None,
    ) -> None:
        if (deployment is None) != (deployment_input is None):
            raise ValueError("deployment and deployment_input are given together")
        self._recovery = recovery
        self._scope = scope
        self._probe = probe
        self._interval_s = interval_s
        self._max_failures = max_consecutive_failures
        self._min_resync_interval_s = min_resync_interval_s
        self._monotonic = monotonic or time.monotonic
        self._deployment = deployment
        self._deployment_input = deployment_input
        self._non_accepted = 0  # consecutive cycles the ledger did not accept
        self._consecutive_failures = 0
        self._tripped_down = False  # this loop owns the EXECUTOR DOWN it sets
        self.resync = resync
        self._last_tick_mono = 0.0

    async def run_loop(self, stop_event: asyncio.Event) -> None:
        while not stop_event.is_set():
            await self._tick()
            self._last_tick_mono = self._monotonic()
            self._probe.record_heartbeat(self.SUB_TASK)
            if await self._wait_next(stop_event):
                reason = self.resync.take()
                await self._debounce(stop_event)
                log.info("periodic_reconcile_resync reason=%s", reason)

    async def _wait_next(self, stop_event: asyncio.Event) -> bool:
        """Sleep up to interval_s, waking early on stop or a resync request.
        Returns True iff a resync was requested (not on timeout/stop)."""
        stop_task = asyncio.create_task(stop_event.wait())
        trig_task = asyncio.create_task(self.resync.wait())
        try:
            done, _pending = await asyncio.wait(
                {stop_task, trig_task},
                timeout=self._interval_s,
                return_when=asyncio.FIRST_COMPLETED,
            )
        finally:
            for t in (stop_task, trig_task):
                t.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await t
        return trig_task in done and not stop_event.is_set()

    async def _debounce(self, stop_event: asyncio.Event) -> None:
        """Enforce >= min_resync_interval_s between ticks; stop-interruptible so a
        storm of triggers can never reconcile faster than the window."""
        remaining = self._min_resync_interval_s - (self._monotonic() - self._last_tick_mono)
        if remaining > 0:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop_event.wait(), timeout=remaining)

    def note_boot(self, decision: str) -> None:
        """Seed the non-accepted streak with the boot cycle's decision."""
        if decision == "accepted":
            self._accepted()
        else:
            self._not_accepted(decision)

    def _not_accepted(self, decision: str) -> None:
        self._non_accepted += 1
        log.warning(
            "periodic_reconcile_not_accepted decision=%s streak=%d",
            decision, self._non_accepted,
        )
        self._probe.update(
            HealthTarget.RECONCILE, HealthStatus.DEGRADED,
            error_message=(
                f"reconcile not accepted decision={decision} "
                f"consecutive={self._non_accepted}"
            ),
        )
        if self._non_accepted == self._max_failures:
            alerts.emit(
                alerts.RECONCILE_NOT_ACCEPTED,
                decision=decision, consecutive=self._non_accepted,
            )

    def _accepted(self) -> None:
        if self._non_accepted == 0:
            return
        log.info("periodic_reconcile_accepted_again after=%d", self._non_accepted)
        self._non_accepted = 0
        self._probe.update(
            HealthTarget.RECONCILE, HealthStatus.HEALTHY,
            error_message="reconcile accepted again",
        )

    async def _tick(self) -> None:
        try:
            cycle = await self._recovery.run(self._scope)
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
        if cycle.decision != "accepted":
            self._not_accepted(cycle.decision)
            return
        self._accepted()

        self._consecutive_failures = 0
        if self._tripped_down:
            self._tripped_down = False
            self._probe.update(
                HealthTarget.EXECUTOR, HealthStatus.HEALTHY,
                error_message="venue reconcile recovered",
            )
        await self._deploy(cycle)

    async def _deploy(self, cycle: CycleResult) -> None:
        if self._deployment is None or self._deployment_input is None:
            return
        try:
            await self._deployment.deploy(
                venue_offers=await self._deployment_input.offers(cycle),
            )
        except Exception:  # deployment must never crash the reconcile backbone
            log.exception("deployment_phase_failed")
