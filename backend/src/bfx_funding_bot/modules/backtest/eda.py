"""Pure-function EDA helpers for Phase 3b.

All functions take a candle list and return summary statistics.
Callers (eda_phase3b.py script) load candles per cell from Neon and
restrict to train portion before calling these.
"""
from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import numpy as np

from bfx_funding_bot.modules.candles.schemas import FundingCandle


def _closes(candles: list[FundingCandle]) -> list[Decimal]:
    return [c.close for c in candles if c.close is not None]


def close_rate_percentiles(candles: list[FundingCandle]) -> dict[str, Decimal]:
    """Return {P25, P50, P75, P90} of close rates."""
    closes_f = [float(c) for c in _closes(candles)]
    if not closes_f:
        return {k: Decimal("0") for k in ("P25", "P50", "P75", "P90")}
    p = np.percentile(closes_f, [25, 50, 75, 90])
    return {
        "P25": Decimal(str(float(p[0]))),
        "P50": Decimal(str(float(p[1]))),
        "P75": Decimal(str(float(p[2]))),
        "P90": Decimal(str(float(p[3]))),
    }


def mean_rate_by_weekday(candles: list[FundingCandle]) -> dict[int, Decimal]:
    """Return {0..6: mean_close_rate}. 0 = Monday (datetime.weekday)."""
    by_wd: dict[int, list[Decimal]] = {i: [] for i in range(7)}
    for c in candles:
        if c.close is None:
            continue
        wd = datetime.fromtimestamp(c.mts / 1000, UTC).weekday()
        by_wd[wd].append(c.close)
    return {
        wd: (sum(vs, Decimal("0")) / Decimal(len(vs)) if vs else Decimal("0"))
        for wd, vs in by_wd.items()
    }


def close_over_ema_sigma(candles: list[FundingCandle], ema_span: int) -> Decimal:
    """Return sigma of (close / EMA(span) - 1) sequence."""
    closes = _closes(candles)
    if len(closes) < 2:
        return Decimal("0")
    alpha = 2 / (ema_span + 1)
    ema = float(closes[0])
    ratios = []
    for c in closes:
        cf = float(c)
        ema = alpha * cf + (1 - alpha) * ema
        if ema > 0:
            ratios.append(cf / ema - 1)
    if len(ratios) < 2:
        return Decimal("0")
    return Decimal(str(float(np.std(ratios))))


def autocorrelation_at_lags(
    candles: list[FundingCandle], lags: list[int]
) -> dict[int, Decimal | None]:
    """ACF of close at given hourly lags. None when var=0 / sample too small."""
    closes = np.array([float(c) for c in _closes(candles)])
    out: dict[int, Decimal | None] = {}
    for lag in lags:
        if len(closes) <= lag + 1:
            out[lag] = None
            continue
        x = closes[:-lag]
        y = closes[lag:]
        var_x = float(np.var(x))
        var_y = float(np.var(y))
        if var_x < 1e-30 or var_y < 1e-30:
            out[lag] = None
        else:
            corr = float(np.corrcoef(x, y)[0, 1])
            out[lag] = Decimal(str(corr))
    return out


def per_quarter_regime_drift(candles: list[FundingCandle]) -> Decimal | None:
    """Returns (max_quarter_mean - min_quarter_mean) / min_quarter_mean.

    Quarters bucketed by candle UTC month (Jan-Mar=Q1 etc.).
    Spec gate: drift >= 0.30 -> mandatory WFO required, Phase 3b conclusions
    invalidated.

    Returns:
        Decimal drift value, or None when the metric is undefined:
          - fewer than 2 distinct (year, quarter) buckets (sample too small)
          - min quarter mean is zero (degenerate market state; drift undefined)
        Callers (script) must distinguish None from a finite drift before
        applying the >= 0.30 spec gate.
    """
    buckets: dict[tuple[int, int], list[Decimal]] = {}
    for c in candles:
        if c.close is None:
            continue
        dt = datetime.fromtimestamp(c.mts / 1000, UTC)
        quarter = (dt.month - 1) // 3 + 1
        buckets.setdefault((dt.year, quarter), []).append(c.close)
    if len(buckets) < 2:
        return None
    means = [
        sum(vs, Decimal("0")) / Decimal(len(vs)) for vs in buckets.values() if vs
    ]
    if not means:
        return None
    lo = min(means)
    hi = max(means)
    if lo == 0:
        return None
    return (hi - lo) / lo
