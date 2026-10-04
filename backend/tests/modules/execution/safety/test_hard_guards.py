"""Hard guards: ManualKill + AuthHealth + Heartbeat + WriterLock."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest

from bfx_funding_bot.core.health import HealthProbe
from bfx_funding_bot.core.telemetry import HealthStatus, HealthTarget
from bfx_funding_bot.modules.execution.protocols import (
    AccountContext,
    Credentials,
)
from bfx_funding_bot.modules.execution.safety.hard_guards import (
    AuthHealthGuard,
    HeartbeatGuard,
    ManualKillGuard,
    WriterLockGuard,
)
from bfx_funding_bot.modules.strategy import DecisionOutcome, DecisionPayload


def _ctx() -> AccountContext:
    return AccountContext("default", Credentials("k", "s"), Decimal("500"))


def _post(symbol: str = "fUST") -> DecisionPayload:
    return DecisionPayload(
        decision_outcome=DecisionOutcome.POST,
        signal_correlation_id=uuid4(),
        offer_rate=0.0001, offer_amount_usdt=100.0, offer_duration_days=2,
        symbol=symbol,
    )


@pytest.mark.asyncio
async def test_manual_kill_allows_when_flag_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    g = ManualKillGuard()
    r = await g.evaluate(_post(), _ctx())
    assert r.allowed is True


@pytest.mark.asyncio
async def test_manual_kill_blocks_while_a_protection_is_pending(monkeypatch: pytest.MonkeyPatch) -> None:
    g = ManualKillGuard(pending_stop=lambda: "loss_limiter")
    r = await g.evaluate(_post(), _ctx())
    assert r.allowed is False
    assert r.guard_name == "manual_kill"


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


class _FakeLock:
    def __init__(self, held: bool) -> None:
        self._held = held

    async def verify_held(self) -> bool:
        return self._held


@pytest.mark.asyncio
async def test_writer_lock_guard_blocks_when_lock_lost() -> None:
    guard = WriterLockGuard(lock=_FakeLock(held=False))
    r = await guard.evaluate(_post(), _ctx())
    assert r.allowed is False
    assert r.guard_name == "writer_lock"
    assert r.reason is not None


@pytest.mark.asyncio
async def test_writer_lock_guard_allows_when_held() -> None:
    guard = WriterLockGuard(lock=_FakeLock(held=True))
    r = await guard.evaluate(_post(), _ctx())
    assert r.allowed is True
    assert r.guard_name == "writer_lock"
