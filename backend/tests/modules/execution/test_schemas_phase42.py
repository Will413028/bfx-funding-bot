"""Phase 4.2 event schema additions."""
from __future__ import annotations

from uuid import uuid4

import pytest
from pydantic import ValidationError

from bfx_funding_bot.core.telemetry import EventType, HealthTarget, Level, Phase
from bfx_funding_bot.modules.marketfeed.schemas import (
    Envelope,
    OrderFillPayload,
    OrderStatusChangePayload,
    OrderSubmitPayload,
    SafetyTriggerPayload,
)
from bfx_funding_bot.modules.strategy import StrategyName


def test_order_submit_payload_minimal() -> None:
    p = OrderSubmitPayload(
        cid=12345,
        offer_id="paper_abc123",
        execution_decision_id="d-schema",
        signal_correlation_id=uuid4(),
        offer_rate=0.0001,
        offer_amount_usdt=100.0,
        offer_duration_days=2,
        is_simulated=True,
        status="submitted",
    )
    assert p.cid == 12345
    assert p.execution_decision_id == "d-schema"
    assert p.is_simulated is True


def test_order_submit_failed_requires_failure_reason() -> None:
    with pytest.raises(ValidationError, match="failure_reason"):
        OrderSubmitPayload(
            cid=1, offer_id=None, execution_decision_id="d-schema", signal_correlation_id=uuid4(),
            offer_rate=0.0001, offer_amount_usdt=100.0, offer_duration_days=2,
            is_simulated=False, status="failed",
        )


def test_order_submit_unknown_and_not_sent_require_failure_reason() -> None:
    common = {
        "cid": 1,
        "offer_id": None,
        "execution_decision_id": "d-schema",
        "signal_correlation_id": uuid4(),
        "offer_rate": 0.0001,
        "offer_amount_usdt": 100.0,
        "offer_duration_days": 2,
        "is_simulated": False,
    }
    for status in ("unknown", "not_sent"):
        with pytest.raises(ValidationError, match="failure_reason"):
            OrderSubmitPayload(**common, status=status)
        payload = OrderSubmitPayload(**common, status=status, failure_reason="reason")
        assert payload.status == status


def test_order_submit_status_is_closed_vocabulary() -> None:
    common = {
        "cid": 1,
        "offer_id": None,
        "execution_decision_id": "d-schema",
        "signal_correlation_id": uuid4(),
        "offer_rate": 0.0001,
        "offer_amount_usdt": 100.0,
        "offer_duration_days": 2,
        "is_simulated": False,
    }
    with pytest.raises(ValidationError, match="status"):
        OrderSubmitPayload(**common, status="filled")


def test_order_submit_payload_requires_execution_decision_id() -> None:
    with pytest.raises(ValidationError, match="execution_decision_id"):
        OrderSubmitPayload(
            cid=1, offer_id="paper_abc", signal_correlation_id=uuid4(),
            offer_rate=0.0001, offer_amount_usdt=100.0, offer_duration_days=2,
            is_simulated=True, status="submitted",
        )


def test_order_submit_payload_rejects_blank_execution_decision_id() -> None:
    with pytest.raises(ValidationError, match="execution_decision_id"):
        OrderSubmitPayload(
            cid=1, offer_id="paper_abc", execution_decision_id=" ",
            signal_correlation_id=uuid4(), offer_rate=0.0001,
            offer_amount_usdt=100.0, offer_duration_days=2,
            is_simulated=True, status="submitted",
        )


def test_order_fill_payload_minimal() -> None:
    p = OrderFillPayload(
        cid=12345, offer_id="paper_abc",
        signal_correlation_id=uuid4(),
        fill_size_usdt=100.0, fill_price=0.0001,
        is_simulated=True,
    )
    assert p.fill_size_usdt == 100.0


def test_order_status_change_payload() -> None:
    p = OrderStatusChangePayload(
        cid=1, offer_id="x",
        signal_correlation_id=uuid4(),
        status="cancelled", reason="user_cancelled",
        is_simulated=False,
    )
    assert p.status == "cancelled"


def test_safety_trigger_payload() -> None:
    p = SafetyTriggerPayload(
        guard_name="allocation_cap",
        reason="cap=500+offer=200>500",
        decision_snapshot={"outcome": "post"},
    )
    assert p.guard_name == "allocation_cap"


def test_event_type_includes_order_status_change() -> None:
    assert EventType.ORDER_STATUS_CHANGE.value == "order_status_change"


def test_health_target_includes_phase42_targets() -> None:
    assert HealthTarget.EXECUTOR.value == "executor"
    assert HealthTarget.SAFETY_CHAIN.value == "safety_chain"


def test_envelope_does_not_invent_an_account_realm() -> None:
    env = Envelope(
        timestamp="2026-05-21T00:00:00+00:00",
        level=Level.INFO,
        phase=Phase.SHADOW,
        strategy=StrategyName.MEAN_REVERSION,
        cell="fUSD_a30",
        event_type=EventType.SIGNAL,
        correlation_id=uuid4(),
        payload={
            "signal_score": 0.5,
            "signal_direction": "post",
            "strategy_attributes": {"k": "v"},
            "budget_seconds": 43200,
        },
    )
    assert env.account_id is None


def test_envelope_validates_order_submit_payload() -> None:
    with pytest.raises(ValidationError):
        Envelope(
            timestamp="2026-05-21T00:00:00+00:00",
            level=Level.INFO, phase=Phase.SHADOW,
            strategy=StrategyName.MEAN_REVERSION, cell="fUSD_a30",
            event_type=EventType.ORDER_SUBMIT,
            correlation_id=uuid4(),
            payload={"missing": "fields"},
        )
