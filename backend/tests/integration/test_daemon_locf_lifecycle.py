"""Phase 4.3 Task 8 — daemon LOCF lifecycle integration tests.

3 tests using testcontainer Postgres + sparse p30 candle fixture.
Each test exercises the REAL DB path (upsert → get_up_to) and verifies
emitted telemetry events from a single scheduler tick.

Value-add over unit tests/test_scheduler.py:
- Real Postgres upsert + get_up_to via asyncpg (exercises actual SQL/index)
- Real reindex_and_ffill on DB-returned rows (not in-memory list)
- Real SignalEngine divergence-check path calling candles_repo.get_up_to again
"""
from __future__ import annotations

from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from bfx_funding_bot.core.health import HealthProbe
from bfx_funding_bot.core.telemetry import EventType, HealthStatus, HealthTarget, Level, Phase
from bfx_funding_bot.modules.candles.repository import get_up_to, upsert_candles
from bfx_funding_bot.modules.candles.schemas import FundingCandle
from bfx_funding_bot.modules.candles.service import reindex_and_ffill
from bfx_funding_bot.modules.execution.diagnostics.sink import NoopDiagnosticsSink
from bfx_funding_bot.modules.marketfeed.divergence_reporter import DivergenceReporter
from bfx_funding_bot.modules.marketfeed.scheduler import _TIMEFRAME_MS
from bfx_funding_bot.modules.marketfeed.signal_engine import SignalEngine
from bfx_funding_bot.modules.marketfeed.strategy_registry import StrategyRegistry
from bfx_funding_bot.modules.strategy import CellConfig
from bfx_funding_bot.modules.strategy.wiring import build_strategy, build_strategy_at_boundary

pytestmark = pytest.mark.integration

# Each test uses an isolated time window to prevent cross-test DB contamination.
# pg_container is session-scoped — data persists across all tests in the run.
# Windows are separated by 100h so get_up_to(lookback=13) can't reach across tests.
_1H_MS = 3_600_000
_BASE_EPOCH_MS = 1_700_100_000_000  # any 1h-aligned epoch ms

# Test 8.1: candle_mts = _BASE_EPOCH_MS
_T1_TICK_MTS   = _BASE_EPOCH_MS + _1H_MS
_T1_CANDLE_MTS = _BASE_EPOCH_MS

# Test 8.2: candle_mts = _BASE_EPOCH_MS + 100h
_T2_TICK_MTS   = _BASE_EPOCH_MS + 101 * _1H_MS
_T2_CANDLE_MTS = _BASE_EPOCH_MS + 100 * _1H_MS

# Test 8.3: candle_mts = _BASE_EPOCH_MS + 200h
_T3_TICK_MTS   = _BASE_EPOCH_MS + 201 * _1H_MS
_T3_CANDLE_MTS = _BASE_EPOCH_MS + 200 * _1H_MS

# fUSD p30 budget from cells.yaml is 12h; keep test self-contained by
# constructing the cell manually.
_BUDGET_HOURS = 12


def _p30_cell(staleness_budget_hours: int = _BUDGET_HOURS) -> CellConfig:
    """Minimal fUSD p30 rate_percentile cell — mirrors cells.yaml params."""
    cell = CellConfig.model_validate({
        "strategy": "rate_percentile",
        "symbol": "fUSD",
        "period_agg": "p30",
        "timeframe": "1h",
        "params": {"percentile": 75, "lookback_hours": 5},  # short lookback for speed
        "reference_amount_usdt": 150.0,
    })
    cell.staleness_budget_hours = staleness_budget_hours
    return cell


def _candle(mts: int, rate: str = "0.0001") -> FundingCandle:
    d = Decimal(rate)
    return FundingCandle(
        symbol="fUSD", period_agg="p30", timeframe="1h",
        mts=mts,
        open=d, close=d, high=d, low=d,
        volume=Decimal("0"),
    )


def _make_event_sink_capture() -> tuple[MagicMock, list[dict[str, Any]]]:
    """Return (mock_event_sink, captured_events_list)."""
    captured: list[dict[str, Any]] = []
    event_sink = MagicMock()
    event_sink.emit = AsyncMock(side_effect=lambda e: captured.append(e))
    return event_sink, captured


async def _make_tick_fn(
    *,
    event_sink: MagicMock,
    session_factory: async_sessionmaker,  # type: ignore[type-arg]
    cell: CellConfig,
    candle_mts: int,
    probe: HealthProbe | None = None,
) -> tuple[HealthProbe, Any]:
    """Build the real on_scheduler_tick closure using REAL postgres repo.

    Mirrors the closure inside daemon.build_daemon() but replaces the full
    build_daemon() setup (warmup, WS, writer, etc.) with only what's needed
    for a single tick.

    candle_mts: the expected candle close time = tick_mts - 1h (caller passes
    the time window's specific candle_mts so registry warmup is anchored correctly).
    """
    from datetime import UTC, datetime
    from uuid import uuid4

    from bfx_funding_bot.core.telemetry import HealthTarget, Level

    if probe is None:
        probe = HealthProbe()

    # Build real SignalEngine backed by real postgres (via session_factory)
    class _RepoBridge:
        async def get_up_to(
            self,
            *,
            symbol: str,
            timeframe: str,
            period_agg: str,
            mts_inclusive: int,
            lookback: int,
        ) -> list[FundingCandle]:
            async with session_factory() as s:
                return await get_up_to(  # type: ignore[no-any-return]
                    s,
                    symbol=symbol,
                    timeframe=timeframe,
                    period_agg=period_agg,
                    mts_inclusive=mts_inclusive,
                    lookback=lookback,
                )

    engine = SignalEngine(
        phase=Phase.SHADOW, event_sink=event_sink, diagnostics=NoopDiagnosticsSink(),
        candles_repo=_RepoBridge(),
        reporter=DivergenceReporter(build_strategy_at_boundary),
    )

    # Pre-warm registry with enough history so signal computation doesn't crash.
    # lookback_hours=5 → strategy needs at least 5 candles.
    registry = StrategyRegistry(build_strategy)
    strategy = build_strategy(cell)
    for i in range(6):
        strategy.observe(_candle(candle_mts - (6 - i) * _1H_MS))
    registry.put(cell, strategy)

    async def on_tick(tick_mts: int) -> None:
        candle_mts = tick_mts - _TIMEFRAME_MS[cell.timeframe]
        assert cell.staleness_budget_hours is not None
        budget_hours: int = cell.staleness_budget_hours
        lookback = budget_hours + 1

        async with session_factory() as s:
            raw_candles = await get_up_to(
                s,
                symbol=cell.symbol,
                timeframe=cell.timeframe,
                period_agg=cell.period_agg,
                mts_inclusive=candle_mts,
                lookback=lookback,
            )

        filled = reindex_and_ffill(
            raw_candles, ref_mts=candle_mts, max_gap_hours=budget_hours,
        )

        if not filled:
            budget_seconds = budget_hours * 3600
            if probe.get_cell_pipeline_status(cell.pair_id) != HealthStatus.DEGRADED:
                probe.set_cell_pipeline_status(cell.pair_id, HealthStatus.DEGRADED)
                await event_sink.emit({
                    "timestamp": datetime.now(UTC).isoformat(),
                    "level": Level.WARN.value,
                    "phase": Phase.SHADOW.value,
                    "strategy": None,
                    "cell": cell.cell_id,
                    "event_type": EventType.HEALTH_CHECK.value,
                    "correlation_id": str(uuid4()),
                    "payload": {
                        "check_target": HealthTarget.SIGNAL_PIPELINE.value,
                        "status": HealthStatus.DEGRADED.value,
                        "error_message": "stale_exceeded: no candles in lookback window",
                        "reason": "stale_exceeded",
                        "stale_seconds": None,
                        "budget_seconds": budget_seconds,
                    },
                })
            return

        latest = filled[-1]

        if latest.candle is None:
            budget_seconds = budget_hours * 3600
            if probe.get_cell_pipeline_status(cell.pair_id) != HealthStatus.DEGRADED:
                probe.set_cell_pipeline_status(cell.pair_id, HealthStatus.DEGRADED)
                await event_sink.emit({
                    "timestamp": datetime.now(UTC).isoformat(),
                    "level": Level.WARN.value,
                    "phase": Phase.SHADOW.value,
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

        if probe.get_cell_pipeline_status(cell.pair_id) == HealthStatus.DEGRADED:
            probe.set_cell_pipeline_status(cell.pair_id, HealthStatus.HEALTHY)
            await event_sink.emit({
                "timestamp": datetime.now(UTC).isoformat(),
                "level": Level.INFO.value,
                "phase": Phase.SHADOW.value,
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
            probe.set_cell_pipeline_status(cell.pair_id, HealthStatus.HEALTHY)

        await engine.process_candle(
            cell=cell,
            candle=latest.candle,
            registry=registry,
            is_stale=latest.is_stale,
            stale_seconds=latest.stale_seconds,
        )

    return probe, on_tick


# ── Test 8.1 — Stale signal on sparse p30 fixture (6h gap) ───────────────────

@pytest.mark.asyncio
async def test_daemon_emits_stale_signal_on_sparse_p30_fixture(
    pg_session_factory: async_sessionmaker,  # type: ignore[type-arg]
) -> None:
    """Real postgres: fUSD_p30 candle 6h before candle_mts (budget=12h) →
    tick emits signal with is_stale=True, stale_seconds=21600, no DEGRADED."""
    gap_hours = 6
    source_candle = _candle(_T1_CANDLE_MTS - gap_hours * _1H_MS)

    async with pg_session_factory() as session:
        await upsert_candles(session, [source_candle])
        await session.commit()

    cell = _p30_cell(staleness_budget_hours=12)
    event_sink, captured = _make_event_sink_capture()
    _, tick = await _make_tick_fn(
        event_sink=event_sink, session_factory=pg_session_factory, cell=cell,
        candle_mts=_T1_CANDLE_MTS,
    )

    await tick(_T1_TICK_MTS)

    signal_events = [
        e for e in captured
        if e["event_type"] == EventType.SIGNAL.value
        and e.get("cell") == cell.cell_id  # "fUSD_p30"
    ]
    assert len(signal_events) >= 1, (
        f"Expected signal event for fUSD_p30 soft-tier stale candle; "
        f"got events={[e['event_type'] for e in captured]}"
    )
    sig = signal_events[0]
    assert sig["payload"]["is_stale"] is True, "6h gap must produce is_stale=True"
    assert sig["payload"]["stale_seconds"] == gap_hours * 3600, (
        f"Expected stale_seconds={gap_hours * 3600}, got {sig['payload']['stale_seconds']}"
    )
    assert sig["payload"]["budget_seconds"] == 12 * 3600

    health_events = [
        e for e in captured
        if e["event_type"] == EventType.HEALTH_CHECK.value
    ]
    assert health_events == [], (
        f"6h gap within budget must not emit DEGRADED; got {health_events}"
    )


# ── Test 8.2 — DEGRADED on sparse p30 beyond budget (18h gap) ────────────────

@pytest.mark.asyncio
async def test_daemon_emits_degraded_on_sparse_p30_beyond_budget(
    pg_session_factory: async_sessionmaker,  # type: ignore[type-arg]
) -> None:
    """Real postgres: fUSD_p30 candle 18h before candle_mts (budget=12h) →
    SIGNAL_PIPELINE DEGRADED emitted; NO signal event."""
    gap_hours = 18
    source_candle = _candle(_T2_CANDLE_MTS - gap_hours * _1H_MS)

    async with pg_session_factory() as session:
        await upsert_candles(session, [source_candle])
        await session.commit()

    cell = _p30_cell(staleness_budget_hours=12)
    event_sink, captured = _make_event_sink_capture()
    _, tick = await _make_tick_fn(
        event_sink=event_sink, session_factory=pg_session_factory, cell=cell,
        candle_mts=_T2_CANDLE_MTS,
    )

    await tick(_T2_TICK_MTS)

    # No signal events for fUSD_p30
    signal_events = [
        e for e in captured
        if e["event_type"] == EventType.SIGNAL.value
        and e.get("cell") == cell.cell_id
    ]
    assert signal_events == [], (
        f"18h gap beyond budget must NOT emit signal; got {signal_events}"
    )

    # Exactly one DEGRADED health_check
    health_checks = [
        e for e in captured
        if e["event_type"] == EventType.HEALTH_CHECK.value
        and e["payload"].get("check_target") == HealthTarget.SIGNAL_PIPELINE.value
        and e["payload"].get("reason") == "stale_exceeded"
    ]
    assert len(health_checks) == 1, (
        f"Expected exactly one DEGRADED health_check; got {health_checks}"
    )
    h = health_checks[0]
    assert h["level"] == Level.WARN.value
    assert h["payload"]["status"] == HealthStatus.DEGRADED.value
    assert h["payload"]["stale_seconds"] == gap_hours * 3600
    assert h["payload"]["budget_seconds"] == 12 * 3600


# ── Test 8.3 — Replay engine byte-equivalence with daemon on sparse input ─────

@pytest.mark.asyncio
async def test_replay_engine_byte_equivalent_with_daemon_on_sparse_input(
    pg_session_factory: async_sessionmaker,  # type: ignore[type-arg]
) -> None:
    """Real postgres 6h gap fixture: daemon tick path produces NO divergence_detail
    events → SignalEngine LOCF replay path is byte-equivalent to live path.

    Divergence (CP1 violation) would appear as a SIGNAL event with level=warn
    containing a 'divergence_detail' key in the payload.
    """
    gap_hours = 6
    source_candle = _candle(_T3_CANDLE_MTS - gap_hours * _1H_MS)

    async with pg_session_factory() as session:
        await upsert_candles(session, [source_candle])
        await session.commit()

    cell = _p30_cell(staleness_budget_hours=12)
    event_sink, captured = _make_event_sink_capture()
    _, tick = await _make_tick_fn(
        event_sink=event_sink, session_factory=pg_session_factory, cell=cell,
        candle_mts=_T3_CANDLE_MTS,
    )

    await tick(_T3_TICK_MTS)

    # CP1 byte-equivalence holds iff no signal event carries divergence_detail
    divergence_events = [
        e for e in captured
        if e["event_type"] == EventType.SIGNAL.value
        and "divergence_detail" in e.get("payload", {})
    ]
    assert divergence_events == [], (
        f"CP1 byte-equivalence violated: divergence_detail found in "
        f"{[e['payload'].get('divergence_detail') for e in divergence_events]}"
    )

    # Also confirm a signal WAS emitted (not vacuously passing because no tick happened)
    signal_events = [
        e for e in captured
        if e["event_type"] == EventType.SIGNAL.value
    ]
    assert len(signal_events) >= 1, (
        "Expected at least one signal emit to validate non-vacuous CP1 check"
    )
