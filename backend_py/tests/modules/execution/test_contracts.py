from __future__ import annotations

from uuid import uuid4

from bfx_funding_bot.modules.execution import contracts
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
