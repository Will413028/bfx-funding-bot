"""Construct the ledger ports; ``apps/bot_ports.py`` and ``apps/read_models.py`` bind them."""

from __future__ import annotations

import time
from collections.abc import Callable, Collection
from decimal import Decimal
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bfx_funding_bot.modules.ledger import (
    UNKNOWN_SETTLE_MS,
    Acceptance,
    AcceptedConservation,
    AcceptedPositions,
    Attempt,
    Authorized,
    AuthorizeRefused,
    CancelAdmitted,
    CancelProvenance,
    CapitalAuthority,
    CommandAttempt,
    CommandJournal,
    CommandOutcome,
    CommandRefused,
    CycleResult,
    ExecutionHistory,
    ForeignOffers,
    LedgerCapitalRead,
    LedgerCapitalReader,
    LedgerConservationReader,
    LedgerCycleReads,
    LedgerJournal,
    LedgerManagedOffers,
    LedgerObservations,
    LedgerUncertainties,
    LockedCancelGuard,
    LockedCommandGuard,
    ManagedOfferReader,
    ManagedOffers,
    Observation,
    ObservationSink,
    ObservationWindow,
    OpenUncertainty,
    OperatorEvidence,
    OperatorReads,
    OperatorResolution,
    Outcome,
    OutcomeAlreadyRecorded,
    PolicyStore,
    Quarantine,
    QuarantineMember,
    QueryAdmissionRefused,
    QueryHandle,
    Resolution,
    Scope,
    ScopeLock,
    UncertaintyReader,
    VenueHintPublisher,
    VenueHintSink,
    VenueObservation,
)
from bfx_funding_bot.modules.ledger._internal import (
    capital_reader,
    clock,
    conservation_read,
    cycle_reads,
    execution_history,
    journal,
    observation,
    operator_evidence,
    operator_reads,
    operator_resolution,
    policy_store,
    ports,
    quarantine,
    reads,
    resolver,
)
from bfx_funding_bot.modules.ledger._internal.venue_hints import LedgerVenueHintSink
from bfx_funding_bot.modules.ledger.tables import SubmissionAttemptJournalRow
from bfx_funding_bot.modules.trading import CapitalScope


def build_venue_hint_sink(
    *, scope: Scope, request_resync: Callable[[str], None], bus: VenueHintPublisher,
    monotonic: Callable[[], float] = time.monotonic,
) -> VenueHintSink:
    """The ledger hint sink: notifications only, reconciliation owns truth."""
    return LedgerVenueHintSink(
        scope=scope, request_resync=request_resync, bus=bus, monotonic=monotonic,
    )


class _SqlCommandJournal:
    def __init__(
        self, session_factory: async_sessionmaker[AsyncSession], *, max_snapshot_age_ms: int
    ) -> None:
        self._session_factory = session_factory
        self._max_snapshot_age_ms = max_snapshot_age_ms

    async def authorize(
        self, session: AsyncSession, scope: Scope, attempt: CommandAttempt,
        basis_token: str, *, now_ms: int, locked_guard: LockedCommandGuard,
    ) -> Authorized | CommandRefused:
        return await journal.authorize_command(
            session, scope, attempt, basis_token, now_ms=now_ms, locked_guard=locked_guard,
            max_snapshot_age_ms=self._max_snapshot_age_ms,
        )

    async def admit_cancel(
        self, session: AsyncSession, scope: Scope, venue_offer_id: str,
        *, now_ms: int, locked_guard: LockedCancelGuard,
    ) -> CancelAdmitted | CommandRefused:
        return await journal.admit_cancel(
            session, scope, venue_offer_id, now_ms=now_ms, locked_guard=locked_guard,
        )

    @staticmethod
    def _command_outcome(stored: Outcome) -> CommandOutcome:
        return CommandOutcome(stored.kind, stored.venue_offer_id, stored.reason,
                              stored.completed_at_ms, stored.evidence)

    async def record_outcome(self, scope: Scope, attempt_id: UUID, outcome: CommandOutcome) -> None:
        async with self._session_factory.begin() as session:
            try:
                await journal.record_outcome(session, scope, Outcome(
                    attempt_id, outcome.kind, outcome.venue_offer_id, outcome.reason,
                    outcome.completed_at_ms, outcome.evidence,
                ))
            except OutcomeAlreadyRecorded as exc:
                assert isinstance(exc.stored, Outcome)
                raise OutcomeAlreadyRecorded(self._command_outcome(exc.stored)) from exc

    async def read_back_outcome(self, scope: Scope, attempt_id: UUID) -> CommandOutcome | None:
        async with self._session_factory() as session:
            attempt = await session.get(SubmissionAttemptJournalRow, attempt_id)
            if attempt is None:
                raise ValueError("attempt does not exist")
            if (attempt.exchange_account_id, attempt.deployment_environment) != (
                scope.exchange_account_id, scope.deployment_environment,
            ):
                raise ValueError("attempt scope mismatch")
            stored = await journal.read_back_outcome(session, attempt_id)
            if stored is not None and stored.attempt_id != attempt_id:
                raise ValueError("outcome identity mismatch")
            return None if stored is None else self._command_outcome(stored)


def build_command_journal(
    session_factory: async_sessionmaker[AsyncSession], *, max_snapshot_age_ms: int
) -> CommandJournal:
    """``max_snapshot_age_ms`` is the one value the legacy repository is built with."""
    return _SqlCommandJournal(session_factory, max_snapshot_age_ms=max_snapshot_age_ms)


class _SqlLedgerJournal:
    async def bump_clock(self, session: AsyncSession, scope: Scope) -> int:
        return await clock.bump_clock(session, scope)

    async def begin_query(
        self, session: AsyncSession, scope: Scope, started_at_ms: int
    ) -> QueryHandle:
        return await clock.begin_query(session, scope, started_at_ms)

    async def authorize_attempt(
        self,
        session: AsyncSession,
        scope: Scope,
        attempt: Attempt,
        basis_token: str,
        *,
        now_ms: int,
    ) -> Authorized | AuthorizeRefused:
        return await journal.authorize_attempt(session, scope, attempt, basis_token, now_ms=now_ms)

    async def close_dangling(
        self, session: AsyncSession, scope: Scope, *, now_ms: int, grace_ms: int
    ) -> tuple[UUID, ...]:
        return await journal.close_dangling(session, scope, now_ms=now_ms, grace_ms=grace_ms)

    async def record_outcome(self, session: AsyncSession, scope: Scope, outcome: Outcome) -> None:
        await journal.record_outcome(session, scope, outcome)

    async def read_back_outcome(self, session: AsyncSession, attempt_id: UUID) -> Outcome | None:
        return await journal.read_back_outcome(session, attempt_id)

    async def record_resolution(
        self, session: AsyncSession, scope: Scope, resolution: Resolution
    ) -> None:
        await journal.record_resolution(session, scope, resolution)

    async def open_quarantine(
        self, session: AsyncSession, scope: Scope, opening: Quarantine
    ) -> None:
        await quarantine.open_quarantine(session, scope, opening)

    async def add_quarantine_member(
        self, session: AsyncSession, scope: Scope, member: QuarantineMember
    ) -> None:
        await quarantine.add_quarantine_member(session, scope, member)


def build_ledger_journal() -> LedgerJournal:
    return _SqlLedgerJournal()


class _SqlLedgerCapitalReader:
    async def read_capital(
        self,
        session: AsyncSession,
        scope: CapitalScope,
        *,
        now_ms: int,
        max_snapshot_age_ms: int,
    ) -> LedgerCapitalRead:
        return await capital_reader.read_capital(
            session, scope, now_ms=now_ms, max_snapshot_age_ms=max_snapshot_age_ms
        )


def build_ledger_capital_reader() -> LedgerCapitalReader:
    return _SqlLedgerCapitalReader()


class _SqlLedgerConservationReader:
    async def latest(self, session: AsyncSession, scope: Scope) -> AcceptedConservation | None:
        return await conservation_read.latest_conservation(session, scope)


def build_ledger_conservation_reader() -> LedgerConservationReader:
    return _SqlLedgerConservationReader()


class _SqlLedgerCycleReads:
    async def accepted_positions(
        self, session: AsyncSession, scope: Scope
    ) -> AcceptedPositions | None:
        return await cycle_reads.accepted_positions(session, scope)

    async def foreign_live_offers(self, session: AsyncSession, scope: Scope) -> ForeignOffers:
        return await cycle_reads.foreign_live_offers(session, scope)


def build_ledger_cycle_reads() -> LedgerCycleReads:
    return _SqlLedgerCycleReads()


class _SqlLedgerObservations:
    async def observation_window(
        self, session: AsyncSession, scope: Scope
    ) -> ObservationWindow:
        return await reads.observation_window(session, scope)

    async def begin_query(
        self, session: AsyncSession, scope: Scope, started_at_ms: int
    ) -> QueryHandle:
        return await clock.begin_query(session, scope, started_at_ms)

    async def accept(
        self,
        session: AsyncSession,
        scope: Scope,
        handle: QueryHandle,
        first: Observation,
        confirmation: Observation,
        confirmation_started_at_ms: int,
    ) -> Acceptance:
        return await observation.accept_observation(
            session, scope, handle, first, confirmation, confirmation_started_at_ms
        )


def build_ledger_observations() -> LedgerObservations:
    return _SqlLedgerObservations()


class _SqlLedgerUncertainties:
    async def open_uncertainties(
        self, session: AsyncSession, scope: Scope, symbol: str | None = None
    ) -> tuple[OpenUncertainty, ...]:
        return await reads.open_uncertainties(session, scope, symbol)


def build_ledger_uncertainties() -> LedgerUncertainties:
    return _SqlLedgerUncertainties()


class _SqlLedgerManagedOffers:
    async def managed_live_offers(
        self, session: AsyncSession, scope: Scope, symbols: Collection[str] | None = None
    ) -> ManagedOffers:
        return await reads.managed_live_offers(session, scope, symbols)

    async def cancel_provenance(
        self, session: AsyncSession, scope: Scope, venue_offer_id: str
    ) -> CancelProvenance | None:
        return await reads.cancel_provenance(session, scope, venue_offer_id)

    async def fingerprints_in_use(
        self, session: AsyncSession, scope: Scope, symbol: str
    ) -> frozenset[Decimal]:
        return await reads.fingerprints_in_use(session, scope, symbol)


def build_ledger_managed_offers() -> LedgerManagedOffers:
    return _SqlLedgerManagedOffers()


class _LedgerObservationCycle:
    def __init__(
        self, session_factory: async_sessionmaker[AsyncSession], venue: VenueObservation,
        *, journal_port: LedgerJournal, observations: LedgerObservations,
        now_ms: Callable[[], int], grace_ms: int, settle_ms: int,
    ) -> None:
        self._factory = session_factory
        self._venue = venue
        self._journal = journal_port
        self._observations = observations
        self._now_ms = now_ms
        self._grace_ms = grace_ms
        self._settle_ms = settle_ms

    async def run(self, scope: Scope) -> CycleResult:
        async with self._factory.begin() as session:
            await clock.lock_scope(session, scope)
            started = self._now_ms()
            await self._journal.close_dangling(
                session, scope, now_ms=started, grace_ms=self._grace_ms,
            )
            window = await self._observations.observation_window(session, scope)
            try:
                query = await self._observations.begin_query(session, scope, started)
            except QueryAdmissionRefused:
                # Commit dangling closures even when younger attempts refuse admission.
                return CycleResult("query_admission_refused")
        first, confirmation, confirmation_started = await self._venue.observe(
            scope, query.started_at_ms, window,
        )
        async with self._factory.begin() as session:
            await clock.lock_scope(session, scope)
            accepted = await self._observations.accept(
                session, scope, query, first, confirmation, confirmation_started,
            )
            # Only an accepted observation resolves: the journal needs the scope's latest
            # accepted one, and a fenced or incomplete one proves nothing. Same locked txn;
            # the caller publishes any notice after commit.
            resolutions: tuple[UUID, ...] = ()
            if accepted.decision == "accepted":
                assert accepted.observation_id is not None
                resolutions = tuple(
                    item.id for item in await resolver.resolve_unknowns(
                        session, scope, accepted.observation_id,
                        now_ms=self._now_ms(), settle_ms=self._settle_ms,
                    )
                )
        return CycleResult(accepted.decision, accepted.observation_id, resolutions)


def build_observation_sink(
    session_factory: async_sessionmaker[AsyncSession], venue: VenueObservation,
    *, grace_ms: int, journal_port: LedgerJournal | None = None,
    observations: LedgerObservations | None = None,
    now_ms: Callable[[], int] = lambda: time.time_ns() // 1_000_000,
    settle_ms: int = UNKNOWN_SETTLE_MS,
) -> ObservationSink:
    """The ledger observation cycle; ``apps/bot_ports.py`` builds it."""
    return _LedgerObservationCycle(
        session_factory, venue, journal_port=journal_port or build_ledger_journal(),
        observations=observations or build_ledger_observations(), now_ms=now_ms,
        grace_ms=grace_ms, settle_ms=settle_ms,
    )


def build_operator_evidence() -> OperatorEvidence:
    """Operator evidence with the attempt match preview, which reads granted columns only
    (any role may run it); the worker's authority is ``build_operator_resolution().apply``."""
    return operator_evidence.LedgerOperatorEvidence(
        operator_reads.LedgerOperatorReads(), operator_resolution.preview_match
    )


def build_operator_resolution() -> OperatorResolution:
    """The ledger operator request path."""
    return operator_resolution.LedgerOperatorResolution(operator_reads.LedgerOperatorReads())


def build_operator_reads() -> OperatorReads:
    """The ledger operator read model."""
    return operator_reads.LedgerOperatorReads()


def build_execution_history(archive: ExecutionHistory) -> ExecutionHistory:
    """The ledger's execution history: the journal above the switch, ``archive`` (the frozen
    legacy event log) below it, behind one cursor (plan Q4)."""
    return execution_history.LedgerExecutionHistory(archive)


def build_capital_authority(
    session_factory: async_sessionmaker[AsyncSession], *, max_snapshot_age_ms: int
) -> CapitalAuthority:
    """The ledger capital authority; pass the one freshness bound the legacy repository uses."""
    return ports.LedgerCapitalAuthority(session_factory, max_snapshot_age_ms=max_snapshot_age_ms)


def build_uncertainty_reader(session_factory: async_sessionmaker[AsyncSession]) -> UncertaintyReader:
    return ports.LedgerUncertaintyReader(session_factory)


def build_managed_offer_reader() -> ManagedOfferReader:
    return ports.LedgerManagedOfferReader()


def build_policy_store(scope: Scope) -> PolicyStore:
    """The ledger policy store of ``scope``; no event stream is replayed."""
    return policy_store.LedgerPolicyStore(scope)


def build_scope_lock() -> ScopeLock:
    return ports.LedgerScopeLock()
