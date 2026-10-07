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
    probe.record_heartbeat("ws_data")
    probe.record_heartbeat("db")
    g = HeartbeatGuard(probe=probe, watched_sub_tasks=["ws_data", "db"])
    r = await g.evaluate(_post(), _ctx())
    assert r.allowed is True


@pytest.mark.asyncio
async def test_heartbeat_blocks_when_any_stale() -> None:
    probe = HealthProbe()
    probe.last_active_ts["ws_data"] = datetime.now(UTC)
    probe.last_active_ts["db"] = datetime.now(UTC) - timedelta(seconds=7 * 60 + 60)
    g = HeartbeatGuard(probe=probe, watched_sub_tasks=["ws_data", "db"])
    r = await g.evaluate(_post(), _ctx())
    assert r.allowed is False
    assert "db" in (r.reason or "")


@pytest.mark.asyncio
async def test_heartbeat_blocks_until_the_first_beat_since_boot() -> None:
    """Fail-closed: a dependency never seen since boot has not proven it answers.
    The first beat lifts the block; nothing else is waited for."""
    probe = HealthProbe()
    g = HeartbeatGuard(probe=probe, watched_sub_tasks=["ws_data"])
    blocked = await g.evaluate(_post(), _ctx())
    assert blocked.allowed is False
    assert blocked.reason == "sub_task=ws_data never recorded since boot"

    probe.record_heartbeat("ws_data")
    assert (await g.evaluate(_post(), _ctx())).allowed is True


@pytest.mark.asyncio
async def test_heartbeat_edge_at_exactly_threshold() -> None:
    probe = HealthProbe()
    probe.last_active_ts["ws_data"] = datetime.now(UTC) - timedelta(seconds=90)
    g = HeartbeatGuard(probe=probe, watched_sub_tasks=["ws_data"])
    r = await g.evaluate(_post(), _ctx())
    # Exactly at threshold = still allowed; strictly greater blocks.
    assert r.allowed is True


def test_heartbeat_watches_dependency_freshness_only() -> None:
    with pytest.raises(ValueError, match="dependency freshness only"):
        HeartbeatGuard(probe=HealthProbe(), watched_sub_tasks=["executor"])


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
