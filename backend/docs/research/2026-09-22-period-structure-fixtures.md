# Period-structure four-arm backtest (Proposal D)

**Run date**: 2026-09-22T16:05:31.751747+00:00
**Fill model**: linear-baseline, fill_alpha=5.0 (above-market quotes decay linearly; run again with a tiny alpha for the always-fill bound).
**Windows**: calendar WFO (train 3mo warmup / test 1mo / step 1mo), fresh state per window, locks truncated at window end.

## Reading (manual, 2026-09-22 — fixtures 2022-01..2026-05, fill_alpha 5.0; twin run `-fill1e-9` is the always-fill bound)

- **Tenor is the lever the deployed strategies never touch.** fUSD `always_30d` earns 11.5% net annualized vs 5.5% for `always_2d`: +0.47%/mo paired, 95% CI [0.34, 0.61], 92% of months, identical under both fill bounds (spread is 0 for both arms, so the linear model has no opinion). What the model cannot say: whether the 30-day book absorbs the offer — that is Gate A0 (period-30 depth from `funding_book_snapshots`) and the live B arm. fUST has no p30 fixture; the VM run adds it.
- **The a30 tenor mismatch was real and quantified.** `mr_a30_legacy − mr_a30` = +0.077%/mo (fUST) / +0.132%/mo (fUSD) at alpha 5, i.e. the deployed a30 cells' backtest edge over always-lend was an artefact of crediting aggregate rates to 2-day locks. Priced against p2, `mr_a30_vs_always_2d` straddles 0 (fUST −0.001 [−0.022, 0.019]; fUSD −0.009 [−0.039, 0.025]); under always-fill it is +0.08/+0.12. The a30 cell's value therefore rests entirely on whether above-p2 quotes fill — the same unresolved question as the FRR baseline (ADR 2026-09-22).
- **AdaptivePeriod survives tenor pricing.** +0.105%/mo (fUST) / +0.179%/mo (fUSD) over `always_2d`, CI excludes 0 under both bounds; 7/14-day tiers priced off a30 (approximation, ~20% of its trades).
- **Next**: VM run with fUST p30 + `always_frr` (D6); A0 depth audit before anyone trusts `always_30d` fills; if A0 shows period-30 depth, AP's `p_long` re-sweep to 30 is the registry-declared follow-up.
- **Regenerated 2026-09-23** after the engine stopped pricing off the observed candle when a market print is missing (`market_series_gap`); series are now aligned on common slots (fUST dropped 1, fUSD dropped 24). Every figure moved by ≤0.0002%/mo; conclusions unchanged.

## Standing caveats

- Linear fill model: above-market quotes decay linearly (alpha=5 -> +10% = 50% fill); the book-replay model (Proposal C) replaces this. Run with --fill-alpha 1e-9 for the always-fill bound; a pair whose CI straddles 0 under both bounds is 'wait for live'.
- Tenor pricing: 2d -> p2, 30d -> p30, every other tenor (AP's 7/14) -> a30 aggregate, an approximation flagged per arm in the 'priced by' column.
- p30 is LOCF'd onto the hourly grid with the cells.yaml 12h staleness budget; slots beyond budget carry no decision.
- Locks are truncated at window end and every window starts with free capital; monthly figures are 'interest earned in-window on locks opened in-window'.
- Deployed MR/AP params were selected on 2022-2026 data, in-sample to selection.
- Platform/credit tail (Bitfinex, Tether) is not backtestable; longer locks lengthen it.

## fUST — 49 monthly OOS windows (2022-01-01 .. 2026-05-28)

- always_30d skipped: no p30 series for this symbol in this data mode
- adaptive_period: ema_span=24 band=(0.5,2.0) p_mid=7 p_long=14 ratio_sigma=0.42049266874194213 (ADR 2026-06-04 D2/D3 locked band; ratio_sigma from ap cells)
- always_frr skipped: funding_stats not available in this data mode
- series: a30=38588 candles, p2=38588 candles; dropped 1 slots not present in every series (engine refuses to price a decision without a market print)

| arm | annualized net % | median monthly % | p25 | worst | best | mean | idle rate | mean fill | priced by |
|---|---|---|---|---|---|---|---|---|---|
| always_2d | 6.01 | 0.4635 | 0.3856 | 0.2619 | 0.8512 | 0.4876 | 0.00 | 1.0000 | p2:731 |
| mr_p2 | 7.09 | 0.5237 | 0.4453 | 0.3027 | 1.1296 | 0.5726 | 0.00 | 1.0000 | p2:727 |
| mr_a30 | 6.00 | 0.4442 | 0.3843 | 0.2153 | 1.0255 | 0.4867 | 0.00 | 0.8995 | p2:727 |
| mr_a30_legacy | 6.98 | 0.5340 | 0.4576 | 0.2319 | 1.1265 | 0.5639 | 0.00 | 1.0000 | observed |
| adaptive_period | 7.35 | 0.5821 | 0.4641 | 0.2060 | 1.1798 | 0.5928 | 0.00 | 0.9828 | a30:102, p2:390 |

### Paired monthly differences (a − b, % per month)

| pair | median | mean [95% CI] | IR | months a>b |
|---|---|---|---|---|
| adaptive_period_vs_always_2d | 0.0653 | 0.1052 [0.0700, 0.1441] | 0.81 | 81.6% |
| mr_a30_vs_always_2d | 0.0046 | -0.0009 [-0.0222, 0.0192] | -0.01 | 55.1% |
| mr_a30_legacy_vs_mr_a30 | 0.0574 | 0.0772 [0.0556, 0.1018] | 0.93 | 95.9% |
| mr_p2_vs_always_2d | 0.0572 | 0.0850 [0.0564, 0.1241] | 0.69 | 81.6% |

### Per-year mean net monthly %

| year | always_2d | mr_p2 | mr_a30 | mr_a30_legacy | adaptive_period |
|---|---|---|---|---|---|
| 2022 | 0.3995 | 0.5863 | 0.4200 | 0.4882 | 0.5138 |
| 2023 | 0.5916 | 0.7070 | 0.6254 | 0.7069 | 0.6953 |
| 2024 | 0.5321 | 0.5764 | 0.5107 | 0.5764 | 0.6669 |
| 2025 | 0.4555 | 0.4795 | 0.4117 | 0.5142 | 0.5462 |
| 2026 | 0.3368 | 0.4060 | 0.3739 | 0.4173 | 0.3806 |

## fUSD — 48 monthly OOS windows (2022-01-01 .. 2026-05-25)

- adaptive_period: ema_span=24 band=(0.5,2.0) p_mid=7 p_long=14 ratio_sigma=0.3766563427150627 (ADR 2026-06-04 D2/D3 locked band; ratio_sigma from ap cells)
- always_frr skipped: funding_stats not available in this data mode
- series: a30=38519 candles, p2=38519 candles, p30=38519 candles; dropped 24 slots not present in every series (engine refuses to price a decision without a market print)

| arm | annualized net % | median monthly % | p25 | worst | best | mean | idle rate | mean fill | priced by |
|---|---|---|---|---|---|---|---|---|---|
| always_2d | 5.48 | 0.4355 | 0.3753 | 0.2474 | 0.7234 | 0.4457 | 0.00 | 1.0000 | p2:716 |
| always_30d | 11.54 | 0.8356 | 0.5429 | 0.3769 | 2.5500 | 0.9153 | 0.00 | 1.0000 | p30:76 |
| mr_p2 | 6.47 | 0.4702 | 0.4025 | 0.2611 | 1.1347 | 0.5237 | 0.00 | 1.0000 | p2:711 |
| mr_a30 | 5.38 | 0.4118 | 0.3124 | 0.2227 | 1.0061 | 0.4379 | 0.00 | 0.8422 | p2:710 |
| mr_a30_legacy | 7.04 | 0.5113 | 0.4220 | 0.2611 | 1.1802 | 0.5688 | 0.00 | 1.0000 | observed |
| adaptive_period | 7.78 | 0.5260 | 0.4487 | 0.2838 | 1.9435 | 0.6265 | 0.00 | 0.9661 | a30:82, p2:402 |

### Paired monthly differences (a − b, % per month)

| pair | median | mean [95% CI] | IR | months a>b |
|---|---|---|---|---|
| always_30d_vs_always_2d | 0.3601 | 0.4696 [0.3391, 0.6132] | 0.97 | 91.7% |
| adaptive_period_vs_always_2d | 0.0904 | 0.1808 [0.1126, 0.2587] | 0.70 | 85.4% |
| mr_a30_vs_always_2d | -0.0219 | -0.0078 [-0.0380, 0.0260] | -0.07 | 39.6% |
| mr_a30_legacy_vs_mr_a30 | 0.0839 | 0.1309 [0.0893, 0.1801] | 0.82 | 95.8% |
| mr_p2_vs_always_2d | 0.0333 | 0.0780 [0.0451, 0.1185] | 0.60 | 81.2% |

### Per-year mean net monthly %

| year | always_2d | always_30d | mr_p2 | mr_a30 | mr_a30_legacy | adaptive_period |
|---|---|---|---|---|---|---|
| 2022 | 0.4703 | 1.2409 | 0.7159 | 0.5170 | 0.7017 | 0.6289 |
| 2023 | 0.5059 | 1.1710 | 0.5790 | 0.5100 | 0.6744 | 0.7760 |
| 2024 | 0.3951 | 0.7690 | 0.4174 | 0.3798 | 0.4494 | 0.5450 |
| 2025 | 0.4420 | 0.6056 | 0.4938 | 0.3970 | 0.5243 | 0.5966 |
| 2026 | 0.3782 | 0.8654 | 0.3814 | 0.3598 | 0.4777 | 0.5073 |
