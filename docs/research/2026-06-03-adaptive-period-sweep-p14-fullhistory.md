# Canary OOS Profitability — fUST MeanReversion (a30, p2)

**Run date**: 2026-06-02T23:25:45.177061+00:00
**Data window**: 2016-01 .. now
**Config**: `configs/cells.canary.yaml` (deployed params, fixed — not re-swept)
**Fill model**: linear (deterministic) — see methodology.

## TL;DR

- Primary metric = **bot-vs-idle**: the strategy's absolute monthly return on budget. An idle balance earns 0%, so this return *is* the bot-vs-idle edge (idle arm = 0% by construction).
- **fUST_a30** (86 months): bot-vs-idle median 0.6790%/mo (annualized 8.57%); worst-month 0.0849%; idle 0.00%; deflated-Sharpe 1.0000.
- **fUST_p2** (86 months): bot-vs-idle median 0.6518%/mo (annualized 8.52%); worst-month 0.0637%; idle 0.00%; deflated-Sharpe 1.0000.
- **fUSD_a30** (115 months): bot-vs-idle median 0.7317%/mo (annualized 12.65%); worst-month 0.1476%; idle 0.00%; deflated-Sharpe 1.0000.
- **fUSD_p2** (115 months): bot-vs-idle median 0.7244%/mo (annualized 11.97%); worst-month 0.1261%; idle 0.00%; deflated-Sharpe 1.0000.
- MR timing alpha (active vs AlwaysMarketRate) is a **secondary diagnostic** — near 0 means MR timing adds little over always-lending; it does NOT gate the bot-vs-idle headline. See per-cell sections.

## OOS honesty caveat

Deployed params were chosen by a sweep over this same 2022-2026 history, so these per-month returns are **in-sample to the parameter-selection process** — an **optimistic** estimate, not pristine OOS. The deflated-Sharpe section quantifies the selection-bias haircut. The only true out-of-sample test is the live canary itself.

- A positive bot-vs-idle backtest asserts the bot beats an idle balance (earns the market rate on the filled portion of budget — see idle rate / mean fill rate per cell), NOT that MR timing beats always-lending — that is the separate MR timing alpha diagnostic below.

## Cell fUST_a30

| Metric | Strategy (= bot-vs-idle) | Baseline (AlwaysMarketRate) |
|---|---|---|
| median monthly % | 0.6790 | 0.5187 |
| p25 monthly % | 0.4822 | 0.4054 |
| worst month % | 0.0849 | 0.0545 |
| best month % | 1.4637 | 1.5645 |
| annualized % | 8.57 | 6.74 |
| Sortino (monthly) | Infinity | Infinity |
| idle rate | 0.00% | 0.00% |
| mean fill rate | 1.0000 | 1.0000 |

**bot-vs-idle median monthly 95% CI (bootstrap):** [0.5969%, 0.7727%]

### MR timing alpha vs passive (secondary diagnostic)

- Diagnostic only — does NOT gate the bot-vs-idle headline. Near 0 means MR timing adds little over always-lending; the product value is bot-vs-idle.

- median active: 0.0932%/mo; mean active: 0.1423%/mo
- information ratio: 0.5660828474736419267232670577
- months strategy > baseline: 75.58%

### Selection bias

- configs tried (n_trials): 4; observations: 86
- **deflated Sharpe: 1.0000** (>0.95 = edge survives selection-bias deflation)

## Cell fUST_p2

| Metric | Strategy (= bot-vs-idle) | Baseline (AlwaysMarketRate) |
|---|---|---|
| median monthly % | 0.6518 | 0.5178 |
| p25 monthly % | 0.4616 | 0.3988 |
| worst month % | 0.0637 | 0.0545 |
| best month % | 1.4709 | 1.9073 |
| annualized % | 8.52 | 6.81 |
| Sortino (monthly) | Infinity | Infinity |
| idle rate | 0.00% | 0.00% |
| mean fill rate | 1.0000 | 1.0000 |

**bot-vs-idle median monthly 95% CI (bootstrap):** [0.5775%, 0.7078%]

### MR timing alpha vs passive (secondary diagnostic)

- Diagnostic only — does NOT gate the bot-vs-idle headline. Near 0 means MR timing adds little over always-lending; the product value is bot-vs-idle.

- median active: 0.0981%/mo; mean active: 0.1339%/mo
- information ratio: 0.5044895405360010959501253885
- months strategy > baseline: 76.74%

### Selection bias

- configs tried (n_trials): 4; observations: 86
- **deflated Sharpe: 1.0000** (>0.95 = edge survives selection-bias deflation)

## Cell fUSD_a30

| Metric | Strategy (= bot-vs-idle) | Baseline (AlwaysMarketRate) |
|---|---|---|
| median monthly % | 0.7317 | 0.5540 |
| p25 monthly % | 0.5069 | 0.4014 |
| worst month % | 0.1476 | 0.1027 |
| best month % | 8.3373 | 3.8230 |
| annualized % | 12.65 | 9.63 |
| Sortino (monthly) | Infinity | Infinity |
| idle rate | 0.00% | 0.00% |
| mean fill rate | 1.0000 | 1.0000 |

**bot-vs-idle median monthly 95% CI (bootstrap):** [0.6558%, 0.7870%]

### MR timing alpha vs passive (secondary diagnostic)

- Diagnostic only — does NOT gate the bot-vs-idle headline. Near 0 means MR timing adds little over always-lending; the product value is bot-vs-idle.

- median active: 0.0933%/mo; mean active: 0.2311%/mo
- information ratio: 0.4176071938718523812446342799
- months strategy > baseline: 73.04%

### Selection bias

- configs tried (n_trials): 4; observations: 115
- **deflated Sharpe: 1.0000** (>0.95 = edge survives selection-bias deflation)

## Cell fUSD_p2

| Metric | Strategy (= bot-vs-idle) | Baseline (AlwaysMarketRate) |
|---|---|---|
| median monthly % | 0.7244 | 0.5410 |
| p25 monthly % | 0.4659 | 0.3969 |
| worst month % | 0.1261 | 0.0983 |
| best month % | 4.4105 | 3.8233 |
| annualized % | 11.97 | 9.47 |
| Sortino (monthly) | Infinity | Infinity |
| idle rate | 0.00% | 0.00% |
| mean fill rate | 1.0000 | 1.0000 |

**bot-vs-idle median monthly 95% CI (bootstrap):** [0.6046%, 0.8295%]

### MR timing alpha vs passive (secondary diagnostic)

- Diagnostic only — does NOT gate the bot-vs-idle headline. Near 0 means MR timing adds little over always-lending; the product value is bot-vs-idle.

- median active: 0.0763%/mo; mean active: 0.1907%/mo
- information ratio: 0.5799035307842233111813748875
- months strategy > baseline: 67.83%

### Selection bias

- configs tried (n_trials): 4; observations: 115
- **deflated Sharpe: 1.0000** (>0.95 = edge survives selection-bias deflation)

## Non-backtestable risk register

The catastrophic risk for a lending bot is **platform/credit/liquidity tail** (Bitfinex insolvency, socialized loss, Tether risk). No backtest rigor addresses it — it is mitigated by the **position cap ($450)** and not lending the full balance, NOT by this report. Treat these numbers as alpha characterization only.
