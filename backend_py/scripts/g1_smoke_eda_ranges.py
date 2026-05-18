"""Phase 3b EDA-derived signal_score [min, max] per (strategy, cell).

Source: docs/research/2026-05-18-phase3b-wfo-results.md
Updated: 2026-05-18 (initial bounds -- widen if false-positives during first
        24hr live; narrow if too lax).

`signal_score` is normalized per strategy (see divergence_reporter._normalize_signal_score):
- rate_percentile: in {-1, +1} (discrete; range is informational)
- mean_reversion: in [-1, +1] (continuous)

TODO(phase-4.3): Once G2 calibration refines _normalize_signal_score, tighten
these bounds to per-cell EDA percentiles instead of full strategy range.
"""

EDA_RANGES: dict[tuple[str, str], dict[str, float]] = {
    # -- RatePercentile (discrete -1 / +1; range allows both)
    ("rate_percentile", "fUSD_p2"):  {"signal_score_min": -1.0, "signal_score_max": 1.0},
    ("rate_percentile", "fUSD_p30"): {"signal_score_min": -1.0, "signal_score_max": 1.0},
    ("rate_percentile", "fUSD_a30"): {"signal_score_min": -1.0, "signal_score_max": 1.0},
    ("rate_percentile", "fUST_p2"):  {"signal_score_min": -1.0, "signal_score_max": 1.0},
    ("rate_percentile", "fUST_p30"): {"signal_score_min": -1.0, "signal_score_max": 1.0},
    ("rate_percentile", "fUST_a30"): {"signal_score_min": -1.0, "signal_score_max": 1.0},

    # -- MeanReversion (continuous in [-1, 1])
    ("mean_reversion", "fUSD_p2"):  {"signal_score_min": -1.0, "signal_score_max": 1.0},
    ("mean_reversion", "fUSD_p30"): {"signal_score_min": -1.0, "signal_score_max": 1.0},
    ("mean_reversion", "fUSD_a30"): {"signal_score_min": -1.0, "signal_score_max": 1.0},
    ("mean_reversion", "fUST_p2"):  {"signal_score_min": -1.0, "signal_score_max": 1.0},
    ("mean_reversion", "fUST_a30"): {"signal_score_min": -1.0, "signal_score_max": 1.0},
}
