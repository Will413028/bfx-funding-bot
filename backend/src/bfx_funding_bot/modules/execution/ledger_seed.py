"""Read the legacy closure of one scope into the ledger's neutral ``SeedClosure`` (S1-4d).

Reads, in the caller's snapshot, the final legacy accepted ``capital_snapshots`` row and its
classification exactly as stored (F7: nothing is re-derived or upgraded), the evidence event
it was classified from, every ``submission_attempts`` row, the claim of each live offer that
has no attempt, the uncertainties, the pending operator requests, the policy heads (evidence)
and the ``trading_state`` watermark (verification only). Refuses (``SeedRefused``) whenever
the legacy state cannot be represented truthfully:

* ``resolved_after_snapshot`` (F5): an uncertainty resolved after the final snapshot's fence;
  the snapshot still holds it unresolved and the ledger cannot take a legacy resolution;
* ``attempt_outcome_missing`` (F6): an attempt without an outcome (boot recovery first);
* ``credit_opening_unkeyable``: a live credit, or its legacy group, without a venue opening or
  period (R3 removed the by-id carry; the ledger keys a lending by (symbol, period, opening));
* ``legacy_snapshot_missing`` / ``legacy_query_pending`` / ``legacy_snapshot_blocked``: no
  clean final head to seed from.

Legacy credit attribution -> ledger ``attribution_basis`` (``LEGACY_ATTRIBUTION``, tested):

==========================  =================  ==============================================
legacy ``basis``            ledger             why
==========================  =================  ==============================================
``funding_trade``           ``trade``          synced trades cover the group
``funding_trade_partial``   ``trade``          more live than traded: ledger also says trade
                                               with the union of candidate cells
``carried``                 ``carry``          a group carried with mixed earlier bases
``recent_fill``             ``recent_fill``    first seen with fills of ours (stays multi-cell)
``none``                    ``unattributed``   no cell (in T only)
==========================  =================  ==============================================

Any other value refuses (``attribution_basis_unknown``); nothing is defaulted. Deleted with
the legacy authority in S1-8.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from decimal import Decimal, InvalidOperation
from hashlib import sha256
from typing import Any, cast
from uuid import UUID, uuid5

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from bfx_funding_bot.modules.execution.audit.tables import ExecutionDecisionRow
from bfx_funding_bot.modules.execution.capital_tables import (
    CapitalPolicyRequestRow,
    CapitalQueryRow,
    CapitalSnapshotRow,
)
from bfx_funding_bot.modules.execution.event_store.entities import (
    VenueCreditObservation,
    VenueOfferObservation,
)
from bfx_funding_bot.modules.execution.event_store.serialization import (
    deserialize_event,
    deserialize_stored_event,
)
from bfx_funding_bot.modules.execution.event_store.tables import EventLogRow, OfferClaimRow
from bfx_funding_bot.modules.execution.events import ReservationIntent, VenueSnapshotObserved
from bfx_funding_bot.modules.execution.safety.tables import (
    TradingControlRequestRow,
    TradingStateRow,
)
from bfx_funding_bot.modules.execution.uncertainty_tables import (
    ExecutionUncertaintyRow,
    SubmissionAttemptRow,
    UncertaintyResolutionRequestRow,
)
from bfx_funding_bot.modules.ledger import (
    AttributionBasis,
    JsonObject,
    OutcomeKind,
    Scope,
    SeedAttempt,
    SeedClosure,
    SeedCredit,
    SeedCreditGroup,
    SeedOffer,
    SeedOutcome,
    SeedQuarantine,
    SeedQuarantineMember,
    SeedRefused,
    SeedSymbol,
    SeedWatermarks,
)
from bfx_funding_bot.modules.ledger.tables import CapitalPolicyHeadRow

SNAPSHOT_SCHEMA_VERSION = 1
LOAN_PREFIX = "loan:"  # legacy credit ids of loans (``auth_rest.LOAN_ID_PREFIX``)
LEGACY_ATTRIBUTION: Mapping[str, AttributionBasis] = {
    "funding_trade": "trade",
    "funding_trade_partial": "trade",
    "carried": "carry",
    "recent_fill": "recent_fill",
    "none": "unattributed",
}
LEGACY_OUTCOMES: Mapping[str, OutcomeKind] = {
    "acknowledged": "ack",
    "rejected": "rejected",
    "not_sent": "not_sent",
    "unknown": "unknown",
}
QUARANTINE_KINDS = frozenset(("unattributed_venue_offer", "unsupported_venue_exposure"))
# A claim-only live offer's synthesized attempt id: uuid5 of the legacy claim identity.
CLAIM_ATTEMPT_NAMESPACE = UUID("3b0c8e52-6a57-5c43-8f8e-2d6e3f9a1c74")


def classification_digest(value: object) -> str:
    """Legacy ``CapitalRepository._digest`` (the snapshot event binds it)."""
    return sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _amount(value: object) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise SeedRefused("legacy_amount_invalid", repr(value)) from None
    if not result.is_finite() or result < 0:
        raise SeedRefused("legacy_amount_invalid", repr(value))
    return result


def _thaw(value: object) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _thaw(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_thaw(item) for item in value]
    if isinstance(value, Decimal):
        return format(value, "f")
    return value


def _flags(flags: Mapping[str, Any]) -> JsonObject | None:
    thawed = _thaw(flags)
    return cast(JsonObject, thawed) if thawed else None


def map_attribution(basis: object) -> AttributionBasis:
    """The ledger attribution of one legacy group basis (``LEGACY_ATTRIBUTION``)."""
    if not isinstance(basis, str) or basis not in LEGACY_ATTRIBUTION:
        raise SeedRefused("attribution_basis_unknown", repr(basis))
    return LEGACY_ATTRIBUTION[basis]


def seed_offer(offer: VenueOfferObservation) -> SeedOffer:
    if offer.status not in ("active", "partially_filled"):
        raise SeedRefused("legacy_offer_status", offer.venue_offer_id)
    return SeedOffer(
        offer.venue_offer_id, offer.symbol, offer.amount_original, offer.amount_remaining,
        offer.rate, offer.period_days, offer.offer_type, _flags(offer.flags),
        cast(Any, offer.status), offer.mts_created, offer.mts_updated,
    )


def credit_identity(credit_id: str) -> tuple[str, str]:
    """(source kind, venue id): a legacy ``loan:`` id is the loan of that venue id."""
    if credit_id.startswith(LOAN_PREFIX):
        venue_id = credit_id.removeprefix(LOAN_PREFIX)
        if not venue_id:
            raise SeedRefused("legacy_credit_id_invalid", credit_id)
        return "loan", venue_id
    return "credit", credit_id


def seed_credit(credit: VenueCreditObservation) -> SeedCredit:
    kind, venue_id = credit_identity(credit.credit_id)
    if credit.status != "active":
        raise SeedRefused("legacy_credit_status", credit.credit_id)
    if credit.mts_opening is None or credit.period_days is None:
        raise SeedRefused("credit_opening_unkeyable", credit.credit_id)
    return SeedCredit(
        cast(Any, kind), venue_id, credit.symbol, credit.amount, credit.rate,
        int(credit.period_days), "active", _flags(credit.flags), credit.mts_created,
        credit.mts_updated, int(credit.mts_opening),
    )


def seed_group(credit_id: str, entry: Mapping[str, Any]) -> SeedCreditGroup:
    kind, venue_id = credit_identity(credit_id)
    if entry.get("opening") is None or entry.get("period") is None:
        raise SeedRefused("credit_opening_unkeyable", credit_id)
    cells = entry.get("cells")
    if not isinstance(cells, list) or not all(isinstance(cell, str) and cell for cell in cells):
        raise SeedRefused("legacy_credit_cells_invalid", credit_id)
    return SeedCreditGroup(
        cast(Any, kind), venue_id, str(entry["symbol"]), _amount(entry["amount"]),
        int(entry["period"]), int(entry["opening"]), map_attribution(entry.get("basis")),
        frozenset(cells),
    )


def seed_symbol(symbol: str, values: Mapping[str, Any]) -> SeedSymbol:
    cells = values.get("cells")
    if not isinstance(cells, Mapping):
        raise SeedRefused("legacy_symbol_invalid", symbol)
    return SeedSymbol(
        symbol, _amount(values["available"]), _amount(values["offered"]),
        _amount(values["credits"]), _amount(values["unattributed_credits"]),
        _amount(values["foreign"]),
        {str(cell): _amount(amount) for cell, amount in cells.items()},
    )


async def _final_snapshot(
    session: AsyncSession, scope: Scope
) -> tuple[CapitalSnapshotRow, VenueSnapshotObserved, VenueSnapshotObserved, CapitalQueryRow]:
    row = await session.scalar(
        select(CapitalSnapshotRow)
        .where(
            CapitalSnapshotRow.exchange_account_id == scope.exchange_account_id,
            CapitalSnapshotRow.deployment_environment == scope.deployment_environment,
        )
        .order_by(CapitalSnapshotRow.event_seq.desc())
        .limit(1)
    )
    if row is None or row.schema_version != SNAPSHOT_SCHEMA_VERSION:
        raise SeedRefused("legacy_snapshot_missing")
    if row.authorization_blocked_reason is not None:
        raise SeedRefused("legacy_snapshot_blocked", row.authorization_blocked_reason)
    latest_query = await session.scalar(
        select(CapitalQueryRow.id)
        .where(
            CapitalQueryRow.exchange_account_id == scope.exchange_account_id,
            CapitalQueryRow.deployment_environment == scope.deployment_environment,
        )
        .order_by(CapitalQueryRow.query_revision.desc())
        .limit(1)
    )
    if latest_query != row.query_id:
        raise SeedRefused("legacy_query_pending")
    query = await session.get(CapitalQueryRow, row.query_id)
    logged = await session.get(EventLogRow, row.event_seq)
    if query is None or logged is None:
        raise SeedRefused("legacy_snapshot_evidence_missing")
    event = deserialize_stored_event(logged)
    if not isinstance(event, VenueSnapshotObserved) or event.capital_confirmation is None:
        raise SeedRefused("legacy_snapshot_evidence_invalid")
    if (event.account_id, event.environment, event.capital_query_id,
            event.capital_classification_digest, event.query_started_at_ms) != (
            str(scope.exchange_account_id), scope.deployment_environment, str(row.query_id),
            classification_digest(row.classification), query.started_at_ms):
        raise SeedRefused("legacy_snapshot_evidence_conflict")
    confirmation = deserialize_event("VENUE_SNAPSHOT_OBSERVED", dict(event.capital_confirmation))
    if not isinstance(confirmation, VenueSnapshotObserved):
        raise SeedRefused("legacy_snapshot_evidence_invalid")
    return row, event, confirmation, query


async def _intent_seqs(session: AsyncSession, scope: Scope) -> dict[str, tuple[int, int]]:
    """decision id -> (intent event_seq, intent occurred_at_ms), every intent of the scope."""
    found: dict[str, tuple[int, int]] = {}
    rows = await session.scalars(
        select(EventLogRow).where(
            EventLogRow.exchange_account_id == scope.exchange_account_id,
            EventLogRow.deployment_environment == scope.deployment_environment,
            EventLogRow.event_type == "RESERVATION_INTENT",
        )
    )
    for row in rows:
        intent = deserialize_stored_event(row)
        if not isinstance(intent, ReservationIntent):
            raise SeedRefused("legacy_intent_conflict", str(row.event_seq))
        decision_id = intent.execution_decision_id
        if decision_id is None:
            continue  # a pre-decision historical intent: no attempt or live claim names it
        if decision_id in found:
            raise SeedRefused("legacy_intent_conflict", str(row.event_seq))
        found[decision_id] = (row.event_seq, row.occurred_at_ms)
    return found


async def _event_time(session: AsyncSession, event_seq: int | None) -> int:
    row = None if event_seq is None else await session.get(EventLogRow, event_seq)
    if row is None:
        raise SeedRefused("legacy_event_missing", str(event_seq))
    return row.occurred_at_ms


def _scoped(row: Any, scope: Scope) -> tuple[Any, Any]:
    return (row.exchange_account_id == scope.exchange_account_id,
            row.deployment_environment == scope.deployment_environment)


async def read_seed_closure(session: AsyncSession, scope: Scope) -> SeedClosure:
    """The closure of ``scope`` in the caller's snapshot, or ``SeedRefused``."""
    snapshot, event, confirmation, query = await _final_snapshot(session, scope)
    classification = snapshot.classification
    fence = snapshot.command_fence
    reflected: Mapping[str, str] = classification.get("reflected") or {}
    settled = set(classification.get("settled") or ())
    unresolved: Mapping[str, str] = classification.get("unresolved") or {}
    ours: Mapping[str, Mapping[str, Any]] = classification.get("offers") or {}

    offers = tuple(seed_offer(offer) for offer in {o.venue_offer_id: o for o in event.offers}.values())
    live = {offer.venue_offer_id: offer for offer in offers}
    credits = tuple(seed_credit(c) for c in {c.credit_id: c for c in event.credits}.values())
    symbols = tuple(
        seed_symbol(name, values) for name, values in sorted(classification["symbols"].items())
    )
    groups = tuple(
        seed_group(credit_id, entry)
        for credit_id, entry in sorted((classification.get("credit_cells") or {}).items())
    )

    intents = await _intent_seqs(session, scope)
    uncertainties = list(await session.scalars(
        select(ExecutionUncertaintyRow).where(*_scoped(ExecutionUncertaintyRow, scope))
    ))
    for item in uncertainties:
        if (item.state == "resolved" and item.resolved_event_seq is not None
                and item.resolved_event_seq > fence):
            raise SeedRefused("resolved_after_snapshot", str(item.uncertainty_id))
    open_unknown = {
        item.attempt_id: item.uncertainty_id for item in uncertainties
        if item.state == "open" and item.kind == "submit_outcome_unknown"
    }

    attempts: list[SeedAttempt] = []
    acked: set[str] = set()
    for row in await session.scalars(
        select(SubmissionAttemptRow).where(*_scoped(SubmissionAttemptRow, scope))
    ):
        attempts.append(await _seed_attempt(
            session, row, intents=intents, fence=fence, reflected=reflected, settled=settled,
            unresolved=unresolved, open_unknown=open_unknown,
        ))
        if row.venue_offer_id is not None and row.outcome_kind == "acknowledged":
            acked.add(row.venue_offer_id)
    if set(open_unknown) - {a.attempt_id for a in attempts if a.outcome.kind == "unknown"}:
        raise SeedRefused("uncertainty_attempt_mismatch")
    for venue_offer_id, entry in sorted(ours.items()):
        if venue_offer_id not in acked:
            attempts.append(await _claim_only_attempt(
                session, scope, venue_offer_id, entry, live, intents,
            ))

    quarantines: list[SeedQuarantine] = []
    for item in sorted(uncertainties, key=lambda u: str(u.uncertainty_id)):
        if item.state == "open" and item.kind in QUARANTINE_KINDS:
            quarantines.append(await _seed_quarantine(session, item, live))
    pending = tuple(sorted(await session.scalars(
        select(UncertaintyResolutionRequestRow.request_id).where(
            *_scoped(UncertaintyResolutionRequestRow, scope),
            UncertaintyResolutionRequestRow.state == "requested",
        )
    ), key=str))
    final_event_seq = int(await session.scalar(
        select(func.max(EventLogRow.event_seq)).where(*_scoped(EventLogRow, scope))
    ) or 0)
    trading_state = await session.scalar(
        select(func.max(TradingStateRow.id)).where(*_scoped(TradingStateRow, scope))
    )
    heads = {
        head.symbol: head.revision
        for head in await session.scalars(
            select(CapitalPolicyHeadRow).where(*_scoped(CapitalPolicyHeadRow, scope))
        )
    }
    carried: dict[str, int] = {}
    for name, row_type in (("capital_policy_requests", CapitalPolicyRequestRow),
                           ("trading_control_requests", TradingControlRequestRow)):
        carried[name] = int(await session.scalar(
            select(func.count()).select_from(row_type).where(
                *_scoped(row_type, scope), row_type.state == "requested")
        ) or 0)
    return SeedClosure(
        scope=scope,
        watermarks=SeedWatermarks(
            final_event_seq, snapshot.event_seq, snapshot.query_id, fence,
            None if trading_state is None else int(trading_state),
        ),
        query_started_at_ms=query.started_at_ms,
        query_finished_at_ms=event.query_finished_at_ms,
        confirmation_finished_at_ms=confirmation.query_finished_at_ms,
        offers=offers,
        credits=credits,
        symbols=symbols,
        credit_groups=groups,
        attempts=tuple(sorted(attempts, key=lambda a: a.attempt_seq)),
        quarantines=tuple(quarantines),
        pending_uncertainty_requests=pending,
        evidence={
            "classification_digest": classification_digest(classification),
            "covered_prefix_hash": snapshot.covered_prefix_hash,
            "policy_head_revisions": dict(sorted(heads.items())),
            "carried_pending_requests": carried,
        },
    )


async def _seed_attempt(
    session: AsyncSession, row: SubmissionAttemptRow, *, intents: Mapping[str, tuple[int, int]],
    fence: int, reflected: Mapping[str, str], settled: set[str], unresolved: Mapping[str, str],
    open_unknown: Mapping[UUID | None, UUID],
) -> SeedAttempt:
    key = str(row.attempt_id)
    if row.outcome_kind is None:
        raise SeedRefused("attempt_outcome_missing", key)
    kind = LEGACY_OUTCOMES.get(row.outcome_kind)
    if kind is None or row.completed_at_ms is None:
        raise SeedRefused("attempt_outcome_invalid", key)
    if (kind == "ack") != (row.venue_offer_id is not None and row.outcome_kind == "acknowledged"):
        raise SeedRefused("attempt_outcome_invalid", key)
    intent = intents.get(row.execution_decision_id)
    if intent is None:
        raise SeedRefused("attempt_intent_missing", key)
    decision = await session.get(ExecutionDecisionRow, row.execution_decision_id)
    if decision is None or not decision.cell_id:
        raise SeedRefused("attempt_decision_missing", key)
    classification: Any
    if key in reflected:
        if reflected[key] != row.venue_offer_id:
            raise SeedRefused("attempt_reflection_conflict", key)
        classification = "reflected"
    elif key in settled:
        classification = "settled"
    elif key in unresolved:
        classification = "unresolved"
    elif intent[0] > fence:
        # The legacy tail after the final fence: the first real basis decides it.
        classification = "unresolved"
    else:
        raise SeedRefused("attempt_unclassified", key)
    provenance: JsonObject = {
        "legacy": "submission_attempt",
        "attempt_id": key,
        "execution_decision_id": row.execution_decision_id,
        "cid": row.cid,
        "intent_event_seq": intent[0],
        "last_event_seq": row.last_event_seq,
        "legacy_payload_sha256": row.payload_sha256,
        "tail": intent[0] > fence,
    }
    if row.attempt_id in open_unknown:
        if kind != "unknown" or classification != "unresolved":
            raise SeedRefused("uncertainty_attempt_mismatch", key)
        provenance["uncertainty_id"] = str(open_unknown[row.attempt_id])
    return SeedAttempt(
        attempt_id=row.attempt_id,
        execution_decision_id=row.execution_decision_id,
        symbol=row.symbol,
        cell_id=decision.cell_id,
        attempt_seq=intent[0],
        normalized_payload=cast(JsonObject, _thaw(row.normalized_payload)),
        started_at_ms=row.started_at_ms,
        outcome=SeedOutcome(
            kind, row.venue_offer_id if kind == "ack" else None, row.outcome_reason,
            row.completed_at_ms,
            {"legacy_outcome_kind": row.outcome_kind, "legacy_last_event_seq": row.last_event_seq},
        ),
        classification=classification,
        provenance=provenance,
    )


async def _claim_only_attempt(
    session: AsyncSession, scope: Scope, venue_offer_id: str, entry: Mapping[str, Any],
    live: Mapping[str, SeedOffer], intents: Mapping[str, tuple[int, int]],
) -> SeedAttempt:
    """The seeded partial of a live offer legacy owns through its claim alone (no attempt)."""
    offer = live.get(venue_offer_id)
    claims = list(await session.scalars(
        select(OfferClaimRow).where(*_scoped(OfferClaimRow, scope),
                                    OfferClaimRow.venue_offer_id == venue_offer_id)
    ))
    if offer is None or len(claims) != 1 or claims[0].execution_decision_id is None:
        raise SeedRefused("claim_provenance_conflict", venue_offer_id)
    claim = claims[0]
    decision_id = claim.execution_decision_id
    assert decision_id is not None
    decision = await session.get(ExecutionDecisionRow, decision_id)
    intent = intents.get(decision_id)
    if (decision is None or intent is None or decision.cell_id != entry.get("cell")
            or claim.symbol != offer.symbol or _amount(claim.size_usdt) != offer.amount_original):
        raise SeedRefused("claim_provenance_conflict", venue_offer_id)
    attempt_id = uuid5(
        CLAIM_ATTEMPT_NAMESPACE,
        f"{scope.exchange_account_id}|{scope.deployment_environment}|{claim.cid}|{decision_id}",
    )
    return SeedAttempt(
        attempt_id=attempt_id,
        execution_decision_id=decision_id,
        symbol=offer.symbol,
        cell_id=decision.cell_id,
        attempt_seq=intent[0],
        # A seeded partial: what legacy recorded of the submit is its amount (the claim).
        normalized_payload={"symbol": offer.symbol, "amount": format(claim.size_usdt, "f")},
        started_at_ms=intent[1],
        outcome=SeedOutcome(
            "ack", venue_offer_id, None, await _event_time(session, claim.last_event_seq),
            {"legacy_claim_state": claim.state, "legacy_claim_event_seq": claim.last_event_seq},
        ),
        classification="reflected",
        provenance={
            "legacy": "offer_claim",
            "cid": claim.cid,
            "execution_decision_id": decision_id,
            "intent_event_seq": intent[0],
            "claim_event_seq": claim.last_event_seq,
            "venue_offer_id": venue_offer_id,
            "signal_correlation_id": claim.signal_correlation_id,
        },
    )


async def _seed_quarantine(
    session: AsyncSession, item: ExecutionUncertaintyRow, live: Mapping[str, SeedOffer],
) -> SeedQuarantine:
    members: tuple[SeedQuarantineMember, ...] = ()
    if item.venue_offer_id is not None:
        offer = live.get(item.venue_offer_id)
        if offer is None:
            raise SeedRefused("quarantine_member_unobserved", item.venue_offer_id)
        members = (SeedQuarantineMember("offer", item.venue_offer_id, offer.amount_remaining),)
    return SeedQuarantine(
        quarantine_id=item.uncertainty_id,
        symbol=item.symbol,
        intended_amount=item.intended_amount,
        opened_at_ms=await _event_time(session, item.opened_event_seq),
        evidence={"legacy_seed": {
            "uncertainty_id": str(item.uncertainty_id),
            "kind": item.kind,
            "correlation_key": item.correlation_key,
            "opened_event_seq": item.opened_event_seq,
            "evidence": _thaw(item.evidence),
        }},
        members=members,
    )


__all__ = [
    "LEGACY_ATTRIBUTION",
    "LEGACY_OUTCOMES",
    "classification_digest",
    "credit_identity",
    "map_attribution",
    "read_seed_closure",
    "seed_credit",
    "seed_group",
    "seed_offer",
]
