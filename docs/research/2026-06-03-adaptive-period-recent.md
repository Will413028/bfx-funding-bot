# Canary OOS Profitability — fUST MeanReversion (a30, p2)

**Run date**: 2026-06-02T18:55:28.373361+00:00
**Data window**: 2022-01 .. now
**Config**: `configs/cells.canary.yaml` (deployed params, fixed — not re-swept)
**Fill model**: linear (deterministic) — see methodology.

## TL;DR

- Primary metric = **bot-vs-idle**: the strategy's absolute monthly return on budget. An idle balance earns 0%, so this return *is* the bot-vs-idle edge (idle arm = 0% by construction).
- **fUST_a30** (50 months): bot-vs-idle median 0.6706%/mo (annualized 9.94%); worst-month 0.2091%; idle 0.00%; deflated-Sharpe 1.0000.
- **fUST_p2** (50 months): bot-vs-idle median 0.6441%/mo (annualized 9.38%); worst-month 0.2091%; idle 0.00%; deflated-Sharpe 1.0000.
- **fUSD_a30** (50 months): bot-vs-idle median 0.7591%/mo (annualized 11.94%); worst-month 0.2886%; idle 0.00%; deflated-Sharpe 1.0000.
- **fUSD_p2** (50 months): bot-vs-idle median 0.6131%/mo (annualized 10.56%); worst-month 0.2874%; idle 0.00%; deflated-Sharpe 1.0000.
- MR timing alpha (active vs AlwaysMarketRate) is a **secondary diagnostic** — near 0 means MR timing adds little over always-lending; it does NOT gate the bot-vs-idle headline. See per-cell sections.

## OOS honesty caveat

Deployed params were chosen by a sweep over this same 2022-2026 history, so these per-month returns are **in-sample to the parameter-selection process** — an **optimistic** estimate, not pristine OOS. The deflated-Sharpe section quantifies the selection-bias haircut. The only true out-of-sample test is the live canary itself.

- A positive bot-vs-idle backtest asserts the bot beats an idle balance (earns the market rate on the filled portion of budget — see idle rate / mean fill rate per cell), NOT that MR timing beats always-lending — that is the separate MR timing alpha diagnostic below.

## Cell fUST_a30

| Metric | Strategy (= bot-vs-idle) | Baseline (AlwaysMarketRate) |
|---|---|---|
| median monthly % | 0.6706 | 0.4949 |
| p25 monthly % | 0.5256 | 0.4079 |
| worst month % | 0.2091 | 0.1687 |
| best month % | 2.6403 | 0.8520 |
| annualized % | 9.94 | 6.20 |
| Sortino (monthly) | Infinity | Infinity |
| idle rate | 0.00% | 0.00% |
| mean fill rate | 1.0000 | 1.0000 |

**bot-vs-idle median monthly 95% CI (bootstrap):** [0.5980%, 0.8138%]

### MR timing alpha vs passive (secondary diagnostic)

- Diagnostic only — does NOT gate the bot-vs-idle headline. Near 0 means MR timing adds little over always-lending; the product value is bot-vs-idle.

- median active: 0.1655%/mo; mean active: 0.2917%/mo
- information ratio: 0.7273154590365781568301511042
- months strategy > baseline: 82.00%

### Selection bias

- configs tried (n_trials): 4; observations: 50
- **deflated Sharpe: 1.0000** (>0.95 = edge survives selection-bias deflation)

## Cell fUST_p2

| Metric | Strategy (= bot-vs-idle) | Baseline (AlwaysMarketRate) |
|---|---|---|
| median monthly % | 0.6441 | 0.4789 |
| p25 monthly % | 0.4725 | 0.4026 |
| worst month % | 0.2091 | 0.2526 |
| best month % | 2.6463 | 0.8550 |
| annualized % | 9.38 | 6.07 |
| Sortino (monthly) | Infinity | Infinity |
| idle rate | 0.00% | 0.00% |
| mean fill rate | 1.0000 | 1.0000 |

**bot-vs-idle median monthly 95% CI (bootstrap):** [0.5857%, 0.7820%]

### MR timing alpha vs passive (secondary diagnostic)

- Diagnostic only — does NOT gate the bot-vs-idle headline. Near 0 means MR timing adds little over always-lending; the product value is bot-vs-idle.

- median active: 0.1340%/mo; mean active: 0.2588%/mo
- information ratio: 0.6465162087469971087572089917
- months strategy > baseline: 82.00%

### Selection bias

- configs tried (n_trials): 4; observations: 50
- **deflated Sharpe: 1.0000** (>0.95 = edge survives selection-bias deflation)

## Cell fUSD_a30

| Metric | Strategy (= bot-vs-idle) | Baseline (AlwaysMarketRate) |
|---|---|---|
| median monthly % | 0.7591 | 0.4657 |
| p25 monthly % | 0.5289 | 0.3831 |
| worst month % | 0.2886 | 0.2549 |
| best month % | 3.3907 | 0.8874 |
| annualized % | 11.94 | 5.94 |
| Sortino (monthly) | Infinity | Infinity |
| idle rate | 0.00% | 0.00% |
| mean fill rate | 1.0000 | 1.0000 |

**bot-vs-idle median monthly 95% CI (bootstrap):** [0.6108%, 1.0348%]

### MR timing alpha vs passive (secondary diagnostic)

- Diagnostic only — does NOT gate the bot-vs-idle headline. Near 0 means MR timing adds little over always-lending; the product value is bot-vs-idle.

- median active: 0.3161%/mo; mean active: 0.4646%/mo
- information ratio: 0.8707517065732632529775634746
- months strategy > baseline: 90.00%

### Selection bias

- configs tried (n_trials): 4; observations: 50
- **deflated Sharpe: 1.0000** (>0.95 = edge survives selection-bias deflation)

## Cell fUSD_p2

| Metric | Strategy (= bot-vs-idle) | Baseline (AlwaysMarketRate) |
|---|---|---|
| median monthly % | 0.6131 | 0.4422 |
| p25 monthly % | 0.4429 | 0.3868 |
| worst month % | 0.2874 | 0.2489 |
| best month % | 3.5690 | 0.7483 |
| annualized % | 10.56 | 5.58 |
| Sortino (monthly) | Infinity | Infinity |
| idle rate | 0.00% | 0.00% |
| mean fill rate | 1.0000 | 1.0000 |

**bot-vs-idle median monthly 95% CI (bootstrap):** [0.5360%, 0.7847%]

### MR timing alpha vs passive (secondary diagnostic)

- Diagnostic only — does NOT gate the bot-vs-idle headline. Near 0 means MR timing adds little over always-lending; the product value is bot-vs-idle.

- median active: 0.1580%/mo; mean active: 0.3888%/mo
- information ratio: 0.7095866274990964691743701873
- months strategy > baseline: 82.00%

### Selection bias

- configs tried (n_trials): 4; observations: 50
- **deflated Sharpe: 1.0000** (>0.95 = edge survives selection-bias deflation)

## Non-backtestable risk register

The catastrophic risk for a lending bot is **platform/credit/liquidity tail** (Bitfinex insolvency, socialized loss, Tether risk). No backtest rigor addresses it — it is mitigated by the **position cap ($450)** and not lending the full balance, NOT by this report. Treat these numbers as alpha characterization only.
