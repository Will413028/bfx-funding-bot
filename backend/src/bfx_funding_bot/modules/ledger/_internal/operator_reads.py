"""Operator-console reads over the ledger (the ``ledger`` authority's ``OperatorReads``).

Runs as the web API's role, so every statement names exactly the columns its
column grant allows: never ``evidence``, ``raw``, ``scope_block`` or
``normalized_payload``. The open set is ``reads.open_uncertainties`` (UNKNOWN
attempts and quarantines without a resolution, R6 quarantines included); the
resolved set is the scope's resolution journal, newest first
(``ix_execution_resolution_scope_resolved``).
"""

from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass
from decimal import Decimal
from typing import Literal
from uuid import UUID

from sqlalchemy import ColumnElement, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import load_only

from bfx_funding_bot.modules.ledger import OfferView, PositionView, Scope, UncertaintyView
from bfx_funding_bot.modules.ledger._internal import reads
from bfx_funding_bot.modules.ledger._internal.attempts import attempts_by_id, fresh_all
from bfx_funding_bot.modules.ledger._internal.basis import previous_basis
from bfx_funding_bot.modules.ledger._internal.provenance import offer_provenance, sole_owner
from bfx_funding_bot.modules.ledger.tables import (
    AcceptedCapitalBasisCreditRow,
    AcceptedCapitalBasisRow,
    AcceptedCapitalBasisSymbolRow,
    ExecutionResolutionJournalRow,
    QuarantineOpeningRow,
    SubmissionAttemptJournalRow,
    TransportOutcomeJournalRow,
    VenueOfferMirrorRow,
)

_ATTEMPT_KIND = reads.UNCERTAINTY_KINDS["attempt"]
_QUARANTINE_KIND = reads.UNCERTAINTY_KINDS["quarantine"]
_R6_REASON = "acked_offer_unobserved"


@dataclass(frozen=True, slots=True)
class _Attempt:
    attempt_id: UUID
    symbol: str
    started_at_ms: int
    intended_amount: Decimal
    outcome_kind: str | None
    outcome_reason: str | None
    outcome_completed_at_ms: int | None


@dataclass(frozen=True, slots=True)
class _Quarantine:
    quarantine_id: UUID
    symbol: str
    intended_amount: Decimal
    opened_at_ms: int
    source_attempt_id: UUID | None


@dataclass(frozen=True, slots=True)
class _Resolution:
    id: UUID
    attempt_id: UUID | None
    quarantine_id: UUID | None
    symbol: str
    actor_kind: str
    actor_id: str
    resolved_at_ms: int
    reason: str


def _attempt_view(
    attempt: _Attempt, resolution: _Resolution | None
) -> UncertaintyView:
    return UncertaintyView(
        uncertainty_id=attempt.attempt_id,
        kind=_ATTEMPT_KIND,
        symbol=attempt.symbol,
        state="open" if resolution is None else "resolved",
        intended_amount=attempt.intended_amount,
        evidence={
            "outcome_reason": attempt.outcome_reason,
            "observed_at_ms": attempt.outcome_completed_at_ms,
        },
        attempt_id=attempt.attempt_id,
        opened_at_ms=attempt.started_at_ms,
        resolved_at_ms=resolution.resolved_at_ms if resolution is not None else None,
        resolved_by_operator_id=_resolved_by(resolution),
        resolution_reason=resolution.reason if resolution is not None else None,
    )


def _quarantine_view(
    quarantine: _Quarantine, resolution: _Resolution | None
) -> UncertaintyView:
    return UncertaintyView(
        uncertainty_id=quarantine.quarantine_id,
        kind=_QUARANTINE_KIND,
        symbol=quarantine.symbol,
        state="open" if resolution is None else "resolved",
        intended_amount=quarantine.intended_amount,
        evidence=(
            {"outcome_reason": _R6_REASON} if quarantine.source_attempt_id is not None else {}
        ),
        opened_at_ms=quarantine.opened_at_ms,
        resolved_at_ms=resolution.resolved_at_ms if resolution is not None else None,
        resolved_by_operator_id=_resolved_by(resolution),
        resolution_reason=resolution.reason if resolution is not None else None,
    )


def _resolved_by(resolution: _Resolution | None) -> str | None:
    if resolution is None or resolution.actor_kind != "operator":
        return None
    return resolution.actor_id


async def _attempts(
    session: AsyncSession, scope: Scope, ids: set[UUID]
) -> dict[UUID, _Attempt]:
    if not ids:
        return {}
    rows = await session.execute(
        select(
            SubmissionAttemptJournalRow.attempt_id,
            SubmissionAttemptJournalRow.symbol,
            SubmissionAttemptJournalRow.started_at_ms,
            SubmissionAttemptJournalRow.intended_amount,
            TransportOutcomeJournalRow.kind,
            TransportOutcomeJournalRow.reason,
            TransportOutcomeJournalRow.completed_at_ms,
        )
        .outerjoin(
            TransportOutcomeJournalRow,
            TransportOutcomeJournalRow.attempt_id == SubmissionAttemptJournalRow.attempt_id,
        )
        .where(
            SubmissionAttemptJournalRow.attempt_id.in_(sorted(ids)),
            SubmissionAttemptJournalRow.exchange_account_id == scope.exchange_account_id,
            SubmissionAttemptJournalRow.deployment_environment == scope.deployment_environment,
        )
    )
    return {row[0]: _Attempt(*row) for row in rows}


async def _quarantines(
    session: AsyncSession, scope: Scope, ids: set[UUID]
) -> dict[UUID, _Quarantine]:
    if not ids:
        return {}
    rows = await session.execute(
        select(
            QuarantineOpeningRow.quarantine_id,
            QuarantineOpeningRow.symbol,
            QuarantineOpeningRow.intended_amount,
            QuarantineOpeningRow.opened_at_ms,
            QuarantineOpeningRow.source_attempt_id,
        ).where(
            QuarantineOpeningRow.quarantine_id.in_(sorted(ids)),
            QuarantineOpeningRow.exchange_account_id == scope.exchange_account_id,
            QuarantineOpeningRow.deployment_environment == scope.deployment_environment,
        )
    )
    return {row[0]: _Quarantine(*row) for row in rows}


_RESOLUTION_COLUMNS = (
    ExecutionResolutionJournalRow.id,
    ExecutionResolutionJournalRow.attempt_id,
    ExecutionResolutionJournalRow.quarantine_id,
    ExecutionResolutionJournalRow.symbol,
    ExecutionResolutionJournalRow.actor_kind,
    ExecutionResolutionJournalRow.actor_id,
    ExecutionResolutionJournalRow.resolved_at_ms,
    ExecutionResolutionJournalRow.reason,
)


@dataclass(frozen=True, slots=True)
class _OfferAttempt:
    attempt_id: UUID
    symbol: str
    started_at_ms: int
    intended_amount: Decimal
    outcome_kind: str | None
    outcome_venue_offer_id: str | None
    outcome_completed_at_ms: int | None


async def _offer_attempts(session: AsyncSession, ids: set[UUID]) -> dict[UUID, _OfferAttempt]:
    """Attempts with their transport outcome, by key; granted columns only."""
    if not ids:
        return {}
    rows = await session.execute(
        select(
            SubmissionAttemptJournalRow.attempt_id,
            SubmissionAttemptJournalRow.symbol,
            SubmissionAttemptJournalRow.started_at_ms,
            SubmissionAttemptJournalRow.intended_amount,
            TransportOutcomeJournalRow.kind,
            TransportOutcomeJournalRow.venue_offer_id,
            TransportOutcomeJournalRow.completed_at_ms,
        )
        .outerjoin(
            TransportOutcomeJournalRow,
            TransportOutcomeJournalRow.attempt_id == SubmissionAttemptJournalRow.attempt_id,
        )
        .where(SubmissionAttemptJournalRow.attempt_id.in_(sorted(ids)))
    )
    return {row[0]: _OfferAttempt(*row) for row in rows}


async def _live_owned_offers(
    session: AsyncSession, scope: Scope
) -> tuple[dict[UUID, VenueOfferMirrorRow], set[str]]:
    """Live mirror rows owned by exactly one attempt (by attempt), and every live venue id.

    The bot's ``reads.managed_live_offers`` also reads ``execution_decisions`` for the
    signal correlation, which the web API has no grant on; an offer needs none here.
    Foreign rows (no provenance) and conflicted ones (contradictory provenance) are
    never owned, but their venue ids still count as reflected.
    """
    mirrors = sorted(
        await fresh_all(
            session,
            select(VenueOfferMirrorRow)
            .options(
                load_only(
                    VenueOfferMirrorRow.symbol,
                    VenueOfferMirrorRow.amount_remaining,
                    VenueOfferMirrorRow.mts_updated,
                )
            )
            .where(
                VenueOfferMirrorRow.exchange_account_id == scope.exchange_account_id,
                VenueOfferMirrorRow.deployment_environment == scope.deployment_environment,
                VenueOfferMirrorRow.present_in_latest_accepted_snapshot,
            ),
        ),
        key=lambda row: row.venue_offer_id,
    )
    provenance = await offer_provenance(session, scope, [row.venue_offer_id for row in mirrors])
    owners = {
        row.attempt_id: row
        for row in await attempts_by_id(
            session,
            [attempt_id for ids in provenance.values() for attempt_id in ids],
            with_payload=False,
        )
    }
    owned: dict[UUID, VenueOfferMirrorRow] = {}
    for mirror in mirrors:
        try:
            owner = sole_owner(provenance[mirror.venue_offer_id], owners, scope, mirror.symbol)
        except LookupError:
            continue
        if owner is not None:
            owned.setdefault(owner.attempt_id, mirror)
    return owned, {row.venue_offer_id for row in mirrors}


class LedgerOperatorReads:
    async def list_uncertainties(
        self,
        session: AsyncSession,
        scope: Scope,
        *,
        state: Literal["open", "resolved"] | None,
        limit: int,
    ) -> tuple[UncertaintyView, ...]:
        views: list[UncertaintyView] = []
        if state in (None, "open"):
            views.extend((await self._open(session, scope))[:limit])
        remaining = limit - len(views)
        if state in (None, "resolved") and remaining > 0:
            views.extend(await self._resolved(session, scope, remaining))
        return tuple(views)

    async def _open(self, session: AsyncSession, scope: Scope) -> list[UncertaintyView]:
        found = await reads.open_uncertainties(session, scope)
        attempts = await _attempts(
            session, scope, {u.subject_id for u in found if u.subject_kind == "attempt"}
        )
        quarantines = await _quarantines(
            session, scope, {u.subject_id for u in found if u.subject_kind == "quarantine"}
        )
        keyed: list[tuple[int, UUID, UncertaintyView]] = []
        for item in found:
            if item.subject_kind == "attempt":
                attempt = attempts.get(item.subject_id)
                if attempt is not None:
                    keyed.append(
                        (attempt.started_at_ms, attempt.attempt_id, _attempt_view(attempt, None))
                    )
            else:
                quarantine = quarantines.get(item.subject_id)
                if quarantine is not None:
                    keyed.append(
                        (
                            quarantine.opened_at_ms,
                            quarantine.quarantine_id,
                            _quarantine_view(quarantine, None),
                        )
                    )
        keyed.sort(key=lambda entry: (entry[0], entry[1]), reverse=True)
        return [view for _, _, view in keyed]

    async def _resolved(
        self, session: AsyncSession, scope: Scope, limit: int
    ) -> list[UncertaintyView]:
        rows = await session.execute(
            select(*_RESOLUTION_COLUMNS)
            .where(
                ExecutionResolutionJournalRow.exchange_account_id == scope.exchange_account_id,
                ExecutionResolutionJournalRow.deployment_environment
                == scope.deployment_environment,
            )
            .order_by(
                ExecutionResolutionJournalRow.resolved_at_ms.desc(),
                ExecutionResolutionJournalRow.id.desc(),
            )
            .limit(limit)
        )
        resolutions = [_Resolution(*row) for row in rows]
        attempts = await _attempts(
            session, scope, {r.attempt_id for r in resolutions if r.attempt_id is not None}
        )
        quarantines = await _quarantines(
            session, scope, {r.quarantine_id for r in resolutions if r.quarantine_id is not None}
        )
        views: list[UncertaintyView] = []
        for resolution in resolutions:
            if resolution.attempt_id is not None:
                attempt = attempts.get(resolution.attempt_id)
                if attempt is not None:
                    views.append(_attempt_view(attempt, resolution))
            elif resolution.quarantine_id is not None:
                quarantine = quarantines.get(resolution.quarantine_id)
                if quarantine is not None:
                    views.append(_quarantine_view(quarantine, resolution))
        return views

    async def get_uncertainty(
        self, session: AsyncSession, scope: Scope, uncertainty_id: UUID
    ) -> UncertaintyView | None:
        attempt = (await _attempts(session, scope, {uncertainty_id})).get(uncertainty_id)
        if attempt is not None and attempt.outcome_kind == "unknown":
            resolution = await self._resolution(
                session, ExecutionResolutionJournalRow.attempt_id == uncertainty_id
            )
            return _attempt_view(attempt, resolution)
        quarantine = (await _quarantines(session, scope, {uncertainty_id})).get(uncertainty_id)
        if quarantine is None:
            return None
        resolution = await self._resolution(
            session, ExecutionResolutionJournalRow.quarantine_id == uncertainty_id
        )
        return _quarantine_view(quarantine, resolution)

    async def _resolution(self, session: AsyncSession, subject: ColumnElement[bool]) -> _Resolution | None:
        row = (
            await session.execute(select(*_RESOLUTION_COLUMNS).where(subject))
        ).one_or_none()
        return _Resolution(*row) if row is not None else None


    async def list_positions(
        self, session: AsyncSession, scope: Scope
    ) -> tuple[PositionView, ...]:
        basis = await previous_basis(session, scope)
        if basis is None:
            return ()
        # ``previous_basis`` loads only the identity columns; the stamp is read by key.
        accepted_at_ms = await session.scalar(
            select(AcceptedCapitalBasisRow.accepted_at_ms).where(
                AcceptedCapitalBasisRow.id == basis.id
            )
        )
        assert accepted_at_ms is not None
        counted = await session.execute(
            select(AcceptedCapitalBasisCreditRow.symbol, func.count())
            .where(AcceptedCapitalBasisCreditRow.basis_id == basis.id)
            .group_by(AcceptedCapitalBasisCreditRow.symbol)
        )
        counts = dict(counted.tuples().all())
        rows = await session.execute(
            select(
                AcceptedCapitalBasisSymbolRow.symbol,
                AcceptedCapitalBasisSymbolRow.available,
                AcceptedCapitalBasisSymbolRow.offered,
                AcceptedCapitalBasisSymbolRow.credits,
                AcceptedCapitalBasisSymbolRow.unattributed_credits,
            )
            .where(AcceptedCapitalBasisSymbolRow.basis_id == basis.id)
            .order_by(AcceptedCapitalBasisSymbolRow.symbol)
        )
        return tuple(
            PositionView(
                symbol=symbol,
                available=available,
                offered=offered,
                lent=credits,
                unattributed_lent=unattributed,
                n_credits=counts.get(symbol, 0),
                last_updated_ms=accepted_at_ms,
                last_reconciled_at_ms=accepted_at_ms,
            )
            for symbol, available, offered, credits, unattributed in rows.tuples()
        )

    async def list_offers(
        self, session: AsyncSession, scope: Scope, *, states: Collection[str]
    ) -> tuple[OfferView, ...]:
        """Managed offers only: pending, open UNKNOWN, claimed. History is not read here."""
        if not set(states) & {"pending", "unknown", "claimed"}:
            return ()
        candidates = await reads.candidate_attempts(session, scope)
        owned, live_ids = await _live_owned_offers(session, scope)
        attempts = await _offer_attempts(
            session, {row.attempt_id for row in candidates.attempts} | set(owned)
        )
        views: list[OfferView] = []

        def add(
            attempt: _OfferAttempt, state: str, venue_offer_id: str | None, updated_ms: int
        ) -> None:
            if state in states:
                views.append(
                    OfferView(
                        offer_key=str(attempt.attempt_id),
                        venue_offer_id=venue_offer_id,
                        state=state,
                        symbol=attempt.symbol,
                        size_usdt=attempt.intended_amount,
                        occurred_at_ms=attempt.started_at_ms,
                        last_updated_ms=updated_ms,
                    )
                )

        for attempt_id, mirror in owned.items():
            attempt = attempts[attempt_id]
            updated = mirror.mts_updated
            if updated is None:
                updated = attempt.outcome_completed_at_ms
            if updated is None:
                updated = attempt.started_at_ms
            add(attempt, "claimed", mirror.venue_offer_id, updated)
        for row in candidates.attempts:
            if row.attempt_id in owned:
                continue
            attempt = attempts[row.attempt_id]
            kind = attempt.outcome_kind
            # The outcome's own stamp; 0 is a real time, so test for None.
            completed = (
                attempt.outcome_completed_at_ms
                if attempt.outcome_completed_at_ms is not None
                else attempt.started_at_ms
            )
            if kind is None:
                add(attempt, "pending", None, attempt.started_at_ms)
            elif kind == "unknown":
                if row.attempt_id not in candidates.resolutions:
                    add(attempt, "unknown", None, completed)
            elif kind == "ack" and attempt.outcome_venue_offer_id not in live_ids:
                # Acknowledged but not yet in an accepted snapshot. A live row that is
                # foreign or conflicted is excluded above, not shown as unreflected.
                add(attempt, "claimed", attempt.outcome_venue_offer_id, completed)
        views.sort(key=lambda view: (view.last_updated_ms, view.offer_key), reverse=True)
        return tuple(views)
