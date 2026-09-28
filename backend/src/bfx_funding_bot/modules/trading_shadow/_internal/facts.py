"""Fold a complete uncertainty set and bounded post-fence attempt tail."""

from dataclasses import dataclass, replace
from typing import Literal
from uuid import UUID, uuid5

from bfx_funding_bot.modules.execution.submit_outcomes import SubmissionAttemptPayload
from bfx_funding_bot.modules.trading import (
    AttemptFact,
    AttemptOutcome,
    CapitalScope,
    UncertaintyFact,
)
from bfx_funding_bot.modules.trading_shadow._internal.evidence import Event, Row, amount, require
from bfx_funding_bot.modules.trading_shadow._internal.resolution import prove_resolution

# event_store/store.py:68-69,1149-1154,1223-1233: IDs name the FIRST opening event.
_SUBMIT_NAMESPACE = UUID("d158ef54-c1dd-54e4-a9e9-9a670c938f73")
_ORPHAN_NAMESPACE = UUID("b1f89542-a63e-584a-b5bc-cd448ed74f3f")
_OUTCOMES = {"RESERVATION_CLAIMED", "RESERVATION_FAILED", "SUBMIT_OUTCOME_UNKNOWN"}
_RESOLUTIONS = {
    "SUBMIT_MATCHED_TO_VENUE_OFFER",
    "UNCERTAINTY_BOUND_TO_VENUE_OFFER",
    "UNCERTAINTY_MARKED_NOT_ACCEPTED",
    "UNCERTAINTY_MANUALLY_RESOLVED",
}


@dataclass(frozen=True, slots=True)
class _Attempt:
    fact: AttemptFact
    payload: SubmissionAttemptPayload
    signal: str


@dataclass(frozen=True, slots=True)
class _Uncertainty:
    fact: UncertaintyFact
    opening: Event
    decision_id: str | None
    kind: str
    first_offer: str | None = None


def _intent(scope: CapitalScope, event: Event, decisions: dict[str, Row]) -> _Attempt:
    p = event.payload
    raw = p["submission_attempt"]
    # submit_outcomes.py:238-273 validates the original normalized payload hash.
    require(
        raw.get("attempt_id") is not None and raw.get("payload_sha256") is not None,
        "attempt_intent_conflict",
        seq=event.seq,
    )
    payload = SubmissionAttemptPayload(**raw)
    decision = decisions.get(payload.execution_decision_id)
    require(decision is not None, "attempt_decision_conflict", seq=event.seq)
    assert decision is not None
    intended = amount(p.get("amount") if p.get("amount") is not None else p["size_usdt"])
    require(
        p.get("size_usdt") is None or amount(p["size_usdt"]) == intended,
        "attempt_amount_conflict",
        seq=event.seq,
    )
    require(
        (
            payload.account_id,
            payload.environment,
            payload.symbol,
            payload.cid,
            payload.execution_decision_id,
        )
        == (scope.account_id, scope.environment, p["symbol"], p["cid"], p["execution_decision_id"])
        and payload.outcome_kind is None
        and payload.completed_at_ms is None
        and payload.outcome_reason is None
        and payload.venue_offer_id is None
        and payload.last_event_seq is None,
        "attempt_intent_scope_conflict",
        seq=event.seq,
    )
    # capital_repository.py:978-997: decision scope, signal and amount all bind the intent.
    require(
        (
            decision["exchange_account_id"],
            decision["deployment_environment"],
            decision["symbol"],
            decision["signal_correlation_id"],
            amount(decision["amount_usdt"]),
        )
        == (scope.account_id, scope.environment, p["symbol"], p["signal_correlation_id"], intended)
        and intended == amount(payload.normalized_payload["amount"])
        and payload.normalized_payload["symbol"] == p["symbol"],
        "attempt_decision_conflict",
        seq=event.seq,
    )
    require(
        p["reservation_ref"]["execution_decision_id"] == payload.execution_decision_id
        and p["reservation_ref"]["cid"] == p["cid"]
        and p["reservation_ref"]["signal_correlation_id"] == p["signal_correlation_id"],
        "attempt_intent_conflict",
        seq=event.seq,
    )
    return _Attempt(
        AttemptFact(
            UUID(str(payload.attempt_id)),
            CapitalScope(scope.account_id, scope.environment, p["symbol"], decision["cell_id"]),
            event.seq,
            intended,
            "pending",
        ),
        payload,
        p["signal_correlation_id"],
    )


def _linked(event: Event, attempts: dict[str, _Attempt]) -> tuple[str | None, _Attempt | None]:
    p = event.payload
    ref = p.get("reservation_ref")
    if ref is None:
        return None, None
    key = str(ref["execution_decision_id"])
    attempt = attempts.get(key)
    require(
        ref["cid"] == p["cid"] and ref["signal_correlation_id"] == p["signal_correlation_id"],
        "attempt_outcome_evidence_identity",
        seq=event.seq,
    )
    if attempt is not None:
        # store.py:573-599 and capital_repository.py:1063-1099: a CID can be reused.
        # Decision ID selects a cycle; its immutable intent seq must precede this row.
        require(
            attempt.fact.intent_seq < event.seq
            and attempt.payload.cid == p["cid"]
            and attempt.fact.scope.symbol == p["symbol"]
            and attempt.signal == p["signal_correlation_id"],
            "attempt_outcome_evidence_identity",
            seq=event.seq,
        )
    return key, attempt


def decode_facts(
    scope: CapitalScope,
    events: tuple[Event, ...],
    decisions: dict[str, Row],
    fence: int,
    max_references: int,
    latest_reconcile_by_resolution: dict[int, int] | None = None,
) -> tuple[
    tuple[AttemptFact, ...],
    tuple[UncertaintyFact, ...],
    int,
]:
    attempts: dict[str, _Attempt] = {}
    uncertainties: dict[UUID, _Uncertainty] = {}
    by_seq = {event.seq: event for event in events}
    require(len(by_seq) == len(events), "snapshot_evidence_conflict")
    latest_reconcile = 0
    references = len(decisions)
    for event in sorted(events, key=lambda value: value.seq):
        p = event.payload
        if event.kind == "VENUE_SNAPSHOT_OBSERVED":
            latest_reconcile = event.seq
        elif event.kind == "RESERVATION_INTENT":
            if p.get("submission_attempt") is None:
                require(event.seq <= fence, "attempt_intent_missing", seq=event.seq)
                continue
            new_attempt = _intent(scope, event, decisions)
            decision_key = new_attempt.payload.execution_decision_id
            require(
                decision_key not in attempts
                and all(
                    prior.fact.attempt_id != new_attempt.fact.attempt_id
                    for prior in attempts.values()
                ),
                "duplicate_attempt_intent",
                seq=event.seq,
            )
            attempts[decision_key] = new_attempt
        elif event.kind in _OUTCOMES:
            outcome_key, outcome_attempt = _linked(event, attempts)
            if outcome_attempt is not None:
                require(
                    outcome_attempt.fact.outcome == "pending",
                    "invalid_attempt_outcome",
                    seq=event.seq,
                )
                # event_store/store.py:603-620: transport ambiguity is never zero commitment.
                outcome: AttemptOutcome = (
                    "acknowledged"
                    if event.kind == "RESERVATION_CLAIMED"
                    else "unknown"
                    if event.kind == "SUBMIT_OUTCOME_UNKNOWN"
                    else "not_sent"
                    if p["reason"] == "local_pre_transport"
                    else "rejected"
                )
                assert outcome_key is not None
                attempts[outcome_key] = replace(
                    outcome_attempt, fact=replace(outcome_attempt.fact, outcome=outcome)
                )
            if event.kind == "SUBMIT_OUTCOME_UNKNOWN":
                require(
                    not any(
                        u.fact.is_open and u.fact.symbol == p["symbol"]
                        for u in uncertainties.values()
                    ),
                    "uncertainty_scope_conflict",
                )
                identity = uuid5(_SUBMIT_NAMESPACE, str(event.event_id))
                uncertainties[identity] = _Uncertainty(
                    UncertaintyFact(
                        identity, scope.account_id, scope.environment, p["symbol"], True
                    ),
                    event,
                    outcome_key if outcome_attempt is not None else None,
                    "submit_outcome_unknown",
                )
        elif event.kind == "VENUE_OFFER_QUARANTINED":
            # store.py:1113-1164: deduplicate first-offer correlation even after
            # closure, merge another offer into the FIRST open orphan for this symbol.
            if any(
                u.fact.symbol == p["symbol"] and u.first_offer == p["venue_offer_id"]
                for u in uncertainties.values()
            ):
                continue
            if any(
                u.fact.symbol == p["symbol"]
                and u.fact.is_open
                and u.kind == "unattributed_venue_offer"
                for u in uncertainties.values()
            ):
                continue
            identity = uuid5(_ORPHAN_NAMESPACE, str(event.event_id))
            uncertainties[identity] = _Uncertainty(
                UncertaintyFact(identity, scope.account_id, scope.environment, p["symbol"], True),
                event,
                None,
                "unattributed_venue_offer",
                p["venue_offer_id"],
            )
        elif event.kind in _RESOLUTIONS:
            references += 1
            require(references <= max_references, "reference_lookup_limit")
            if event.kind == "SUBMIT_MATCHED_TO_VENUE_OFFER":
                resolution_key, resolution_attempt = _linked(event, attempts)
                matching = [
                    u
                    for u in uncertainties.values()
                    if resolution_key is not None
                    and u.decision_id == resolution_key
                    and u.fact.is_open
                ]
                require(len(matching) == 1, "unknown_match_evidence_gap")
                uncertainty = matching[0]
            else:
                uncertainty = uncertainties[UUID(p["uncertainty_id"])]
                resolution_key = uncertainty.decision_id
                resolution_attempt = (
                    attempts.get(resolution_key) if resolution_key is not None else None
                )
                require(p["kind"] == uncertainty.kind, "unknown_match_evidence_gap")
            require(
                uncertainty.fact.is_open and uncertainty.fact.symbol == p["symbol"],
                "unknown_match_evidence_gap",
            )
            reconcile = by_seq.get(p["reconcile_event_seq"])
            require(reconcile is not None, "unknown_match_evidence_gap")
            assert reconcile is not None
            reconcile_before = (
                latest_reconcile
                if latest_reconcile_by_resolution is None
                else latest_reconcile_by_resolution[event.seq]
            )
            prove_resolution(
                event,
                uncertainty.opening,
                reconcile,
                reconcile_before,
                resolution_attempt.payload if resolution_attempt is not None else None,
            )
            uncertainties[uncertainty.fact.uncertainty_id] = replace(
                uncertainty,
                fact=replace(uncertainty.fact, is_open=False),
            )
            if resolution_attempt is not None:
                require(
                    resolution_attempt.fact.outcome == "unknown"
                    and resolution_attempt.fact.resolution is None,
                    "invalid_attempt_outcome",
                )
                assert resolution_key is not None
                # trading/capital.py:133-158: keep original UNKNOWN even when the
                # legacy match projector (store.py:623-627) overwrites its outcome.
                resolution: Literal["not_sent", "acknowledged"] = (
                    "not_sent"
                    if event.kind == "UNCERTAINTY_MARKED_NOT_ACCEPTED"
                    else "acknowledged"
                )
                attempts[resolution_key] = replace(
                    resolution_attempt, fact=replace(resolution_attempt.fact, resolution=resolution)
                )
    return (
        tuple(
            sorted(
                (a.fact for a in attempts.values() if a.fact.intent_seq > fence),
                key=lambda fact: fact.intent_seq,
            )
        ),
        tuple(
            sorted(
                (u.fact for u in uncertainties.values()), key=lambda fact: str(fact.uncertainty_id)
            )
        ),
        references,
    )
