# Canary OOS Profitability — fUST MeanReversion (a30, p2)

**Run date**: 2026-05-28T05:51:19.544117+00:00
**Data window**: 2022-01 .. now
**Config**: `configs/cells.canary.yaml` (deployed params, fixed — not re-swept)
**Fill model**: linear (deterministic) — see methodology.

## TL;DR

- **fUST_a30** (49 months): strat median 0.5547%/mo (annualized 7.20%), baseline 0.4951%/mo; active median 0.0628%/mo, IR 1.016275821228169796986813166; worst-month 0.2404%, idle 0.00%; deflated-Sharpe 1.0000.
- **fUST_p2** (49 months): strat median 0.5430%/mo (annualized 7.31%), baseline 0.4816%/mo; active median 0.0678%/mo, IR 0.7384548648752518865367471980; worst-month 0.3054%, idle 0.00%; deflated-Sharpe 1.0000.

## OOS honesty caveat

Deployed params were chosen by a sweep over this same 2022-2026 history, so these per-month returns are **in-sample to the parameter-selection process** — an **optimistic** estimate, not pristine OOS. The deflated-Sharpe section quantifies the selection-bias haircut. The only true out-of-sample test is the live canary itself.

## Cell fUST_a30

| Metric | Strategy | Baseline (AlwaysFRR) |
|---|---|---|
| median monthly % | 0.5547 | 0.4951 |
| p25 monthly % | 0.4690 | 0.4140 |
| worst month % | 0.2404 | 0.1687 |
| best month % | 1.1371 | 0.8520 |
| annualized % | 7.20 | 6.26 |
| Sortino (monthly) | Infinity | Infinity |
| idle rate | 0.00% | 0.00% |
| mean fill rate | 1.0000 | 1.0000 |

**Strategy median monthly 95% CI (bootstrap):** [0.5036%, 0.6091%]

### Active return vs passive

- median active: 0.0628%/mo; mean active: 0.0737%/mo
- information ratio: 1.016275821228169796986813166
- months strategy > baseline: 85.71%

### Selection bias

- configs tried (n_trials): 9; observations: 49
- **deflated Sharpe: 1.0000** (>0.95 = edge survives selection-bias deflation)

## Cell fUST_p2

| Metric | Strategy | Baseline (AlwaysFRR) |
|---|---|---|
| median monthly % | 0.5430 | 0.4816 |
| p25 monthly % | 0.4632 | 0.4047 |
| worst month % | 0.3054 | 0.2646 |
| best month % | 1.1402 | 0.8550 |
| annualized % | 7.31 | 6.13 |
| Sortino (monthly) | Infinity | Infinity |
| idle rate | 0.00% | 0.00% |
| mean fill rate | 1.0000 | 1.0000 |

**Strategy median monthly 95% CI (bootstrap):** [0.4884%, 0.5792%]

### Active return vs passive

- median active: 0.0678%/mo; mean active: 0.0924%/mo
- information ratio: 0.7384548648752518865367471980
- months strategy > baseline: 83.67%

### Selection bias

- configs tried (n_trials): 9; observations: 49
- **deflated Sharpe: 1.0000** (>0.95 = edge survives selection-bias deflation)

## Non-backtestable risk register

The catastrophic risk for a lending bot is **platform/credit/liquidity tail** (Bitfinex insolvency, socialized loss, Tether risk). No backtest rigor addresses it — it is mitigated by the **position cap ($450)** and not lending the full balance, NOT by this report. Treat these numbers as alpha characterization only.
