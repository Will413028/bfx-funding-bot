"""Boot warmup + the rehydrate tick must leave live state equal to replay.

At boot the bot warms each cell, then replays the boundary a cold start skips
(`last_candle_close_mts`): one real tick on the candle that just closed. If the
warmup already observed that candle, the tick observes it a second time and the
MR EMA takes an extra step -- every later tick then diverges from replay until
the EMA tail decays, which at span 24 outlasts the gap between deploys.
"""
from __future__ import annotations

from decimal import Decimal

from bfx_funding_bot.modules.candles.schemas import FundingCandle
from bfx_funding_bot.modules.marketfeed.divergence_reporter import (
    DivergenceReporter,
    ExtractedSignal,
)
from bfx_funding_bot.modules.marketfeed.scheduler import last_candle_close_mts
from bfx_funding_bot.modules.marketfeed.warmup import warmup_ref_mts
from bfx_funding_bot.modules.strategy import CellConfig
from bfx_funding_bot.modules.strategy.wiring import build_strategy_at_boundary

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
