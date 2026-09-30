"""Dormant ledger reads: open uncertainties, managed live offers, fingerprints.

Every read runs on the caller's session and is bounded by keys, the live
mirror, or the latest accepted basis -- never by history:

1. latest accepted basis: ``previous_basis`` (query revision DESC via
   ``ix_ledger_observation_query_scope_revision`` -> unique
   ``ledger_observation.query_id`` -> unique ``accepted_capital_basis.observation_id``,
   LIMIT 1).
2. its ``unresolved`` attempts (``accepted_capital_basis_attempt`` PK prefix
   ``basis_id``), loaded by attempt PK.
3. the tail ``attempt_seq > attempt_seq_high_water``
   (``uq_submission_attempt_scope_seq``), at most ``MAX_TAIL_ATTEMPTS`` rows;
   one more raises ``LedgerReadUnbounded`` (fail closed).
4. outcomes (PK) and resolutions (``uq_execution_resolution_attempt``) of 2 ∪ 3.
5. unresolved quarantines: ``quarantine.unresolved_quarantines`` (the basis's
   listed ones + ``ix_quarantine_opening_scope_revision``).
6. live mirror rows: ``ix_venue_offer_mirror_live`` (partial on
   ``present_in_latest_accepted_snapshot``), or the mirror PK for one offer.
7. provenance of those venue ids (``_internal.provenance``: outcome and
   resolution ``venue_offer_id`` indexes), their attempts (PK) and decisions
   (``execution_decisions`` PK).

Why 2 ∪ 3 covers every attempt that can still be open: each basis considers
the attempts after the previous high water plus the previous basis's
unresolved ones, so an attempt at or below the latest high water that is not
listed unresolved was settled or reflected by then.
"""

from __future__ import annotations

from collections.abc import Collection, Iterable
from dataclasses import dataclass
from decimal import Decimal
from uuid import UUID

from sqlalchemy import Select, Text, column, select, table
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import load_only

from bfx_funding_bot.modules.ledger import (
    CancelProvenance,
    LedgerReadUnbounded,
    ManagedOffer,
    ManagedOffers,
    OpenUncertainty,
    ProvenanceConflict,
    Scope,
)
from bfx_funding_bot.modules.ledger._internal.attempts import (
    MAX_TAIL_ATTEMPTS,
    attempt_evidence,
    attempts_by_id,
    fresh_all,
    open_unknowns,
    payload_amount,
    tail_attempts,
)
from bfx_funding_bot.modules.ledger._internal.basis import previous_basis
from bfx_funding_bot.modules.ledger._internal.provenance import offer_provenance, sole_owner
from bfx_funding_bot.modules.ledger._internal.quarantine import unresolved_quarantines
from bfx_funding_bot.modules.ledger.tables import (
    AcceptedCapitalBasisAttemptRow,
    AcceptedCapitalBasisRow,
    SubmissionAttemptJournalRow,
    VenueOfferMirrorRow,
)

# The decision row is execution's audit table; ledger reads only its key and
# signal correlation (the attempt's FK names it) and never imports execution.
_DECISIONS = table(
    "execution_decisions",
    column("decision_id", Text),
    column("signal_correlation_id", Text),
)
# Settled attempts free their fingerprint; everything else still holds it.
_SETTLED_OUTCOMES = frozenset(("rejected", "not_sent"))


@dataclass(frozen=True, slots=True)
class _Candidates:
    """Attempts that may still be open (basis-unresolved ∪ tail) and their evidence."""

    basis: AcceptedCapitalBasisRow | None
    attempts: tuple[SubmissionAttemptJournalRow, ...]
    outcomes: dict[UUID, str]
    resolutions: dict[UUID, str]


@dataclass(frozen=True, slots=True)
class _Live:
    managed: tuple[tuple[VenueOfferMirrorRow, SubmissionAttemptJournalRow, str], ...]
    conflicts: tuple[VenueOfferMirrorRow, ...]


async def _candidates(session: AsyncSession, scope: Scope) -> _Candidates:
    basis = await previous_basis(session, scope)
    if basis is None:
        # Every attempt names a basis (FK), so a scope without one has none.
        return _Candidates(None, (), {}, {})
    listed = await fresh_all(
        session,
        select(AcceptedCapitalBasisAttemptRow).where(
            AcceptedCapitalBasisAttemptRow.basis_id == basis.id,
            AcceptedCapitalBasisAttemptRow.classification == "unresolved",
        ),
    )
    tail = await tail_attempts(
        session,
        scope.exchange_account_id,
        scope.deployment_environment,
        basis.attempt_seq_high_water,
        limit=MAX_TAIL_ATTEMPTS + 1,
    )
    if len(tail) > MAX_TAIL_ATTEMPTS:
        raise LedgerReadUnbounded(f"more than {MAX_TAIL_ATTEMPTS} attempts after basis {basis.id}")
    unresolved = await attempts_by_id(session, [row.attempt_id for row in listed])
    attempts = tuple(sorted((*unresolved, *tail), key=lambda row: row.attempt_seq))
    outcomes, resolutions = await attempt_evidence(session, [row.attempt_id for row in attempts])
    return _Candidates(basis, attempts, outcomes, resolutions)


async def open_uncertainties(
    session: AsyncSession, scope: Scope, symbol: str | None = None
) -> tuple[OpenUncertainty, ...]:
    """UNKNOWN attempts without resolution (by attempt order), then unresolved quarantines."""
    candidates = await _candidates(session, scope)
    by_id = {row.attempt_id: row for row in candidates.attempts}
    found = [
        OpenUncertainty(
            "attempt",
            attempt_id,
            attempt_symbol,
            payload_amount(by_id[attempt_id].normalized_payload),
        )
        for attempt_id, attempt_symbol in open_unknowns(
            ((row.attempt_id, row.symbol) for row in candidates.attempts),
            candidates.outcomes,
            candidates.resolutions,
        )
        if symbol is None or attempt_symbol == symbol
    ]
    found.extend(
        OpenUncertainty("quarantine", row.quarantine_id, row.symbol, row.intended_amount)
        for row in await unresolved_quarantines(session, scope, candidates.basis, symbol=symbol)
    )
    return tuple(found)


def _live_mirror(scope: Scope) -> Select[tuple[VenueOfferMirrorRow]]:
    # A terminal row is never present (ck_venue_offer_mirror_terminal), so the
    # present flag alone selects the live, non-terminal offers. Written as the
    # bare column so it matches the partial index predicate of
    # ``ix_venue_offer_mirror_live`` (``IS TRUE`` does not, and scans the scope).
    return (
        select(VenueOfferMirrorRow)
        .options(
            load_only(
                VenueOfferMirrorRow.symbol,
                VenueOfferMirrorRow.amount_original,
                VenueOfferMirrorRow.amount_remaining,
            )
        )
        .where(
            VenueOfferMirrorRow.exchange_account_id == scope.exchange_account_id,
            VenueOfferMirrorRow.deployment_environment == scope.deployment_environment,
            VenueOfferMirrorRow.present_in_latest_accepted_snapshot,
        )
    )


async def _live(
    session: AsyncSession, scope: Scope, statement: Select[tuple[VenueOfferMirrorRow]]
) -> _Live:
    """Live mirror rows joined to their one attempt and its decision's correlation."""
    # Ordered here, not in SQL: an ORDER BY on the key steers the planner to the PK.
    mirrors = sorted(await fresh_all(session, statement), key=lambda row: row.venue_offer_id)
    provenance = await offer_provenance(session, scope, [row.venue_offer_id for row in mirrors])
    attempts = {
        row.attempt_id: row
        for row in await attempts_by_id(
            session, [attempt_id for ids in provenance.values() for attempt_id in ids]
        )
    }
    owned: list[tuple[VenueOfferMirrorRow, SubmissionAttemptJournalRow]] = []
    conflicts: list[VenueOfferMirrorRow] = []
    for mirror in mirrors:
        try:
            owner = sole_owner(provenance[mirror.venue_offer_id], attempts, scope, mirror.symbol)
        except LookupError:
            conflicts.append(mirror)
            continue
        if owner is not None:
            owned.append((mirror, owner))
    correlations = await _correlations(session, (owner.execution_decision_id for _, owner in owned))
    managed: list[tuple[VenueOfferMirrorRow, SubmissionAttemptJournalRow, str]] = []
    for mirror, owner in owned:
        correlation = correlations.get(owner.execution_decision_id)
        if correlation is None:  # the attempt FK makes this unreachable; never guess
            conflicts.append(mirror)
        else:
            managed.append((mirror, owner, correlation))
    return _Live(tuple(managed), tuple(sorted(conflicts, key=lambda row: row.venue_offer_id)))


async def _correlations(session: AsyncSession, decision_ids: Iterable[str]) -> dict[str, str]:
    ids = sorted(set(decision_ids))
    if not ids:
        return {}
    rows = await session.execute(
        select(_DECISIONS.c.decision_id, _DECISIONS.c.signal_correlation_id).where(
            _DECISIONS.c.decision_id.in_(ids)
        )
    )
    return dict(rows.tuples().all())


def _managed_offer(
    mirror: VenueOfferMirrorRow, attempt: SubmissionAttemptJournalRow, correlation: str
) -> ManagedOffer:
    return ManagedOffer(
        mirror.venue_offer_id,
        mirror.symbol,
        mirror.amount_remaining,
        attempt.attempt_id,
        attempt.execution_decision_id,
        attempt.cell_id,
        correlation,
    )


async def managed_live_offers(
    session: AsyncSession, scope: Scope, symbols: Collection[str] | None = None
) -> ManagedOffers:
    statement = _live_mirror(scope)
    if symbols is not None:
        if not symbols:
            return ManagedOffers((), ())
        statement = statement.where(VenueOfferMirrorRow.symbol.in_(sorted(set(symbols))))
    live = await _live(session, scope, statement)
    return ManagedOffers(
        tuple(_managed_offer(*item) for item in live.managed),
        tuple(row.venue_offer_id for row in live.conflicts),
    )


async def cancel_provenance(
    session: AsyncSession, scope: Scope, venue_offer_id: str
) -> CancelProvenance | None:
    live = await _live(
        session,
        scope,
        _live_mirror(scope).where(VenueOfferMirrorRow.venue_offer_id == venue_offer_id),
    )
    if live.conflicts:
        raise ProvenanceConflict(venue_offer_id)
    if not live.managed:
        return None
    ((mirror, attempt, correlation),) = live.managed
    return CancelProvenance(
        mirror.venue_offer_id,
        mirror.symbol,
        attempt.attempt_id,
        attempt.execution_decision_id,
        attempt.cell_id,
        correlation,
    )


def _settled(outcome: str | None, resolution: str | None) -> bool:
    return outcome in _SETTLED_OUTCOMES or (outcome == "unknown" and resolution == "not_accepted")


async def fingerprints_in_use(
    session: AsyncSession, scope: Scope, symbol: str
) -> frozenset[Decimal]:
    """Submitted amounts of ``symbol`` that still hold their fingerprint.

    - live managed offers: the attempt's submitted amount and the offer's
      original amount (a partial fill does not free it);
    - live offers with contradictory provenance: their original amount;
    - every attempt that may still be open (basis-unresolved ∪ tail) unless
      settled: rejected, not_sent, or UNKNOWN resolved ``not_accepted``. So no
      outcome, open UNKNOWN, and ack/bound attempts no accepted basis has
      reflected yet all hold.
    """
    live = await _live(
        session, scope, _live_mirror(scope).where(VenueOfferMirrorRow.symbol == symbol)
    )
    held: list[Decimal | None] = []
    for mirror, attempt, _ in live.managed:
        held += [payload_amount(attempt.normalized_payload), mirror.amount_original]
    held += [mirror.amount_original for mirror in live.conflicts]
    candidates = await _candidates(session, scope)
    held += [
        payload_amount(row.normalized_payload)
        for row in candidates.attempts
        if row.symbol == symbol
        and not _settled(
            candidates.outcomes.get(row.attempt_id), candidates.resolutions.get(row.attempt_id)
        )
    ]
    return frozenset(amount for amount in held if amount is not None)


__all__ = [
    "cancel_provenance",
    "fingerprints_in_use",
    "managed_live_offers",
    "open_uncertainties",
]
