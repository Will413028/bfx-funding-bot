# Canary OOS Profitability — fUST MeanReversion (a30, p2)

**Run date**: 2026-06-02T23:24:38.533529+00:00
**Data window**: 2022-01 .. now
**Config**: `configs/cells.canary.yaml` (deployed params, fixed — not re-swept)
**Fill model**: linear (deterministic) — see methodology.

## TL;DR

- Primary metric = **bot-vs-idle**: the strategy's absolute monthly return on budget. An idle balance earns 0%, so this return *is* the bot-vs-idle edge (idle arm = 0% by construction).
- **fUST_a30** (50 months): bot-vs-idle median 0.6654%/mo (annualized 8.62%); worst-month 0.2091%; idle 0.00%; deflated-Sharpe 1.0000.
- **fUST_p2** (50 months): bot-vs-idle median 0.6084%/mo (annualized 8.20%); worst-month 0.2091%; idle 0.00%; deflated-Sharpe 1.0000.
- **fUSD_a30** (50 months): bot-vs-idle median 0.6095%/mo (annualized 9.18%); worst-month 0.2886%; idle 0.00%; deflated-Sharpe 1.0000.
- **fUSD_p2** (50 months): bot-vs-idle median 0.5845%/mo (annualized 8.21%); worst-month 0.2874%; idle 0.00%; deflated-Sharpe 1.0000.
- MR timing alpha (active vs AlwaysMarketRate) is a **secondary diagnostic** — near 0 means MR timing adds little over always-lending; it does NOT gate the bot-vs-idle headline. See per-cell sections.

## OOS honesty caveat

Deployed params were chosen by a sweep over this same 2022-2026 history, so these per-month returns are **in-sample to the parameter-selection process** — an **optimistic** estimate, not pristine OOS. The deflated-Sharpe section quantifies the selection-bias haircut. The only true out-of-sample test is the live canary itself.

- A positive bot-vs-idle backtest asserts the bot beats an idle balance (earns the market rate on the filled portion of budget — see idle rate / mean fill rate per cell), NOT that MR timing beats always-lending — that is the separate MR timing alpha diagnostic below.

## Cell fUST_a30

| Metric | Strategy (= bot-vs-idle) | Baseline (AlwaysMarketRate) |
|---|---|---|
| median monthly % | 0.6654 | 0.4949 |
| p25 monthly % | 0.5019 | 0.4079 |
| worst month % | 0.2091 | 0.1687 |
| best month % | 1.4637 | 0.8520 |
| annualized % | 8.62 | 6.20 |
| Sortino (monthly) | Infinity | Infinity |
| idle rate | 0.00% | 0.00% |
| mean fill rate | 1.0000 | 1.0000 |

**bot-vs-idle median monthly 95% CI (bootstrap):** [0.5878%, 0.7698%]

### MR timing alpha vs passive (secondary diagnostic)

- Diagnostic only — does NOT gate the bot-vs-idle headline. Near 0 means MR timing adds little over always-lending; the product value is bot-vs-idle.

- median active: 0.1351%/mo; mean active: 0.1897%/mo
- information ratio: 0.8434463164184953098537732134
- months strategy > baseline: 84.00%

### Selection bias

- configs tried (n_trials): 4; observations: 50
- **deflated Sharpe: 1.0000** (>0.95 = edge survives selection-bias deflation)

## Cell fUST_p2

| Metric | Strategy (= bot-vs-idle) | Baseline (AlwaysMarketRate) |
|---|---|---|
| median monthly % | 0.6084 | 0.4789 |
| p25 monthly % | 0.4616 | 0.4026 |
| worst month % | 0.2091 | 0.2526 |
| best month % | 1.4709 | 0.8550 |
| annualized % | 8.20 | 6.07 |
| Sortino (monthly) | Infinity | Infinity |
| idle rate | 0.00% | 0.00% |
| mean fill rate | 1.0000 | 1.0000 |

**bot-vs-idle median monthly 95% CI (bootstrap):** [0.5331%, 0.6762%]

### MR timing alpha vs passive (secondary diagnostic)

- Diagnostic only — does NOT gate the bot-vs-idle headline. Near 0 means MR timing adds little over always-lending; the product value is bot-vs-idle.

- median active: 0.1067%/mo; mean active: 0.1673%/mo
- information ratio: 0.7550106423648323814552059885
- months strategy > baseline: 82.00%

### Selection bias

- configs tried (n_trials): 4; observations: 50
- **deflated Sharpe: 1.0000** (>0.95 = edge survives selection-bias deflation)

## Cell fUSD_a30

| Metric | Strategy (= bot-vs-idle) | Baseline (AlwaysMarketRate) |
|---|---|---|
| median monthly % | 0.6095 | 0.4657 |
| p25 monthly % | 0.4875 | 0.3831 |
| worst month % | 0.2886 | 0.2549 |
| best month % | 2.1052 | 0.8874 |
| annualized % | 9.18 | 5.94 |
| Sortino (monthly) | Infinity | Infinity |
| idle rate | 0.00% | 0.00% |
| mean fill rate | 1.0000 | 1.0000 |

**bot-vs-idle median monthly 95% CI (bootstrap):** [0.5563%, 0.7278%]

### MR timing alpha vs passive (secondary diagnostic)

- Diagnostic only — does NOT gate the bot-vs-idle headline. Near 0 means MR timing adds little over always-lending; the product value is bot-vs-idle.

- median active: 0.1422%/mo; mean active: 0.2538%/mo
- information ratio: 0.7480827617939353510174292769
- months strategy > baseline: 90.00%

### Selection bias

- configs tried (n_trials): 4; observations: 50
- **deflated Sharpe: 1.0000** (>0.95 = edge survives selection-bias deflation)

## Cell fUSD_p2

| Metric | Strategy (= bot-vs-idle) | Baseline (AlwaysMarketRate) |
|---|---|---|
| median monthly % | 0.5845 | 0.4422 |
| p25 monthly % | 0.4429 | 0.3868 |
| worst month % | 0.2874 | 0.2489 |
| best month % | 1.9588 | 0.7483 |
| annualized % | 8.21 | 5.58 |
| Sortino (monthly) | Infinity | Infinity |
| idle rate | 0.00% | 0.00% |
| mean fill rate | 1.0000 | 1.0000 |

**bot-vs-idle median monthly 95% CI (bootstrap):** [0.5151%, 0.6811%]

### MR timing alpha vs passive (secondary diagnostic)

- Diagnostic only — does NOT gate the bot-vs-idle headline. Near 0 means MR timing adds little over always-lending; the product value is bot-vs-idle.

- median active: 0.1320%/mo; mean active: 0.2068%/mo
- information ratio: 0.8165941624680380227057415462
- months strategy > baseline: 82.00%

### Selection bias

- configs tried (n_trials): 4; observations: 50
- **deflated Sharpe: 1.0000** (>0.95 = edge survives selection-bias deflation)

## Non-backtestable risk register

The catastrophic risk for a lending bot is **platform/credit/liquidity tail** (Bitfinex insolvency, socialized loss, Tether risk). No backtest rigor addresses it — it is mitigated by the **position cap ($450)** and not lending the full balance, NOT by this report. Treat these numbers as alpha characterization only.
