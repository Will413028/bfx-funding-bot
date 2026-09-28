"""One-off investigation: MR fixed-param sensitivity on fUST cells (backlog #4 reconciliation).

Question: the deployed canary config (ema_span=168, threshold_sigma=1.0) is inert on
fUST (ratio_sigma~0.99 -> lower_band ~ -0.99 -> never pauses -> == passive AlwaysMarketRate).
Does ANY other FIXED MR combo (the 6-cell Phase 3b grid) actually pause and beat passive
on fUST_a30 / fUST_p2? Distinguishes "deployed the wrong fixed combo (recoverable alpha)"
from "no fixed-param edge; WFO margin was adaptive-selection overfitting".

For each cell x (ema_span in {24,168}) x (threshold_sigma in {0.5,1.0,1.5}):
  ratio_sigma = close_over_ema_sigma(train, ema_span)  (same derivation as cells.yaml)
  run the SAME rolling OOS as run_oos_profitability.py, report active-vs-passive + idle.

Usage:  cd backend && uv run python scripts/explore_mr_param_sensitivity.py
"""
from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from decimal import Decimal

from bfx_funding_bot.core.db import make_engine, make_session_factory, session_scope
from bfx_funding_bot.core.settings import Settings
from bfx_funding_bot.modules.backtest.config import BacktestConfig
from bfx_funding_bot.modules.backtest.eda import close_over_ema_sigma
from bfx_funding_bot.modules.backtest.engine import run_backtest
from bfx_funding_bot.modules.backtest.oos_profitability import (
    WindowOutcome,
    active_return_summary,
    summarize_oos,
)
from bfx_funding_bot.modules.backtest.split import compute_train_end_mts
from bfx_funding_bot.modules.backtest.wfo import compute_wfo_windows
from bfx_funding_bot.modules.candles.repository import get_candles_in_range
from bfx_funding_bot.modules.strategy import AlwaysMarketRateStrategy, MeanReversionStrategy

logger = logging.getLogger("explore_mr")

START_MTS = int(datetime(2022, 1, 1, tzinfo=UTC).timestamp() * 1000)
LINEAR = BacktestConfig(fill_model="linear-baseline")
CELLS = [("fUST", "a30"), ("fUST", "p2")]
EMA_SPANS = [24, 168]
THRESHOLDS = [Decimal("0.5"), Decimal("1.0"), Decimal("1.5")]


def _oc(result, mts: int) -> WindowOutcome:
    return WindowOutcome(month_mts=mts, net_monthly=result.net_monthly_return_pct,
                         n_trades=result.n_trades, fill_rate=result.fill_rate)


async def _amain() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    settings = Settings()
    engine = make_engine(settings)
    factory = make_session_factory(engine)
    end_mts = int(datetime.now(UTC).timestamp() * 1000)
    try:
        for symbol, pa in CELLS:
            async with session_scope(factory) as session:
                candles = await get_candles_in_range(
                    session, symbol=symbol, timeframe="1h", period_agg=pa,
                    start_mts=START_MTS, end_mts=end_mts,
                )
            train_end = compute_train_end_mts(candles)
            train = [c for c in candles if c.mts <= train_end]
            sigmas = {span: close_over_ema_sigma(train, span) for span in EMA_SPANS}
            windows = compute_wfo_windows(candles)

            # baseline once
            base_oc: list[WindowOutcome] = []
            for w in windows:
                sl = [c for c in candles if w.train_start_mts <= c.mts <= w.test_end_mts]
                rb = run_backtest(sl, AlwaysMarketRateStrategy(period_days=2), LINEAR,
                                  w.test_start_mts, w.test_end_mts)
                base_oc.append(_oc(rb, w.test_start_mts))
            base_med = summarize_oos(base_oc).median_monthly

            print(f"\n## {symbol}_{pa}  ({len(windows)} windows; baseline median {base_med:.4f}%/mo)")
            print(f"   sigma_24={sigmas[24]:.4f}  sigma_168={sigmas[168]:.4f}")
            print("| ema_span | thr_sigma | ratio_sigma | lower_band | strat_med% | active_med% | outperf% | idle% | DEPLOYED |")
            print("|---|---|---|---|---|---|---|---|---|")
            for span in EMA_SPANS:
                sigma = sigmas[span]
                for thr in THRESHOLDS:
                    lower_band = -thr * sigma
                    strat_oc: list[WindowOutcome] = []
                    for w in windows:
                        sl = [c for c in candles if w.train_start_mts <= c.mts <= w.test_end_mts]
                        s = MeanReversionStrategy(ema_span=span, threshold_sigma=thr, ratio_sigma=sigma)
                        rs = run_backtest(sl, s, LINEAR, w.test_start_mts, w.test_end_mts)
                        strat_oc.append(_oc(rs, w.test_start_mts))
                    summ = summarize_oos(strat_oc)
                    act = active_return_summary(strat_oc, base_oc)
                    deployed = "<<<" if (span == 168 and thr == Decimal("1.0")) else ""
                    print(f"| {span} | {thr} | {sigma:.4f} | {lower_band:.4f} | "
                          f"{summ.median_monthly:.4f} | {act.median_active:+.4f} | "
                          f"{act.pct_months_outperform:.1%} | {summ.idle_rate:.1%} | {deployed} |")
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(_amain())
