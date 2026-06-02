# Canary OOS Profitability — fUST MeanReversion (a30, p2)

**Run date**: 2026-06-02T23:24:27.732314+00:00
**Data window**: 2022-01 .. now
**Config**: `configs/cells.canary.yaml` (deployed params, fixed — not re-swept)
**Fill model**: linear (deterministic) — see methodology.

## TL;DR

- Primary metric = **bot-vs-idle**: the strategy's absolute monthly return on budget. An idle balance earns 0%, so this return *is* the bot-vs-idle edge (idle arm = 0% by construction).
- **fUST_a30** (50 months): bot-vs-idle median 0.6056%/mo (annualized 7.79%); worst-month 0.2091%; idle 0.00%; deflated-Sharpe 1.0000.
- **fUST_p2** (50 months): bot-vs-idle median 0.5954%/mo (annualized 7.54%); worst-month 0.2091%; idle 0.00%; deflated-Sharpe 1.0000.
- **fUSD_a30** (50 months): bot-vs-idle median 0.5578%/mo (annualized 7.56%); worst-month 0.2886%; idle 0.00%; deflated-Sharpe 1.0000.
- **fUSD_p2** (50 months): bot-vs-idle median 0.5117%/mo (annualized 6.94%); worst-month 0.2874%; idle 0.00%; deflated-Sharpe 1.0000.
- MR timing alpha (active vs AlwaysMarketRate) is a **secondary diagnostic** — near 0 means MR timing adds little over always-lending; it does NOT gate the bot-vs-idle headline. See per-cell sections.

## OOS honesty caveat

Deployed params were chosen by a sweep over this same 2022-2026 history, so these per-month returns are **in-sample to the parameter-selection process** — an **optimistic** estimate, not pristine OOS. The deflated-Sharpe section quantifies the selection-bias haircut. The only true out-of-sample test is the live canary itself.

- A positive bot-vs-idle backtest asserts the bot beats an idle balance (earns the market rate on the filled portion of budget — see idle rate / mean fill rate per cell), NOT that MR timing beats always-lending — that is the separate MR timing alpha diagnostic below.

## Cell fUST_a30

| Metric | Strategy (= bot-vs-idle) | Baseline (AlwaysMarketRate) |
|---|---|---|
| median monthly % | 0.6056 | 0.4949 |
| p25 monthly % | 0.4836 | 0.4079 |
| worst month % | 0.2091 | 0.1687 |
| best month % | 1.2078 | 0.8520 |
| annualized % | 7.79 | 6.20 |
| Sortino (monthly) | Infinity | Infinity |
| idle rate | 0.00% | 0.00% |
| mean fill rate | 1.0000 | 1.0000 |

**bot-vs-idle median monthly 95% CI (bootstrap):** [0.5646%, 0.6689%]

### MR timing alpha vs passive (secondary diagnostic)

- Diagnostic only — does NOT gate the bot-vs-idle headline. Near 0 means MR timing adds little over always-lending; the product value is bot-vs-idle.

- median active: 0.1188%/mo; mean active: 0.1253%/mo
- information ratio: 1.030255170098568634208717252
- months strategy > baseline: 84.00%

### Selection bias

- configs tried (n_trials): 4; observations: 50
- **deflated Sharpe: 1.0000** (>0.95 = edge survives selection-bias deflation)

## Cell fUST_p2

| Metric | Strategy (= bot-vs-idle) | Baseline (AlwaysMarketRate) |
|---|---|---|
| median monthly % | 0.5954 | 0.4789 |
| p25 monthly % | 0.4616 | 0.4026 |
| worst month % | 0.2091 | 0.2526 |
| best month % | 1.2061 | 0.8550 |
| annualized % | 7.54 | 6.07 |
| Sortino (monthly) | Infinity | Infinity |
| idle rate | 0.00% | 0.00% |
| mean fill rate | 1.0000 | 1.0000 |

**bot-vs-idle median monthly 95% CI (bootstrap):** [0.5169%, 0.6515%]

### MR timing alpha vs passive (secondary diagnostic)

- Diagnostic only — does NOT gate the bot-vs-idle headline. Near 0 means MR timing adds little over always-lending; the product value is bot-vs-idle.

- median active: 0.1002%/mo; mean active: 0.1153%/mo
- information ratio: 0.9192117770423086578604550842
- months strategy > baseline: 80.00%

### Selection bias

- configs tried (n_trials): 4; observations: 50
- **deflated Sharpe: 1.0000** (>0.95 = edge survives selection-bias deflation)

## Cell fUSD_a30

| Metric | Strategy (= bot-vs-idle) | Baseline (AlwaysMarketRate) |
|---|---|---|
| median monthly % | 0.5578 | 0.4657 |
| p25 monthly % | 0.4402 | 0.3831 |
| worst month % | 0.2886 | 0.2549 |
| best month % | 1.5445 | 0.8874 |
| annualized % | 7.56 | 5.94 |
| Sortino (monthly) | Infinity | Infinity |
| idle rate | 0.00% | 0.00% |
| mean fill rate | 1.0000 | 1.0000 |

**bot-vs-idle median monthly 95% CI (bootstrap):** [0.5149%, 0.6303%]

### MR timing alpha vs passive (secondary diagnostic)

- Diagnostic only — does NOT gate the bot-vs-idle headline. Near 0 means MR timing adds little over always-lending; the product value is bot-vs-idle.

- median active: 0.0991%/mo; mean active: 0.1279%/mo
- information ratio: 0.7674146721270266421172182541
- months strategy > baseline: 88.00%

### Selection bias

- configs tried (n_trials): 4; observations: 50
- **deflated Sharpe: 1.0000** (>0.95 = edge survives selection-bias deflation)

## Cell fUSD_p2

| Metric | Strategy (= bot-vs-idle) | Baseline (AlwaysMarketRate) |
|---|---|---|
| median monthly % | 0.5117 | 0.4422 |
| p25 monthly % | 0.4220 | 0.3868 |
| worst month % | 0.2874 | 0.2489 |
| best month % | 1.5864 | 0.7483 |
| annualized % | 6.94 | 5.58 |
| Sortino (monthly) | Infinity | Infinity |
| idle rate | 0.00% | 0.00% |
| mean fill rate | 1.0000 | 1.0000 |

**bot-vs-idle median monthly 95% CI (bootstrap):** [0.4488%, 0.5814%]

### MR timing alpha vs passive (secondary diagnostic)

- Diagnostic only — does NOT gate the bot-vs-idle headline. Near 0 means MR timing adds little over always-lending; the product value is bot-vs-idle.

- median active: 0.0868%/mo; mean active: 0.1075%/mo
- information ratio: 0.6607457406389650345125043085
- months strategy > baseline: 78.00%

### Selection bias

- configs tried (n_trials): 4; observations: 50
- **deflated Sharpe: 1.0000** (>0.95 = edge survives selection-bias deflation)

## Non-backtestable risk register

The catastrophic risk for a lending bot is **platform/credit/liquidity tail** (Bitfinex insolvency, socialized loss, Tether risk). No backtest rigor addresses it — it is mitigated by the **position cap ($450)** and not lending the full balance, NOT by this report. Treat these numbers as alpha characterization only.
