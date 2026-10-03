"""What follows a ledger observation cycle: protection, NAV and alerts.

The legacy reconcile does these inside ``BootRecovery.run`` (``_protect``, the monitors,
the ``PositionReconciled`` fan-out). The ledger cycle only observes and accepts, so the
same policy runs here, strictly after ``inner.run`` returned: its transactions are
committed, every read below sees what the cycle stored, and a failing effect can neither
undo nor fail the observation. This wraps the ledger cycle only; the legacy sink does
all of this itself, and wrapping it would publish every position twice.

Which cycles act:

* Accepted: protection (a trigger-class capital refusal trips, none is a clean
  observation), the NAV signal, the notices of automatic UNKNOWN resolutions.
* Every cycle: the conservation, foreign-exposure and quarantine-age alerts. They read the
  latest accepted basis and the open uncertainties, so a cycle that was not accepted shows
  them nothing new; each de-duplicates by what it alerts about.

A cycle that was not accepted neither trips nor counts as clean: it observed nothing the
capital authority could judge.
"""
from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from decimal import Decimal
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bfx_funding_bot.external.bitfinex.auth_rest import ActiveFundingOffer
from bfx_funding_bot.modules.execution.boot_recovery import (
    ForeignExposureMonitor,
    QuarantineAgeMonitor,
)
from bfx_funding_bot.modules.execution.bus import DomainEventBus
from bfx_funding_bot.modules.execution.command_boundary import publish_best_effort
from bfx_funding_bot.modules.execution.events import PositionReconciled
from bfx_funding_bot.modules.execution.observation_sink import (
    LegacyCycleResult,
    LegacyObservationSink,
)
from bfx_funding_bot.modules.execution.safety.protection import (
    FOREIGN_LENDING,
    VENUE_LENT_ABOVE_LEDGER,
    ReconcileProtectionPort,
    capital_block_trigger,
)
from bfx_funding_bot.modules.ledger import (
    CapitalAuthority,
    CapitalBlocked,
    CycleResult,
    ForeignOffer,
    LedgerConservationReader,
    LedgerCycleReads,
    ObservationSink,
    OperatorReads,
    Scope,
    SymbolConservation,
    UnknownResolutionNotice,
    observation_evidence_ref,
)
from bfx_funding_bot.modules.observability import alerts
from bfx_funding_bot.modules.trading import CapitalScope

log = logging.getLogger(__name__)

_LEGACY_REFUSED = ("ledger cycle effects wrap the ledger cycle only: the legacy reconcile "
                   "runs these itself, and wrapping it would publish every position twice")

# The open set is small by construction (one entry per unresolved UNKNOWN); the bound only
# keeps the read finite, and is far above anything the age monitor needs to track.
_OPEN_UNCERTAINTY_LIMIT = 10_000


@dataclass(frozen=True, slots=True)
class _AgingUnknown:
    """An open uncertainty as the quarantine age monitor reads it."""

    attempt_id: UUID
    symbol: str
    started_at_ms: int
    amount: Decimal


class LedgerCycleEffects:
    """An ``ObservationSink`` that runs the cycle's effects after ``inner`` returned."""

    def __init__(
        self,
        inner: ObservationSink,
        *,
        scope: Scope,
        account_id: str,
        session_factory: async_sessionmaker[AsyncSession],
        capital: CapitalAuthority,
        cells: Sequence[tuple[str, str]],
        protection: ReconcileProtectionPort,
        bus: DomainEventBus,
        reads: LedgerCycleReads,
        conservation: LedgerConservationReader,
        operator_reads: OperatorReads,
        foreign_exposure: ForeignExposureMonitor,
        quarantine_age: QuarantineAgeMonitor,
        foreign_grace_ms: int,
        clock: Callable[[], int],
    ) -> None:
        if foreign_grace_ms < 0:
            raise ValueError("foreign_grace_ms must be non-negative")
        if isinstance(inner, LegacyObservationSink):
            raise TypeError(_LEGACY_REFUSED)
        self._inner = inner
        self._scope = scope
        self._account_id = account_id
        self._sf = session_factory
        self._capital = capital
        # (symbol, cell) pairs, deduplicated in order: one capital read each.
        self._cells = tuple(dict.fromkeys(cells))
        self._protection = protection
        self._bus = bus
        self._reads = reads
        self._conservation = conservation
        self._operator_reads = operator_reads
        self._foreign_exposure = foreign_exposure
        self._quarantine_age = quarantine_age
        self._foreign_grace_ms = foreign_grace_ms
        self._clock = clock
        # The basis whose foreign lending was already alerted.
        self._conservation_alerted: UUID | None = None

    async def run(self, scope: Scope) -> CycleResult:
        cycle = await self._inner.run(scope)
        if isinstance(cycle, LegacyCycleResult):
            raise TypeError(_LEGACY_REFUSED)
        # The cycle is durable. A failing effect is logged and never fails or repeats it.
        if cycle.decision == "accepted":
            await self._guard("protection", self._protect(cycle))
            await self._guard("nav", self._publish_nav(cycle))
            await self._guard("resolutions", self._publish_resolutions(cycle))
        await self._guard("conservation", self._alert_conservation())
        await self._guard("foreign_exposure", self._alert_foreign_exposure())
        await self._guard("quarantine_age", self._alert_quarantine_age())
        return cycle

    async def _guard(self, effect: str, work: Awaitable[None]) -> None:
        try:
            await work
        except Exception as exc:
            log.critical("ledger_cycle_effect_failed effect=%s err=%r", effect, exc)

    async def _protect(self, cycle: CycleResult) -> None:
        assert cycle.observation_id is not None
        tripped = False
        unreadable = False
        reported: set[tuple[str, str]] = set()
        for symbol, cell in self._cells:
            try:
                read = await self._capital.read(
                    CapitalScope(
                        self._scope.exchange_account_id, self._scope.deployment_environment,
                        symbol, cell,
                    ),
                    now_ms=self._clock(), session=None,
                )
            except Exception as exc:
                # Not judged: no trip, and no evidence that the condition cleared either.
                unreadable = True
                log.critical("ledger_cycle_capital_read_failed symbol=%s cell=%s err=%r",
                             symbol, cell, exc)
                continue
            if not isinstance(read, CapitalBlocked):
                continue
            trigger = capital_block_trigger(read.reason)
            if trigger is None:
                continue  # a transient refusal: spending is blocked, nothing is halted
            tripped = True
            evidence = ", ".join(f"{key}={value}" for key, value in read.evidence)
            self._trip(
                reported, symbol, trigger,
                f"capital authority refused {symbol}/{cell}: {read.reason}"
                + (f" ({evidence})" if evidence else ""),
            )
        # Lending nothing explains is judged per symbol of the cycle's own basis, whether or
        # not a cell is configured for it (the capital read above only sees configured cells).
        try:
            unexplained = await self._unexplained_symbols(cycle)
        except Exception as exc:
            unreadable = True
            log.critical("ledger_cycle_conservation_read_failed err=%r", exc)
        else:
            for verdict in unexplained:
                tripped = True
                self._trip(
                    reported, verdict.symbol, VENUE_LENT_ABOVE_LEDGER,
                    f"symbol={verdict.symbol} lent_unexplained={verdict.lent_unexplained} "
                    f"foreign_executed={verdict.foreign_executed} "
                    f"fill_conflicts={verdict.fill_conflicts}",
                )
        if not tripped and not unreadable:
            self._protection.observe_clean(observation_evidence_ref(cycle.observation_id))

    def _trip(self, reported: set[tuple[str, str]], symbol: str, trigger: str,
              detail: str) -> None:
        """One trip per (symbol, trigger) and cycle, whichever read found it first."""
        if (symbol, trigger) in reported:
            return
        reported.add((symbol, trigger))
        self._protection.trip(trigger, detail)

    async def _unexplained_symbols(self, cycle: CycleResult) -> list[SymbolConservation]:
        async with self._sf() as session, session.begin():
            basis = await self._reads.accepted_positions(session, self._scope)
            latest = await self._conservation.latest(session, self._scope)
        if basis is None or latest is None or basis.observation_id != cycle.observation_id:
            # Not the cycle's own basis (or none): it proves nothing about this cycle.
            raise LookupError(f"no conservation verdict for observation {cycle.observation_id}")
        return [v for v in latest.symbols if v.conservation == "unexplained_lending"]

    async def _publish_nav(self, cycle: CycleResult) -> None:
        async with self._sf() as session, session.begin():
            positions = await self._reads.accepted_positions(session, self._scope)
        if positions is None or positions.observation_id != cycle.observation_id:
            # Another basis than the cycle's: publishing it could repeat a cycle's signal.
            log.warning("ledger_nav_skipped cycle=%s basis=%s", cycle.observation_id,
                        None if positions is None else positions.observation_id)
            return
        for position in positions.symbols:
            await publish_best_effort(self._bus, PositionReconciled(
                account_id=self._account_id,
                symbol=position.symbol,
                # Legacy reserved is every active offer; a foreign one is the account's too,
                # and leaving it out would read a manual offer as a NAV drop.
                reserved=position.offered + position.foreign_offers,
                realized=position.credits,
                available=position.available,
                n_offers=position.n_offers,
                n_credits=position.n_credits,
                occurred_at_ms=positions.accepted_at_ms,
            ))

    async def _publish_resolutions(self, cycle: CycleResult) -> None:
        assert cycle.observation_id is not None
        for resolution_id in cycle.resolutions:
            await publish_best_effort(self._bus, UnknownResolutionNotice(
                self._scope, cycle.observation_id, resolution_id,
            ))

    async def _alert_conservation(self) -> None:
        async with self._sf() as session, session.begin():
            latest = await self._conservation.latest(session, self._scope)
        # One basis is judged once, however many cycles read it before the next is accepted.
        if latest is None or latest.basis_id == self._conservation_alerted:
            return
        self._conservation_alerted = latest.basis_id
        for verdict in latest.symbols:
            if verdict.conservation != "foreign_lending":
                continue  # unexplained lending is a trip, through the capital read
            detail = (f"symbol={verdict.symbol} lent_unexplained={verdict.lent_unexplained} "
                      f"foreign_executed={verdict.foreign_executed}")
            log.warning("foreign_lending %s", detail)
            alerts.emit(FOREIGN_LENDING, detail=detail)

    async def _alert_foreign_exposure(self) -> None:
        async with self._sf() as session, session.begin():
            found = await self._reads.foreign_live_offers(session, self._scope)
        now_ms = self._clock()
        self._foreign_exposure.observe(
            [_active_offer(offer) for offer in found.offers
             if now_ms - offer.mts_created >= self._foreign_grace_ms],
            active_ids=set(found.live_offer_ids),
        )

    async def _alert_quarantine_age(self) -> None:
        async with self._sf() as session, session.begin():
            open_views = await self._operator_reads.list_uncertainties(
                session, self._scope, state="open", limit=_OPEN_UNCERTAINTY_LIMIT,
            )
        self._quarantine_age.observe(
            [_AgingUnknown(view.uncertainty_id, view.symbol, view.opened_at_ms,
                           view.intended_amount)
             for view in open_views if view.opened_at_ms is not None],
            now_ms=self._clock(),
        )


def _active_offer(offer: ForeignOffer) -> ActiveFundingOffer:
    return ActiveFundingOffer(
        venue_offer_id=offer.venue_offer_id,
        symbol=offer.symbol,
        amount=offer.amount_remaining,
        rate=float(offer.rate) if offer.rate is not None else 0.0,
        period_days=offer.period_days if offer.period_days is not None else 0,
        mts_created=offer.mts_created,
        status=offer.status,
        amount_original=offer.amount_original,
        rate_observed=offer.rate_observed,
        rate_decimal=offer.rate,
    )


__all__ = ["LedgerCycleEffects"]
