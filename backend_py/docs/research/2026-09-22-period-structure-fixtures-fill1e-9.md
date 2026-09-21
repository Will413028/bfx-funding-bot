# Period-structure four-arm backtest (Proposal D)

**Run date**: 2026-09-21T22:57:23.388163+00:00
**Fill model**: linear-baseline, fill_alpha=1E-9 (above-market quotes decay linearly; run again with a tiny alpha for the always-fill bound).
**Windows**: calendar WFO (train 3mo warmup / test 1mo / step 1mo), fresh state per window, locks truncated at window end.

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
- series: a30=38589 candles, p2=38588 candles

| arm | annualized net % | median monthly % | p25 | worst | best | mean | idle rate | mean fill | priced by |
|---|---|---|---|---|---|---|---|---|---|
| always_2d | 6.01 | 0.4635 | 0.3856 | 0.2619 | 0.8512 | 0.4876 | 0.00 | 1.0000 | p2:731 |
| mr_p2 | 7.09 | 0.5237 | 0.4453 | 0.3027 | 1.1296 | 0.5726 | 0.00 | 1.0000 | p2:727 |
| mr_a30 | 6.98 | 0.5340 | 0.4576 | 0.2319 | 1.1265 | 0.5638 | 0.00 | 1.0000 | p2:727 |
| mr_a30_legacy | 6.98 | 0.5340 | 0.4576 | 0.2319 | 1.1265 | 0.5638 | 0.00 | 1.0000 | observed |
| adaptive_period | 7.45 | 0.5910 | 0.4738 | 0.2060 | 1.1884 | 0.6005 | 0.00 | 1.0000 | a30:102, p2:388 |

### Paired monthly differences (a − b, % per month)

| pair | median | mean [95% CI] | IR | months a>b |
|---|---|---|---|---|
| adaptive_period_vs_always_2d | 0.0970 | 0.1129 [0.0795, 0.1500] | 0.90 | 91.8% |
| mr_a30_vs_always_2d | 0.0628 | 0.0762 [0.0567, 0.0971] | 1.05 | 91.8% |
| mr_a30_legacy_vs_mr_a30 | 0.0000 | 0.0000 [0.0000, 0.0000] | 0.37 | 95.9% |
| mr_p2_vs_always_2d | 0.0572 | 0.0850 [0.0564, 0.1241] | 0.69 | 81.6% |

### Per-year mean net monthly %

| year | always_2d | mr_p2 | mr_a30 | mr_a30_legacy | adaptive_period |
|---|---|---|---|---|---|
| 2022 | 0.3995 | 0.5863 | 0.4882 | 0.4882 | 0.5157 |
| 2023 | 0.5916 | 0.7070 | 0.7069 | 0.7069 | 0.7080 |
| 2024 | 0.5321 | 0.5764 | 0.5764 | 0.5764 | 0.6737 |
| 2025 | 0.4555 | 0.4795 | 0.5142 | 0.5142 | 0.5562 |
| 2026 | 0.3368 | 0.4060 | 0.4164 | 0.4164 | 0.3821 |

## fUSD — 48 monthly OOS windows (2022-01-01 .. 2026-05-25)

- adaptive_period: ema_span=24 band=(0.5,2.0) p_mid=7 p_long=14 ratio_sigma=0.3766563427150627 (ADR 2026-06-04 D2/D3 locked band; ratio_sigma from ap cells)
- always_frr skipped: funding_stats not available in this data mode
- series: a30=38523 candles, p2=38519 candles, p30=38539 candles

| arm | annualized net % | median monthly % | p25 | worst | best | mean | idle rate | mean fill | priced by |
|---|---|---|---|---|---|---|---|---|---|
| always_2d | 5.48 | 0.4355 | 0.3753 | 0.2474 | 0.7234 | 0.4457 | 0.00 | 1.0000 | p2:716 |
| always_30d | 11.54 | 0.8356 | 0.5429 | 0.3769 | 2.5500 | 0.9155 | 0.00 | 1.0000 | p30:76 |
| mr_p2 | 6.47 | 0.4702 | 0.4025 | 0.2611 | 1.1347 | 0.5237 | 0.00 | 1.0000 | p2:711 |
| mr_a30 | 7.04 | 0.5113 | 0.4220 | 0.2611 | 1.1802 | 0.5689 | 0.00 | 1.0000 | p2:710 |
| mr_a30_legacy | 7.04 | 0.5113 | 0.4220 | 0.2611 | 1.1802 | 0.5689 | 0.00 | 1.0000 | observed |
| adaptive_period | 7.88 | 0.5340 | 0.4606 | 0.2886 | 1.9435 | 0.6348 | 0.00 | 1.0000 | a30:81, p2:403 |

### Paired monthly differences (a − b, % per month)

| pair | median | mean [95% CI] | IR | months a>b |
|---|---|---|---|---|
| always_30d_vs_always_2d | 0.3601 | 0.4698 [0.3393, 0.6133] | 0.97 | 91.7% |
| adaptive_period_vs_always_2d | 0.1110 | 0.1891 [0.1207, 0.2675] | 0.73 | 87.5% |
| mr_a30_vs_always_2d | 0.0719 | 0.1232 [0.0828, 0.1701] | 0.80 | 95.8% |
| mr_a30_legacy_vs_mr_a30 | 0.0000 | 0.0000 [0.0000, 0.0000] | 0.64 | 97.9% |
| mr_p2_vs_always_2d | 0.0333 | 0.0780 [0.0451, 0.1185] | 0.60 | 81.2% |

### Per-year mean net monthly %

| year | always_2d | always_30d | mr_p2 | mr_a30 | mr_a30_legacy | adaptive_period |
|---|---|---|---|---|---|---|
| 2022 | 0.4703 | 1.2414 | 0.7159 | 0.7024 | 0.7024 | 0.6453 |
| 2023 | 0.5059 | 1.1710 | 0.5790 | 0.6744 | 0.6744 | 0.7805 |
| 2024 | 0.3951 | 0.7688 | 0.4174 | 0.4494 | 0.4494 | 0.5576 |
| 2025 | 0.4420 | 0.6056 | 0.4938 | 0.5243 | 0.5243 | 0.6041 |
| 2026 | 0.3782 | 0.8669 | 0.3814 | 0.4775 | 0.4775 | 0.5008 |
