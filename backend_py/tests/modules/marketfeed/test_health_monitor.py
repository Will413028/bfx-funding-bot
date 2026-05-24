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
    monitor = HealthMonitor(phase=Phase.PAPER, event_sink=axiom, probe=probe,
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
    monitor = HealthMonitor(phase=Phase.PAPER, event_sink=axiom, probe=probe,
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


class TestCurrentStatus:
    """Bug B fix (5/20): daemon needs to detect HEALTHY transitions without
    spamming update() every poll tick."""

    def test_returns_none_when_target_never_set(self):
        probe = HealthProbe()
        assert probe.current_status(HealthTarget.BITFINEX_WS) is None

    def test_returns_status_after_update(self):
        probe = HealthProbe()
        probe.update(HealthTarget.BITFINEX_WS, HealthStatus.DEGRADED,
                     last_msg_age_ms=999_999, reconnect_count_last_hour=1,
                     error_message="ws_disconnect")
        assert probe.current_status(HealthTarget.BITFINEX_WS) == HealthStatus.DEGRADED

    def test_reflects_last_transition(self):
        probe = HealthProbe()
        probe.update(HealthTarget.BITFINEX_WS, HealthStatus.DEGRADED,
                     last_msg_age_ms=999_999, reconnect_count_last_hour=1,
                     error_message="ws_disconnect")
        probe.update(HealthTarget.BITFINEX_WS, HealthStatus.HEALTHY,
                     last_msg_age_ms=200, reconnect_count_last_hour=1)
        assert probe.current_status(HealthTarget.BITFINEX_WS) == HealthStatus.HEALTHY


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
            event_sink=fake_axiom,
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

    # ── Phase 4.3 Task 5: scan_staleness carve-out ───────────────────────────

    async def test_no_fatal_escalation_for_signal_pipeline_stale_exceeded(
        self, monitor, fake_axiom,
    ) -> None:
        """SIGNAL_PIPELINE DEGRADED (reason=stale_exceeded) never raises FatalError.

        Simulates the case where a p30 cell has been marked DEGRADED by the
        daemon LOCF emit path (Task 4) and scan_staleness is called. Per spec
        Phase 4.3 carve-out, stale_exceeded is expected sparseness — the
        monitor must stay DEGRADED and NEVER escalate to fatal, even if the
        cell has been degraded for a long time.
        """
        pair_id = "rate_percentile:fUSD_p30"
        monitor.probe.set_cell_pipeline_status(pair_id, HealthStatus.DEGRADED)

        # No exception should be raised — even though DEGRADED is present
        result = await monitor.scan_staleness()

        # Record appears in stale list with SIGNAL_PIPELINE prefix, never "down"
        assert len(result) == 1
        rec = result[0]
        assert rec["sub_task"] == f"SIGNAL_PIPELINE:{pair_id}"
        assert rec["severity"] == "degraded"
        assert rec["age_s"] is None

        # Observability emit happened (WARN, not ERROR/FATAL)
        assert len(fake_axiom.emitted) == 1
        emitted = fake_axiom.emitted[0]
        assert emitted["level"] == "warn"
        assert emitted["payload"]["check_target"] == "signal_pipeline"
        assert emitted["payload"]["status"] == "degraded"
        assert pair_id in emitted["payload"]["error_message"]
        assert "not escalating" in emitted["payload"]["error_message"]

    async def test_fatal_escalation_for_other_reasons_unchanged(
        self, monitor,
    ) -> None:
        """Phase 4.2.0 D4 behavior preserved: non-SIGNAL_PIPELINE targets still
        raise FatalError at 3× threshold.

        SIGNAL_PIPELINE DEGRADED is present simultaneously — it must NOT
        prevent the fatal raise for the other sub-task (candle_writer here).
        """
        pair_id = "rate_percentile:fUSD_p30"
        monitor.probe.set_cell_pipeline_status(pair_id, HealthStatus.DEGRADED)

        # candle_writer threshold = 65*60 s; 3× = 195*60 s → use 200*60 s
        monitor.probe.last_active_ts["candle_writer"] = (
            datetime.now(UTC) - timedelta(seconds=200 * 60)
        )

        with pytest.raises(FatalError) as exc:
            await monitor.scan_staleness()

        assert "candle_writer" in str(exc.value)

    async def test_signal_pipeline_down_also_emits_warn_no_fatal(
        self, monitor, fake_axiom,
    ) -> None:
        """SIGNAL_PIPELINE DOWN (future-proofing) also emits without escalating fatal.

        Today's daemon LOCF path only sets DEGRADED, but the guard accommodates DOWN
        for forward-compat (cell permanent disqualification or similar).
        """
        pair_id = "rate_percentile:fUSD_p30"
        monitor.probe.set_cell_pipeline_status(pair_id, HealthStatus.DOWN)

        # No exception should be raised — spec carve-out covers both DEGRADED and DOWN
        result = await monitor.scan_staleness()

        assert len(result) == 1
        rec = result[0]
        assert rec["sub_task"] == f"SIGNAL_PIPELINE:{pair_id}"
        assert rec["severity"] == "down"
        assert rec["age_s"] is None

        # Observability emit happened (WARN, not ERROR/FATAL)
        assert len(fake_axiom.emitted) == 1
        emitted = fake_axiom.emitted[0]
        assert emitted["level"] == "warn"
        assert emitted["payload"]["check_target"] == "signal_pipeline"
        assert emitted["payload"]["status"] == "down"
        assert pair_id in emitted["payload"]["error_message"]
        assert "not escalating" in emitted["payload"]["error_message"]


class TestSubTaskThresholds:
    def test_thresholds_defined_for_all_subtasks(self):
        """Per spec D4 heartbeat threshold table. Phase 3c T10: axiom removed."""
        for name in ("ws", "candle_writer", "scheduler", "health_check", "db_keepalive"):
            assert name in SUB_TASK_THRESHOLDS
        assert "axiom" not in SUB_TASK_THRESHOLDS

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
        # Phase 3c T10: "axiom" removed from SUB_TASK_THRESHOLDS (AxiomClient deleted).
        # Unknown sub-tasks fall back to _DEFAULT_THRESHOLD_S (60s).
        assert SUB_TASK_THRESHOLDS["health_check"] == 6 * 60
        assert SUB_TASK_THRESHOLDS["db_keepalive"] == 7 * 60
