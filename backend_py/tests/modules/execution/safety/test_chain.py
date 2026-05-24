"""SafetyGuardChain: short-circuit / timeout / fail-closed / heartbeat / safety_trigger emit."""
from __future__ import annotations

import asyncio
from decimal import Decimal
from typing import Any
from uuid import uuid4

import pytest

from bfx_funding_bot.modules.execution.protocols import (
    AccountContext,
    Credentials,
    GuardResult,
)
from bfx_funding_bot.modules.execution.safety.chain import (
    GUARD_EVAL_TIMEOUT_SECONDS,
    SafetyGuardChain,
)
from bfx_funding_bot.modules.marketfeed.health_monitor import HealthProbe
from bfx_funding_bot.modules.marketfeed.schemas import (
    DecisionOutcome,
    DecisionPayload,
    EventType,
    Phase,
    StrategyName,
)


def _ctx() -> AccountContext:
    return AccountContext("default", Credentials("k", "s"), Decimal("500"))


def _post() -> DecisionPayload:
    return DecisionPayload(
        decision_outcome=DecisionOutcome.POST,
        signal_correlation_id=uuid4(),
        offer_rate=0.0001, offer_amount_usdt=100.0, offer_duration_days=2,
    )


class _EventCapture:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    async def emit(self, event: dict[str, Any]) -> None:
        self.events.append(event)


class _AllowGuard:
    def __init__(self, name: str) -> None:
        self.name = name
        self.is_calibrated = False
        self.called = False

    async def evaluate(self, d: DecisionPayload, c: AccountContext) -> GuardResult:
        self.called = True
        return GuardResult(allowed=True, guard_name=self.name)


class _BlockGuard:
    def __init__(self, name: str) -> None:
        self.name = name
        self.is_calibrated = False

    async def evaluate(self, d: DecisionPayload, c: AccountContext) -> GuardResult:
        return GuardResult(allowed=False, guard_name=self.name, reason="blocked")


class _CrashGuard:
    name = "crash"
    is_calibrated = False

    async def evaluate(self, d: DecisionPayload, c: AccountContext) -> GuardResult:
        raise RuntimeError("boom")


class _HangGuard:
    name = "hang"
    is_calibrated = False

    async def evaluate(self, d: DecisionPayload, c: AccountContext) -> GuardResult:
        await asyncio.sleep(GUARD_EVAL_TIMEOUT_SECONDS + 5)
        return GuardResult(allowed=True, guard_name=self.name)


@pytest.mark.asyncio
async def test_chain_short_circuits_on_first_block() -> None:
    g1 = _AllowGuard("g1")
    g2 = _BlockGuard("g2")
    g3 = _AllowGuard("g3")
    chain = SafetyGuardChain(
        guards=[g1, g2, g3], probe=HealthProbe(), diagnostics=_EventCapture(),
        phase=Phase.PAPER, strategy=StrategyName.MEAN_REVERSION, cell="fUSD_a30",
        account_id="default",
    )
    r = await chain.evaluate(_post(), _ctx())
    assert r.allowed is False
    assert r.guard_name == "g2"
    assert g1.called is True
    assert g3.called is False  # short-circuit


@pytest.mark.asyncio
async def test_chain_all_run_when_all_pass() -> None:
    g1, g2, g3 = _AllowGuard("a"), _AllowGuard("b"), _AllowGuard("c")
    chain = SafetyGuardChain(
        guards=[g1, g2, g3], probe=HealthProbe(), diagnostics=_EventCapture(),
        phase=Phase.PAPER, strategy=StrategyName.MEAN_REVERSION, cell="fUSD_a30",
        account_id="default",
    )
    r = await chain.evaluate(_post(), _ctx())
    assert r.allowed is True
    assert all(g.called for g in (g1, g2, g3))


@pytest.mark.asyncio
async def test_chain_heartbeat_recorded_on_eval() -> None:
    probe = HealthProbe()
    chain = SafetyGuardChain(
        guards=[_AllowGuard("a")], probe=probe, diagnostics=_EventCapture(),
        phase=Phase.PAPER, strategy=StrategyName.MEAN_REVERSION, cell="fUSD_a30",
        account_id="default",
    )
    await chain.evaluate(_post(), _ctx())
    assert "safety_chain" in probe.last_active_ts


@pytest.mark.asyncio
async def test_chain_internal_exception_fail_closed() -> None:
    diagnostics = _EventCapture()
    chain = SafetyGuardChain(
        guards=[_CrashGuard()], probe=HealthProbe(), diagnostics=diagnostics,
        phase=Phase.PAPER, strategy=StrategyName.MEAN_REVERSION, cell="fUSD_a30",
        account_id="default",
    )
    r = await chain.evaluate(_post(), _ctx())
    assert r.allowed is False
    # safety_trigger emitted with critical level + guard_internal_error reason
    triggers = [e for e in diagnostics.events if e["event_type"] == EventType.SAFETY_TRIGGER.value]
    assert len(triggers) == 1
    assert triggers[0]["level"] == "critical"
    assert "guard_internal_error" in triggers[0]["payload"]["reason"]


@pytest.mark.asyncio
async def test_chain_eval_timeout_fail_closed() -> None:
    diagnostics = _EventCapture()
    chain = SafetyGuardChain(
        guards=[_HangGuard()], probe=HealthProbe(), diagnostics=diagnostics,
        phase=Phase.PAPER, strategy=StrategyName.MEAN_REVERSION, cell="fUSD_a30",
        account_id="default",
    )
    r = await chain.evaluate(_post(), _ctx())
    assert r.allowed is False
    triggers = [e for e in diagnostics.events if e["event_type"] == EventType.SAFETY_TRIGGER.value]
    assert len(triggers) == 1
    assert "eval_timeout" in triggers[0]["payload"]["reason"]


@pytest.mark.asyncio
async def test_chain_empty_guards_allows() -> None:
    chain = SafetyGuardChain(
        guards=[], probe=HealthProbe(), diagnostics=_EventCapture(),
        phase=Phase.PAPER, strategy=StrategyName.MEAN_REVERSION, cell="fUSD_a30",
        account_id="default",
    )
    r = await chain.evaluate(_post(), _ctx())
    assert r.allowed is True


@pytest.mark.asyncio
async def test_chain_block_emits_safety_trigger() -> None:
    diagnostics = _EventCapture()
    chain = SafetyGuardChain(
        guards=[_BlockGuard("cap")], probe=HealthProbe(), diagnostics=diagnostics,
        phase=Phase.PAPER, strategy=StrategyName.MEAN_REVERSION, cell="fUSD_a30",
        account_id="default",
    )
    await chain.evaluate(_post(), _ctx())
    triggers = [e for e in diagnostics.events if e["event_type"] == EventType.SAFETY_TRIGGER.value]
    assert len(triggers) == 1
    assert triggers[0]["payload"]["guard_name"] == "cap"
    assert triggers[0]["level"] == "warn"  # hard block = warn; critical only for internal error
