"""Path A — happy path: a tentative POST passes the always-on guards without a safety trigger."""
from __future__ import annotations

from decimal import Decimal
from typing import Any
from uuid import uuid4

import pytest

from bfx_funding_bot.core.health import HealthProbe
from bfx_funding_bot.core.telemetry import Phase
from bfx_funding_bot.modules.execution.protocols import (
    AccountContext,
    Credentials,
)
from bfx_funding_bot.modules.execution.safety.chain import SafetyGuardChain
from bfx_funding_bot.modules.execution.safety.hard_guards import (
    AuthHealthGuard,
    HeartbeatGuard,
    ManualKillGuard,
)
from bfx_funding_bot.modules.strategy import DecisionOutcome, DecisionPayload, StrategyName

pytestmark = pytest.mark.integration


class _EventCapture:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    async def emit(self, ev: dict[str, Any]) -> None:
        self.events.append(ev)


@pytest.mark.asyncio
async def test_path_a_full_event_sequence(monkeypatch: pytest.MonkeyPatch) -> None:
    diagnostics = _EventCapture()
    probe = HealthProbe()
    probe.record_heartbeat("ws_data")

    ctx = AccountContext("default", Credentials("k", "s"), Decimal("500"))

    chain = SafetyGuardChain(
        guards=[
            ManualKillGuard(),
            AuthHealthGuard(probe=probe),
            HeartbeatGuard(probe=probe, watched_sub_tasks=["ws_data"]),
        ],
        probe=probe,
        diagnostics=diagnostics,
        phase=Phase.SHADOW,
        strategy=StrategyName.MEAN_REVERSION,
        cell="fUSD_a30",
        account_id="default",
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
    assert diagnostics.events == []  # allowed path emits no SAFETY_TRIGGER
