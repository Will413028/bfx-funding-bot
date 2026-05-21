"""Path B — safety block: tentative POST → AllocationCap blocks → safety_trigger
+ FINAL decision SKIP(SAFETY_BLOCK) emitted ONCE; executor not called.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Any
from uuid import uuid4

import pytest

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


class _CaptureAxiom:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    async def emit(self, ev: dict[str, Any]) -> None:
        self.events.append(ev)


@pytest.mark.asyncio
async def test_path_b_safety_block_emits_safety_trigger_and_single_skip() -> None:
    axiom = _CaptureAxiom()
    probe = HealthProbe()

    class _FakeQuery:
        async def query_order_fills(
            self, account_id: str, since: Any,
        ) -> list[dict[str, Any]]:
            return [{
                "event_type": EventType.ORDER_FILL.value,
                "account_id": "default",
                "payload": {
                    "cid": 1,
                    "offer_id": "paper_x",
                    "signal_correlation_id": str(uuid4()),
                    "fill_size_usdt": 600.0,
                    "fill_price": 0.0001,
                    "is_simulated": True,
                },
            }]

    from datetime import UTC, datetime, timedelta
    ledger = await PaperPositionLedger.replay_from_axiom(
        account_id="default",
        since=datetime.now(UTC) - timedelta(days=30),
        axiom_query=_FakeQuery(),
    )
    ctx = AccountContext("default", Credentials("k", "s"), Decimal("500"))

    chain = SafetyGuardChain(
        guards=[AllocationCapGuard(ledger=ledger)],
        probe=probe,
        axiom=axiom,
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
    )
    result = await chain.evaluate(tentative, ctx)
    assert result.allowed is False

    safety_triggers = [
        e for e in axiom.events
        if e["event_type"] == EventType.SAFETY_TRIGGER.value
    ]
    assert len(safety_triggers) == 1
    assert safety_triggers[0]["payload"]["guard_name"] == "allocation_cap"

    final = DecisionPayload(
        decision_outcome=DecisionOutcome.SKIP,
        signal_correlation_id=tentative.signal_correlation_id,
        skip_reason=SkipReason.SAFETY_BLOCK,
        skip_reason_detail=result.reason,
    )
    assert final.skip_reason == SkipReason.SAFETY_BLOCK
    assert "cap" in (final.skip_reason_detail or "")
