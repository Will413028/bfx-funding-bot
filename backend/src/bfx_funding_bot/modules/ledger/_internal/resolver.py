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
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bfx_funding_bot.modules.ledger import (
    UNKNOWN_SETTLE_MS,
    AutoDecision,
    JsonObject,
    MatchEvidence,
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
from bfx_funding_bot.modules.ledger._internal.clock import lock_scope
from bfx_funding_bot.modules.ledger._internal.journal import canonical_payload, record_resolution
from bfx_funding_bot.modules.ledger.read.basis import previous_basis
from bfx_funding_bot.modules.ledger.read.match_evidence import build_terms, load_match_evidence
from bfx_funding_bot.modules.ledger.read.provenance import offer_provenance
from bfx_funding_bot.modules.ledger.read.quarantine import unresolved_quarantines
from bfx_funding_bot.modules.ledger.read.reads import open_unknown_attempt_ids
from bfx_funding_bot.modules.ledger.tables import (
    QuarantineMemberRow,
    SubmissionAttemptJournalRow,
    TransportOutcomeJournalRow,
)
from bfx_funding_bot.modules.observability import alerts

log = logging.getLogger(__name__)

# The operator-visible actor of a system resolution (the value the legacy reconcile used).
SYSTEM_RESOLVER = "system:reconcile"


def _terms(
    attempt_id: UUID, symbol: str, payload: object, digest: str, started_at_ms: int,
    unknown_recorded_at_ms: int,
) -> UnknownTerms | None:
    """The attempt's terms from its payload, or None when the row cannot be trusted as a subject.

    The payload digest is rechecked so a forged projection cannot change the
    identity matched; a payload without type/flags (a seeded legacy partial) is
    never matched: it stays open for an operator. The worker's path (the web API's
    role cannot read the payload).
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
    if payload.get("symbol") != symbol:
        return None
    return build_terms(
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
                alerts.LEDGER_UNKNOWN_RESOLUTION_REJECTED, level=alerts.WARNING,
                exchange_account_id=str(scope.exchange_account_id),
                attempt_id=str(terms.attempt_id), symbol=terms.symbol,
                action=resolution.action, error=str(exc),
            )
            continue
        resolved.append(resolution)
    return tuple(resolved)


__all__ = [
    "SYSTEM_RESOLVER",
    "resolve_unknowns",
]
