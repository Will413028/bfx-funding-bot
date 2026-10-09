"""Warmup orchestrator — DB read + REST gap-fill + strategy.observe sequential.

Design basis: phase 4.1 paper/shadow infra design Section "Data Flow" Flow 1
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bfx_funding_bot.external.bitfinex.rest import BitfinexREST
from bfx_funding_bot.modules.candles.gap_fill import fill_gap_from_rest
from bfx_funding_bot.modules.candles.repository import (
    get_up_to,
    seal_closed_periods,
)
from bfx_funding_bot.modules.candles.schemas import FundingCandle
from bfx_funding_bot.modules.candles.tables import FundingCandleRow
from bfx_funding_bot.modules.marketfeed.scheduler import last_candle_close_mts
from bfx_funding_bot.modules.marketfeed.strategy_registry import (
    BoundaryStrategyBuilder,
    StrategyRegistry,
)
from bfx_funding_bot.modules.strategy import CellConfig, StrategyName

log = logging.getLogger(__name__)

_TIMEFRAME_MS = {"15m": 15 * 60_000, "30m": 30 * 60_000, "1h": 60 * 60_000}


@dataclass(frozen=True)
class WarmupResult:
    cell_id: str
    observed_count: int
    gap_filled: int


def _lookback_for(cell: CellConfig) -> int:
    """Minimum candles needed for strategy state init."""
    if cell.strategy == StrategyName.RATE_PERCENTILE:
        return int(cell.params["lookback_hours"])
    if cell.strategy == StrategyName.MEAN_REVERSION:
        return 200  # heuristic: EMA + sigma stabilize within ~200 candles
    raise ValueError(f"unsupported strategy {cell.strategy!r}")


def warmup_ref_mts(cell: CellConfig, *, now_mts: int) -> int:
    """The slot warmup leaves unobserved: the candle the boot rehydrate tick takes.

    Boot replays `last_candle_close_mts(now_mts)` as a real tick, and that tick
    observes the candle that just closed. Warming up to `now_mts` would observe it
    here as well, so the rehydrate tick would count it twice -- an extra EMA step
    that keeps live diverging from replay until the tail decays. Stopping one
    candle short leaves live exactly where replay rebuilds for that tick.
    """
    return last_candle_close_mts(timeframe=cell.timeframe, now_ms=now_mts) - (
        _TIMEFRAME_MS[cell.timeframe]
    )


async def read_warmup_history(
    session: AsyncSession, cell: CellConfig, *, ref_mts: int,
) -> list[FundingCandle]:
    """The rows replay reads for the tick at `ref_mts`, so warmup starts from them.

    `DivergenceReporter` rebuilds from the last `lookback + 1` sealed rows up to
    the tick's candle (signal_engine). A time range ending at an unaligned boot
    clock instead yields one row fewer, which leaves a bounded window (RP's deque)
    unfilled for the first tick.
    """
    return await get_up_to(
        session,
        symbol=cell.symbol, timeframe=cell.timeframe, period_agg=cell.period_agg,
        mts_inclusive=ref_mts, lookback=_lookback_for(cell) + 1,
    )


async def warmup_cell(
    *,
    cell: CellConfig,
    registry: StrategyRegistry,
    boundary_builder: BoundaryStrategyBuilder,
    bitfinex: BitfinexREST,
    session: AsyncSession,
    now_mts: int,
    ref_mts: int | None = None,
) -> WarmupResult:
    """Warm `cell` so its next observe is the candle at `ref_mts`.

    `ref_mts` defaults to `warmup_ref_mts`, the candle the boot rehydrate tick
    takes; a caller rebuilding for a later tick passes that tick's candle.
    """
    step = _TIMEFRAME_MS[cell.timeframe]
    lookback = _lookback_for(cell)
    if ref_mts is None:
        ref_mts = warmup_ref_mts(cell, now_mts=now_mts)

    # 1. Find last known DB candle
    db_max = await _get_last_mts(session, cell)

    # 2. Gap-fill: catch DB up to "now"
    fill = await fill_gap_from_rest(
        bitfinex=bitfinex, session=session,
        symbol=cell.symbol, timeframe=cell.timeframe, period_agg=cell.period_agg,
        last_known_mts=db_max if db_max is not None else now_mts - lookback * step,
        now_mts=now_mts,
    )

    # 3. Read last `lookback` candles from DB. Seal elapsed periods first, for
    # the same reason the scheduler does: a period that has closed cannot gain
    # more trades, and leaving it unsealed hides it from this final-only read.
    await seal_closed_periods(
        session,
        symbol=cell.symbol, timeframe=cell.timeframe, period_agg=cell.period_agg,
        now_ms=now_mts,
    )
    history = await read_warmup_history(session, cell, ref_mts=ref_mts)

    # 4. Phase 4.3 LOCF symmetry: delegate to build_strategy_at_boundary —
    # single source of truth shared with divergence_reporter.py so the live
    # state populated here is byte-equivalent to the reference state replay
    # rebuilds per tick. See that function's docstring for full semantics
    # (LOCF over hourly grid ending at ref_mts, drop boundary slot, observe
    # non-None remainder).
    if cell.staleness_budget_hours is None:
        raise AssertionError(
            f"cell {cell.pair_id} staleness_budget_hours not resolved; "
            "was load_config() called?"
        )
    result = boundary_builder(
        cell=cell, history=history,
        ref_mts=ref_mts, budget_hours=cell.staleness_budget_hours,
    )
    registry.put(cell, result.strategy)
    log.info(
        "warmup_complete cell=%s raw=%d locf_observed=%d gap_filled=%d",
        cell.pair_id, len(history), result.observed_count, fill.candles_fetched,
    )
    return WarmupResult(
        cell_id=cell.pair_id,
        observed_count=result.observed_count,
        gap_filled=fill.candles_fetched,
    )


async def _get_last_mts(session: AsyncSession, cell: CellConfig) -> int | None:
    """Return max mts for this cell or None if empty."""
    row = (
        await session.execute(
            select(FundingCandleRow.mts)
            .where(
                FundingCandleRow.symbol == cell.symbol,
                FundingCandleRow.timeframe == cell.timeframe,
                FundingCandleRow.period_agg == cell.period_agg,
            )
            .order_by(FundingCandleRow.mts.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    return int(row) if row is not None else None
