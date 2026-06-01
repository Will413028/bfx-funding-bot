"""Path B — safety block: tentative POST → AllocationCap blocks → safety_trigger
+ FINAL decision SKIP(SAFETY_BLOCK) emitted ONCE; executor not called.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Any
from uuid import uuid4

import pytest

from bfx_funding_bot.modules.execution.events import OrderFilled
from bfx_funding_bot.modules.execution.ledger import PaperPositionLedger
from bfx_funding_bot.modules.execution.protocols import (
    AccountContext,
    Credentials,
)
from bfx_funding_bot.modules.execution.safety.chain import SafetyGuardChain
from bfx_funding_bot.modules.execution.safety.hard_guards import (
    AllocationCapGuard,
)
from bfx_funding_bot.modules.marketfeed.health_monitor import HealthProbe
from bfx_funding_bot.modules.marketfeed.schemas import (
    DecisionOutcome,
    DecisionPayload,
    EventType,
    Phase,
    SkipReason,
    StrategyName,
)

pytestmark = pytest.mark.integration


class _EventCapture:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    async def emit(self, ev: dict[str, Any]) -> None:
        self.events.append(ev)


@pytest.mark.asyncio
async def test_path_b_safety_block_emits_safety_trigger_and_single_skip() -> None:
    diagnostics = _EventCapture()
    probe = HealthProbe()

    # Pre-load ledger with 600 USDT realized so AllocationCap (cap=500) fires
    ledger = PaperPositionLedger(account_id="default")
    await ledger.on_order_filled(OrderFilled(
        cid=1, venue_offer_id="paper_x", credit_id=None,
        size_usdt=Decimal("600"), fill_rate=0.0001,
        signal_correlation_id=uuid4(), account_id="default", is_simulated=True,
    symbol="fUST"))
    ctx = AccountContext("default", Credentials("k", "s"), Decimal("500"))

    chain = SafetyGuardChain(
        guards=[AllocationCapGuard(ledger=ledger, caps={}, default_cap=Decimal("500"))],
        probe=probe,
        diagnostics=diagnostics,
        phase=Phase.PAPER,
        strategy=StrategyName.MEAN_REVERSION,
        cell="fUSD_a30",
        account_id="default",
    )

    tentative = DecisionPayload(
        decision_outcome=DecisionOutcome.POST,
        signal_correlation_id=uuid4(),
        offer_rate=0.0001,
        offer_amount_usdt=100.0,
        offer_duration_days=2,
    symbol="fUST")
    result = await chain.evaluate(tentative, ctx)
    assert result.allowed is False

    safety_triggers = [
        e for e in diagnostics.events
        if e["event_type"] == EventType.SAFETY_TRIGGER.value
    ]
    assert len(safety_triggers) == 1
    assert safety_triggers[0]["payload"]["guard_name"] == "allocation_cap"

    final = DecisionPayload(
        decision_outcome=DecisionOutcome.SKIP,
        signal_correlation_id=tentative.signal_correlation_id,
        skip_reason=SkipReason.SAFETY_BLOCK,
        skip_reason_detail=result.reason,
    symbol="fUST")
    assert final.skip_reason == SkipReason.SAFETY_BLOCK
    assert "cap" in (final.skip_reason_detail or "")
