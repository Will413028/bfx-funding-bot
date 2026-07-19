# src/bfx_funding_bot/modules/backtest/signal_eda.py
"""Offline signal-screening EDA funnel.

Pure functions only (no DB/I/O). Scores candidate signals for predictive power
against forward market-rate movement. See
docs/superpowers/specs/2026-06-05-signal-eda-funnel-design.md.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
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
    candle inside the window get NaN.

    Intended for a DAILY frame (call resample_daily first); the per-row mask is
    O(n^2) and will be slow on sub-daily frames."""
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


def frr_curvature(df: pd.DataFrame, k: int) -> pd.Series:
    """Discrete second difference of FRR: (frr_t - frr_{t-k}) - (frr_{t-k} - frr_{t-2k}),
    normalized by rolling std. Captures trend curvature/inflection (rate-of-change of
    the rate-of-change, i.e. acceleration) rather than frr_trend's first-order
    momentum. Relative form (D4): normalized by the rolling std of raw FRR (its
    scale), not of the second difference itself.

    2026-07-19 phase3c re-operationalization candidate (post frr_trend/spike_detect
    KILL, 2026-06-06 ADR): a sign-stable first-order trend can still miss the
    inflection point where a rate move is decelerating/about to reverse -- curvature
    targets exactly that turning-point signal."""
    frr = df["frr"]
    d1 = frr - frr.shift(k)
    d2 = d1 - d1.shift(k)
    return d2 / frr.rolling(max(k * 6, 12), min_periods=k * 2).std()


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


def spike_pctile(df: pd.DataFrame, w: int) -> pd.Series:
    """Rolling percentile rank of FRR within its own trailing window: a quantile-based
    spike detector. Unlike spike_z (z-score), a large spike cannot inflate its own
    baseline here -- percentile rank is order-based, not moment-based -- so it is
    robust to the heavy-tailed FRR distribution (documented per-year FRR drift/spikes)
    that can dilute a z-score's read of the very spike it targets.

    2026-07-19 phase3c re-operationalization candidate (post spike_detect KILL,
    2026-06-06 ADR): reuses _rolling_pctile (already vetted for funding_supply /
    utilization) applied to FRR itself."""
    return _rolling_pctile(df["frr"], w)


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

_MIN_IC_PAIRS = 10        # minimum non-null pairs for a meaningful rank correlation
_MIN_QUINTILE_PAIRS = 25  # 5 quintiles x 5 observations minimum


def spearman_ic(signal: pd.Series, target: pd.Series) -> float:
    """Spearman rank IC between signal_t and forward target. NaN if < 10 paired
    non-null observations. Uses numpy ranks to avoid scipy dependency.
    Returns NaN for a constant (zero-variance) signal or target."""
    pair = pd.concat(
        [signal.reset_index(drop=True), target.reset_index(drop=True)], axis=1
    ).dropna()
    if len(pair) < _MIN_IC_PAIRS:
        return float("nan")
    rx = pair.iloc[:, 0].rank()
    ry = pair.iloc[:, 1].rank()
    if rx.std() == 0 or ry.std() == 0:  # constant signal/target -> IC undefined
        return float("nan")
    return float(rx.corr(ry))  # Pearson-on-ranks == Spearman; avoids scipy dependency


def quintile_spread(signal: pd.Series, target: pd.Series) -> tuple[float, bool]:
    """Mean target in the top signal-quintile minus the bottom, plus whether the
    5 bucket means are monotone. NaN spread if < 25 pairs or quintiles collapse."""
    pair = pd.concat(
        [signal.reset_index(drop=True).rename("s"), target.reset_index(drop=True).rename("t")],
        axis=1,
    ).dropna()
    if len(pair) < _MIN_QUINTILE_PAIRS:
        return float("nan"), False
    try:
        pair["q"] = pd.qcut(pair["s"], 5, labels=False, duplicates="drop")
    except ValueError:
        return float("nan"), False
    means = pair.groupby("q")["t"].mean()
    if len(means) < 2:
        return float("nan"), False
    spread = float(means.iloc[-1] - means.iloc[0])
    monotonic = bool(means.is_monotonic_increasing or means.is_monotonic_decreasing)
    return spread, monotonic


_MIN_BOOTSTRAP = 30


@dataclass(frozen=True)
class BootstrapIC:
    """Result of block_bootstrap_ic.

    point:       Spearman IC on the full paired sample.
    ci_lo/ci_hi: percentile CI from the bootstrap distribution (default 95%).
    p_value:     two-sided crossing rate = 2 * min(frac_pos, frac_neg) over
                 non-NaN bootstrap ICs. 0.0 is a VALID floor (no resample
                 crossed zero), not a missing value.
    n:           number of paired (non-NaN) observations used.
    """
    point: float
    ci_lo: float
    ci_hi: float
    p_value: float
    n: int


def block_bootstrap_ic(
    signal: pd.Series,
    target: pd.Series,
    *,
    block_size: int = 20,
    n_boot: int = 1000,
    seed: int = SEED,
    alpha: float = 0.05,
) -> BootstrapIC:
    """Circular block-bootstrap CI + two-sided p-value for the Spearman IC.

    Block resampling preserves serial autocorrelation (a plain bootstrap would
    understate the CI on a daily rate series). p_value is the two-sided bootstrap
    crossing rate over non-NaN resamples. All-NaN result if < 30 paired
    observations. Zero-variance resample blocks yield NaN ICs that are dropped.
    n_boot=1000 gives stable 95% CI on typical EDA sizes (<=5k rows)."""
    pair = (
        pd.concat([signal.reset_index(drop=True), target.reset_index(drop=True)], axis=1)
        .dropna()
        .to_numpy()
    )
    n = len(pair)
    if n < _MIN_BOOTSTRAP:
        return BootstrapIC(float("nan"), float("nan"), float("nan"), float("nan"), n)
    point = spearman_ic(pd.Series(pair[:, 0]), pd.Series(pair[:, 1]))
    rng = np.random.default_rng(seed)
    n_blocks = int(np.ceil(n / block_size))
    ics = np.empty(n_boot)
    # block_size=20: ~3-4x the typical ~5-day autocorrelation length of daily
    # funding rates; errs conservative (wider CI) than a tighter block.
    with np.errstate(invalid="ignore", divide="ignore"):
        for b in range(n_boot):
            starts = rng.integers(0, n, n_blocks)
            idx = np.concatenate([(np.arange(s, s + block_size) % n) for s in starts])[:n]
            samp = pair[idx]
            rx = pd.Series(samp[:, 0]).rank()
            ry = pd.Series(samp[:, 1]).rank()
            ics[b] = rx.corr(ry) if (rx.std() != 0 and ry.std() != 0) else np.nan
    valid = ics[~np.isnan(ics)]
    if len(valid) == 0:
        return BootstrapIC(point, float("nan"), float("nan"), float("nan"), n)
    lo, hi = np.percentile(valid, [100 * alpha / 2, 100 * (1 - alpha / 2)])
    frac_pos = float(np.mean(valid > 0))
    frac_neg = float(np.mean(valid < 0))
    p_value = 2.0 * min(frac_pos, frac_neg)
    return BootstrapIC(point, float(lo), float(hi), p_value, n)


def bh_fdr(pvalues: list[float], *, alpha: float = 0.05) -> list[bool]:
    """Benjamini-Hochberg step-up. Returns a rejected-mask aligned to the input
    order. NaN p-values are treated as 1.0 (never rejected). Empty -> empty."""
    m = len(pvalues)
    if m == 0:
        return []
    clean = [1.0 if (p is None or np.isnan(p)) else float(p) for p in pvalues]
    order = sorted(range(m), key=lambda i: clean[i])
    max_rank = 0
    for rank, i in enumerate(order, start=1):
        if clean[i] <= rank / m * alpha:
            max_rank = rank
    rejected = [False] * m
    for rank, i in enumerate(order, start=1):
        if rank <= max_rank:
            rejected[i] = True
    return rejected


# ---------------------------------------------------------------------------
# Task 6: GO/KILL decision over the cell × regime grid
# ---------------------------------------------------------------------------

IC_FLOOR = 0.03  # empirical minimum |IC| for a defensible tradeable edge on low-SNR funding data
CELL_MAJORITY = 3  # of 4 cells

_CELLS = ("fUST_a30", "fUST_p2", "fUSD_a30", "fUSD_p2")


@dataclass(frozen=True)
class CellRegimeIC:
    cell: str
    regime: str  # "early" | "late"
    horizon: int
    ic: float
    fdr_significant: bool


@dataclass(frozen=True)
class SignalVerdict:
    signal: str
    verdict: str  # "GO" | "KILL"
    reason: str
    median_ic: float


def _sign(x: float) -> int:
    return (x > 0) - (x < 0)


def decide_signal(signal: str, obs: list[CellRegimeIC]) -> SignalVerdict:
    """Apply the four GO gates (spec §2): FDR-significant, same sign across both
    regimes, same sign across >=3/4 cells, |median IC| >= floor. Otherwise KILL
    with the binding reason. median_ic is over all non-NaN observations."""
    sig = [o for o in obs if o.fdr_significant]
    valid = [o.ic for o in obs if not np.isnan(o.ic)]
    median_ic = float(np.median(valid)) if valid else float("nan")

    if not sig:
        return SignalVerdict(signal, "KILL", "no FDR-significant IC in any cell/regime", median_ic)

    # cells where BOTH regimes are significant and agree in sign
    robust_cell_signs: list[int] = []
    for cell in _CELLS:
        ce = [o for o in sig if o.cell == cell]
        early = [o for o in ce if o.regime == "early"]
        late = [o for o in ce if o.regime == "late"]
        if not early or not late:
            continue
        se = _sign(float(np.median([o.ic for o in early])))
        sl = _sign(float(np.median([o.ic for o in late])))
        if se != 0 and se == sl:
            robust_cell_signs.append(se)

    if not robust_cell_signs:
        return SignalVerdict(signal, "KILL", "sign flips across regimes (regime-fragile)", median_ic)
    if len(robust_cell_signs) < CELL_MAJORITY:
        return SignalVerdict(
            signal, "KILL",
            f"only {len(robust_cell_signs)}/4 cells robust (< {CELL_MAJORITY} majority)",
            median_ic,
        )
    if abs(sum(robust_cell_signs)) < CELL_MAJORITY:
        return SignalVerdict(signal, "KILL", "robust cells disagree in sign", median_ic)
    if abs(median_ic) < IC_FLOOR:
        return SignalVerdict(
            signal, "KILL", f"median |IC| {abs(median_ic):.3f} below floor {IC_FLOOR}", median_ic
        )
    return SignalVerdict(
        signal, "GO", f"{len(robust_cell_signs)}/4 cells robust, median IC {median_ic:.3f}", median_ic
    )


def render_report(verdicts: list[SignalVerdict], *, data_window: str) -> str:
    """Markdown summary: GO/KILL table + per-signal reason. Null-result safe
    (renders even if every signal is KILL)."""
    lines = [
        "# Signal EDA Funnel",
        "",
        f"**Data window:** {data_window}",
        "",
        "Screens 4 candidate signals (FRR-trend, SpikeDetect, funding_supply, "
        "utilization) for predictive power against forward rate change. GO requires "
        "FDR-significant IC, same sign across both regimes, >=3/4 cells robust, and "
        "|median IC| >= 0.03. Null result (all KILL) is a valid conclusion.",
        "",
        "## Verdicts",
        "",
        "| Signal | Verdict | Median IC | Reason |",
        "| --- | --- | --- | --- |",
    ]
    go = [v for v in verdicts if v.verdict == "GO"]
    for v in verdicts:
        lines.append(f"| {v.signal} | **{v.verdict}** | {v.median_ic:.3f} | {v.reason} |")
    lines += ["", "## Promotion", ""]
    if go:
        lines.append("Signals to promote to full strategy (engine plumbing + WFO/OOS):")
        lines += [f"- **{v.signal}** (median IC {v.median_ic:.3f})" for v in go]
    else:
        lines.append(
            "**Null result** — no signal cleared the bar. None promoted. "
            "Candidate pool unchanged; these signals are recorded as falsified."
        )
    return "\n".join(lines) + "\n"


def resample_daily(frame: pd.DataFrame) -> pd.DataFrame:
    """Collapse a sub-daily frame to one row per UTC day (the day's last
    observation). Raw funding candles are hourly; the signal rolling windows
    count ROWS, so without daily resampling a w=14 window would span ~14 hours
    instead of 14 days, and the O(n^2) forward window would be ~24x larger.
    Empty -> empty."""
    if frame.empty:
        return frame
    day = frame["mts"] // 86_400_000
    return frame.groupby(day, sort=True).last().reset_index(drop=True)
