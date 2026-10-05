"""Automatic protections: the conditions that stop trading by themselves.

Lending envelope ADR 2026-09-25 D3, ladder level 3: only conditions meaning the
bot's knowledge of its own offers is wrong. Each writes ``HALTED`` with
``cause=auto``; the planner then pulls the *managed* offers by venue id, every
tick until none is left (``DeploymentReconciler._pull_if_stopped``). Offers
placed by hand are never touched: the venue cancel-all is the operator's kill.

============================= ===============================================
trigger                       raised where
============================= ===============================================
``unclassifiable_commitment`` a durable commitment cannot be placed in the
                              snapshot (capital acceptance or a planner read)
``offer_amount_mismatch``     a managed offer's venue amount differs from what
                              was submitted (classifier ``offer_amount_conflict``)
``identity_conflict``         the ledger and the venue, or two ledger records,
                              disagree about who or what a commitment is
``venue_lent_above_ledger``   a confirmed snapshot shows more lent than the
                              ledger, our fills and foreign fills can explain
``command_rate_exceeded``     the command gate kept throttling venue writes
============================= ===============================================

What is deliberately not here: an UNKNOWN submit or an unreadable snapshot
quarantines its currency until evidence resolves it (level 2, capital
repository); a foreign offer and a NAV drop only alert (level 4,
:class:`NavDropMonitor`); a lost writer lock ends the process
(:class:`WriterLockWatch`), because it is about which process may write, not
about the trading decision.

What never trips: realized (lent) falling because a loan ended, offers moving to
lent because a fill was caught by reconcile instead of WS, offers disappearing
because a cancel or expiry was caught by reconcile -- the correctness backbone
doing its job (ADR 2026-05-29 credit-aware reconcile v2).

Tripping is synchronous and never waits: it records a pending stop that the
trading-state guard honours immediately, and queues the durable HALTED. A
supervised task (:meth:`AutomaticProtection.run`) writes it outside every lock.
That split is what keeps a trip raised while the command gate's account lock is
held or inside a recovery transaction from deadlocking against the write,
which takes the same account lock in its own transaction.

An automatic HALTED ends by itself once its condition has cleared (ADR
2026-09-26 auto-halt-resumes-when-condition-clears): at least
``AUTO_RESUME_MIN_HALT_MS`` after it, and after ``AUTO_RESUME_CLEAN_SNAPSHOTS``
consecutive accepted snapshots in which nothing tripped -- any trip starts the
count again. Condition-based, not time-based: a conflict no retry resolves keeps
tripping and so keeps the halt; a timing race or a throttle burst clears. The
database allows at most ``AUTO_RESUME_MAX_PER_WINDOW`` automatic resumes per
rolling window; past that the halt stays for an operator and says so. An
operator's halt is never lifted automatically.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol

from bfx_funding_bot.modules.execution.events import PositionReconciled
from bfx_funding_bot.modules.execution.safety.trading_state import (
    ACTIVE,
    AUTO_RESUME_MIN_HALT_MS,
    CAUSE_AUTO,
    HALTED,
    AutoResumeLimitReached,
    IllegalTradingTransition,
    TradingState,
    TransitionResult,
)
from bfx_funding_bot.modules.ledger.conservation import LEDGER_EPSILON
from bfx_funding_bot.modules.observability import alerts

log = logging.getLogger(__name__)

UNCLASSIFIABLE_COMMITMENT = "unclassifiable_commitment"
OFFER_AMOUNT_MISMATCH = "offer_amount_mismatch"
IDENTITY_CONFLICT = "identity_conflict"
VENUE_LENT_ABOVE_LEDGER = "venue_lent_above_ledger"
COMMAND_RATE_EXCEEDED = "command_rate_exceeded"

TRIGGERS = frozenset({
    UNCLASSIFIABLE_COMMITMENT, OFFER_AMOUNT_MISMATCH, IDENTITY_CONFLICT, VENUE_LENT_ABOVE_LEDGER,
    COMMAND_RATE_EXCEEDED,
})

# Level 4 alerts (modules/observability/alerts).
NAV_DROP = "nav_drop"
FOREIGN_LENDING = "foreign_lending"

# Capital classifier refusals that are protections, by the reason it raises.
# Others (a pending query, an unstable or stale snapshot, a changed fence) are
# transient observation states: they block spending and retry, not halt.
CAPITAL_BLOCK_TRIGGERS: dict[str, str] = {
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
        # Ledger authority (S1-2c): an attempt in the tail whose outcome or
        # resolution contradicts its own record, and an uncertainty fact from
        # another scope -- the same integrity class as the legacy codes above.
        "attempt_evidence_conflict",
        "uncertainty_scope_conflict",
    ), IDENTITY_CONFLICT),
    # Ledger conservation (S1-3e5c): the accepted basis stored lending that no
    # observed offer and no foreign execution explains.
    "venue_lent_above_ledger": VENUE_LENT_ABOVE_LEDGER,
}
# Ledger refusals that deliberately stay transient: more attempts since the
# latest accepted basis than one read folds. The read already refuses to spend,
# and the next accepted basis bounds the tail again, so it retries, not halts.
CAPITAL_BLOCK_TRANSIENT: frozenset[str] = frozenset({"attempt_tail_unbounded"})


def capital_block_trigger(reason: str) -> str | None:
    """The protection trigger a capital authority refusal trips, or None (transient)."""
    return CAPITAL_BLOCK_TRIGGERS.get(reason)

# Consecutive accepted snapshots without a trip that show a halt's condition cleared.
AUTO_RESUME_CLEAN_SNAPSHOTS = 3
AUTO_RESUME_ACTOR = "auto-resume"

_DETAIL_LIMIT = 400


@dataclass(frozen=True, slots=True)
class SymbolLedgerDelta:
    """One symbol's ledger immediately before a complete snapshot, and what it saw.

    ``baseline`` is False when the ledger had never been set by a venue
    observation for this symbol (new currency, first deployment): there is
    nothing to compare against, so the observation only establishes one.
    """
    symbol: str
    prior_offered: Decimal
    prior_lent: Decimal
    observed_offered: Decimal
    observed_lent: Decimal
    baseline: bool

    @property
    def offered_change(self) -> Decimal:
        return self.observed_offered - self.prior_offered

    @property
    def lent_change(self) -> Decimal:
        return self.observed_lent - self.prior_lent


class ProtectionPort(Protocol):
    def trip(self, trigger: str, detail: str) -> None: ...


class ReconcileProtectionPort(ProtectionPort, Protocol):
    def observe_clean(self, evidence: str | None) -> None: ...


class _TradingState(Protocol):
    async def current(self) -> TradingState | None: ...

    async def transition(self, state: str, *, cause: str, actor: str, reason: str,
                         now_ms: int | None = None) -> TransitionResult: ...


@dataclass(frozen=True, slots=True)
class Trip:
    trigger: str
    detail: str
    at_ms: int


@dataclass(frozen=True, slots=True)
class CleanObservation:
    # Opaque reference to the accepted observation (legacy: its snapshot event_seq).
    evidence: str | None
    at_ms: int


class AutomaticProtection:
    """Collects trips and turns them into a durable HALTED/auto, outside every lock.

    Also the one place that lifts a HALTED/auto again, from the same supervised
    task, so a resume and a halt are never written concurrently by it.
    """

    def __init__(self, *, clock: Callable[[], int] | None = None, retry_s: float = 5.0) -> None:
        self._clock = clock or (lambda: int(time.time() * 1000))
        self._retry_s = retry_s
        # One inbox, handled in arrival order: a trip and a clean observation are
        # never reordered against each other.
        self._queue: asyncio.Queue[Trip | CleanObservation] = asyncio.Queue()
        self._pending: Trip | None = None
        self._trading: _TradingState | None = None
        # Trips that found HALTED already in force: logged and counted.
        self.persisting = 0
        # Accepted snapshots in which nothing tripped, newest last; a trip clears it.
        self._clean: list[CleanObservation] = []
        self._limit_alerted: set[int] = set()
        self.auto_resumes = 0

    def bind(self, trading_state: _TradingState) -> None:
        self._trading = trading_state

    def trip(self, trigger: str, detail: str) -> None:
        """Record the stop now and queue the HALTED. Never blocks, never raises."""
        if trigger not in TRIGGERS:
            # Still stop: an unknown trigger name is a programming error, not a
            # reason to keep trading.
            log.error("automatic_protection_unknown_trigger trigger=%s", trigger)
        tripped = Trip(trigger=trigger, detail=detail[:_DETAIL_LIMIT], at_ms=self._clock())
        self._clean.clear()
        if self._pending is None:
            self._pending = tripped
        self._queue.put_nowait(tripped)
        log.critical("automatic_protection_tripped trigger=%s detail=%s", trigger, tripped.detail)
        alerts.emit(alerts.PROTECTION_TRIPPED, trigger=trigger, detail=tripped.detail)  # T8

    def observe_clean(self, evidence: str | None) -> None:
        """An accepted snapshot in which nothing tripped. Never blocks, never raises.

        ``evidence`` names the observation. One that is already counted is not counted again:
        the auto-resume rule is *distinct* clean observations. Without a reference every
        call is its own observation.
        """
        if evidence is not None and any(obs.evidence == evidence for obs in self._clean):
            return
        observation = CleanObservation(evidence=evidence, at_ms=self._clock())
        self._clean.append(observation)
        del self._clean[:-AUTO_RESUME_CLEAN_SNAPSHOTS]
        self._queue.put_nowait(observation)

    def pending_reason(self) -> str | None:
        """Why new offers are stopped before the HALTED is committed, if they are."""
        pending = self._pending
        return None if pending is None else f"{pending.trigger}: {pending.detail}"

    async def run(self, stop: asyncio.Event) -> None:
        """Write HALTED for queued trips, and lift a cleared HALTED/auto, until
        ``stop``; supervised by the daemon."""
        while not stop.is_set():
            first = await self._next(stop)
            if first is None:
                return
            items = [first, *self._drain()]
            batch = [item for item in items if isinstance(item, Trip)]
            if batch:
                # A trip in the batch outranks any clean observation queued with it.
                await self._engage(batch, stop)
            else:
                await self.resume_if_cleared()

    async def run_pending(self) -> None:
        """Engage everything queued so far, once. For boot paths and tests."""
        batch = [item for item in self._drain() if isinstance(item, Trip)]
        if batch:
            await self._engage(batch, None)

    async def _next(self, stop: asyncio.Event) -> Trip | CleanObservation | None:
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

    async def resume_if_cleared(self) -> bool:
        """Lift the current HALTED/auto if its condition has cleared; True if lifted.

        Cleared means: nothing pending or queued, the halt is at least
        ``AUTO_RESUME_MIN_HALT_MS`` old, and the last ``AUTO_RESUME_CLEAN_SNAPSHOTS``
        clean observations all came after it (a trip in between would have
        emptied them). The database refuses a resume past its rolling limit;
        that is alerted once per halt and the halt stays for an operator.
        """
        if self._trading is None or self._pending is not None:
            return False
        try:
            current = await self._trading.current()
        except Exception as exc:
            log.warning("auto_resume_state_unreadable error=%r", exc)
            return False
        if current is None or current.state != HALTED or current.cause != CAUSE_AUTO:
            return False
        clean = [obs for obs in self._clean if obs.at_ms > current.created_at_ms]
        now_ms = self._clock()
        if (len(clean) < AUTO_RESUME_CLEAN_SNAPSHOTS
                or now_ms - current.created_at_ms < AUTO_RESUME_MIN_HALT_MS):
            return False
        refs = ", ".join(str(obs.evidence) for obs in clean)
        minutes = (now_ms - current.created_at_ms) // 60_000
        reason = (f"condition cleared after {minutes} min; clean observations: {refs}; "
                  f"halt #{current.id} was {current.reason}")[:1000]
        try:
            result = await self._trading.transition(
                ACTIVE, cause=CAUSE_AUTO, actor=AUTO_RESUME_ACTOR, reason=reason, now_ms=now_ms)
        except AutoResumeLimitReached as exc:
            log.warning("auto_resume_refused state_id=%s error=%s", current.id, exc)
            if current.id not in self._limit_alerted:
                self._limit_alerted.add(current.id)
                alerts.emit(alerts.AUTO_RESUME_LIMIT_REACHED, state_id=current.id,
                            reason=current.reason[:_DETAIL_LIMIT])
            return False
        except IllegalTradingTransition as exc:
            log.warning("auto_resume_refused state_id=%s error=%s", current.id, exc)
            return False
        except Exception as exc:
            log.warning("auto_resume_failed state_id=%s error=%r", current.id, exc)
            return False
        if not result.changed:
            return False
        self._clean.clear()
        self.auto_resumes += 1
        log.warning("auto_resumed state_id=%s halt_id=%s %s", result.state.id, current.id, reason)
        return True


    def _drain(self) -> list[Trip | CleanObservation]:
        drained: list[Trip | CleanObservation] = []
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
            if self._trading is None:
                error: str | None = "trading state not bound"
            else:
                try:
                    # Already HALTED: the condition persisting is not a new stop.
                    result = await self._trading.transition(
                        HALTED, cause=CAUSE_AUTO, actor=f"auto:{first.trigger}", reason=reason,
                        now_ms=self._clock())
                except Exception as exc:
                    error = repr(exc)
                else:
                    error = None
                    if result.changed:
                        log.critical("automatic_protection_engaged triggers=%s state_id=%s",
                                     triggers, result.state.id)
                    else:
                        self.persisting += len(batch)
                        log.warning("automatic_protection_condition_persists triggers=%s "
                                    "state_id=%s (already HALTED)", triggers, result.state.id)
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


@dataclass(frozen=True, slots=True)
class ConservationVerdict:
    # Lent nothing -- ours or anyone's -- explains: the ledger is wrong (level 3).
    anomalies: tuple[str, ...]
    # Lent explained only by foreign offers that filled unseen (level 4).
    foreign: tuple[str, ...]


class LedgerConservation:
    """Decides whether a snapshot's lent can be explained.

    Per symbol, against the ledger just before the snapshot: lent may fall (a
    loan ended) and may rise by what offers lost (fills, including ones WS
    missed -- the ledger's offers are every venue offer, ours or foreign). What
    is left is lending no observed offer accounts for. Lending envelope D2: the
    account may also carry foreign offers, and one can be placed and filled
    between two snapshots without ever being seen; the offer history shows it
    executed. So the remainder is first set against ``foreign_executed`` (the
    symbol's unmanaged offers that executed since the previous accepted
    snapshot): fully covered is foreign lending (alert), anything beyond is
    ``venue_lent_above_ledger``.

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

    def observe(self, deltas: Iterable[SymbolLedgerDelta], *, confirmed: bool,
                foreign_executed: Mapping[str, Decimal] | None = None) -> ConservationVerdict:
        anomalies: list[str] = []
        foreign: list[str] = []
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
            if unexplained <= self._epsilon:
                continue
            by_foreign = (foreign_executed or {}).get(delta.symbol, Decimal(0))
            detail = (f"symbol={delta.symbol} lent_change={lent_change} "
                      f"offered_change={offered_change} unexplained={unexplained} "
                      f"foreign_executed={by_foreign}")
            if unexplained - by_foreign > self._epsilon:
                anomalies.append(detail)
            else:
                foreign.append(detail)
        return ConservationVerdict(tuple(anomalies), tuple(foreign))


class _NavSource(Protocol):
    async def on_position_reconciled(self, event: PositionReconciled) -> None: ...
    def realized_loss_pct_24h(self, symbol: str) -> float: ...
    def drawdown_pct(self, symbol: str) -> float: ...


class NavDropMonitor:
    """Level 4: alert when a currency's NAV fell past a threshold; never stops.

    Lending envelope D3: funding NAV is native units (available + offered +
    lent), so a bad rate only earns less -- it cannot lower NAV. What lowers it
    is a withdrawal, a transfer or a platform loss, none of which stopping the
    lending undoes. Wraps the NAV tracker's PositionReconciled handler (the bus
    runs handlers concurrently, so a sibling could read metrics before they are
    updated) and alerts once per breach.
    """

    def __init__(self, *, source: _NavSource, realized_loss_threshold_pct: float | None,
                 drawdown_threshold_pct: float | None) -> None:
        self._source = source
        self._loss = realized_loss_threshold_pct
        self._drawdown = drawdown_threshold_pct
        self._alerted: set[tuple[str, str]] = set()

    async def on_position_reconciled(self, event: PositionReconciled) -> None:
        await self._source.on_position_reconciled(event)
        symbol = event.symbol
        for metric, threshold, value in (
            ("realized_loss_pct_24h", self._loss, self._source.realized_loss_pct_24h(symbol)),
            ("drawdown_pct", self._drawdown, self._source.drawdown_pct(symbol)),
        ):
            if threshold is None or value <= threshold:
                self._alerted.discard((symbol, metric))
                continue
            if (symbol, metric) in self._alerted:
                continue
            self._alerted.add((symbol, metric))
            log.warning("nav_drop symbol=%s %s=%.4f threshold=%s", symbol, metric, value, threshold)
            alerts.emit(NAV_DROP, level=alerts.WARNING, symbol=symbol, metric=metric,
                        value=f"{value:.4f}", threshold=threshold)


class _WriterLock(Protocol):
    async def refresh(self) -> bool: ...


class WriterLockLostError(RuntimeError):
    """The writer lock is gone: this process must stop writing and exit."""


class WriterLockWatch:
    """One liveness check of the writer lock; a loss ends the process.

    ``refresh`` re-acquires a lock whose connection dropped while nobody else
    holds it; only a lock still not held afterwards (another writer has it, or
    re-acquisition failed) is a loss. That is process fencing, not a trading
    decision: raising takes the daemon's task group down, the container
    restarts, and boot waits for the lock again. No trading state is written.
    """

    def __init__(self, *, lock: _WriterLock) -> None:
        self._lock = lock

    async def check(self) -> bool:
        if await self._lock.refresh():
            return True
        log.critical("writer_lock_lost: exiting so no second writer talks to the venue")
        alerts.emit(alerts.DAEMON_FATAL, error="writer lock not held after refresh; exiting")
        raise WriterLockLostError("writer lock not held after refresh")


__all__ = [
    "AUTO_RESUME_ACTOR",
    "AUTO_RESUME_CLEAN_SNAPSHOTS",
    "CAPITAL_BLOCK_TRANSIENT",
    "CAPITAL_BLOCK_TRIGGERS",
    "COMMAND_RATE_EXCEEDED",
    "FOREIGN_LENDING",
    "IDENTITY_CONFLICT",
    "NAV_DROP",
    "OFFER_AMOUNT_MISMATCH",
    "TRIGGERS",
    "UNCLASSIFIABLE_COMMITMENT",
    "VENUE_LENT_ABOVE_LEDGER",
    "AutomaticProtection",
    "CleanObservation",
    "ConservationVerdict",
    "LedgerConservation",
    "NavDropMonitor",
    "ProtectionPort",
    "ReconcileProtectionPort",
    "Trip",
    "WriterLockLostError",
    "WriterLockWatch",
    "capital_block_trigger",
]
