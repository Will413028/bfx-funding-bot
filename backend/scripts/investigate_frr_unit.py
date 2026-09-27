"""scripts/investigate_frr_unit.py -- Phase 3a Stage 2.

Pulls (mts, symbol, frr, avg_period, candle.close) sample from Neon by
JOINing funding_stats point-in-time (latest fs.mts <= candle.mts) with
funding_candles (1h p2). Fits 5 named hypotheses via OLS regression
(close = a x predictor + b), computes R2 + per-year/per-symbol slope
invariance + median relative error.

Writes data/research/frr_hypothesis_results.json. Exit 0 if at least one
hypothesis passes Gate 1 (R²>0.99 + slope_cv<0.05 + slope_diff<0.05 +
median_rel_err<0.05); exit 1 if all fail.

Outcome (every hypothesis failed: FRR is not a unit-convertible market-rate
proxy) is recorded in backend/ARCHITECTURE.md, backtest engine contract.
"""
from __future__ import annotations

import asyncio
import json
import sys
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pandas as pd
from sqlalchemy import text

from bfx_funding_bot.core.db import (
    make_engine,
    make_session_factory,
    session_scope,
)
from bfx_funding_bot.core.settings import Settings

HYPOTHESES: dict[str, dict[str, object]] = {
    "H1": {
        "label": "frr is per-day rate",
        "predictor": lambda df: df["frr"],
        "requires_avg_period": False,
    },
    "H2": {
        "label": "frr is per-second rate",
        "predictor": lambda df: df["frr"] * 86400.0,
        "requires_avg_period": False,
    },
    "H3": {
        "label": "frr x avg_period (per-period rate)",
        "predictor": lambda df: df["frr"] * df["avg_period"],
        "requires_avg_period": True,
    },
    "H4": {
        "label": "frr x avg_period x 86400 (per-period-second hybrid)",
        "predictor": lambda df: df["frr"] * df["avg_period"] * 86400.0,
        "requires_avg_period": True,
    },
    "H5": {
        "label": "frr / 365 (annualized rate)",
        "predictor": lambda df: df["frr"] / 365.0,
        "requires_avg_period": False,
    },
}

GATE_R2_MIN = 0.99
GATE_SLOPE_CV_MAX = 0.05
GATE_SLOPE_DIFF_MAX = 0.05
GATE_MEDIAN_REL_ERR_MAX = 0.05

LOAD_SQL = """
SELECT
    c.mts AS mts,
    c.symbol AS symbol,
    (
        SELECT fs.frr FROM funding_stats fs
        WHERE fs.symbol = c.symbol AND fs.mts <= c.mts
        ORDER BY fs.mts DESC LIMIT 1
    ) AS frr,
    (
        SELECT fs.avg_period FROM funding_stats fs
        WHERE fs.symbol = c.symbol AND fs.mts <= c.mts
        ORDER BY fs.mts DESC LIMIT 1
    ) AS avg_period,
    c.close AS close
FROM funding_candles c
WHERE c.symbol IN ('fUSD', 'fUST')
  AND c.timeframe = '1h'
  AND c.period_agg = 'p2'
  AND c.close IS NOT NULL
ORDER BY c.mts ASC
"""


async def load_sample() -> pd.DataFrame:
    settings = Settings()
    engine = make_engine(settings)
    factory = make_session_factory(engine)
    try:
        async with session_scope(factory) as session:
            result = await session.execute(text(LOAD_SQL))
            rows = result.fetchall()
    finally:
        await engine.dispose()
    df = pd.DataFrame(rows, columns=["mts", "symbol", "frr", "avg_period", "close"])
    df = df.dropna(subset=["frr", "avg_period", "close"])
    df["frr"] = df["frr"].astype(float)
    df["avg_period"] = df["avg_period"].astype(float)
    df["close"] = df["close"].astype(float)
    df = df[(df["close"] > 0) & (df["frr"] > 0) & (df["avg_period"] > 0)]
    df["year"] = pd.to_datetime(df["mts"], unit="ms", utc=True).dt.year
    return df.reset_index(drop=True)


def fit_ols(predictor: np.ndarray, target: np.ndarray) -> tuple[float, float, float]:
    """Returns (slope, intercept, r2). NaN for any if input too small."""
    if len(predictor) < 2 or np.var(predictor) == 0:
        return float("nan"), float("nan"), float("nan")
    design = np.vstack([predictor, np.ones(len(predictor))]).T
    coef, *_ = np.linalg.lstsq(design, target, rcond=None)
    slope, intercept = float(coef[0]), float(coef[1])
    pred = slope * predictor + intercept
    ss_res = float(np.sum((target - pred) ** 2))
    ss_tot = float(np.sum((target - target.mean()) ** 2))
    r2 = 1.0 - ss_res / ss_tot if ss_tot > 0 else 0.0
    return slope, intercept, r2


def evaluate_hypothesis(
    hid: str,
    hdef: dict[str, object],
    df: pd.DataFrame,
) -> dict[str, object]:
    predictor_fn: Callable[..., pd.Series] = hdef["predictor"]  # type: ignore[assignment]
    pred_full = predictor_fn(df).to_numpy(dtype=float)
    target_full = df["close"].to_numpy(dtype=float)
    valid = (
        ~np.isnan(pred_full)
        & ~np.isnan(target_full)
        & ~np.isinf(pred_full)
        & ~np.isinf(target_full)
    )
    pred = pred_full[valid]
    target = target_full[valid]
    df_v = df.loc[valid].reset_index(drop=True)

    slope, intercept, r2 = fit_ols(pred, target)
    if np.isnan(slope):
        median_rel_err = float("nan")
    else:
        reconstructed = slope * pred + intercept
        denom = np.where(target != 0, np.abs(target), 1.0)
        median_rel_err = float(np.median(np.abs(target - reconstructed) / denom))

    # Per-year slope invariance
    per_year_slopes: dict[int, float] = {}
    for year, group in df_v.groupby("year"):
        if len(group) < 50:
            continue
        gp = predictor_fn(group).to_numpy(dtype=float)
        gt = group["close"].to_numpy(dtype=float)
        s, _, _ = fit_ols(gp, gt)
        if not np.isnan(s):
            per_year_slopes[int(year)] = s
    if per_year_slopes:
        arr = np.array(list(per_year_slopes.values()))
        mean_s = float(arr.mean())
        slope_cv = float(arr.std() / abs(mean_s)) if mean_s != 0 else float("nan")
    else:
        slope_cv = float("nan")

    # Per-symbol slope invariance
    per_symbol_slopes: dict[str, float] = {}
    for sym, group in df_v.groupby("symbol"):
        if len(group) < 50:
            continue
        gp = predictor_fn(group).to_numpy(dtype=float)
        gt = group["close"].to_numpy(dtype=float)
        s, _, _ = fit_ols(gp, gt)
        if not np.isnan(s):
            per_symbol_slopes[str(sym)] = s
    if len(per_symbol_slopes) >= 2:
        sym_arr = np.array(list(per_symbol_slopes.values()))
        mean_s = float(sym_arr.mean())
        slope_diff = float(
            (sym_arr.max() - sym_arr.min()) / abs(mean_s)
        ) if mean_s != 0 else float("nan")
    else:
        slope_diff = float("nan")

    pass_gate = bool(
        not np.isnan(r2)
        and r2 > GATE_R2_MIN
        and not np.isnan(slope_cv)
        and slope_cv < GATE_SLOPE_CV_MAX
        and not np.isnan(slope_diff)
        and slope_diff < GATE_SLOPE_DIFF_MAX
        and not np.isnan(median_rel_err)
        and median_rel_err < GATE_MEDIAN_REL_ERR_MAX
    )

    return {
        "id": hid,
        "predictor_label": hdef["label"],
        "requires_avg_period": hdef["requires_avg_period"],
        "r2": r2,
        "slope": slope,
        "intercept": intercept,
        "median_rel_err": median_rel_err,
        "per_year_slope_cv": slope_cv,
        "per_symbol_slope_diff": slope_diff,
        "per_year_slopes": {str(k): v for k, v in sorted(per_year_slopes.items())},
        "per_symbol_slopes": dict(sorted(per_symbol_slopes.items())),
        "pass_gate": pass_gate,
    }


def select_winner(results: list[dict[str, object]]) -> dict[str, object] | None:
    """Highest R² wins; tie-break by simpler predictor (H1 < H2 < H3 < H4 < H5)."""
    passing = [r for r in results if r["pass_gate"]]
    if not passing:
        return None
    passing.sort(key=lambda r: (-r["r2"], r["id"]))  # type: ignore[operator]
    return passing[0]


async def amain() -> int:
    df = await load_sample()
    print(f"Loaded {len(df)} samples after dropna/positivity filter")
    print(f"Symbols: {df['symbol'].value_counts().to_dict()}")
    print(f"Year range: {int(df['year'].min())}-{int(df['year'].max())}")

    results = [evaluate_hypothesis(hid, hdef, df) for hid, hdef in HYPOTHESES.items()]
    winner = select_winner(results)

    output: dict[str, object] = {
        "generated_at_utc": datetime.now(UTC).isoformat(),
        "sample_size": len(df),
        "symbols_used": sorted(df["symbol"].unique().tolist()),
        "year_range": [int(df["year"].min()), int(df["year"].max())],
        "gate_thresholds": {
            "r2_min": GATE_R2_MIN,
            "slope_cv_max": GATE_SLOPE_CV_MAX,
            "slope_diff_max": GATE_SLOPE_DIFF_MAX,
            "median_rel_err_max": GATE_MEDIAN_REL_ERR_MAX,
        },
        "hypotheses": results,
        "winning_hypothesis": winner["id"] if winner else None,
        "winning_factor": winner["slope"] if winner else None,
        "winning_intercept": winner["intercept"] if winner else None,
        "winning_requires_avg_period": (
            winner["requires_avg_period"] if winner else False
        ),
    }

    out_path = Path(__file__).parent.parent / "data" / "research" / "frr_hypothesis_results.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(output, indent=2, default=str))
    print(f"Wrote {out_path}")

    print()
    print("=== Hypothesis Results ===")
    for r in results:
        mark = "✓" if r["pass_gate"] else "✗"
        print(
            f"[{mark}] {r['id']} ({r['predictor_label']})"
            f" — R²={r['r2']:.4f}, slope_cv={r['per_year_slope_cv']:.4f},"
            f" slope_diff={r['per_symbol_slope_diff']:.4f},"
            f" median_rel_err={r['median_rel_err']:.4f}"
        )

    if winner:
        print()
        print(f"🎯 Winning hypothesis: {winner['id']} — {winner['predictor_label']}")
        print(f"   slope (conversion factor) = {winner['slope']:.10e}")
        print(f"   intercept                  = {winner['intercept']:.10e}")
        print(f"   requires avg_period        = {winner['requires_avg_period']}")
        return 0

    print()
    print("❌ Gate 1 FAIL — no hypothesis met all 4 thresholds.")
    print("   See data/research/frr_hypothesis_results.json for diagnostics.")
    return 1


def main() -> None:
    sys.exit(asyncio.run(amain()))


if __name__ == "__main__":
    main()
