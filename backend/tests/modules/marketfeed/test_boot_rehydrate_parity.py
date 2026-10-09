"""Boot warmup + the rehydrate tick must leave live state equal to replay.

At boot the bot warms each cell, then replays the boundary a cold start skips
(`last_candle_close_mts`): one real tick on the candle that just closed. If the
warmup already observed that candle, the tick observes it a second time and the
MR EMA takes an extra step -- every later tick then diverges from replay until
the EMA tail decays, which at span 24 outlasts the gap between deploys.
"""
from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from bfx_funding_bot.modules.candles.schemas import FundingCandle
from bfx_funding_bot.modules.marketfeed.daemon import Daemon
from bfx_funding_bot.modules.marketfeed.divergence_reporter import (
    DivergenceReporter,
    ExtractedSignal,
)
from bfx_funding_bot.modules.marketfeed.scheduler import last_candle_close_mts
from bfx_funding_bot.modules.marketfeed.strategy_registry import StrategyRegistry
from bfx_funding_bot.modules.marketfeed.warmup import (
    WarmupResult,
    rehydrate_boot_boundary,
    warmup_ref_mts,
)
from bfx_funding_bot.modules.strategy import CellConfig
from bfx_funding_bot.modules.strategy.wiring import build_strategy, build_strategy_at_boundary

_HOUR = 3_600_000
_START = 1_791_000_000_000 - (1_791_000_000_000 % _HOUR)


def _candle(i: int) -> FundingCandle:
    rate = Decimal("0.00015") + Decimal(i % 7) * Decimal("0.000004")
    return FundingCandle(
        symbol="fUST", timeframe="1h", period_agg="a30", mts=_START + i * _HOUR,
        open=rate, close=rate, high=rate, low=rate, volume=Decimal("100"),
    )


def _cell() -> CellConfig:
    return CellConfig.model_validate({
        "strategy": "mean_reversion", "symbol": "fUST", "period_agg": "a30",
        "timeframe": "1h", "reference_amount_usdt": 150.0,
        "staleness_budget_hours": 2,
        "params": {"threshold_sigma": 0.5, "ratio_sigma": 0.0042, "ema_span": 24},
    })


def test_rehydrate_tick_after_warmup_matches_replay() -> None:
    cell = _cell()
    # Process comes up 26 minutes into the hour after candle 250 closed.
    history = [_candle(i) for i in range(252)]
    now_ms = history[-1].mts + 26 * 60_000
    boundary = last_candle_close_mts(timeframe="1h", now_ms=now_ms)
    candle_mts = boundary - _HOUR
    tick_candle = next(c for c in history if c.mts == candle_mts)
    sealed = [c for c in history if c.mts <= candle_mts]

    # Warmup (warmup_cell's builder call), then the rehydrate tick's observe.
    live = build_strategy_at_boundary(
        cell=cell, history=history,
        ref_mts=warmup_ref_mts(cell, now_mts=now_ms), budget_hours=2,
    ).strategy
    live_signal = ExtractedSignal.extract(cell, live, tick_candle)

    diff = DivergenceReporter(build_strategy_at_boundary).check(
        cell=cell, raw_history=sealed, boundary_candle=tick_candle,
        budget_hours=2, live_signal=live_signal,
    )
    assert diff is None, diff["diff_fields"] if diff else None


def _warmed(cell: CellConfig, history: list[FundingCandle], now_ms: int) -> tuple[
    StrategyRegistry, dict[str, WarmupResult],
]:
    """What warmup_cell leaves: the strategy, and the rows it read up to its ref."""
    ref = warmup_ref_mts(cell, now_mts=now_ms)
    rows = [c for c in history if c.mts <= ref][-(200 + 1):]
    registry = StrategyRegistry(build_strategy)
    registry.put(cell, build_strategy_at_boundary(
        cell=cell, history=rows, ref_mts=ref, budget_hours=2,
    ).strategy)
    warm = WarmupResult(
        cell_id=cell.pair_id, observed_count=len(rows) - 1, gap_filled=0,
        history=tuple(rows),
    )
    return registry, {cell.pair_id: warm}


def _next_tick_diff(
    cell: CellConfig, registry: StrategyRegistry, history: list[FundingCandle],
    candle_mts: int,
) -> list[str] | None:
    candle = next(c for c in history if c.mts == candle_mts)
    live = registry.get(cell)
    assert live is not None
    live_signal = ExtractedSignal.extract(cell, live, candle)
    rows = [c for c in history if c.mts <= candle_mts][-(200 + 1):]
    diff = DivergenceReporter(build_strategy_at_boundary).check(
        cell=cell, raw_history=rows, boundary_candle=candle,
        budget_hours=2, live_signal=live_signal,
    )
    return diff["diff_fields"] if diff else None


async def test_rehydrate_ticks_the_boundary_of_the_warmup_clock() -> None:
    cell = _cell()
    history = [_candle(i) for i in range(252)]
    now_ms = history[-1].mts + 26 * 60_000
    registry, warmups = _warmed(cell, history, now_ms)
    ticked: list[int] = []

    async def tick(c: CellConfig, boundary: int) -> None:
        ticked.append(boundary)

    await rehydrate_boot_boundary(
        cells=[cell], now_mts=now_ms, warmups=warmups, registry=registry,
        boundary_builder=build_strategy_at_boundary, tick=tick,
    )

    assert ticked == [last_candle_close_mts(timeframe="1h", now_ms=now_ms)]


async def test_a_failed_rehydrate_tick_leaves_the_cell_ready_for_the_next_tick() -> None:
    """The tick fails before observing the candle warmup left for it; the cell is
    rebuilt from warmup's rows (no database) so the scheduler's first tick still
    matches replay instead of running one candle short."""
    cell = _cell()
    history = [_candle(i) for i in range(252)]
    now_ms = history[-2].mts + 26 * 60_000  # candle 250 is the scheduler's first
    registry, warmups = _warmed(cell, history, now_ms)
    boundary = last_candle_close_mts(timeframe="1h", now_ms=now_ms)

    async def tick(c: CellConfig, b: int) -> None:
        raise ConnectionError("database unavailable")

    await rehydrate_boot_boundary(
        cells=[cell], now_mts=now_ms, warmups=warmups, registry=registry,
        boundary_builder=build_strategy_at_boundary, tick=tick,
    )

    assert _next_tick_diff(cell, registry, history, candle_mts=boundary) is None


async def test_the_daemon_arms_the_scheduler_from_the_boot_clock() -> None:
    cell = _cell()
    scheduler = MagicMock()
    fake = SimpleNamespace(
        config=SimpleNamespace(cells=[cell]), scheduler=scheduler, boot_mts=1_791_000_000_123,
        _boot_until_observed=AsyncMock(return_value=False),
    )

    await Daemon.run(fake)  # type: ignore[arg-type]

    scheduler.register_from_now.assert_called_once_with(cell, now_ms=1_791_000_000_123)
