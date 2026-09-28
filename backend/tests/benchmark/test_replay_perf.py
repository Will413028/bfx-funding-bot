"""R4.1.1 mitigation: backtest replay (used in divergence reporter) must
complete under 500ms for 11 cell x 168 candle history.

Targets the worst case (lookback_hours=168 in cells.yaml) — divergence
reporter rebuilds strategy state from scratch every candle close, so this
sets the upper-bound CPU budget within the 5s scheduler buffer.
"""
from __future__ import annotations

import time
from decimal import Decimal

import pytest

from bfx_funding_bot.modules.candles.schemas import FundingCandle
from bfx_funding_bot.modules.marketfeed.divergence_reporter import (
    DivergenceReporter,
    ExtractedSignal,
)
from bfx_funding_bot.modules.marketfeed.strategy_registry import build_strategy
from bfx_funding_bot.modules.strategy import CellConfig


def _make_history(n: int) -> list[FundingCandle]:
    return [
        FundingCandle(
            symbol="fUSD", timeframe="1h", period_agg="a30",
            mts=1747584000000 + i * 3600_000,
            open=Decimal(f"0.0001{i % 10}"), close=Decimal(f"0.0001{i % 10}"),
            high=Decimal(f"0.0001{i % 10}"), low=Decimal(f"0.0001{i % 10}"),
            volume=Decimal("100"),
        )
        for i in range(n)
    ]


@pytest.mark.benchmark
@pytest.mark.parametrize("strategy_name,params", [
    ("rate_percentile", {"percentile": 75, "lookback_hours": 168}),
    ("mean_reversion", {"threshold_sigma": 1.5, "ratio_sigma": 0.0042, "ema_span": 100}),
])
def test_replay_under_500ms_for_11_cells_168_candles(strategy_name: str, params: dict) -> None:
    cell = CellConfig.model_validate({
        "strategy": strategy_name, "symbol": "fUSD", "period_agg": "a30",
        "timeframe": "1h", "params": params, "reference_amount_usdt": 150.0,
        "staleness_budget_hours": 2,
    })
    history = _make_history(168)
    reporter = DivergenceReporter()

    live = build_strategy(cell)
    for c in history[:-1]:
        live.observe(c)
    live_sig = ExtractedSignal.extract(cell, live, history[-1])

    start = time.perf_counter()
    for _ in range(11):
        reporter.check(
            cell=cell, raw_history=history, boundary_candle=history[-1],
            budget_hours=2, live_signal=live_sig,
        )
    elapsed_ms = (time.perf_counter() - start) * 1000

    print(f"\n{strategy_name}: 11 cells x 168 candles = {elapsed_ms:.1f}ms")
    assert elapsed_ms < 500, (
        f"replay too slow: {elapsed_ms:.1f}ms > 500ms budget. "
        "If unavoidable, increase scheduler buffer_s past 5s, or split "
        "divergence check into async batch (defer to 4.3)."
    )
