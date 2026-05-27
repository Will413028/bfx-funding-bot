# Canary OOS Profitability — fUST MeanReversion (a30, p2)

**Run date**: 2026-05-27T23:35:48.291676+00:00
**Data window**: 2022-01 .. now
**Config**: `configs/cells.canary.yaml` (deployed params, fixed — not re-swept)
**Fill model**: linear (deterministic) — see methodology.

## TL;DR

- **fUST_a30** (49 months): strat median 0.4951%/mo (annualized 6.26%), baseline 0.4951%/mo; active median 0.0000%/mo, IR 0; worst-month 0.1687%, idle 0.00%; deflated-Sharpe 1.0000.
- **fUST_p2** (49 months): strat median 0.4816%/mo (annualized 6.13%), baseline 0.4816%/mo; active median 0.0000%/mo, IR 0; worst-month 0.2646%, idle 0.00%; deflated-Sharpe 1.0000.

## OOS honesty caveat

Deployed params were chosen by a sweep over this same 2022-2026 history, so these per-month returns are **in-sample to the parameter-selection process** — an **optimistic** estimate, not pristine OOS. The deflated-Sharpe section quantifies the selection-bias haircut. The only true out-of-sample test is the live canary itself.

## Cell fUST_a30

| Metric | Strategy | Baseline (AlwaysFRR) |
|---|---|---|
| median monthly % | 0.4951 | 0.4951 |
| p25 monthly % | 0.4140 | 0.4140 |
| worst month % | 0.1687 | 0.1687 |
| best month % | 0.8520 | 0.8520 |
| annualized % | 6.26 | 6.26 |
| Sortino (monthly) | Infinity | Infinity |
| idle rate | 0.00% | 0.00% |
| mean fill rate | 1.0000 | 1.0000 |

**Strategy median monthly 95% CI (bootstrap):** [0.4552%, 0.5513%]

### Active return vs passive

- median active: 0.0000%/mo; mean active: 0.0000%/mo
- information ratio: 0
- months strategy > baseline: 0.00%

### Selection bias

- configs tried (n_trials): 9; observations: 49
- **deflated Sharpe: 1.0000** (>0.95 = edge survives selection-bias deflation)

## Cell fUST_p2

| Metric | Strategy | Baseline (AlwaysFRR) |
|---|---|---|
| median monthly % | 0.4816 | 0.4816 |
| p25 monthly % | 0.4047 | 0.4047 |
| worst month % | 0.2646 | 0.2646 |
| best month % | 0.8550 | 0.8550 |
| annualized % | 6.13 | 6.13 |
| Sortino (monthly) | Infinity | Infinity |
| idle rate | 0.00% | 0.00% |
| mean fill rate | 1.0000 | 1.0000 |

**Strategy median monthly 95% CI (bootstrap):** [0.4227%, 0.5373%]

### Active return vs passive

- median active: 0.0000%/mo; mean active: 0.0000%/mo
- information ratio: 0
- months strategy > baseline: 0.00%

### Selection bias

- configs tried (n_trials): 9; observations: 49
- **deflated Sharpe: 1.0000** (>0.95 = edge survives selection-bias deflation)

## Non-backtestable risk register

The catastrophic risk for a lending bot is **platform/credit/liquidity tail** (Bitfinex insolvency, socialized loss, Tether risk). No backtest rigor addresses it — it is mitigated by the **position cap ($450)** and not lending the full balance, NOT by this report. Treat these numbers as alpha characterization only.
