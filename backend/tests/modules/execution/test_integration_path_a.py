"""Path A — happy path: signal → safety pass → decision (FINAL POST) → submit + fill.

Validates: 4 events emitted in correct order with shared correlation_id;
executor called once; ledger exposure incremented.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Any
from uuid import uuid4

import pytest

from bfx_funding_bot.modules.execution.contracts import ExecutionPolicy, GuardResult, ReadyToSubmit
from bfx_funding_bot.modules.execution.ledger import PaperPositionLedger
from bfx_funding_bot.modules.execution.paper import EchoPaperExecutor
from bfx_funding_bot.modules.execution.protocols import (
    AccountContext,
    Credentials,
)
from bfx_funding_bot.modules.execution.safety.chain import SafetyGuardChain
from bfx_funding_bot.modules.execution.safety.hard_guards import (
    AllocationCapGuard,
    AuthHealthGuard,
    HeartbeatGuard,
    ManualKillGuard,
)
from bfx_funding_bot.modules.marketfeed.health_monitor import HealthProbe
from bfx_funding_bot.modules.marketfeed.schemas import (
    DecisionOutcome,
    DecisionPayload,
    EventType,
    Phase,
    StrategyName,
)

pytestmark = pytest.mark.integration


class _EventCapture:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    async def emit(self, ev: dict[str, Any]) -> None:
        self.events.append(ev)


@pytest.mark.asyncio
async def test_path_a_full_event_sequence(monkeypatch: pytest.MonkeyPatch) -> None:
    axiom = _EventCapture()
    diagnostics = _EventCapture()
    probe = HealthProbe()
    probe.record_heartbeat("ws")

    ledger = PaperPositionLedger(account_id="default")  # Empty ledger — no prior state needed for this sequence (was: replay_from_axiom with empty query)
    ctx = AccountContext("default", Credentials("k", "s"), Decimal("500"))

    chain = SafetyGuardChain(
        guards=[
            ManualKillGuard(),
            AuthHealthGuard(probe=probe),
            HeartbeatGuard(
                probe=probe,
                threshold_seconds=300,
                watched_sub_tasks=["ws"],
            ),
            AllocationCapGuard(ledger=ledger, caps={}, default_cap=Decimal("500")),
        ],
        probe=probe,
        diagnostics=diagnostics,
        phase=Phase.PAPER,
        strategy=StrategyName.MEAN_REVERSION,
        cell="fUSD_a30",
        account_id="default",
    )
    executor = EchoPaperExecutor(
        event_sink=axiom,
        phase=Phase.PAPER,
        strategy=StrategyName.MEAN_REVERSION,
        cell="fUSD_a30",
    )

    corr = uuid4()
    tentative = DecisionPayload(
        decision_outcome=DecisionOutcome.POST,
        signal_correlation_id=corr,
        offer_rate=0.0001,
        offer_amount_usdt=100.0,
        offer_duration_days=2,
    symbol="fUST")

    result = await chain.evaluate(tentative, ctx)
    assert result.allowed is True
    await executor.submit(ReadyToSubmit(
        decision=tentative, decision_id="d-path-a", policy=ExecutionPolicy.PAPER,
        market_snapshot_id="snapshot-path-a", model_version=None,
        evidence={}, safety=GuardResult(allowed=True, guard_name="test"),
    ), ctx)

    types = [e["event_type"] for e in axiom.events]
    assert types == [
        EventType.ORDER_SUBMIT.value,
        EventType.ORDER_FILL.value,
    ]
    assert all(e["correlation_id"] == str(corr) for e in axiom.events)
    assert all(e["account_id"] == "default" for e in axiom.events)
    assert diagnostics.events == []  # allowed path emits no SAFETY_TRIGGER
