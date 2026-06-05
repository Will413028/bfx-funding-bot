# src/bfx_funding_bot/modules/backtest/signal_eda.py
"""Offline signal-screening EDA funnel.

Pure functions only (no DB/I/O). Scores candidate signals for predictive power
against forward market-rate movement. See
docs/superpowers/specs/2026-06-05-signal-eda-funnel-design.md.
"""
from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

import numpy as np
import pandas as pd

from bfx_funding_bot.modules.candles.schemas import FundingCandle
from bfx_funding_bot.modules.funding_stats.schemas import FundingStat

# SEED, HORIZONS: shared defaults consumed by later tasks (bootstrap seed, driver horizons).
SEED = 20260605
HORIZONS = [2, 7, 14, 30]
SPLIT_MTS = int(datetime(2022, 1, 1, tzinfo=UTC).timestamp() * 1000)  # disjoint regime boundary


def _decimal_to_float(d: object) -> float:
    return float(d) if d is not None else float("nan")  # type: ignore[arg-type]


def build_signal_frame(
    candles: list[FundingCandle], stats: list[FundingStat]
) -> pd.DataFrame:
    """Align funding_stats onto the candle time grid via backward as-of join.

    Returns a frame sorted by mts with columns: mts, close, frr, avg_period,
    funding_amount, funding_amount_used. Each candle row carries the most recent
    funding_stat at or before its mts (merge_asof backward).
    Note: funding_below_threshold is intentionally omitted — not a candidate signal for this EDA."""
    c = (
        pd.DataFrame([{"mts": x.mts, "close": _decimal_to_float(x.close)} for x in candles])
        .sort_values("mts")
        .reset_index(drop=True)
    )
    s = (
        pd.DataFrame(
            [
                {
                    "mts": x.mts,
                    "frr": _decimal_to_float(x.frr),
                    "avg_period": _decimal_to_float(x.avg_period),
                    "funding_amount": _decimal_to_float(x.funding_amount),
                    "funding_amount_used": _decimal_to_float(x.funding_amount_used),
                }
                for x in stats
            ]
        )
        .sort_values("mts")
        .reset_index(drop=True)
    )
    return pd.merge_asof(c, s, on="mts", direction="backward")


def add_forward_rate_change(df: pd.DataFrame, horizons_days: list[int]) -> pd.DataFrame:
    """For each horizon H, add column fwd_d{H} = mean(close in (t, t+H days]) - close_t.

    Time-based (ms) window, robust to candle spacing. Rows without any forward
    candle inside the window get NaN."""
    out = df.copy()
    mts = out["mts"].to_numpy()
    close = out["close"].to_numpy()
    for h in horizons_days:
        horizon_ms = h * 86_400_000
        fwd = np.full(len(out), np.nan)
        for i in range(len(out)):
            mask = (mts > mts[i]) & (mts <= mts[i] + horizon_ms)
            if mask.any():
                fwd[i] = close[mask].mean() - close[i]
        out[f"fwd_d{h}"] = fwd
    return out


def split_regime(
    df: pd.DataFrame, split_mts: int = SPLIT_MTS
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Disjoint early (mts < split) / late (mts >= split) regimes."""
    early = df[df["mts"] < split_mts].reset_index(drop=True)
    late = df[df["mts"] >= split_mts].reset_index(drop=True)
    return early, late


def frr_trend(df: pd.DataFrame, k: int) -> pd.Series:
    """Normalized k-step FRR momentum: (frr_t - frr_{t-k}) / rolling_std. Relative,
    so per-year FRR slope drift does not contaminate it. Normalized by the rolling std
    of raw FRR (its scale), not of the delta."""
    frr = df["frr"]
    delta = frr - frr.shift(k)
    return delta / frr.rolling(max(k * 4, 8), min_periods=k).std()


def spike_z(df: pd.DataFrame, w: int) -> pd.Series:
    """FRR z-score vs its own rolling baseline (spike detector). Relative form.
    Zero-std windows (flat region) return 0 rather than NaN."""
    frr = df["frr"]
    roll = frr.rolling(w)
    std = roll.std()
    z = (frr - roll.mean()) / std
    # flat window: numerator=0, std=0 -> 0/0=NaN; define z=0 there
    return z.where(std != 0, other=0.0)


def _rolling_pctile(series: pd.Series, w: int) -> pd.Series:
    """Percentile rank of the latest value within its trailing window of size w."""
    return series.rolling(w).apply(lambda x: (x.iloc[-1] >= x).mean(), raw=False)


def amount_pctile(df: pd.DataFrame, w: int) -> pd.Series:
    """Rolling percentile rank of funding_amount (supply level, regime-free)."""
    return _rolling_pctile(df["funding_amount"], w)


def utilization_pctile(df: pd.DataFrame, w: int) -> pd.Series:
    """Rolling percentile of utilization = used/amount (demand pressure)."""
    util = df["funding_amount_used"] / df["funding_amount"]
    return _rolling_pctile(util, w)


_TREND_LAG = 3        # 3-day FRR momentum
_SPIKE_WINDOW = 14    # 2-week rolling baseline for the spike z-score
_PCTILE_WINDOW = 30   # ~1-month context for supply / utilization percentile

# Signal registry: name -> callable producing the signal series. All rolling/relative.
SIGNALS: dict[str, Callable[[pd.DataFrame], pd.Series]] = {
    "frr_trend": lambda df: frr_trend(df, k=_TREND_LAG),
    "spike_detect": lambda df: spike_z(df, w=_SPIKE_WINDOW),
    "funding_supply": lambda df: amount_pctile(df, w=_PCTILE_WINDOW),
    "utilization": lambda df: utilization_pctile(df, w=_PCTILE_WINDOW),
}
