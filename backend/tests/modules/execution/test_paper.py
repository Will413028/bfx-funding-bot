"""EchoPaperExecutor: synchronous echo submit→submit+fill same tick."""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import pytest

from bfx_funding_bot.modules.execution.contracts import (
    ExecutionPolicy,
    GuardResult,
    ReadyToSubmit,
)
from bfx_funding_bot.modules.execution.paper import EchoPaperExecutor
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


class _EventCapture:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    async def emit(self, event: dict[str, Any]) -> None:
        self.events.append(event)


def _ctx() -> AccountContext:
    return AccountContext("default", Credentials("k", "s"), Decimal("500"))


def _decision(corr: UUID) -> DecisionPayload:
    return DecisionPayload(
        decision_outcome=DecisionOutcome.POST,
        signal_correlation_id=corr,
        offer_rate=0.0001, offer_amount_usdt=100.0, offer_duration_days=2,
    symbol="fUST")


def _ready_to_submit(*, corr: UUID, rate: float = 0.0001, decision_id: str = "d-paper") -> ReadyToSubmit:
    decision = _decision(corr).model_copy(update={"offer_rate": Decimal(str(rate))})
    return ReadyToSubmit(
        decision=decision,
        decision_id=decision_id,
        policy=ExecutionPolicy.PAPER,
        market_snapshot_id="snapshot-paper",
        model_version=None,
        evidence={},
        safety=GuardResult(allowed=True, guard_name="test"),
    )


@pytest.mark.asyncio
async def test_submit_emits_order_submit_then_order_fill() -> None:
    axiom = _EventCapture()
    ex = EchoPaperExecutor(
        event_sink=axiom, phase=Phase.PAPER,
        strategy=StrategyName.MEAN_REVERSION, cell="fUSD_a30",
        date_provider=lambda: date(2026, 5, 21),
    )
    corr = uuid4()
    result = await ex.submit(_ready_to_submit(corr=corr), _ctx())
    assert result.status == "filled"
    assert result.venue_offer_id is not None
    assert result.venue_offer_id.startswith("paper_")
    assert len(axiom.events) == 2
    assert axiom.events[0]["event_type"] == EventType.ORDER_SUBMIT.value
    assert axiom.events[1]["event_type"] == EventType.ORDER_FILL.value
    assert axiom.events[0]["payload"]["is_simulated"] is True
    assert axiom.events[1]["payload"]["is_simulated"] is True
    assert axiom.events[0]["payload"]["execution_decision_id"] == "d-paper"


@pytest.mark.asyncio
async def test_submit_same_correlation_id_same_date_same_cid() -> None:
    axiom = _EventCapture()
    ex = EchoPaperExecutor(
        event_sink=axiom, phase=Phase.PAPER,
        strategy=StrategyName.MEAN_REVERSION, cell="fUSD_a30",
        date_provider=lambda: date(2026, 5, 21),
    )
    corr = uuid4()
    r1 = await ex.submit(_ready_to_submit(corr=corr), _ctx())
    r2 = await ex.submit(_ready_to_submit(corr=corr), _ctx())
    assert r1.cid == r2.cid  # deterministic for retry safety


@pytest.mark.asyncio
async def test_submit_uses_provided_cid() -> None:
    axiom = _EventCapture()
    ex = EchoPaperExecutor(
        event_sink=axiom, phase=Phase.PAPER,
        strategy=StrategyName.MEAN_REVERSION, cell="fUSD_a30",
        date_provider=lambda: date(2026, 5, 21),
    )
    result = await ex.submit(_ready_to_submit(corr=uuid4()), _ctx(), cid=99999)
    assert result.cid == 99999
    assert axiom.events[0]["payload"]["cid"] == 99999


@pytest.mark.asyncio
async def test_submit_without_cid_falls_back_to_generated() -> None:
    axiom = _EventCapture()
    ex = EchoPaperExecutor(
        event_sink=axiom, phase=Phase.PAPER,
        strategy=StrategyName.MEAN_REVERSION, cell="fUSD_a30",
        date_provider=lambda: date(2026, 5, 21),
    )
    corr = uuid4()
    r1 = await ex.submit(_ready_to_submit(corr=corr), _ctx())
    r2 = await ex.submit(_ready_to_submit(corr=corr), _ctx(), cid=None)
    assert r1.cid == r2.cid


@pytest.mark.asyncio
async def test_submit_fill_price_and_size_match_decision() -> None:
    axiom = _EventCapture()
    ex = EchoPaperExecutor(
        event_sink=axiom, phase=Phase.PAPER,
        strategy=StrategyName.MEAN_REVERSION, cell="fUSD_a30",
        date_provider=lambda: date(2026, 5, 21),
    )
    await ex.submit(_ready_to_submit(corr=uuid4()), _ctx())
    fill = axiom.events[1]["payload"]
    assert fill["fill_size_usdt"] == 100.0
    assert fill["fill_price"] == 0.0001


@pytest.mark.asyncio
async def test_paper_executor_uses_the_rate_inside_ready_to_submit() -> None:
    axiom = _EventCapture()
    ex = EchoPaperExecutor(
        event_sink=axiom, phase=Phase.PAPER,
        strategy=StrategyName.MEAN_REVERSION, cell="fUSD_a30",
    )

    result = await ex.submit(_ready_to_submit(corr=uuid4(), rate=0.00023, decision_id="d-7"), _ctx())

    assert result.status == "filled"
    assert result.raw_response == {"offer_rate": "0.00023"}
    assert axiom.events[0]["payload"]["offer_rate"] == "0.00023"
    assert axiom.events[0]["payload"]["execution_decision_id"] == "d-7"
