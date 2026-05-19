from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest

from bfx_funding_bot.core.errors import FatalError
from bfx_funding_bot.modules.marketfeed.health_monitor import (
    SUB_TASK_THRESHOLDS,
    HealthMonitor,
    HealthProbe,
)
from bfx_funding_bot.modules.marketfeed.schemas import (
    HealthStatus,
    HealthTarget,
    Phase,
)


async def test_state_change_emits_immediately():
    axiom = AsyncMock()
    probe = HealthProbe()
    monitor = HealthMonitor(phase=Phase.PAPER, axiom=axiom, probe=probe,
                            heartbeat_interval_s=10.0)
    await monitor.start()
    probe.update(HealthTarget.BITFINEX_WS, HealthStatus.HEALTHY,
                 last_msg_age_ms=100, reconnect_count_last_hour=0)
    await asyncio.sleep(0.1)
    probe.update(HealthTarget.BITFINEX_WS, HealthStatus.DEGRADED,
                 last_msg_age_ms=400_000, reconnect_count_last_hour=1,
                 error_message="hb_timeout")
    await asyncio.sleep(0.2)
    await monitor.stop()

    statuses = [
        call.args[0]["payload"]["status"]
        for call in axiom.emit.call_args_list
    ]
    assert HealthStatus.HEALTHY.value in statuses
    assert HealthStatus.DEGRADED.value in statuses


async def test_heartbeat_emits_periodically():
    axiom = AsyncMock()
    probe = HealthProbe()
    probe.update(HealthTarget.BITFINEX_WS, HealthStatus.HEALTHY,
                 last_msg_age_ms=100, reconnect_count_last_hour=0)
    monitor = HealthMonitor(phase=Phase.PAPER, axiom=axiom, probe=probe,
                            heartbeat_interval_s=0.1)
    await monitor.start()
    await asyncio.sleep(0.35)
    await monitor.stop()

    assert axiom.emit.call_count >= 2  # multiple heartbeats fired


class TestHeartbeatRegistry:
    def test_record_heartbeat_updates_last_active_ts(self):
        probe = HealthProbe()
        before = datetime.now(UTC)
        probe.record_heartbeat("candle_writer")
        after = datetime.now(UTC)
        ts = probe.last_active_ts["candle_writer"]
        assert before <= ts <= after

    def test_record_heartbeat_overwrites(self):
        probe = HealthProbe()
        probe.record_heartbeat("candle_writer")
        first = probe.last_active_ts["candle_writer"]
        time.sleep(0.01)
        probe.record_heartbeat("candle_writer")
        second = probe.last_active_ts["candle_writer"]
        assert second > first


class TestStalenessScan:
    @pytest.fixture
    def fake_axiom(self):
        class _Fake:
            def __init__(self): self.emitted = []
            async def emit(self, event): self.emitted.append(event)
        return _Fake()

    @pytest.fixture
    def monitor(self, fake_axiom):
        probe = HealthProbe()
        return HealthMonitor(
            phase=Phase.SHADOW,
            axiom=fake_axiom,
            probe=probe,
            heartbeat_interval_s=300.0,
        )

    async def test_no_emit_when_fresh(self, monitor):
        monitor.probe.record_heartbeat("candle_writer")
        result = await monitor.scan_staleness()
        assert result == []  # no stale tasks

    async def test_no_emit_when_empty_registry(self, monitor):
        """scan_staleness on empty last_active_ts → returns [] without crashing."""
        assert monitor.probe.last_active_ts == {}
        result = await monitor.scan_staleness()
        assert result == []

    async def test_emit_degraded_when_over_threshold(self, monitor, fake_axiom):
        # axiom threshold 60s → 70s > threshold but < 2× = degraded
        monitor.probe.last_active_ts["axiom"] = (
            datetime.now(UTC) - timedelta(seconds=70)
        )
        result = await monitor.scan_staleness()
        assert len(result) == 1
        rec = result[0]
        assert rec["sub_task"] == "axiom"
        assert rec["severity"] == "degraded"
        assert len(fake_axiom.emitted) == 1
        emitted = fake_axiom.emitted[0]
        assert emitted["event_type"] == "health_check"
        assert emitted["payload"]["check_target"] == "axiom"
        assert emitted["payload"]["status"] == "degraded"

    async def test_emit_down_when_over_2x_threshold(self, monitor):
        # axiom threshold 60s → 130s > 2× (120s) but < 3× (180s) = down
        monitor.probe.last_active_ts["axiom"] = (
            datetime.now(UTC) - timedelta(seconds=130)
        )
        result = await monitor.scan_staleness()
        assert result[0]["severity"] == "down"

    async def test_fatal_escalation_at_3x_threshold(self, monitor, fake_axiom):
        # axiom threshold 60s → 190s > 3× (180s) = fatal
        monitor.probe.last_active_ts["axiom"] = (
            datetime.now(UTC) - timedelta(seconds=190)
        )
        with pytest.raises(FatalError) as exc:
            await monitor.scan_staleness()
        assert "axiom" in str(exc.value)
        assert "190" in str(exc.value) or "stale" in str(exc.value).lower()
        # Verify emit happened before raise — axiom has the down event
        assert len(fake_axiom.emitted) == 1
        assert fake_axiom.emitted[0]["payload"]["check_target"] == "axiom"
        assert fake_axiom.emitted[0]["payload"]["status"] == "down"  # 190s > 2×60s

    async def test_unknown_subtask_uses_default_threshold(self, monitor):
        """No registered threshold → treat as default (60s)."""
        monitor.probe.last_active_ts["custom_task"] = (
            datetime.now(UTC) - timedelta(seconds=70)
        )
        result = await monitor.scan_staleness()
        assert len(result) == 1
        assert result[0]["sub_task"] == "custom_task"


class TestSubTaskThresholds:
    def test_thresholds_defined_for_all_subtasks(self):
        """Per spec D4 heartbeat threshold table."""
        for name in ("ws", "candle_writer", "scheduler", "axiom", "health_check", "db_keepalive"):
            assert name in SUB_TASK_THRESHOLDS

    def test_threshold_values_match_spec(self):
        # Post-shadow-run lessons:
        # - v1 (d90363fa): 1h funding cells silently publish only on tick →
        #   raise candle_writer to 65min, ws stays as fast detector.
        # - v2 (136fbd07): candle channel yield != ws frames (hb returns early
        #   in _handle_raw). ws heartbeat now polled from ws_client.last_msg_age_ms()
        #   every 15s; threshold 90s covers 4-5 missed hb frames.
        assert SUB_TASK_THRESHOLDS["ws"] == 90
        assert SUB_TASK_THRESHOLDS["candle_writer"] == 65 * 60
        assert SUB_TASK_THRESHOLDS["scheduler"] == 65 * 60
        assert SUB_TASK_THRESHOLDS["axiom"] == 60
        assert SUB_TASK_THRESHOLDS["health_check"] == 6 * 60
        assert SUB_TASK_THRESHOLDS["db_keepalive"] == 7 * 60
