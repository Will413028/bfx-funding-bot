"""Automatic UNKNOWN resolution inside the observation cycle's locked acceptance txn.

Subjects are open UNKNOWN attempts; the evidence is the stored rows of the
observation just accepted (the same bytes an operator is judged against), read
back through ``load_match_evidence``. Only a provable exact match or a provable
absence resolves; everything else stays open for the uncertainty gate and an
operator. Quarantines are never touched here (R6 is the basis classifier's).
"""

from __future__ import annotations

import logging
from collections import Counter
from decimal import Decimal, InvalidOperation
from hashlib import sha256
from typing import cast
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bfx_funding_bot.modules.ledger import (
    UNKNOWN_SETTLE_MS,
    AutoDecision,
    Coverage,
    JsonObject,
    MatchEvidence,
    Offer,
    OfferHistory,
    OfferStatus,
    OfferTerminalKind,
    Resolution,
    ResolutionAlreadyRecorded,
    ResolutionRejected,
    Scope,
    UnknownMatch,
    UnknownTerms,
    decide_unknown,
    match_unknown,
    observation_evidence_ref,
)
from bfx_funding_bot.modules.ledger._internal import history_symbols
from bfx_funding_bot.modules.ledger._internal.basis import previous_basis
from bfx_funding_bot.modules.ledger._internal.clock import lock_scope
from bfx_funding_bot.modules.ledger._internal.journal import canonical_payload, record_resolution
from bfx_funding_bot.modules.ledger._internal.provenance import offer_provenance
from bfx_funding_bot.modules.ledger._internal.quarantine import unresolved_quarantines
from bfx_funding_bot.modules.ledger._internal.reads import open_unknown_attempt_ids
from bfx_funding_bot.modules.ledger.tables import (
    LedgerObservationOfferHistoryRow,
    LedgerObservationOfferRow,
    LedgerObservationQueryRow,
    LedgerObservationRow,
    QuarantineMemberRow,
    SubmissionAttemptJournalRow,
    TransportOutcomeJournalRow,
)
from bfx_funding_bot.modules.observability import alerts

log = logging.getLogger(__name__)

# Legacy ``execution.boot_recovery.SYSTEM_RESOLVER``: the operator-visible actor of a system resolution.
SYSTEM_RESOLVER = "system:reconcile"
RESOLUTION_REJECTED_ALERT = "ledger_unknown_resolution_rejected"


def _offer(row: LedgerObservationOfferRow | LedgerObservationOfferHistoryRow) -> Offer:
    return Offer(
        row.venue_offer_id, row.symbol, row.amount_original, row.amount_remaining, row.rate,
        row.rate_observed, row.period_days, row.offer_type, row.flags,
        cast(OfferStatus, row.status), row.mts_created, row.mts_updated, row.raw,
    )


def _page_count(counts: object, key: str) -> int:
    value = counts.get(key) if isinstance(counts, dict) else None
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


async def load_match_evidence(session: AsyncSession, observation_id: UUID) -> MatchEvidence | None:
    """The stored observation as matching evidence; None when it does not exist."""
    row = await session.get(LedgerObservationRow, observation_id, populate_existing=True)
    if row is None:
        return None
    query = await session.get(LedgerObservationQueryRow, row.query_id)
    if query is None:
        return None
    first = row.evidence.get("first_page_counts")
    coverage = Coverage(
        wallets_complete=row.wallets_complete,
        offers_complete=row.offers_complete,
        credits_complete=row.credits_complete,
        loans_complete=row.loans_complete,
        offer_history_complete=row.offer_history_complete,
        credit_history_complete=row.credit_history_complete,
        wallet_pages=_page_count(first, "wallet"),
        offer_pages=_page_count(first, "offer"),
        credit_pages=_page_count(first, "credit"),
        loan_pages=_page_count(first, "loan"),
        offer_history_pages=row.offer_history_pages or 0,
        credit_history_pages=row.credit_history_pages or 0,
        history_requested_start_ms=row.history_requested_start_ms,
        history_requested_end_ms=row.history_requested_end_ms,
        history_oldest_mts_created=row.history_oldest_mts_created,
        history_newest_mts_created=row.history_newest_mts_created,
        trades_complete=row.trades_complete,
        trades_requested_start_ms=row.trades_requested_start_ms,
        trades_requested_end_ms=row.trades_requested_end_ms,
        history_symbols=history_symbols.decode(row.evidence),
    )
    offers = await session.scalars(
        select(LedgerObservationOfferRow)
        .where(LedgerObservationOfferRow.observation_id == observation_id)
        .order_by(LedgerObservationOfferRow.venue_offer_id)
    )
    history = await session.scalars(
        select(LedgerObservationOfferHistoryRow)
        .where(LedgerObservationOfferHistoryRow.observation_id == observation_id)
        .order_by(
            LedgerObservationOfferHistoryRow.venue_offer_id,
            LedgerObservationOfferHistoryRow.occurred_at_ms,
        )
    )
    return MatchEvidence(
        observation_id,
        query.started_at_ms,
        row.query_finished_at_ms,
        coverage,
        tuple(_offer(item) for item in offers),
        tuple(
            OfferHistory(
                _offer(item), cast(OfferTerminalKind, item.terminal_kind), item.occurred_at_ms
            )
            for item in history
        ),
    )


def _terms(
    attempt_id: UUID, symbol: str, payload: object, digest: str, started_at_ms: int,
    unknown_recorded_at_ms: int,
) -> UnknownTerms | None:
    """The attempt's terms, or None when the journal row cannot be trusted as a subject.

    The payload digest is rechecked so a forged projection cannot change the
    identity matched; a payload without type/flags (a seeded legacy partial) is
    never matched: it stays open for an operator.
    """
    if not isinstance(payload, dict) or sha256(canonical_payload(payload)).hexdigest() != digest:
        return None
    try:
        amount = Decimal(str(payload["amount"]))
        rate = Decimal(str(payload["rate"]))
        period = payload["period"]
        offer_type = payload["type"]
        flags = payload["flags"]
    except (KeyError, InvalidOperation):
        return None
    if (
        payload.get("symbol") != symbol
        or not isinstance(period, int)
        or isinstance(period, bool)
        or period <= 0
        or not isinstance(offer_type, str)
        or not offer_type
        or not isinstance(flags, (dict, int))
        or isinstance(flags, bool)
        or not amount.is_finite()
        or amount < 0
        or not rate.is_finite()
        or started_at_ms < 0
    ):
        return None
    return UnknownTerms(
        attempt_id, symbol, amount, rate, period, offer_type, flags, started_at_ms,
        unknown_recorded_at_ms,
    )


async def _subjects(session: AsyncSession, scope: Scope) -> list[UnknownTerms]:
    ids = await open_unknown_attempt_ids(session, scope)
    if not ids:
        return []
    rows = await session.execute(
        select(
            SubmissionAttemptJournalRow.attempt_id,
            SubmissionAttemptJournalRow.symbol,
            SubmissionAttemptJournalRow.normalized_payload,
            SubmissionAttemptJournalRow.payload_sha256,
            SubmissionAttemptJournalRow.started_at_ms,
            TransportOutcomeJournalRow.completed_at_ms,
        )
        .join(
            TransportOutcomeJournalRow,
            TransportOutcomeJournalRow.attempt_id == SubmissionAttemptJournalRow.attempt_id,
        )
        .where(
            SubmissionAttemptJournalRow.attempt_id.in_(ids),
            SubmissionAttemptJournalRow.exchange_account_id == scope.exchange_account_id,
            SubmissionAttemptJournalRow.deployment_environment == scope.deployment_environment,
        )
        .order_by(SubmissionAttemptJournalRow.attempt_seq)
    )
    terms = (_terms(*row) for row in rows.all())
    return [item for item in terms if item is not None]


async def _attributed(
    session: AsyncSession, scope: Scope, venue_ids: frozenset[str]
) -> frozenset[str]:
    """Offers some attempt already names, or an unresolved quarantine already holds."""
    if not venue_ids:
        return frozenset()
    ids = sorted(venue_ids)
    named = {
        venue_id for venue_id, attempts in (await offer_provenance(session, scope, ids)).items()
        if attempts
    }
    open_quarantines = [
        row.quarantine_id
        for row in await unresolved_quarantines(session, scope, await previous_basis(session, scope))
    ]
    if open_quarantines:
        named.update(
            await session.scalars(
                select(QuarantineMemberRow.venue_object_id).where(
                    QuarantineMemberRow.quarantine_id.in_(open_quarantines),
                    QuarantineMemberRow.source_kind == "offer",
                    QuarantineMemberRow.venue_object_id.in_(ids),
                )
            )
        )
    return frozenset(named)


def _resolution(
    terms: UnknownTerms, decision: AutoDecision, match: UnknownMatch, evidence: MatchEvidence,
    *, now_ms: int,
) -> Resolution:
    detail: JsonObject = {
        "evidence_ref": observation_evidence_ref(evidence.observation_id),
        "query_started_at_ms": evidence.query_started_at_ms,
        "query_finished_at_ms": evidence.query_finished_at_ms,
        "match_kind": match.kind,
        "candidate_count": len(match.candidate_venue_offer_ids),
    }
    if decision.action == "bound_to_venue":
        detail["venue_offer_id"] = decision.venue_offer_id
        detail["venue_status"] = match.venue_status
    assert decision.action != "leave_open"
    return Resolution(
        id=uuid4(), symbol=terms.symbol, action=decision.action,
        venue_offer_id=decision.venue_offer_id, observation_id=evidence.observation_id,
        actor_kind="system", actor_id=SYSTEM_RESOLVER, resolved_at_ms=now_ms,
        reason=decision.reason, evidence=detail, attempt_id=terms.attempt_id,
        candidate_count=len(match.candidate_venue_offer_ids),
    )


async def resolve_unknowns(
    session: AsyncSession, scope: Scope, observation_id: UUID, *, now_ms: int,
    settle_ms: int = UNKNOWN_SETTLE_MS,
) -> tuple[Resolution, ...]:
    """Resolve every open UNKNOWN the just-accepted observation proves, one savepoint each.

    A journal refusal (``ResolutionRejected`` / ``ResolutionAlreadyRecorded``) rolls back
    only that resolution, leaves the UNKNOWN open and alerts; any other error is a bug
    and propagates, taking the whole acceptance with it.
    """
    await lock_scope(session, scope)
    subjects = await _subjects(session, scope)
    if not subjects:
        return ()
    evidence = await load_match_evidence(session, observation_id)
    if evidence is None:
        raise ValueError("accepted observation is missing")
    matches = [(terms, match_unknown(terms, evidence)) for terms in subjects]
    # An offer two open UNKNOWNs could both be: neither binds it, whatever their order.
    shared = frozenset(
        offer_id
        for offer_id, count in Counter(
            offer_id for _, match in matches for offer_id in match.candidate_venue_offer_ids
        ).items()
        if count > 1
    )
    attributed = await _attributed(
        session, scope,
        frozenset(offer_id for _, match in matches for offer_id in match.candidate_venue_offer_ids),
    )
    resolved: list[Resolution] = []
    for terms, match in matches:
        decision = decide_unknown(
            terms, evidence, match, settle_ms=settle_ms, attributed=attributed, shared=shared,
        )
        if decision.action == "leave_open":
            log.info(
                "unknown_stays_open attempt=%s symbol=%s evidence=%s reason=%s candidates=%d",
                terms.attempt_id, terms.symbol, match.kind, decision.reason,
                len(match.candidate_venue_offer_ids),
            )
            continue
        resolution = _resolution(terms, decision, match, evidence, now_ms=now_ms)
        try:
            async with session.begin_nested():
                await record_resolution(session, scope, resolution)
        except (ResolutionRejected, ResolutionAlreadyRecorded) as exc:
            log.warning(
                "unknown_resolution_refused attempt=%s action=%s error=%s",
                terms.attempt_id, resolution.action, exc,
            )
            alerts.emit(
                RESOLUTION_REJECTED_ALERT, level=alerts.WARNING,
                exchange_account_id=str(scope.exchange_account_id),
                deployment_environment=scope.deployment_environment,
                attempt_id=str(terms.attempt_id), symbol=terms.symbol,
                action=resolution.action, error=str(exc),
            )
            continue
        resolved.append(resolution)
    return tuple(resolved)


__all__ = ["SYSTEM_RESOLVER", "load_match_evidence", "resolve_unknowns"]
