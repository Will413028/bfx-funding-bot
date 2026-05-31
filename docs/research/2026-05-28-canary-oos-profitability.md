# Canary OOS Profitability — fUST MeanReversion (a30, p2)

**Run date**: 2026-05-31T04:03:37.421350+00:00
**Data window**: 2022-01 .. now
**Config**: `configs/cells.canary.yaml` (deployed params, fixed — not re-swept)
**Fill model**: linear (deterministic) — see methodology.

## TL;DR

- Primary metric = **bot-vs-idle**: the strategy's absolute monthly return on budget. An idle balance earns 0%, so this return *is* the bot-vs-idle edge (idle arm = 0% by construction).
- **fUST_a30** (49 months): bot-vs-idle median 0.5547%/mo (annualized 7.20%); worst-month 0.2404%; idle 0.00%; deflated-Sharpe 1.0000.
- **fUST_p2** (49 months): bot-vs-idle median 0.5430%/mo (annualized 7.31%); worst-month 0.3054%; idle 0.00%; deflated-Sharpe 1.0000.
- MR timing alpha (active vs AlwaysMarketRate) is a **secondary diagnostic** — near 0 means MR timing adds little over always-lending; it does NOT gate the bot-vs-idle headline. See per-cell sections.

## OOS honesty caveat

Deployed params were chosen by a sweep over this same 2022-2026 history, so these per-month returns are **in-sample to the parameter-selection process** — an **optimistic** estimate, not pristine OOS. The deflated-Sharpe section quantifies the selection-bias haircut. The only true out-of-sample test is the live canary itself.

- A positive bot-vs-idle backtest asserts the bot beats an idle balance (earns the market rate on the filled portion of budget — see idle rate / mean fill rate per cell), NOT that MR timing beats always-lending — that is the separate MR timing alpha diagnostic below.

## Cell fUST_a30

| Metric | Strategy (= bot-vs-idle) | Baseline (AlwaysMarketRate) |
|---|---|---|
| median monthly % | 0.5547 | 0.4951 |
| p25 monthly % | 0.4690 | 0.4140 |
| worst month % | 0.2404 | 0.1687 |
| best month % | 1.1371 | 0.8520 |
| annualized % | 7.20 | 6.26 |
| Sortino (monthly) | Infinity | Infinity |
| idle rate | 0.00% | 0.00% |
| mean fill rate | 1.0000 | 1.0000 |

**bot-vs-idle median monthly 95% CI (bootstrap):** [0.5036%, 0.6091%]

### MR timing alpha vs passive (secondary diagnostic)

- Diagnostic only — does NOT gate the bot-vs-idle headline. Near 0 means MR timing adds little over always-lending; the product value is bot-vs-idle.

- median active: 0.0628%/mo; mean active: 0.0737%/mo
- information ratio: 1.016275821228169796986813166
- months strategy > baseline: 85.71%

### Selection bias

- configs tried (n_trials): 9; observations: 49
- **deflated Sharpe: 1.0000** (>0.95 = edge survives selection-bias deflation)

## Cell fUST_p2

| Metric | Strategy (= bot-vs-idle) | Baseline (AlwaysMarketRate) |
|---|---|---|
| median monthly % | 0.5430 | 0.4816 |
| p25 monthly % | 0.4632 | 0.4047 |
| worst month % | 0.3054 | 0.2646 |
| best month % | 1.1402 | 0.8550 |
| annualized % | 7.31 | 6.13 |
| Sortino (monthly) | Infinity | Infinity |
| idle rate | 0.00% | 0.00% |
| mean fill rate | 1.0000 | 1.0000 |

**bot-vs-idle median monthly 95% CI (bootstrap):** [0.4884%, 0.5792%]

### MR timing alpha vs passive (secondary diagnostic)

- Diagnostic only — does NOT gate the bot-vs-idle headline. Near 0 means MR timing adds little over always-lending; the product value is bot-vs-idle.

- median active: 0.0678%/mo; mean active: 0.0924%/mo
- information ratio: 0.7384548648752518865367471980
- months strategy > baseline: 83.67%

### Selection bias

- configs tried (n_trials): 9; observations: 49
- **deflated Sharpe: 1.0000** (>0.95 = edge survives selection-bias deflation)

## Non-backtestable risk register

The catastrophic risk for a lending bot is **platform/credit/liquidity tail** (Bitfinex insolvency, socialized loss, Tether risk). No backtest rigor addresses it — it is mitigated by the **position cap ($450)** and not lending the full balance, NOT by this report. Treat these numbers as alpha characterization only.
