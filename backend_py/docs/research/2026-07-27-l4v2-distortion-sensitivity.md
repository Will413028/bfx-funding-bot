# L4 — Candle distortion sensitivity

Generated: 2026-07-27T09:13:17.926715+00:00

Replays the OOS evaluation with settled closes perturbed by the measured
distortion distribution. Answers whether the conclusions are fragile — NOT
what live would have earned (that series is unrecoverable).

- distortion rate: 0.17424242424242425 (a floor — see research doc)
- runs per cell: 500
- sample: n=20, single series, 7-day window

| cell | baseline median | perturbed p05 | p50 | p95 | baseline positive% | perturbed positive p05 |
|---|---|---|---|---|---|---|
| fUST_a30 | 0.5725% | 0.5352% | 0.5500% | 0.5675% | 100.0% | 100.0% |
| fUST_p2 | 0.5701% | 0.5261% | 0.5427% | 0.5545% | 100.0% | 100.0% |
| fUSD_a30 | 0.6218% | 0.5808% | 0.6021% | 0.6155% | 100.0% | 100.0% |
| fUSD_p2 | 0.6224% | 0.5554% | 0.5761% | 0.5945% | 100.0% | 100.0% |

## Raw

```json
{
  "generated_at": "2026-07-27T09:13:17.926715+00:00",
  "runs": 500,
  "distortion_rate": 0.17424242424242425,
  "cells": [
    {
      "cell": "fUST_a30",
      "baseline_secs_per_pass": 1.62,
      "baseline": {
        "median_monthly": 0.5725317957456418,
        "positive_month_ratio": 1.0,
        "n_windows": 87
      },
      "runs": 500,
      "distortion_rate": 0.17424242424242425,
      "perturbed_median_monthly": {
        "min": 0.5242464805578766,
        "p05": 0.5351643285440922,
        "p50": 0.5500233305424262,
        "p95": 0.5674975631710512,
        "max": 0.5772338491597068,
        "mean": 0.5509727366109389
      },
      "perturbed_positive_ratio": {
        "min": 1.0,
        "p05": 1.0,
        "p50": 1.0,
        "p95": 1.0,
        "max": 1.0,
        "mean": 1.0
      },
      "share_of_runs_below_baseline_median": 0.992
    },
    {
      "cell": "fUST_p2",
      "baseline_secs_per_pass": 1.62,
      "baseline": {
        "median_monthly": 0.5701385538616525,
        "positive_month_ratio": 1.0,
        "n_windows": 87
      },
      "runs": 500,
      "distortion_rate": 0.17424242424242425,
      "perturbed_median_monthly": {
        "min": 0.5097831207741019,
        "p05": 0.5260842928944305,
        "p50": 0.5427382255301192,
        "p95": 0.5545189948824688,
        "max": 0.5749646848155943,
        "mean": 0.5426241542472713
      },
      "perturbed_positive_ratio": {
        "min": 1.0,
        "p05": 1.0,
        "p50": 1.0,
        "p95": 1.0,
        "max": 1.0,
        "mean": 1.0
      },
      "share_of_runs_below_baseline_median": 0.998
    },
    {
      "cell": "fUSD_a30",
      "baseline_secs_per_pass": 2.8,
      "baseline": {
        "median_monthly": 0.6218194610079566,
        "positive_month_ratio": 1.0,
        "n_windows": 116
      },
      "runs": 500,
      "distortion_rate": 0.17424242424242425,
      "perturbed_median_monthly": {
        "min": 0.5683370571271656,
        "p05": 0.5808309041064705,
        "p50": 0.6020613210566576,
        "p95": 0.6155160710209693,
        "max": 0.6211889071983807,
        "mean": 0.6002630849476019
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
      "baseline_secs_per_pass": 2.87,
      "baseline": {
        "median_monthly": 0.6223549755485345,
        "positive_month_ratio": 1.0,
        "n_windows": 116
      },
      "runs": 500,
      "distortion_rate": 0.17424242424242425,
      "perturbed_median_monthly": {
        "min": 0.5351682550158496,
        "p05": 0.5553992254734541,
        "p50": 0.5761061169220951,
        "p95": 0.594540075595289,
        "max": 0.60895383725841,
        "mean": 0.5756854049265718
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
