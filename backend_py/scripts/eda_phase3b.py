"""Phase 3b EDA -- per-cell statistics on train portion of post-2022 candles.

Outputs JSON-ish markdown summary to stdout for review/commit to
docs/research/2026-05-17-phase3b-eda.md.

Per Phase 3b spec section "Step 0":
  - Only train portion of each cell is used (avoids data snooping)
  - Outputs per cell: percentiles, weekday means, close/EMA sigma, ACF, regime drift

Usage:
    cd backend_py
    uv run python scripts/eda_phase3b.py > /tmp/phase3b_eda.txt
"""
from __future__ import annotations

import asyncio
import logging
import sys
from datetime import UTC, datetime
from decimal import Decimal
from itertools import product

from bfx_funding_bot.core.db import make_engine, make_session_factory, session_scope
from bfx_funding_bot.core.settings import Settings
from bfx_funding_bot.modules.backtest.eda import (
    autocorrelation_at_lags,
    close_over_ema_sigma,
    close_rate_percentiles,
    mean_rate_by_weekday,
    per_quarter_regime_drift,
)
from bfx_funding_bot.modules.backtest.split import compute_train_end_mts
from bfx_funding_bot.modules.candles.repository import get_candles_in_range

logger = logging.getLogger("eda_phase3b")

SYMBOLS = ["fUSD", "fUST"]
PERIOD_AGGS = ["p2", "p30", "a30"]
WINDOW_START_MTS = int(datetime(2022, 1, 1, tzinfo=UTC).timestamp() * 1000)
WINDOW_END_MTS = int(datetime.now(UTC).timestamp() * 1000)


async def _amain() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    settings = Settings()
    engine = make_engine(settings)
    session_factory = make_session_factory(engine)

    print("# Phase 3b EDA - per-cell train-portion stats\n")
    print(
        f"Window: {datetime.fromtimestamp(WINDOW_START_MTS / 1000, UTC).date()} - today UTC\n"
    )

    try:
        for symbol, period_agg in product(SYMBOLS, PERIOD_AGGS):
            print(f"\n## Cell: {symbol} x {period_agg}\n")
            async with session_scope(session_factory) as session:
                candles = await get_candles_in_range(
                    session,
                    symbol=symbol,
                    timeframe="1h",
                    period_agg=period_agg,
                    start_mts=WINDOW_START_MTS,
                    end_mts=WINDOW_END_MTS,
                )
            if len(candles) < 720:
                print(f"- SKIP: len(candles)={len(candles)} < 720 (1 month)")
                continue
            train_end_mts = compute_train_end_mts(candles)
            train = [c for c in candles if c.mts <= train_end_mts]

            pct = close_rate_percentiles(train)
            wd = mean_rate_by_weekday(train)
            sigma24 = close_over_ema_sigma(train, ema_span=24)
            sigma168 = close_over_ema_sigma(train, ema_span=168)
            acf = autocorrelation_at_lags(train, lags=[1, 24, 168, 720])
            drift = per_quarter_regime_drift(train)

            weekday_mean = (wd[0] + wd[1] + wd[2] + wd[3]) / 4
            weekend_effect: Decimal | None = None
            if weekday_mean != Decimal("0"):
                weekend_mean = (wd[4] + wd[5] + wd[6]) / 3
                weekend_effect = (weekend_mean - weekday_mean) / weekday_mean

            print(f"- n_candles (post-2022): {len(candles)}; train n: {len(train)}")
            print(f"- train_end_mts: {train_end_mts}")
            print(
                f"- Percentiles: P25={pct['P25']}, P50={pct['P50']},"
                f" P75={pct['P75']}, P90={pct['P90']}"
            )
            print(
                f"- Mean rate by weekday (0=Mon..6=Sun): {dict(sorted(wd.items()))}"
            )
            print(
                f"- Weekend (Fri/Sat/Sun) effect size vs weekday: {weekend_effect}"
            )
            print(f"- close/EMA(24h) sigma: {sigma24}")
            print(f"- close/EMA(168h) sigma: {sigma168}")
            print(f"- ACF: {acf}")
            print(f"- Per-quarter regime drift: {drift}")
            if drift is None:
                print(
                    "- NOTE: Drift undefined (insufficient quarters or zero"
                    " min-quarter mean) - spec gate cannot be evaluated for this cell."
                )
            elif drift >= Decimal("0.30"):
                print(
                    "- WARNING: Drift >= 30% - spec gate: Phase 3b conclusions"
                    " invalid; mandatory WFO upgrade required."
                )
            if weekend_effect is not None and weekend_effect < Decimal("0.05"):
                print(
                    "- NOTE: Weekend effect < 5%: WeekendPremium drop rule"
                    " triggered for this cell."
                )
        return 0
    except Exception:
        logger.exception("EDA failed")
        return 2
    finally:
        await engine.dispose()


def main() -> None:
    sys.exit(asyncio.run(_amain()))


if __name__ == "__main__":
    main()
