"""Hard guards: ManualKill + AuthHealth + Heartbeat (first 3 of 4)."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest

from bfx_funding_bot.modules.execution.protocols import (
    AccountContext,
    Credentials,
)
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
    HealthStatus,
    HealthTarget,
)


def _ctx() -> AccountContext:
    return AccountContext("default", Credentials("k", "s"), Decimal("500"))


def _post() -> DecisionPayload:
    return DecisionPayload(
        decision_outcome=DecisionOutcome.POST,
        signal_correlation_id=uuid4(),
        offer_rate=0.0001, offer_amount_usdt=100.0, offer_duration_days=2,
    )


@pytest.mark.asyncio
async def test_manual_kill_allows_when_flag_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BFX_KILL_SWITCH", raising=False)
    g = ManualKillGuard()
    r = await g.evaluate(_post(), _ctx())
    assert r.allowed is True


@pytest.mark.asyncio
async def test_manual_kill_blocks_when_flag_true(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BFX_KILL_SWITCH", "true")
    g = ManualKillGuard()
    r = await g.evaluate(_post(), _ctx())
    assert r.allowed is False
    assert r.guard_name == "manual_kill"


@pytest.mark.asyncio
async def test_manual_kill_is_not_calibrated() -> None:
    assert ManualKillGuard().is_calibrated is False


@pytest.mark.asyncio
async def test_auth_health_allows_when_executor_healthy() -> None:
    probe = HealthProbe()
    probe.update(HealthTarget.EXECUTOR, HealthStatus.HEALTHY)
    g = AuthHealthGuard(probe=probe)
    r = await g.evaluate(_post(), _ctx())
    assert r.allowed is True


@pytest.mark.asyncio
async def test_auth_health_blocks_when_executor_down() -> None:
    probe = HealthProbe()
    probe.update(HealthTarget.EXECUTOR, HealthStatus.DOWN, error_message="x")
    g = AuthHealthGuard(probe=probe)
    r = await g.evaluate(_post(), _ctx())
    assert r.allowed is False
    assert "executor" in (r.reason or "")


@pytest.mark.asyncio
async def test_auth_health_allows_when_target_never_set() -> None:
    # Day-1: executor target may not have been updated yet — allow by default.
    probe = HealthProbe()
    g = AuthHealthGuard(probe=probe)
    r = await g.evaluate(_post(), _ctx())
    assert r.allowed is True


@pytest.mark.asyncio
async def test_heartbeat_allows_when_all_fresh() -> None:
    probe = HealthProbe()
    probe.record_heartbeat("safety_chain")
    probe.record_heartbeat("executor")
    g = HeartbeatGuard(probe=probe, threshold_seconds=300,
                       watched_sub_tasks=["safety_chain", "executor"])
    r = await g.evaluate(_post(), _ctx())
    assert r.allowed is True


@pytest.mark.asyncio
async def test_heartbeat_blocks_when_any_stale() -> None:
    probe = HealthProbe()
    probe.last_active_ts["safety_chain"] = datetime.now(UTC)
    probe.last_active_ts["executor"] = datetime.now(UTC) - timedelta(seconds=400)
    g = HeartbeatGuard(probe=probe, threshold_seconds=300,
                       watched_sub_tasks=["safety_chain", "executor"])
    r = await g.evaluate(_post(), _ctx())
    assert r.allowed is False
    assert "executor" in (r.reason or "")


@pytest.mark.asyncio
async def test_heartbeat_allows_when_target_never_recorded() -> None:
    # Day-1 boot: heartbeat dict may not have the key yet.
    probe = HealthProbe()
    g = HeartbeatGuard(probe=probe, threshold_seconds=300,
                       watched_sub_tasks=["safety_chain"])
    r = await g.evaluate(_post(), _ctx())
    assert r.allowed is True


@pytest.mark.asyncio
async def test_heartbeat_edge_at_exactly_threshold() -> None:
    probe = HealthProbe()
    probe.last_active_ts["x"] = datetime.now(UTC) - timedelta(seconds=300)
    g = HeartbeatGuard(probe=probe, threshold_seconds=300,
                       watched_sub_tasks=["x"])
    r = await g.evaluate(_post(), _ctx())
    # Exactly at threshold = still allowed; strictly greater blocks.
    assert r.allowed is True


class _FakeLedger:
    def __init__(self, exposure: Decimal) -> None:
        self._exposure = exposure

    def current_exposure(self) -> Decimal:
        return self._exposure


@pytest.mark.asyncio
async def test_allocation_cap_allows_under_cap() -> None:
    ctx = AccountContext("default", Credentials("k", "s"), Decimal("500"))
    ledger = _FakeLedger(Decimal("100"))
    g = AllocationCapGuard(ledger=ledger)
    decision = _post()  # offer_amount_usdt=100
    r = await g.evaluate(decision, ctx)
    assert r.allowed is True


@pytest.mark.asyncio
async def test_allocation_cap_blocks_over_cap() -> None:
    ctx = AccountContext("default", Credentials("k", "s"), Decimal("500"))
    ledger = _FakeLedger(Decimal("450"))
    g = AllocationCapGuard(ledger=ledger)
    decision = _post()  # 100 → 450+100=550 > 500
    r = await g.evaluate(decision, ctx)
    assert r.allowed is False
    assert "cap" in (r.reason or "")


@pytest.mark.asyncio
async def test_allocation_cap_edge_at_exactly_cap() -> None:
    ctx = AccountContext("default", Credentials("k", "s"), Decimal("500"))
    ledger = _FakeLedger(Decimal("400"))  # 400 + 100 = 500 (exactly)
    g = AllocationCapGuard(ledger=ledger)
    r = await g.evaluate(_post(), ctx)
    # Exactly at cap = allowed; strictly over blocks.
    assert r.allowed is True


@pytest.mark.asyncio
async def test_allocation_cap_skip_decision_always_allowed() -> None:
    ctx = AccountContext("default", Credentials("k", "s"), Decimal("100"))
    ledger = _FakeLedger(Decimal("99999"))
    g = AllocationCapGuard(ledger=ledger)
    skip = DecisionPayload(
        decision_outcome=DecisionOutcome.SKIP,
        signal_correlation_id=uuid4(),
        skip_reason="below_threshold",
    )
    r = await g.evaluate(skip, ctx)
    assert r.allowed is True  # SKIP decisions never consume cap
