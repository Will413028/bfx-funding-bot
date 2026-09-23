# Signal EDA Funnel

**Data window:** 2016-07-31-now, 4 cells, 128 observations

Screens 4 candidate signals (FRR-trend, SpikeDetect, funding_supply, utilization) for predictive power against forward rate change. GO requires FDR-significant IC, same sign across both regimes, >=3/4 cells robust, and |median IC| >= 0.03. Null result (all KILL) is a valid conclusion.

## Verdicts

| Signal | Verdict | Median IC | Reason |
| --- | --- | --- | --- |
| frr_trend | **KILL** | -0.037 | sign flips across regimes (regime-fragile) |
| funding_supply | **KILL** | -0.027 | no FDR-significant IC in any cell/regime |
| spike_detect | **KILL** | -0.060 | sign flips across regimes (regime-fragile) |
| utilization | **KILL** | -0.012 | no FDR-significant IC in any cell/regime |

## Promotion

**Null result** — no signal cleared the bar. None promoted. Candidate pool unchanged; these signals are recorded as falsified.

Full per-cell × regime × horizon IC / p-value / quintile grid in the .json sidecar.
