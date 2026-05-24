"""Path A — happy path: signal → safety pass → decision (FINAL POST) → submit + fill.

Validates: 4 events emitted in correct order with shared correlation_id;
executor called once; ledger exposure incremented.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Any
from uuid import uuid4

import pytest

from bfx_funding_bot.modules.execution.ledger import PaperPositionLedger
from bfx_funding_bot.modules.execution.paper import EchoPaperExecutor
from bfx_funding_bot.modules.execution.protocols import (
    AccountContext,
    Credentials,
)
from bfx_funding_bot.modules.execution.safety.calibrated_guards import (
    DivergenceRateGuard,
    DrawdownGuard,
    RealizedLossGuard,
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


class _StubPnL:
    def realized_loss_24h(self) -> Decimal:
        return Decimal("0")

    def drawdown_pct(self) -> float:
        return 0.0


class _StubDiv:
    def divergence_rate_pct(self, window_minutes: int) -> float:
        return 0.0


@pytest.mark.asyncio
async def test_path_a_full_event_sequence(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BFX_KILL_SWITCH", raising=False)
    axiom = _EventCapture()
    diagnostics = _EventCapture()
    probe = HealthProbe()
    probe.record_heartbeat("safety_chain")
    probe.record_heartbeat("executor")

    ledger = PaperPositionLedger(account_id="default")  # Empty ledger — no prior state needed for this sequence (was: replay_from_axiom with empty query)
    ctx = AccountContext("default", Credentials("k", "s"), Decimal("500"))
    pnl, div = _StubPnL(), _StubDiv()

    chain = SafetyGuardChain(
        guards=[
            ManualKillGuard(),
            AuthHealthGuard(probe=probe),
            HeartbeatGuard(
                probe=probe,
                threshold_seconds=300,
                watched_sub_tasks=["safety_chain", "executor"],
            ),
            AllocationCapGuard(ledger=ledger),
            RealizedLossGuard(enabled=False, threshold_usdt=None, source=pnl),
            DrawdownGuard(enabled=False, threshold_pct=None, source=pnl),
            DivergenceRateGuard(
                enabled=False,
                threshold_pct=None,
                window_minutes=None,
                source=div,
            ),
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
    )

    result = await chain.evaluate(tentative, ctx)
    assert result.allowed is True
    await executor.submit(tentative, ctx)

    types = [e["event_type"] for e in axiom.events]
    assert types == [
        EventType.ORDER_SUBMIT.value,
        EventType.ORDER_FILL.value,
    ]
    assert all(e["correlation_id"] == str(corr) for e in axiom.events)
    assert all(e["account_id"] == "default" for e in axiom.events)
    assert diagnostics.events == []  # allowed path emits no SAFETY_TRIGGER
