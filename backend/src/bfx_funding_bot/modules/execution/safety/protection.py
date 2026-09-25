"""Automatic protections: conditions that halt trading by themselves.

ADR 2026-09-25 D5 / plan §1 "Automatic HALTED". Each trigger below writes
``HALTED`` with ``cause=auto`` and runs the venue funding cancel-all through
:class:`~bfx_funding_bot.modules.execution.safety.kill_switch.KillSwitch`:

========================== ==================================================
trigger                    raised where
========================== ==================================================
``submit_outcome_unknown`` a submit ended UNKNOWN (command gate), or recovery
                           turned a crash-interrupted PENDING into UNKNOWN
``orphan_quarantined``     recovery quarantined an active venue offer with no
                           local provenance
``unattributed_offer``     the capital classifier met an active offer it cannot
                           attribute (the orphan seen from the capital side)
``unclassifiable_commitment`` a durable commitment cannot be placed in the
                           snapshot (capital acceptance or a planner read)
``offer_amount_mismatch``  a managed offer's venue amount differs from what was
                           submitted (capital classifier ``offer_amount_conflict``)
``identity_conflict``      the ledger and the venue, or two ledger records,
                           disagree about who or what a commitment is (the
                           classifier's provenance/attempt/evidence conflicts)
``venue_lent_above_ledger`` a confirmed snapshot shows more lent than the
                           ledger plus fills of our own offers can explain
``loss_limiter``           a currency's 24h loss or drawdown crossed its limit
``writer_lock_lost``       the writer lock is not held after a recovery attempt
========================== ==================================================

What never trips: realized (lent) falling because a loan ended, offers moving to
lent because a fill was caught by reconcile instead of WS, offers disappearing
because a cancel or expiry was caught by reconcile -- the correctness backbone
doing its job (ADR 2026-05-29 credit-aware reconcile v2).

Tripping is synchronous and never waits: it records a pending stop that the
trading-state guard honours immediately, and queues the kill. A supervised task
(:meth:`AutomaticProtection.run`) performs the kill outside every lock. That
split is what keeps a trip raised while the command gate's account lock is held
(an UNKNOWN submit) or inside a recovery transaction from deadlocking against
the kill switch, which itself waits for that lock and opens its own transaction.

An automatic HALTED is never lifted automatically; ``cause=auto`` is what the
resume path uses to require a probation period.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol

from bfx_funding_bot.modules.execution.event_store.store import SymbolLedgerDelta
from bfx_funding_bot.modules.execution.events import PositionReconciled
from bfx_funding_bot.modules.execution.safety.kill_switch import KillResult
from bfx_funding_bot.modules.execution.safety.trading_state import CAUSE_AUTO
from bfx_funding_bot.modules.observability import alerts

log = logging.getLogger(__name__)

SUBMIT_OUTCOME_UNKNOWN = "submit_outcome_unknown"
ORPHAN_QUARANTINED = "orphan_quarantined"
UNATTRIBUTED_OFFER = "unattributed_offer"
UNCLASSIFIABLE_COMMITMENT = "unclassifiable_commitment"
OFFER_AMOUNT_MISMATCH = "offer_amount_mismatch"
IDENTITY_CONFLICT = "identity_conflict"
VENUE_LENT_ABOVE_LEDGER = "venue_lent_above_ledger"
LOSS_LIMITER = "loss_limiter"
WRITER_LOCK_LOST = "writer_lock_lost"

TRIGGERS = frozenset({
    SUBMIT_OUTCOME_UNKNOWN, ORPHAN_QUARANTINED, UNATTRIBUTED_OFFER,
    UNCLASSIFIABLE_COMMITMENT, OFFER_AMOUNT_MISMATCH, IDENTITY_CONFLICT, VENUE_LENT_ABOVE_LEDGER,
    LOSS_LIMITER, WRITER_LOCK_LOST,
})

# Capital classifier refusals that are protections, by the reason it raises.
# Others (a pending query, an unstable or stale snapshot, a changed fence) are
# transient observation states: they block spending and retry, not halt.
CAPITAL_BLOCK_TRIGGERS: dict[str, str] = {
    "unattributed_offer": UNATTRIBUTED_OFFER,
    "unclassifiable_commitment": UNCLASSIFIABLE_COMMITMENT,
    "offer_amount_conflict": OFFER_AMOUNT_MISMATCH,
    # Integrity: the same commitment or venue object described two ways. Each
    # of these means a durable record and its evidence disagree, which no
    # retry resolves -- fail closed rather than keep trading around it.
    **dict.fromkeys((
        "offer_provenance_conflict",
        "offer_attempt_conflict",
        "attempt_decision_conflict",
        "attempt_amount_conflict",
        "attempt_projection_conflict",
        "attempt_intent_conflict",
        "attempt_intent_scope_conflict",
        "attempt_outcome_evidence_conflict",
        "attempt_outcome_evidence_identity",
        "attempt_outcome_evidence_scope",
        "duplicate_attempt_intent",
        "execution_unknown_resolution_conflict",
        "snapshot_conflicting_identity",
    ), IDENTITY_CONFLICT),
}

# Same tolerance as the reconcile divergence report.
LEDGER_EPSILON = Decimal("0.01")
_DETAIL_LIMIT = 400


class ProtectionPort(Protocol):
    def trip(self, trigger: str, detail: str) -> None: ...


class _KillSwitch(Protocol):
    async def engage(self, *, cause: str, actor: str, reason: str,
                     when_already_halted: str = "retry") -> KillResult: ...


@dataclass(frozen=True, slots=True)
class Trip:
    trigger: str
    detail: str
    at_ms: int


class AutomaticProtection:
    """Collects trips and turns them into the kill, outside every lock."""

    def __init__(self, *, clock: Callable[[], int] | None = None, retry_s: float = 5.0) -> None:
        self._clock = clock or (lambda: int(time.time() * 1000))
        self._retry_s = retry_s
        self._queue: asyncio.Queue[Trip] = asyncio.Queue()
        self._pending: Trip | None = None
        self._kill_switch: _KillSwitch | None = None
        # Trips that found HALTED already in force: logged, counted, no venue call.
        self.persisting = 0

    def bind(self, kill_switch: _KillSwitch) -> None:
        self._kill_switch = kill_switch

    def trip(self, trigger: str, detail: str) -> None:
        """Record the stop now and queue the kill. Never blocks, never raises."""
        if trigger not in TRIGGERS:
            # Still stop: an unknown trigger name is a programming error, not a
            # reason to keep trading.
            log.error("automatic_protection_unknown_trigger trigger=%s", trigger)
        tripped = Trip(trigger=trigger, detail=detail[:_DETAIL_LIMIT], at_ms=self._clock())
        if self._pending is None:
            self._pending = tripped
        self._queue.put_nowait(tripped)
        log.critical("automatic_protection_tripped trigger=%s detail=%s", trigger, tripped.detail)
        alerts.emit(alerts.PROTECTION_TRIPPED, trigger=trigger, detail=tripped.detail)  # T8

    def pending_reason(self) -> str | None:
        """Why new offers are stopped before the HALTED is committed, if they are."""
        pending = self._pending
        return None if pending is None else f"{pending.trigger}: {pending.detail}"

    async def run(self, stop: asyncio.Event) -> None:
        """Engage the kill for queued trips until ``stop``; supervised by the daemon."""
        while not stop.is_set():
            first = await self._next(stop)
            if first is None:
                return
            batch = [first, *self._drain()]
            await self._engage(batch, stop)

    async def run_pending(self) -> None:
        """Engage everything queued so far, once. For boot paths and tests."""
        batch = self._drain()
        if batch:
            await self._engage(batch, None)

    async def _next(self, stop: asyncio.Event) -> Trip | None:
        getter = asyncio.create_task(self._queue.get())
        waiter = asyncio.create_task(stop.wait())
        try:
            done, _ = await asyncio.wait({getter, waiter}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in (getter, waiter):
                if not task.done():
                    task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await task
        return getter.result() if getter in done else None

    def _drain(self) -> list[Trip]:
        drained: list[Trip] = []
        while True:
            try:
                drained.append(self._queue.get_nowait())
            except asyncio.QueueEmpty:
                return drained

    async def _engage(self, batch: list[Trip], stop: asyncio.Event | None) -> None:
        first = batch[0]
        triggers = sorted({trip.trigger for trip in batch})
        reason = "; ".join(f"{trip.trigger}: {trip.detail}" for trip in batch)[:1000]
        while True:
            if self._kill_switch is None:
                error: str | None = "kill switch not bound"
            else:
                try:
                    # Already HALTED: the condition persisting is not a new
                    # stop. Only the transition into HALTED runs cancel-all and
                    # writes audit rows; an operator's /admin/halt retries it.
                    result = await self._kill_switch.engage(
                        cause=CAUSE_AUTO, actor=f"auto:{first.trigger}", reason=reason,
                        when_already_halted="skip",
                    )
                except Exception as exc:
                    error = repr(exc)
                else:
                    error = None
                    if result.state_changed:
                        log.critical("automatic_protection_engaged triggers=%s state_id=%s "
                                     "cancel_all_complete=%s", triggers, result.state.id,
                                     result.complete)
                    else:
                        self.persisting += len(batch)
                        log.warning("automatic_protection_condition_persists triggers=%s "
                                    "state_id=%s (already HALTED; no cancel-all)", triggers,
                                    result.state.id)
            if error is None:
                # HALTED is durable now; the guard reads it from the database.
                if self._queue.empty():
                    self._pending = None
                return
            # The stop stays pending (new offers stay blocked) until HALTED is
            # written; keep trying rather than dropping a protection.
            log.critical("automatic_protection_engage_failed triggers=%s error=%s", triggers, error)
            if stop is None:
                return
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=self._retry_s)
            if stop.is_set():
                return


class LedgerConservation:
    """Decides whether a snapshot's lent can be explained by the ledger.

    Per symbol, against the ledger just before the snapshot: lent may fall
    (a loan ended) and may rise only by what our own offers lost (fills,
    including ones WS missed). Anything above that is money lent at the venue
    that this bot did not lend -- ``venue_lent_above_ledger``. That halts
    whatever its cause: the account is dedicated to this bot and venue
    auto-renew is off, so lending the bot did not do is by definition
    unexplained (Will, 2026-09-25).

    Only snapshots the capital authority accepted are judged: acceptance needs
    two identical fetches, so a fill landing between the offers and the credits
    request (which counts the same money twice) cannot be mistaken for new
    lending. The deltas of unaccepted snapshots are carried to the next accepted
    one, because such a snapshot still resets the ledger and would otherwise
    absorb an anomaly unseen. A symbol with no earlier venue observation only
    establishes its baseline.
    """

    def __init__(self, *, epsilon: Decimal = LEDGER_EPSILON) -> None:
        self._epsilon = epsilon
        self._carry: dict[str, tuple[Decimal, Decimal]] = {}

    def observe(self, deltas: Iterable[SymbolLedgerDelta], *, confirmed: bool) -> list[str]:
        anomalies: list[str] = []
        for delta in deltas:
            if not delta.baseline:
                self._carry.pop(delta.symbol, None)
                continue
            carried_offered, carried_lent = self._carry.pop(delta.symbol, (Decimal(0), Decimal(0)))
            offered_change = carried_offered + delta.offered_change
            lent_change = carried_lent + delta.lent_change
            if not confirmed:
                self._carry[delta.symbol] = (offered_change, lent_change)
                continue
            unexplained = lent_change - max(Decimal(0), -offered_change)
            if unexplained > self._epsilon:
                anomalies.append(
                    f"symbol={delta.symbol} lent_change={lent_change} "
                    f"offered_change={offered_change} unexplained={unexplained}"
                )
        return anomalies


class _NavSource(Protocol):
    async def on_position_reconciled(self, event: PositionReconciled) -> None: ...
    def realized_loss_pct_24h(self, symbol: str) -> float: ...
    def drawdown_pct(self, symbol: str) -> float: ...


class LossLimitMonitor:
    """The loss limiter as a protection, evaluated where its inputs change.

    Wraps the NAV tracker's PositionReconciled handler (the bus runs handlers
    concurrently, so a sibling subscriber could read the metrics before they
    are updated) and trips once a currency crosses its limit. The L2 guards
    keep blocking offers on their own; the trip makes the stop durable, so a
    24h window rolling past the loss no longer resumes lending.
    """

    def __init__(self, *, source: _NavSource, protection: ProtectionPort,
                 realized_loss_threshold_pct: float | None,
                 drawdown_threshold_pct: float | None) -> None:
        self._source = source
        self._protection = protection
        self._loss = realized_loss_threshold_pct
        self._drawdown = drawdown_threshold_pct
        self._tripped: set[tuple[str, str]] = set()

    async def on_position_reconciled(self, event: PositionReconciled) -> None:
        await self._source.on_position_reconciled(event)
        symbol = event.symbol
        for metric, threshold, value in (
            ("realized_loss_pct_24h", self._loss, self._source.realized_loss_pct_24h(symbol)),
            ("drawdown_pct", self._drawdown, self._source.drawdown_pct(symbol)),
        ):
            if threshold is None or value <= threshold:
                self._tripped.discard((symbol, metric))
                continue
            if (symbol, metric) in self._tripped:
                continue  # already tripped for this breach; HALTED is not lifted automatically
            self._tripped.add((symbol, metric))
            self._protection.trip(LOSS_LIMITER, f"{metric}[{symbol}]={value:.4f} > {threshold}")


class _WriterLock(Protocol):
    async def refresh(self) -> bool: ...


class WriterLockWatch:
    """One liveness check of the writer lock; trips when it is not held after recovery.

    ``refresh`` re-acquires a lock whose connection dropped while nobody else
    holds it; only a lock still not held afterwards (another writer has it, or
    re-acquisition failed) is a loss.
    """

    def __init__(self, *, lock: _WriterLock, protection: ProtectionPort) -> None:
        self._lock = lock
        self._protection = protection
        self._lost = False

    async def check(self) -> bool:
        held = await self._lock.refresh()
        if held:
            self._lost = False
        elif not self._lost:
            self._lost = True
            self._protection.trip(WRITER_LOCK_LOST, "writer lock not held after refresh")
        return held


__all__ = [
    "CAPITAL_BLOCK_TRIGGERS",
    "IDENTITY_CONFLICT",
    "LEDGER_EPSILON",
    "LOSS_LIMITER",
    "OFFER_AMOUNT_MISMATCH",
    "ORPHAN_QUARANTINED",
    "SUBMIT_OUTCOME_UNKNOWN",
    "TRIGGERS",
    "UNATTRIBUTED_OFFER",
    "UNCLASSIFIABLE_COMMITMENT",
    "VENUE_LENT_ABOVE_LEDGER",
    "WRITER_LOCK_LOST",
    "AutomaticProtection",
    "LedgerConservation",
    "LossLimitMonitor",
    "ProtectionPort",
    "Trip",
    "WriterLockWatch",
]
