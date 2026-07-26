"""Replay a backtest under the candle distortion live actually suffered.

Backtests read `funding_candles` as it stands today — the settled value. Before
2026-07-27 the live daemon observed whatever the venue had pushed at the moment
the scheduler read, and the venue kept revising that value afterwards. So every
historical result assumed a price the bot could not have had.

The true point-in-time series is unrecoverable (in-place upsert, no timestamps),
so this module does the only thing left: perturb the settled series with the
distortion distribution we managed to measure, and see whether the conclusions
survive. That answers "are the results fragile", NOT "what would live have earned".

Sample + its biases: docs/research/2026-07-27-candle-distortion-sample.md
ADR D2: wiki/projects/bfx-funding-bot/decisions/2026-07-27-candle-immutability-bitemporal.md
"""
from __future__ import annotations

import random
from decimal import Decimal

from bfx_funding_bot.modules.candles.schemas import FundingCandle


def perturb_candles(
    candles: list[FundingCandle],
    *,
    distortion_rate: float,
    pct_samples: list[float],
    seed: int,
) -> list[FundingCandle]:
    """Return `candles` with a fraction of closes moved to their live-observed value.

    Args:
        candles: Settled series (what a backtest normally reads).
        distortion_rate: Fraction of candles to distort. Measured value is 0.174
            (23 of 132) — a floor, since the sample only contains distortions
            large enough to have tripped a divergence warning.
        pct_samples: Empirical `(final - live) / live * 100` observations, drawn
            with replacement.
        seed: Fixes the draw so a Monte Carlo run can be re-audited.

    Only `close` moves: it is the sole field feeding a strategy decision
    (`LendDecision.rate` takes `candle.close`), so perturbing OHLV would add
    variance without adding realism.
    """
    if not 0.0 <= distortion_rate <= 1.0:
        raise ValueError(f"distortion_rate must be in [0, 1], got {distortion_rate}")
    if not pct_samples:
        raise ValueError("pct_samples must not be empty")

    rng = random.Random(seed)
    out: list[FundingCandle] = []
    for candle in candles:
        if candle.close is None or rng.random() >= distortion_rate:
            out.append(candle)
            continue
        pct = Decimal(str(rng.choice(pct_samples)))
        # pct is defined as (final - live)/live, so live = final / (1 + pct/100).
        divisor = Decimal("1") + pct / Decimal("100")
        if divisor <= 0:
            out.append(candle)  # a <= -100% observation would flip the sign; skip
            continue
        out.append(candle.model_copy(update={"close": candle.close / divisor}))
    return out
