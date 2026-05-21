"""Order / safety event emit helpers — assert payload shape + envelope discipline."""
from __future__ import annotations

from decimal import Decimal
from typing import Any
from uuid import uuid4

import pytest

from bfx_funding_bot.modules.execution.emit import (
    emit_order_fill,
    emit_order_status_change,
    emit_order_submit,
    emit_safety_trigger,
)
from bfx_funding_bot.modules.execution.protocols import (
    AccountContext,
    Credentials,
)
from bfx_funding_bot.modules.marketfeed.schemas import (
    DecisionOutcome,
    DecisionPayload,
    EventType,
    Phase,
    StrategyName,
)


class _CaptureAxiom:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    async def emit(self, event: dict[str, Any]) -> None:
        self.events.append(event)


def _ctx() -> AccountContext:
    return AccountContext(
        account_id="default",
        credentials=Credentials(api_key="k", api_secret="s"),
        allocation_cap_usdt=Decimal("500"),
    )


def _decision(corr: Any) -> DecisionPayload:
    return DecisionPayload(
        decision_outcome=DecisionOutcome.POST,
        signal_correlation_id=corr,
        offer_rate=0.0001, offer_amount_usdt=100.0, offer_duration_days=2,
    )


@pytest.mark.asyncio
async def test_emit_order_submit_paper_shape() -> None:
    axiom = _CaptureAxiom()
    corr = uuid4()
    await emit_order_submit(
        axiom=axiom, phase=Phase.PAPER, strategy=StrategyName.MEAN_REVERSION,
        cell="fUSD_a30", decision=_decision(corr), ctx=_ctx(),
        cid=42, offer_id="paper_abc", is_simulated=True, status="submitted",
    )
    assert len(axiom.events) == 1
    ev = axiom.events[0]
    assert ev["event_type"] == EventType.ORDER_SUBMIT.value
    assert ev["account_id"] == "default"
    assert ev["correlation_id"] == str(corr)
    assert ev["payload"]["cid"] == 42
    assert ev["payload"]["is_simulated"] is True


@pytest.mark.asyncio
async def test_emit_order_submit_failed_requires_reason() -> None:
    axiom = _CaptureAxiom()
    with pytest.raises(ValueError, match="failure_reason"):
        await emit_order_submit(
            axiom=axiom, phase=Phase.PAPER, strategy=StrategyName.MEAN_REVERSION,
            cell="fUSD_a30", decision=_decision(uuid4()), ctx=_ctx(),
            cid=1, offer_id=None, is_simulated=False, status="failed",
            failure_reason=None,
        )


@pytest.mark.asyncio
async def test_emit_order_fill_shape() -> None:
    axiom = _CaptureAxiom()
    corr = uuid4()
    await emit_order_fill(
        axiom=axiom, phase=Phase.PAPER, strategy=StrategyName.MEAN_REVERSION,
        cell="fUSD_a30", decision=_decision(corr), ctx=_ctx(),
        cid=1, offer_id="paper_x", fill_size_usdt=100.0, fill_price=0.0001,
        is_simulated=True,
    )
    assert axiom.events[0]["event_type"] == EventType.ORDER_FILL.value


@pytest.mark.asyncio
async def test_emit_order_status_change_partial() -> None:
    axiom = _CaptureAxiom()
    await emit_order_status_change(
        axiom=axiom, phase=Phase.PAPER, strategy=StrategyName.MEAN_REVERSION,
        cell="fUSD_a30", correlation_id=uuid4(), account_id="default",
        cid=1, offer_id="x", status="partially_filled",
        filled_size_delta_usdt=50.0, is_simulated=False, reason=None,
    )
    p = axiom.events[0]["payload"]
    assert p["status"] == "partially_filled"
    assert p["filled_size_delta_usdt"] == 50.0


@pytest.mark.asyncio
async def test_emit_safety_trigger_shape() -> None:
    axiom = _CaptureAxiom()
    corr = uuid4()
    await emit_safety_trigger(
        axiom=axiom, phase=Phase.PAPER, strategy=StrategyName.MEAN_REVERSION,
        cell="fUSD_a30", correlation_id=corr, account_id="default",
        level="warn", guard_name="allocation_cap",
        reason="cap=500+offer=200>500",
        decision_snapshot={"outcome": "post"},
    )
    ev = axiom.events[0]
    assert ev["event_type"] == EventType.SAFETY_TRIGGER.value
    assert ev["level"] == "warn"
    assert ev["payload"]["guard_name"] == "allocation_cap"
