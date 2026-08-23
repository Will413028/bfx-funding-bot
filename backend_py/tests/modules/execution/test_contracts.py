from __future__ import annotations

import inspect
from dataclasses import FrozenInstanceError
from typing import get_type_hints
from uuid import uuid4

import pytest

from bfx_funding_bot.modules.execution import contracts, protocols
from bfx_funding_bot.modules.marketfeed import schemas


def _decision() -> schemas.DecisionPayload:
    return schemas.DecisionPayload(
        decision_outcome=schemas.DecisionOutcome.POST,
        signal_correlation_id=uuid4(),
        offer_rate=0.0002,
        offer_amount_usdt=150.0,
        offer_duration_days=30,
        symbol="fUSD",
    )


def test_blocked_execution_is_not_submit_ready() -> None:
    blocked = contracts.BlockedExecution(
        decision_id="d-1",
        candidate=_decision(),
        reason=contracts.BlockReason.BOOK_STALE,
        failed_dependency="market_snapshot",
        evidence={"age_ms": 31_000},
    )

    assert blocked.outcome is contracts.DecisionOutcome.BLOCKED
    assert not isinstance(blocked, contracts.ReadyToSubmit)


def test_ready_to_submit_has_ready_outcome_and_immutable_safety() -> None:
    safety = contracts.GuardResult(allowed=True, guard_name="allocation_cap")
    ready = contracts.ReadyToSubmit(
        decision=_decision(),
        decision_id="d-1",
        policy=contracts.ExecutionPolicy.BOOK_GUARDED,
        market_snapshot_id="snapshot-1",
        model_version=None,
        evidence={"period_days": 30},
        safety=safety,
    )

    assert ready.outcome is contracts.DecisionOutcome.READY
    assert ready.safety == safety
    with pytest.raises(FrozenInstanceError):
        safety.allowed = False


def test_ready_to_submit_rejects_blank_decision_id() -> None:
    with pytest.raises(ValueError, match="decision_id"):
        contracts.ReadyToSubmit(
            decision=_decision(), decision_id=" ",
            policy=contracts.ExecutionPolicy.PAPER,
            market_snapshot_id="snapshot-1", model_version=None,
            evidence={}, safety=contracts.GuardResult(True, "test"),
        )


def test_reservation_ref_binds_venue_offer_once() -> None:
    ref = contracts.ReservationRef(
        execution_decision_id="d-1", cid=42,
        signal_correlation_id=_decision().signal_correlation_id,
    )
    bound = ref.bind_venue_offer("voi-1")

    assert bound.venue_offer_id == "voi-1"
    assert bound.bind_venue_offer("voi-1") is bound
    with pytest.raises(ValueError, match="already bound"):
        bound.bind_venue_offer("voi-2")


def test_no_recommendation_has_explicit_outcome_without_candidate() -> None:
    outcome = contracts.NoRecommendation(
        decision_id="d-2",
        candidate=None,
        reason=contracts.BlockReason.OPTIMIZER_UNAVAILABLE,
        evidence={"model": "unavailable"},
    )

    assert outcome.outcome is contracts.DecisionOutcome.NO_RECOMMENDATION
    assert outcome.candidate is None


def test_protocol_annotations_are_runtime_resolvable() -> None:
    guard_hints = get_type_hints(protocols.GuardRule.evaluate)
    signature = inspect.signature(protocols.ExecutorPort.submit)
    submit_hints = get_type_hints(protocols.ExecutorPort.submit)

    assert guard_hints["decision"] is schemas.DecisionPayload
    assert guard_hints["ctx"] is protocols.AccountContext
    assert guard_hints["return"] is contracts.GuardResult
    assert list(signature.parameters) == ["self", "ready", "ctx", "cid", "reservation_ref"]
    assert signature.parameters["cid"].kind is inspect.Parameter.KEYWORD_ONLY
    assert signature.parameters["reservation_ref"].kind is inspect.Parameter.KEYWORD_ONLY
    assert submit_hints["ready"] is contracts.ReadyToSubmit
    assert submit_hints["ctx"] is protocols.AccountContext
    assert submit_hints["return"] is protocols.SubmittedOrder


def test_execution_contract_enum_values_are_stable() -> None:
    assert [policy.value for policy in contracts.ExecutionPolicy] == [
        "paper",
        "book_guarded",
        "optimizer_shadow",
        "optimizer_live",
    ]
    assert [outcome.value for outcome in contracts.DecisionOutcome] == [
        "ready",
        "blocked",
        "no_recommendation",
    ]
    assert [reason.value for reason in contracts.BlockReason] == [
        "book_fetch_failed",
        "book_not_initialized",
        "book_stale",
        "book_sequence_invalid",
        "book_checksum_invalid",
        "period_not_found",
        "insufficient_period_depth",
        "fill_model_missing",
        "fill_model_low_confidence",
        "optimizer_unavailable",
        "safety_guard_blocked",
        "execution_audit_unavailable",
    ]
