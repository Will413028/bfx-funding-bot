"""Legacy implementations of the ledger's consumer read ports (S1-3c1).

The capital epoch is still ``legacy``: these adapters answer the ledger facade's
Protocols (``CapitalAuthority``, ``UncertaintyReader``, ``ManagedOfferReader``,
``ScopeLock``) from the event-sourced authority, running exactly the reads the
consumers used to run themselves. Only composition roots (``apps``, operator
scripts) construct them; consumers see the Protocols.
"""

from __future__ import annotations

from collections.abc import Collection
from typing import Any

from sqlalchemy import ColumnElement, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bfx_funding_bot.modules.execution.amount_fingerprint import fingerprints_in_use
from bfx_funding_bot.modules.execution.capital_repository import (
    AppliedCapitalPolicy,
    CapitalBlockedError,
    CapitalRepository,
    CapitalView,
    policy_from_row,
    read_policy_row,
)
from bfx_funding_bot.modules.execution.capital_runtime import CapitalRuntime
from bfx_funding_bot.modules.execution.event_store.tables import VenueOfferStateRow
from bfx_funding_bot.modules.execution.uncertainty_tables import ExecutionUncertaintyRow
from bfx_funding_bot.modules.ledger import (
    CapitalAvailable,
    CapitalBlocked,
    CapitalRead,
    LiveManagedOffer,
    PolicyRefused,
    Scope,
    UncertaintyRecord,
)
from bfx_funding_bot.modules.trading import AppliedPolicy, CapitalPolicy, CapitalScope


def legacy_basis_token(snapshot_seq: int) -> str:
    """The legacy basis token: the accepted snapshot's event_seq as a decimal string."""
    return str(snapshot_seq)


def legacy_snapshot_seq(token: str) -> int:
    """Back to the snapshot_seq the legacy command boundary binds; refuse anything else."""
    if not (token.isascii() and token.isdigit()) or legacy_basis_token(int(token)) != token:
        raise CapitalBlockedError("snapshot_changed")
    return int(token)


def available_from_view(view: CapitalView, scope: CapitalScope) -> CapitalAvailable:
    applied = view.applied
    return CapitalAvailable(
        applied=AppliedPolicy(
            account_id=scope.account_id,
            environment=scope.environment,
            symbol=scope.symbol,
            revision=applied.revision,
            digest=applied.digest,
            revision_id=applied.revision_id,
            policy=applied.policy,
        ),
        snapshot=view.snapshot,
        budget=view.budget,
        unattributed_credit_exposure=view.unattributed_credit_exposure,
        basis_token=legacy_basis_token(view.snapshot_seq),
        attribution=view.attribution,
    )


def _check_scope(repository: CapitalRepository, account_id: object, environment: str) -> None:
    if (account_id, environment) != (repository.account_id, repository.environment):
        raise ValueError("capital_scope_conflict")


class LegacyCapitalAuthority:
    """``CapitalAuthority`` over ``CapitalRepository`` (account-locked replay read)."""

    def __init__(self, runtime: CapitalRuntime) -> None:
        self._runtime = runtime

    async def read(
        self, scope: CapitalScope, *, now_ms: int, session: AsyncSession | None = None
    ) -> CapitalRead:
        repository = self._runtime.repository
        _check_scope(repository, scope.account_id, scope.environment)
        if session is None:
            # Advisory-lock replay can update projections. A preview rolls those
            # updates back; only the command/snapshot owner may commit them.
            async with self._runtime.session_factory() as owned:
                return await self._read(scope, now_ms=now_ms, session=owned)
        return await self._read(scope, now_ms=now_ms, session=session)

    async def _read(
        self, scope: CapitalScope, *, now_ms: int, session: AsyncSession
    ) -> CapitalRead:
        repository = self._runtime.repository
        try:
            view = await repository.read_capital(
                session,
                symbol=scope.symbol,
                cell_id=scope.cell_id,
                now_ms=now_ms,
            )
        except CapitalBlockedError as exc:
            return CapitalBlocked(str(exc))
        return available_from_view(view, scope)

    async def read_policy(
        self, session: AsyncSession, scope: Scope, symbol: str
    ) -> AppliedPolicy | CapitalBlocked:
        try:
            row = await read_policy_row(
                session,
                account_id=scope.exchange_account_id,
                environment=scope.deployment_environment,
                symbol=symbol,
            )
            policy = policy_from_row(row)
        except CapitalBlockedError as exc:
            return CapitalBlocked(str(exc))
        return AppliedPolicy(
            account_id=scope.exchange_account_id,
            environment=scope.deployment_environment,
            symbol=symbol,
            revision=row.revision,
            digest=row.digest,
            revision_id=row.id,
            policy=policy,
        )


class LegacyUncertaintyReader:
    """``UncertaintyReader`` over the ``execution_uncertainties`` projection."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def list_open(
        self, session: AsyncSession | None, scope: Scope, symbol: str | None = None
    ) -> tuple[UncertaintyRecord, ...]:
        if session is None:
            async with self._session_factory() as owned:
                return await self._list_open(owned, scope, symbol)
        return await self._list_open(session, scope, symbol)

    async def _list_open(
        self, session: AsyncSession, scope: Scope, symbol: str | None
    ) -> tuple[UncertaintyRecord, ...]:
        query = select(ExecutionUncertaintyRow).where(
            ExecutionUncertaintyRow.exchange_account_id == scope.exchange_account_id,
            ExecutionUncertaintyRow.deployment_environment == scope.deployment_environment,
            ExecutionUncertaintyRow.state == "open",
        )
        if symbol is not None:
            query = query.where(ExecutionUncertaintyRow.symbol == symbol)
        return tuple(
            UncertaintyRecord(
                exchange_account_id=row.exchange_account_id,
                deployment_environment=row.deployment_environment,
                symbol=row.symbol,
                kind=row.kind,
            )
            for row in (await session.scalars(query)).all()
        )

    async def has_open(self, session: AsyncSession | None, scope: Scope, symbol: str) -> bool:
        if session is None:
            async with self._session_factory() as owned:
                return await self._has_open(owned, scope, symbol)
        return await self._has_open(session, scope, symbol)

    async def _has_open(self, session: AsyncSession, scope: Scope, symbol: str) -> bool:
        row = await session.scalar(
            select(ExecutionUncertaintyRow.uncertainty_id)
            .where(
                ExecutionUncertaintyRow.exchange_account_id == scope.exchange_account_id,
                ExecutionUncertaintyRow.deployment_environment == scope.deployment_environment,
                ExecutionUncertaintyRow.symbol == symbol,
                ExecutionUncertaintyRow.state == "open",
            )
            .limit(1)
        )
        return row is not None


def _live(scope: Scope) -> tuple[ColumnElement[bool], ...]:
    return (
        VenueOfferStateRow.exchange_account_id == scope.exchange_account_id,
        VenueOfferStateRow.deployment_environment == scope.deployment_environment,
        VenueOfferStateRow.is_terminal.is_(False),
    )


class LegacyManagedOffers:
    """``ManagedOfferReader`` over ``venue_offer_state`` and the claim/attempt tables.

    Managed means a durable intent traces to the offer
    (``venue_offer_state.execution_decision_id``); manual and auto-renew
    offers are not managed.
    """

    async def live(
        self, session: AsyncSession, scope: Scope, symbols: Collection[str] | None = None
    ) -> tuple[LiveManagedOffer, ...]:
        query = select(VenueOfferStateRow).where(
            *_live(scope),
            VenueOfferStateRow.execution_decision_id.is_not(None),
        )
        if symbols is not None:
            query = query.where(VenueOfferStateRow.symbol.in_(list(symbols)))
        rows = (await session.scalars(query.order_by(VenueOfferStateRow.venue_offer_id))).all()
        return tuple(
            LiveManagedOffer(
                venue_offer_id=row.venue_offer_id,
                symbol=row.symbol,
                signal_correlation_id=row.signal_correlation_id,
            )
            for row in rows
        )

    async def count_live(self, session: AsyncSession, scope: Scope, symbol: str) -> int:
        count = await session.scalar(
            select(func.count())
            .select_from(VenueOfferStateRow)
            .where(
                *_live(scope),
                VenueOfferStateRow.symbol == symbol,
                VenueOfferStateRow.execution_decision_id.is_not(None),
            )
        )
        return int(count or 0)

    async def live_symbols(self, session: AsyncSession, scope: Scope) -> frozenset[str]:
        return frozenset(
            await session.scalars(select(VenueOfferStateRow.symbol).where(*_live(scope)).distinct())
        )

    async def fingerprints_in_use(
        self, session: AsyncSession, scope: Scope, symbol: str
    ) -> frozenset[int]:
        return await fingerprints_in_use(
            session,
            account_id=scope.exchange_account_id,
            environment=scope.deployment_environment,
            symbol=symbol,
        )


class LegacyScopeLock:
    """``ScopeLock``: the account event stream's xact lock, readiness check and replay."""

    def __init__(self, repository: CapitalRepository) -> None:
        self._repository = repository

    async def lock(self, session: AsyncSession, scope: Scope) -> None:
        _check_scope(self._repository, scope.exchange_account_id, scope.deployment_environment)
        await self._repository.writer.prepare_locked(session, account_id=scope.exchange_account_id)


class LegacyPolicyStore:
    """``PolicyStore`` over ``CapitalRepository``: every call replays the account stream first."""

    def __init__(self, repository: CapitalRepository) -> None:
        self._repository = repository
        self._scope = Scope(repository.account_id, repository.environment)

    @property
    def scope(self) -> Scope:
        return self._scope

    async def read_applied(self, session: AsyncSession, *, symbol: str) -> AppliedPolicy:
        try:
            applied = await self._repository.read_applied(session, symbol=symbol)
        except CapitalBlockedError as exc:
            raise PolicyRefused(str(exc)) from exc
        return self._applied(symbol, applied)

    async def apply_policy(
        self, session: AsyncSession, *, symbol: str, policy: CapitalPolicy,
        expected_revision: int, source: dict[str, Any],
    ) -> AppliedPolicy:
        try:
            applied = await self._repository.apply_policy(
                session, symbol=symbol, policy=policy,
                expected_revision=expected_revision, source=source,
            )
        except CapitalBlockedError as exc:
            raise PolicyRefused(str(exc)) from exc
        return self._applied(symbol, applied)

    def _applied(self, symbol: str, applied: AppliedCapitalPolicy) -> AppliedPolicy:
        return AppliedPolicy(
            account_id=self._scope.exchange_account_id,
            environment=self._scope.deployment_environment, symbol=symbol,
            revision=applied.revision, digest=applied.digest,
            revision_id=applied.revision_id, policy=applied.policy,
        )


__all__ = [
    "LegacyCapitalAuthority",
    "LegacyManagedOffers",
    "LegacyPolicyStore",
    "LegacyScopeLock",
    "LegacyUncertaintyReader",
    "available_from_view",
    "legacy_basis_token",
    "legacy_snapshot_seq",
]
