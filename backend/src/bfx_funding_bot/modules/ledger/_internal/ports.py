"""Ledger implementations of the facade's consumer read ports (S1-3e1).

Apps build them (``apps/bot_ports.py``). They answer the facade's Protocols from the
ledger's journals, accepted basis and offer mirror.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Collection
from contextlib import asynccontextmanager

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bfx_funding_bot.modules.ledger import (
    CapitalAvailable,
    CapitalBlocked,
    CapitalRead,
    LiveManagedOffer,
    OpenUncertainty,
    ProvenanceConflict,
    Scope,
    UncertaintyRecord,
    encode_basis_token,
)
from bfx_funding_bot.modules.ledger._internal import capital_reader, clock, reads
from bfx_funding_bot.modules.trading import (
    AppliedPolicy,
    Available,
    Blocked,
    CapitalScope,
    fingerprint_of,
)


@asynccontextmanager
async def _snapshot_session(
    factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncSession]:
    """A short REPEATABLE READ READ ONLY session; always rolled back, never committed."""
    async with factory() as session:
        try:
            await session.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"))
            yield session
        finally:
            await session.rollback()


def _blocked(blocked: Blocked) -> CapitalBlocked:
    return CapitalBlocked(blocked.reason, blocked.evidence)


class LedgerCapitalAuthority:
    """``CapitalAuthority`` over the latest accepted basis, its tail and the policy head.

    ``max_snapshot_age_ms`` is injected: the composition root passes the one
    value the legacy repository is built with.
    """

    def __init__(
        self, session_factory: async_sessionmaker[AsyncSession], *, max_snapshot_age_ms: int
    ) -> None:
        self._session_factory = session_factory
        self._max_snapshot_age_ms = max_snapshot_age_ms

    async def read(
        self, scope: CapitalScope, *, now_ms: int, session: AsyncSession | None = None
    ) -> CapitalRead:
        if session is None:
            async with _snapshot_session(self._session_factory) as owned:
                read = await capital_reader.read_capital(
                    owned, scope, now_ms=now_ms, max_snapshot_age_ms=self._max_snapshot_age_ms
                )
        else:
            # Same key as every ledger writer and the legacy writer: a writer that
            # committed before this point is visible to the READ COMMITTED reads
            # below, and one after it waits until the caller's transaction ends.
            await clock.lock_scope(session, Scope(scope.account_id, scope.environment))
            read = await capital_reader.read_capital_locked(
                session, scope, now_ms=now_ms, max_snapshot_age_ms=self._max_snapshot_age_ms
            )
        result = read.result
        if isinstance(result, Blocked):
            return _blocked(result)
        assert isinstance(result, Available)
        view = result.view
        if read.query_id is None or read.clock_revision is None:
            # Unreachable (an accepted basis has a query and a clock row); never guess a token.
            return CapitalBlocked("snapshot_evidence_conflict", (("basis_token", "unavailable"),))
        return CapitalAvailable(
            applied=view.applied,
            snapshot=view.snapshot,
            budget=view.budget,
            unattributed_credit_exposure=view.unattributed_credit_exposure,
            basis_token=encode_basis_token(read.query_id, read.clock_revision),
        )

    async def read_policy(
        self, session: AsyncSession, scope: Scope, symbol: str
    ) -> AppliedPolicy | CapitalBlocked:
        policy = await capital_reader.read_policy(session, scope, symbol)
        return _blocked(policy) if isinstance(policy, Blocked) else policy


def _record(scope: Scope, item: OpenUncertainty) -> UncertaintyRecord:
    return UncertaintyRecord(
        scope.exchange_account_id,
        scope.deployment_environment,
        item.symbol,
        reads.UNCERTAINTY_KINDS[item.subject_kind],
    )


class LedgerUncertaintyReader:
    """``UncertaintyReader``: UNKNOWN attempts and unresolved quarantines."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def list_open(
        self, session: AsyncSession | None, scope: Scope, symbol: str | None = None
    ) -> tuple[UncertaintyRecord, ...]:
        if session is None:
            async with _snapshot_session(self._session_factory) as owned:
                return await self.list_open(owned, scope, symbol)
        return tuple(
            _record(scope, item) for item in await reads.open_uncertainties(session, scope, symbol)
        )

    async def has_open(self, session: AsyncSession | None, scope: Scope, symbol: str) -> bool:
        if session is None:
            async with _snapshot_session(self._session_factory) as owned:
                return await self.has_open(owned, scope, symbol)
        return await reads.has_open_uncertainty(session, scope, symbol)


class LedgerManagedOfferReader:
    """``ManagedOfferReader`` over the offer mirror of the latest accepted snapshot."""

    async def live(
        self, session: AsyncSession, scope: Scope, symbols: Collection[str] | None = None
    ) -> tuple[LiveManagedOffer, ...]:
        found = await reads.managed_live_offers(session, scope, symbols)
        if found.provenance_conflicts:
            # Neither managed nor foreign may be guessed: the caller fails closed.
            raise ProvenanceConflict(found.provenance_conflicts[0])
        return tuple(
            LiveManagedOffer(offer.venue_offer_id, offer.symbol, offer.signal_correlation_id)
            for offer in found.offers
        )

    async def count_live(self, session: AsyncSession, scope: Scope, symbol: str) -> int:
        return await reads.count_live_offers(session, scope, symbol)

    async def live_symbols(self, session: AsyncSession, scope: Scope) -> frozenset[str]:
        return await reads.live_symbols(session, scope)

    async def fingerprints_in_use(
        self, session: AsyncSession, scope: Scope, symbol: str
    ) -> frozenset[int]:
        # Falsy drops both an invalid wire amount (None) and "no fingerprint" (0), as legacy does.
        return frozenset(
            fingerprint
            for amount in await reads.fingerprints_in_use(session, scope, symbol)
            if (fingerprint := fingerprint_of(amount))
        )


class LedgerScopeLock:
    """``ScopeLock``: the scope's transaction advisory lock (re-entrant)."""

    async def lock(self, session: AsyncSession, scope: Scope) -> None:
        await clock.lock_scope(session, scope)
