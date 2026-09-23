"""PeriodicReconcile — runtime correctness backbone (spec 2026-05-27).

Runs the venue snapshot reconcile (BootRecovery.run) on an interval. The WS
stream is a latency optimization; this loop is what GUARANTEES the ledger
converges to venue truth, so the bot can never get permanently stuck at the
allocation cap when the stream silently breaks.

- Divergence: a release/claim on a periodic (non-boot) run means the stream
  missed an event -> RECONCILE DEGRADED (self-clears on the next clean tick) +
  WARN so the silent breakage surfaces. This loop FULLY OWNS HealthTarget.RECONCILE
  and does NOT touch HealthTarget.BITFINEX_REST (owned by the daemon REST poller).
- Fail-safe: consecutive venue-fetch failures -> EXECUTOR DOWN so AuthHealthGuard
  blocks new offers (never trade on a stale ledger). Cleared on recovery.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Callable
from decimal import Decimal
from typing import Protocol

from bfx_funding_bot.external.bitfinex.auth_rest import ActiveFundingOffer
from bfx_funding_bot.modules.execution.boot_recovery import ReconcileResult
from bfx_funding_bot.modules.marketfeed.schemas import HealthStatus, HealthTarget

log = logging.getLogger(__name__)

_DRIFT_EPSILON = Decimal("0.01")


class _Recovery(Protocol):
    async def run(self) -> ReconcileResult: ...


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
        recovery: _Recovery,
        probe: _Probe,
        interval_s: float,
        max_consecutive_failures: int = 3,
        min_resync_interval_s: float = 10.0,
        monotonic: Callable[[], float] | None = None,
        deployment: _Deployment | None = None,
    ) -> None:
        self._recovery = recovery
        self._probe = probe
        self._interval_s = interval_s
        self._max_failures = max_consecutive_failures
        self._min_resync_interval_s = min_resync_interval_s
        self._monotonic = monotonic or time.monotonic
        self._deployment = deployment
        self._consecutive_failures = 0
        self._tripped_down = False  # this loop owns the EXECUTOR DOWN it sets
        self._divergence_flagged = False  # this loop owns HealthTarget.RECONCILE
        self._resync_event = asyncio.Event()
        self._resync_reason = ""
        self._last_tick_mono = 0.0
        self._recent_fences: tuple[tuple[int, int], ...] = ()

    @property
    def recent_fences(self) -> tuple[tuple[int, int], ...]:
        """The two latest successful reconcile fences for bounded canary evidence.

        This remains diagnostic runtime state only; the preflight authority is
        the immutable, account-local ``reconcile_observation`` projection.
        """
        return self._recent_fences

    def request_resync(self, reason: str) -> None:
        """Request one off-interval reconcile. Synchronous and safe to call from a
        WS callback (same event loop). Multiple calls before the next wake collapse
        into a single reconcile (the Event is idempotent)."""
        self._resync_reason = reason  # best-effort: if several callers race, last wins
        self._resync_event.set()

    async def run_loop(self, stop_event: asyncio.Event) -> None:
        while not stop_event.is_set():
            await self._tick()
            self._last_tick_mono = self._monotonic()
            self._probe.record_heartbeat(self.SUB_TASK)
            if await self._wait_next(stop_event):
                reason = self._resync_reason
                self._resync_event.clear()
                await self._debounce(stop_event)
                log.info("periodic_reconcile_resync reason=%s", reason)

    async def _wait_next(self, stop_event: asyncio.Event) -> bool:
        """Sleep up to interval_s, waking early on stop or a resync request.
        Returns True iff a resync was requested (not on timeout/stop)."""
        stop_task = asyncio.create_task(stop_event.wait())
        trig_task = asyncio.create_task(self._resync_event.wait())
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
        if result.snapshot_event_seq is not None:
            self._recent_fences = (
                *self._recent_fences[-1:],
                (result.snapshot_event_seq, int(time.time() * 1000)),
            )
        if self._tripped_down:
            self._tripped_down = False
            self._probe.update(
                HealthTarget.EXECUTOR, HealthStatus.HEALTHY,
                error_message="venue reconcile recovered",
            )
        drifted = (
            result.realized_drift_usdt > _DRIFT_EPSILON
            or result.reserved_drift_usdt > _DRIFT_EPSILON
        )
        if (
            result.n_released > 0
            or result.n_claimed > 0
            or result.n_matched > 0
            or result.n_quarantined > 0
            or drifted
        ):
            log.warning(
                "periodic_reconcile_divergence released=%d claimed=%d failed=%d "
                "matched=%d quarantined=%d realized_drift=%s reserved_drift=%s "
                "— WS lifecycle path missed events",
                result.n_released, result.n_claimed, result.n_failed,
                result.n_matched, result.n_quarantined,
                result.realized_drift_usdt, result.reserved_drift_usdt,
            )
            self._divergence_flagged = True
            self._probe.update(
                HealthTarget.RECONCILE, HealthStatus.DEGRADED,
                error_message=(
                    f"reconcile drift released={result.n_released} "
                    f"claimed={result.n_claimed} "
                    f"matched={result.n_matched} "
                    f"quarantined={result.n_quarantined} "
                    f"realized_drift={result.realized_drift_usdt} "
                    f"reserved_drift={result.reserved_drift_usdt}"
                ),
            )
        elif self._divergence_flagged:
            self._divergence_flagged = False
            self._probe.update(
                HealthTarget.RECONCILE, HealthStatus.HEALTHY,
                error_message="reconcile drift cleared",
            )
        if self._deployment is not None:
            try:
                await self._deployment.deploy(venue_offers=result.venue_offers)
            except Exception:  # deployment must never crash the reconcile backbone
                log.exception("deployment_phase_failed")
