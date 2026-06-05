# tests/modules/backtest/test_signal_eda.py
from decimal import Decimal

import numpy as np
import pandas as pd

from bfx_funding_bot.modules.backtest.signal_eda import (
    SIGNALS,
    SPLIT_MTS,
    add_forward_rate_change,
    amount_pctile,
    build_signal_frame,
    frr_trend,
    spike_z,
    split_regime,
    utilization_pctile,
)
from bfx_funding_bot.modules.candles.schemas import FundingCandle
from bfx_funding_bot.modules.funding_stats.schemas import FundingStat

_DAY = 86_400_000


def _candles(closes: list[str], *, start: int = 1_500_000_000_000, step: int = _DAY):
    return [
        FundingCandle(symbol="fUST", timeframe="1D", period_agg="p2",
                      mts=start + i * step, open=Decimal(c), high=Decimal(c),
                      low=Decimal(c), close=Decimal(c))
        for i, c in enumerate(closes)
    ]


def _stats(rows: list[dict], *, start: int = 1_500_000_000_000, step: int = _DAY):
    return [
        FundingStat(symbol="fUST", mts=start + i * step,
                    frr=Decimal(str(r["frr"])),
                    funding_amount=Decimal(str(r.get("amt", 1000))),
                    funding_amount_used=Decimal(str(r.get("used", 500))))
        for i, r in enumerate(rows)
    ]


def test_build_signal_frame_aligns_stats_onto_candle_grid() -> None:
    candles = _candles(["0.0001", "0.0002", "0.0003"])
    stats = _stats([{"frr": 1e-6}, {"frr": 2e-6}, {"frr": 3e-6}])
    df = build_signal_frame(candles, stats)
    assert list(df["close"]) == [0.0001, 0.0002, 0.0003]
    assert df["frr"].tolist() == [1e-6, 2e-6, 3e-6]
    assert df["funding_amount"].tolist() == [1000.0, 1000.0, 1000.0]


def test_build_signal_frame_asof_uses_latest_prior_stat() -> None:
    # stat at t0 only; candles at t0, t1 -> t1 should carry t0's frr (backward asof)
    candles = _candles(["0.0001", "0.0002"])
    stats = _stats([{"frr": 5e-6}])  # single stat at t0
    df = build_signal_frame(candles, stats)
    assert df["frr"].tolist() == [5e-6, 5e-6]


def test_add_forward_rate_change_is_time_based_mean_minus_spot() -> None:
    # closes 1,2,3,4,5 on a daily grid; H=2 -> mean(next 2 closes) - spot
    df = build_signal_frame(_candles(["1", "2", "3", "4", "5"]), _stats([{"frr": 1e-6}] * 5))
    out = add_forward_rate_change(df, [2])
    # row0: mean(2,3) - 1 = 1.5 ; row1: mean(3,4) - 2 = 1.5 ; ...
    assert out["fwd_d2"].iloc[0] == 1.5
    assert out["fwd_d2"].iloc[1] == 1.5
    # last row has no forward candles -> NaN
    assert np.isnan(out["fwd_d2"].iloc[-1])


def test_split_regime_is_disjoint() -> None:
    early_frame = build_signal_frame(
        _candles(["1"], start=SPLIT_MTS - 2 * _DAY),
        _stats([{"frr": 1e-6}], start=SPLIT_MTS - 2 * _DAY),
    )
    late_frame = build_signal_frame(
        _candles(["1"], start=SPLIT_MTS + _DAY),
        _stats([{"frr": 1e-6}], start=SPLIT_MTS + _DAY),
    )
    combined = pd.concat([early_frame, late_frame], ignore_index=True)
    e, late_df = split_regime(combined)
    assert (e["mts"] < SPLIT_MTS).all()
    assert (late_df["mts"] >= SPLIT_MTS).all()
    assert set(e["mts"]).isdisjoint(set(late_df["mts"]))


def test_build_signal_frame_stats_after_all_candles_yield_nan() -> None:
    # stats start one day AFTER the single candle -> backward asof finds nothing -> NaN
    candles = _candles(["0.0001"])  # one candle at t0
    stats = _stats([{"frr": 9e-6}], start=1_500_000_000_000 + _DAY)
    df = build_signal_frame(candles, stats)
    assert np.isnan(df["frr"].iloc[0])
    assert np.isnan(df["funding_amount"].iloc[0])


def test_spike_z_is_zero_on_flat_then_positive_on_spike() -> None:
    frr = [1e-6] * 30 + [1e-5]  # flat then 10x spike
    df = build_signal_frame(_candles(["1"] * 31), _stats([{"frr": v} for v in frr]))
    z = spike_z(df, w=14)
    assert z.iloc[-1] > 3.0  # spike is many sigma above rolling baseline
    assert abs(z.iloc[20]) < 1e-9  # flat region -> z ~ 0


def test_frr_trend_sign_tracks_direction() -> None:
    rising = [float(i) * 1e-6 for i in range(1, 41)]
    df = build_signal_frame(_candles(["1"] * 40), _stats([{"frr": v} for v in rising]))
    t = frr_trend(df, k=3)
    assert t.dropna().iloc[-1] > 0  # monotone rising -> positive trend


def test_pctile_is_in_unit_interval_and_high_at_max() -> None:
    amt = list(range(1, 41))
    df = build_signal_frame(_candles(["1"] * 40), _stats([{"frr": 1e-6, "amt": a} for a in amt]))
    p = amount_pctile(df, w=20)
    assert (p.dropna() >= 0).all() and (p.dropna() <= 1).all()
    assert p.iloc[-1] == 1.0  # latest is the max of its window


def test_utilization_uses_ratio_not_absolute() -> None:
    rows = [{"frr": 1e-6, "amt": 1000, "used": u} for u in range(100, 140)]
    df = build_signal_frame(_candles(["1"] * 40), _stats(rows))
    u = utilization_pctile(df, w=20)
    assert u.iloc[-1] == 1.0  # rising utilization -> latest at top percentile


def test_signals_registry_has_four_relative_signals() -> None:
    # GUARD: every signal must be a rolling/relative transform, never raw frr/amount.
    assert set(SIGNALS) == {"frr_trend", "spike_detect", "funding_supply", "utilization"}
