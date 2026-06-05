# src/bfx_funding_bot/modules/backtest/signal_eda.py
"""Offline signal-screening EDA funnel.

Pure functions only (no DB/I/O). Scores candidate signals for predictive power
against forward market-rate movement. See
docs/superpowers/specs/2026-06-05-signal-eda-funnel-design.md.
"""
from __future__ import annotations

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
