from __future__ import annotations

import asyncio
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from bfx_funding_bot.modules.candles.schemas import FundingCandle
from bfx_funding_bot.modules.candles.service import reindex_and_ffill
from bfx_funding_bot.modules.marketfeed.config import CellConfig
from bfx_funding_bot.modules.marketfeed.health_monitor import HealthProbe
from bfx_funding_bot.modules.marketfeed.scheduler import (
    Scheduler,
    next_candle_close_mts,
    now_ms_utc,
)
from bfx_funding_bot.modules.marketfeed.schemas import (
    EventType,
    HealthStatus,
    HealthTarget,
    Level,
    Phase,
)
from bfx_funding_bot.modules.marketfeed.signal_engine import SignalEngine
from bfx_funding_bot.modules.marketfeed.strategy_registry import (
    StrategyRegistry,
    build_strategy,
)

# ── base reference time (aligned to 1h boundary) ──────────────────────────────
_REF_MTS = 1747584000000  # 2025-05-18 12:00:00 UTC
_1H_MS = 3_600_000


def _cell(staleness_budget_hours: int = 12) -> CellConfig:
    """p30 cell with resolved staleness_budget_hours (simulates load_config())."""
    cell = CellConfig.model_validate({
        "strategy": "rate_percentile",
        "symbol": "fUSD",
        "period_agg": "p30",
        "timeframe": "1h",
        "params": {"percentile": 75, "lookback_hours": 5},
        "reference_amount_usdt": 150.0,
    })
    cell.staleness_budget_hours = staleness_budget_hours
    return cell


def _candle(mts: int) -> FundingCandle:
    return FundingCandle(
        symbol="fUSD", timeframe="1h", period_agg="p30",
        mts=mts,
        open=Decimal("0.0001"), close=Decimal("0.0001"),
        high=Decimal("0.0001"), low=Decimal("0.0001"),
        volume=Decimal("100"),
    )


def _make_signal_engine(axiom: MagicMock, cell: CellConfig) -> SignalEngine:
    """Build SignalEngine with pre-warmed strategy in registry."""
    candles_repo = MagicMock()
    # Return a minimal history so divergence check doesn't crash
    candles_repo.get_up_to = AsyncMock(return_value=[_candle(_REF_MTS)])
    return SignalEngine(phase=Phase.PAPER, axiom=axiom, candles_repo=candles_repo)


def _make_registry(cell: CellConfig) -> StrategyRegistry:
    reg = StrategyRegistry()
    strategy = build_strategy(cell)
    # Pre-warm with enough history so signal can be extracted
    for i in range(6):
        strategy.observe(_candle(_REF_MTS - (6 - i) * _1H_MS))
    reg.put(cell, strategy)
    return reg


def _make_tick_handler(
    axiom: MagicMock,
    raw_candles_provider: list[FundingCandle],
    cell: CellConfig,
) -> tuple[dict[str, Any], Any]:
    """
    Build a minimal on_scheduler_tick closure that replicates daemon.py logic:
    - get_up_to returns raw_candles_provider
    - LOCF fill → tier check → emit
    Returns (_cell_pipeline_status dict, tick coroutine function).
    """
    engine = _make_signal_engine(axiom, cell)
    registry = _make_registry(cell)
    _cell_pipeline_status: dict[str, HealthStatus] = {}

    async def on_scheduler_tick(cell: CellConfig, mts: int) -> None:
        from datetime import UTC, datetime
        from uuid import uuid4

        from bfx_funding_bot.modules.marketfeed.scheduler import _TIMEFRAME_MS

        candle_mts = mts - _TIMEFRAME_MS[cell.timeframe]
        budget_hours: int = cell.staleness_budget_hours

        raw_candles = raw_candles_provider

        filled = reindex_and_ffill(
            raw_candles, ref_mts=candle_mts, max_gap_hours=budget_hours,
        )

        if not filled:
            budget_seconds = budget_hours * 3600
            prev = _cell_pipeline_status.get(cell.pair_id)
            if prev != HealthStatus.DEGRADED:
                _cell_pipeline_status[cell.pair_id] = HealthStatus.DEGRADED
                await axiom.emit({
                    "timestamp": datetime.now(UTC).isoformat(),
                    "level": Level.WARN.value,
                    "phase": Phase.PAPER.value,
                    "strategy": None,
                    "cell": cell.cell_id,
                    "event_type": EventType.HEALTH_CHECK.value,
                    "correlation_id": str(uuid4()),
                    "payload": {
                        "check_target": HealthTarget.SIGNAL_PIPELINE.value,
                        "status": HealthStatus.DEGRADED.value,
                        "error_message": "stale_exceeded: no candles in lookback window",
                        "reason": "stale_exceeded",
                        "stale_seconds": budget_seconds + 3600,
                        "budget_seconds": budget_seconds,
                    },
                })
            return

        latest = filled[-1]

        if latest.candle is None:
            budget_seconds = budget_hours * 3600
            prev = _cell_pipeline_status.get(cell.pair_id)
            if prev != HealthStatus.DEGRADED:
                _cell_pipeline_status[cell.pair_id] = HealthStatus.DEGRADED
                await axiom.emit({
                    "timestamp": datetime.now(UTC).isoformat(),
                    "level": Level.WARN.value,
                    "phase": Phase.PAPER.value,
                    "strategy": None,
                    "cell": cell.cell_id,
                    "event_type": EventType.HEALTH_CHECK.value,
                    "correlation_id": str(uuid4()),
                    "payload": {
                        "check_target": HealthTarget.SIGNAL_PIPELINE.value,
                        "status": HealthStatus.DEGRADED.value,
                        "error_message": "stale_exceeded",
                        "reason": "stale_exceeded",
                        "stale_seconds": latest.stale_seconds,
                        "budget_seconds": budget_seconds,
                    },
                })
            return

        prev = _cell_pipeline_status.get(cell.pair_id)
        if prev == HealthStatus.DEGRADED:
            _cell_pipeline_status[cell.pair_id] = HealthStatus.HEALTHY
            await axiom.emit({
                "timestamp": datetime.now(UTC).isoformat(),
                "level": Level.INFO.value,
                "phase": Phase.PAPER.value,
                "strategy": None,
                "cell": cell.cell_id,
                "event_type": EventType.HEALTH_CHECK.value,
                "correlation_id": str(uuid4()),
                "payload": {
                    "check_target": HealthTarget.SIGNAL_PIPELINE.value,
                    "status": HealthStatus.HEALTHY.value,
                },
            })
        else:
            _cell_pipeline_status[cell.pair_id] = HealthStatus.HEALTHY

        await engine.process_candle(
            cell=cell,
            candle=latest.candle,
            registry=registry,
            is_stale=latest.is_stale,
            stale_seconds=latest.stale_seconds,
        )

    return _cell_pipeline_status, on_scheduler_tick


# ── existing scheduler tests ──────────────────────────────────────────────────

def test_next_candle_close_aligns_to_timeframe():
    now_ms = 1747584000000 + 1234  # just past hour mark
    nxt = next_candle_close_mts(timeframe="1h", now_ms=now_ms)
    assert nxt == 1747584000000 + 3600_000


def test_next_candle_close_30m():
    now_ms = 1747584000000 + 60_000  # 1 minute past hour
    nxt = next_candle_close_mts(timeframe="30m", now_ms=now_ms)
    assert nxt == 1747584000000 + 30 * 60_000


async def test_scheduler_fires_callback_after_timeframe_plus_buffer():
    cell = _cell()
    callbacks: list[int] = []

    async def cb(c: CellConfig, mts: int) -> None:
        callbacks.append(mts)

    sched = Scheduler(callback=cb, probe=HealthProbe(), buffer_s=0.05)
    # Schedule fire at "now" — buffer 0.05s means it fires almost immediately
    sched.register(cell, fire_at_mts=now_ms_utc() - 100)

    await sched.start()
    await asyncio.sleep(0.3)
    await sched.stop()

    assert len(callbacks) >= 1


# ── Phase 4.3 LOCF: scheduler both-tier emit tests ───────────────────────────
# mts fired by scheduler = _REF_MTS + 1h (boundary T+1);
# candle_mts inside tick = T+1 - 1h = _REF_MTS (the just-closed candle).

_TICK_MTS = _REF_MTS + _1H_MS  # boundary T that fires the tick


async def test_emit_signal_with_fresh_candle() -> None:
    """Test 4.1: fresh candle at candle_mts → signal with is_stale=False, stale_seconds=0."""
    cell = _cell(staleness_budget_hours=12)
    # candle_mts will be _TICK_MTS - 1h = _REF_MTS
    candle_mts = _TICK_MTS - _1H_MS  # = _REF_MTS
    raw = [_candle(candle_mts)]  # exactly at candle_mts → fresh

    captured: list[dict[str, Any]] = []
    axiom = MagicMock()
    axiom.emit = AsyncMock(side_effect=lambda e: captured.append(e))

    _, tick = _make_tick_handler(axiom, raw, cell)
    await tick(cell, _TICK_MTS)

    signal_events = [e for e in captured if e["event_type"] == EventType.SIGNAL.value]
    assert len(signal_events) >= 1, "Expected at least one signal event"
    sig = signal_events[0]
    assert sig["payload"]["is_stale"] is False
    assert sig["payload"]["stale_seconds"] == 0
    assert sig["payload"]["budget_seconds"] == 12 * 3600

    health_events = [e for e in captured if e["event_type"] == EventType.HEALTH_CHECK.value]
    assert health_events == [], "No health check events expected for fresh candle"


async def test_emit_signal_with_stale_within_budget() -> None:
    """Test 4.2: 6h gap, budget=12h → signal with is_stale=True, stale_seconds=21600."""
    cell = _cell(staleness_budget_hours=12)
    candle_mts = _TICK_MTS - _1H_MS  # = _REF_MTS
    # Candle is 6h before candle_mts → soft tier (6h < 12h budget)
    gap_hours = 6
    raw = [_candle(candle_mts - gap_hours * _1H_MS)]

    captured: list[dict[str, Any]] = []
    axiom = MagicMock()
    axiom.emit = AsyncMock(side_effect=lambda e: captured.append(e))

    _, tick = _make_tick_handler(axiom, raw, cell)
    await tick(cell, _TICK_MTS)

    signal_events = [e for e in captured if e["event_type"] == EventType.SIGNAL.value]
    assert len(signal_events) >= 1, "Expected signal event for soft-tier stale candle"
    sig = signal_events[0]
    assert sig["payload"]["is_stale"] is True
    assert sig["payload"]["stale_seconds"] == gap_hours * 3600

    health_events = [e for e in captured if e["event_type"] == EventType.HEALTH_CHECK.value]
    assert health_events == [], "No DEGRADED expected within budget"


async def test_emit_degraded_when_stale_exceeded() -> None:
    """Test 4.3: 18h gap, budget=12h → DEGRADED health check; no signal emit."""
    cell = _cell(staleness_budget_hours=12)
    candle_mts = _TICK_MTS - _1H_MS  # = _REF_MTS
    # Candle is 18h before candle_mts → hard tier (18h > 12h budget)
    gap_hours = 18
    raw = [_candle(candle_mts - gap_hours * _1H_MS)]

    captured: list[dict[str, Any]] = []
    axiom = MagicMock()
    axiom.emit = AsyncMock(side_effect=lambda e: captured.append(e))

    _, tick = _make_tick_handler(axiom, raw, cell)
    await tick(cell, _TICK_MTS)

    # No signal events
    signal_events = [e for e in captured if e["event_type"] == EventType.SIGNAL.value]
    assert signal_events == [], "No signal expected for hard-tier stale"

    # Exactly one DEGRADED health check
    health_events = [e for e in captured if e["event_type"] == EventType.HEALTH_CHECK.value]
    assert len(health_events) == 1
    h = health_events[0]
    assert h["level"] == Level.WARN.value
    assert h["payload"]["status"] == HealthStatus.DEGRADED.value
    assert h["payload"]["check_target"] == HealthTarget.SIGNAL_PIPELINE.value
    assert h["payload"]["reason"] == "stale_exceeded"
    assert h["payload"]["stale_seconds"] == gap_hours * 3600
    assert h["payload"]["budget_seconds"] == 12 * 3600


async def test_no_duplicate_degraded_on_subsequent_stale_ticks() -> None:
    """Test 4.4: cell DEGRADED on tick 1; tick 2 still stale → NO duplicate DEGRADED emit."""
    cell = _cell(staleness_budget_hours=12)
    candle_mts = _TICK_MTS - _1H_MS
    gap_hours = 18
    raw = [_candle(candle_mts - gap_hours * _1H_MS)]

    captured: list[dict[str, Any]] = []
    axiom = MagicMock()
    axiom.emit = AsyncMock(side_effect=lambda e: captured.append(e))

    status_dict, tick = _make_tick_handler(axiom, raw, cell)

    # Tick 1 — transitions to DEGRADED
    await tick(cell, _TICK_MTS)
    assert status_dict.get(cell.pair_id) == HealthStatus.DEGRADED
    health_after_tick1 = [e for e in captured if e["event_type"] == EventType.HEALTH_CHECK.value]
    assert len(health_after_tick1) == 1, "Tick 1 must emit exactly one DEGRADED"

    # Tick 2 — still stale, should NOT emit another DEGRADED
    captured_before_tick2 = len(captured)
    await tick(cell, _TICK_MTS + _1H_MS)
    new_events = captured[captured_before_tick2:]
    new_health = [e for e in new_events if e["event_type"] == EventType.HEALTH_CHECK.value]
    assert new_health == [], "Tick 2 must not emit duplicate DEGRADED (sticky)"


async def test_emit_healthy_restore_on_transition_from_stale_exceeded() -> None:
    """Test 4.5: cell was DEGRADED; next tick has fresh candle → HEALTHY transition + signal."""
    cell = _cell(staleness_budget_hours=12)
    candle_mts_tick1 = _TICK_MTS - _1H_MS
    gap_hours = 18
    stale_raw = [_candle(candle_mts_tick1 - gap_hours * _1H_MS)]

    captured: list[dict[str, Any]] = []
    axiom = MagicMock()
    axiom.emit = AsyncMock(side_effect=lambda e: captured.append(e))

    status_dict, _ = _make_tick_handler(axiom, stale_raw, cell)

    # We need two separate tick handlers sharing the same _cell_pipeline_status.
    # Rebuild tick2 with fresh candle but same status_dict.
    candle_mts_tick2 = _TICK_MTS + _1H_MS - _1H_MS  # = _TICK_MTS
    fresh_raw = [_candle(candle_mts_tick2)]

    engine2 = _make_signal_engine(axiom, cell)
    registry2 = _make_registry(cell)

    async def tick_fresh(cell: CellConfig, mts: int) -> None:
        from datetime import UTC, datetime
        from uuid import uuid4

        from bfx_funding_bot.modules.marketfeed.scheduler import _TIMEFRAME_MS

        candle_mts = mts - _TIMEFRAME_MS[cell.timeframe]
        budget_hours: int = cell.staleness_budget_hours
        raw = fresh_raw
        filled = reindex_and_ffill(raw, ref_mts=candle_mts, max_gap_hours=budget_hours)

        if not filled:
            return
        latest = filled[-1]
        if latest.candle is None:
            return

        prev = status_dict.get(cell.pair_id)
        if prev == HealthStatus.DEGRADED:
            status_dict[cell.pair_id] = HealthStatus.HEALTHY
            await axiom.emit({
                "timestamp": datetime.now(UTC).isoformat(),
                "level": Level.INFO.value,
                "phase": Phase.PAPER.value,
                "strategy": None,
                "cell": cell.cell_id,
                "event_type": EventType.HEALTH_CHECK.value,
                "correlation_id": str(uuid4()),
                "payload": {
                    "check_target": HealthTarget.SIGNAL_PIPELINE.value,
                    "status": HealthStatus.HEALTHY.value,
                },
            })
        else:
            status_dict[cell.pair_id] = HealthStatus.HEALTHY

        await engine2.process_candle(
            cell=cell,
            candle=latest.candle,
            registry=registry2,
            is_stale=latest.is_stale,
            stale_seconds=latest.stale_seconds,
        )

    # Tick 1: stale tick — goes DEGRADED via stale_raw in status_dict
    # We drive the status_dict directly (simpler than a full tick1 call that would
    # use the tick handler above — they share different raw candle lists):
    status_dict[cell.pair_id] = HealthStatus.DEGRADED

    # Tick 2: fresh candle — should emit HEALTHY restore + signal
    await tick_fresh(cell, _TICK_MTS + _1H_MS)

    # HEALTHY restore event
    health_events = [e for e in captured if e["event_type"] == EventType.HEALTH_CHECK.value]
    assert len(health_events) == 1, "Expected exactly one HEALTHY restore event"
    h = health_events[0]
    assert h["level"] == Level.INFO.value
    assert h["payload"]["status"] == HealthStatus.HEALTHY.value
    assert h["payload"]["check_target"] == HealthTarget.SIGNAL_PIPELINE.value

    # Signal emitted after restore
    signal_events = [e for e in captured if e["event_type"] == EventType.SIGNAL.value]
    assert len(signal_events) >= 1, "Expected signal after HEALTHY restore"
    # Fresh candle → is_stale=False
    assert signal_events[0]["payload"]["is_stale"] is False
