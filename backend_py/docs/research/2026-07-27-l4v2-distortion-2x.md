# L4 — Candle distortion sensitivity

Generated: 2026-07-27T13:55:07.358889+00:00

Replays the OOS evaluation with settled closes perturbed by the measured
distortion distribution. Answers whether the conclusions are fragile — NOT
what live would have earned (that series is unrecoverable).

- distortion rate: 0.3485 (a floor — see research doc)
- runs per cell: 500
- sample: n=20, single series, 7-day window

| cell | baseline median | perturbed p05 | p50 | p95 | baseline positive% | perturbed positive p05 |
|---|---|---|---|---|---|---|
| fUST_a30 | 0.5725% | 0.5058% | 0.5255% | 0.5429% | 100.0% | 100.0% |
| fUST_p2 | 0.5701% | 0.4961% | 0.5174% | 0.5349% | 100.0% | 100.0% |
| fUSD_a30 | 0.6218% | 0.5481% | 0.5712% | 0.5906% | 100.0% | 100.0% |
| fUSD_p2 | 0.6224% | 0.5234% | 0.5449% | 0.5684% | 100.0% | 100.0% |

## Raw

```json
{
  "generated_at": "2026-07-27T13:55:07.358889+00:00",
  "runs": 500,
  "distortion_rate": 0.3485,
  "cells": [
    {
      "cell": "fUST_a30",
      "baseline_secs_per_pass": 1.67,
      "baseline": {
        "median_monthly": 0.5725317957456418,
        "positive_month_ratio": 1.0,
        "n_windows": 87
      },
      "runs": 500,
      "distortion_rate": 0.3485,
      "perturbed_median_monthly": {
        "min": 0.4962614210234473,
        "p05": 0.5057779200410605,
        "p50": 0.5254582143245937,
        "p95": 0.5429092582687078,
        "max": 0.56484007717051,
        "mean": 0.5250185467390496
      },
      "perturbed_positive_ratio": {
        "min": 1.0,
        "p05": 1.0,
        "p50": 1.0,
        "p95": 1.0,
        "max": 1.0,
        "mean": 1.0
      },
      "share_of_runs_below_baseline_median": 1.0
    },
    {
      "cell": "fUST_p2",
      "baseline_secs_per_pass": 1.65,
      "baseline": {
        "median_monthly": 0.5701385538616525,
        "positive_month_ratio": 1.0,
        "n_windows": 87
      },
      "runs": 500,
      "distortion_rate": 0.3485,
      "perturbed_median_monthly": {
        "min": 0.47158253619684026,
        "p05": 0.49609137099014317,
        "p50": 0.5173563442586155,
        "p95": 0.5349203072108483,
        "max": 0.5466367202580797,
        "mean": 0.5165069711262223
      },
      "perturbed_positive_ratio": {
        "min": 1.0,
        "p05": 1.0,
        "p50": 1.0,
        "p95": 1.0,
        "max": 1.0,
        "mean": 1.0
      },
      "share_of_runs_below_baseline_median": 1.0
    },
    {
      "cell": "fUSD_a30",
      "baseline_secs_per_pass": 2.73,
      "baseline": {
        "median_monthly": 0.6218194610079566,
        "positive_month_ratio": 1.0,
        "n_windows": 116
      },
      "runs": 500,
      "distortion_rate": 0.3485,
      "perturbed_median_monthly": {
        "min": 0.5319497176422703,
        "p05": 0.5480628719476291,
        "p50": 0.5711861769354442,
        "p95": 0.5905870746911517,
        "max": 0.6165748650730543,
        "mean": 0.5708859868675411
      },
      "perturbed_positive_ratio": {
        "min": 1.0,
        "p05": 1.0,
        "p50": 1.0,
        "p95": 1.0,
        "max": 1.0,
        "mean": 1.0
      },
      "share_of_runs_below_baseline_median": 1.0
    },
    {
      "cell": "fUSD_p2",
      "baseline_secs_per_pass": 2.79,
      "baseline": {
        "median_monthly": 0.6223549755485345,
        "positive_month_ratio": 1.0,
        "n_windows": 116
      },
      "runs": 500,
      "distortion_rate": 0.3485,
      "perturbed_median_monthly": {
        "min": 0.5066567992895037,
        "p05": 0.523392752259127,
        "p50": 0.5448727212227806,
        "p95": 0.5684164379437666,
        "max": 0.5935022380066279,
        "mean": 0.5454677887512466
      },
      "perturbed_positive_ratio": {
        "min": 1.0,
        "p05": 1.0,
        "p50": 1.0,
        "p95": 1.0,
        "max": 1.0,
        "mean": 1.0
      },
      "share_of_runs_below_baseline_median": 1.0
    }
  ]
}
```
