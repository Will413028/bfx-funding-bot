"""Order / safety event emit helpers — assert payload shape + envelope discipline."""
from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest

from bfx_funding_bot.core.telemetry import EventType, Phase
from bfx_funding_bot.modules.execution.contracts import (
    ExecutionPolicy,
    GuardResult,
    ReadyToSubmit,
)
from bfx_funding_bot.modules.execution.emit import (
    emit_order_submit,
    emit_safety_trigger,
)
from bfx_funding_bot.modules.execution.protocols import (
    AccountContext,
    Credentials,
)
from bfx_funding_bot.modules.strategy import DecisionOutcome, DecisionPayload, StrategyName


class _EventCapture:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    async def emit(self, event: dict[str, Any]) -> None:
        self.events.append(event)


def _ctx() -> AccountContext:
    return AccountContext(
        account_id="default",
        credentials=Credentials(api_key="k", api_secret="s"),
    )


def _decision(corr: Any) -> DecisionPayload:
    return DecisionPayload(
        decision_outcome=DecisionOutcome.POST,
        signal_correlation_id=corr,
        offer_rate=0.0001, offer_amount_usdt=100.0, offer_duration_days=2,
    symbol="fUST")


def _ready(corr: Any, *, decision_id: str = "d-emit") -> ReadyToSubmit:
    return ReadyToSubmit(
        decision=_decision(corr),
        decision_id=decision_id,
        policy=ExecutionPolicy.BOOK_GUARDED,
        market_snapshot_id="snapshot-emit",
        model_version=None,
        evidence={},
        safety=GuardResult(allowed=True, guard_name="test"),
    )


@pytest.mark.asyncio
async def test_emit_order_submit_paper_shape() -> None:
    axiom = _EventCapture()
    corr = uuid4()
    await emit_order_submit(
        event_sink=axiom, phase=Phase.SHADOW, strategy=StrategyName.MEAN_REVERSION,
        cell="fUSD_a30", ready=_ready(corr, decision_id="d-emit-1"), ctx=_ctx(),
        offer_id="paper_abc", is_simulated=True, status="submitted",
    )
    assert len(axiom.events) == 1
    ev = axiom.events[0]
    assert ev["event_type"] == EventType.ORDER_SUBMIT.value
    assert ev["account_id"] == "default"
    assert ev["correlation_id"] == str(corr)
    assert "cid" not in ev["payload"]
    assert ev["payload"]["execution_decision_id"] == "d-emit-1"
    assert ev["payload"]["is_simulated"] is True


@pytest.mark.asyncio
async def test_emit_order_submit_failed_requires_reason() -> None:
    axiom = _EventCapture()
    with pytest.raises(ValueError, match="failure_reason"):
        await emit_order_submit(
            event_sink=axiom, phase=Phase.SHADOW, strategy=StrategyName.MEAN_REVERSION,
            cell="fUSD_a30", ready=_ready(uuid4()), ctx=_ctx(),
            offer_id=None, is_simulated=False, status="failed",
            failure_reason=None,
        )


@pytest.mark.asyncio
async def test_emit_safety_trigger_shape() -> None:
    diagnostics = _EventCapture()
    corr = uuid4()
    await emit_safety_trigger(
        diagnostics=diagnostics, phase=Phase.SHADOW, strategy=StrategyName.MEAN_REVERSION,
        cell="fUSD_a30", correlation_id=corr, account_id="default",
        level="warn", guard_name="allocation_cap",
        reason="cap=500+offer=200>500",
        decision_snapshot={"outcome": "post"},
    )
    ev = diagnostics.events[0]
    assert ev["event_type"] == EventType.SAFETY_TRIGGER.value
    assert ev["level"] == "warn"
    assert ev["payload"]["guard_name"] == "allocation_cap"
