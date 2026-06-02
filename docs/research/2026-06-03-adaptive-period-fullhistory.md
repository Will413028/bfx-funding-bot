# Canary OOS Profitability — fUST MeanReversion (a30, p2)

**Run date**: 2026-06-02T18:56:47.777990+00:00
**Data window**: 2016-01 .. now
**Config**: `configs/cells.canary.yaml` (deployed params, fixed — not re-swept)
**Fill model**: linear (deterministic) — see methodology.

## TL;DR

- Primary metric = **bot-vs-idle**: the strategy's absolute monthly return on budget. An idle balance earns 0%, so this return *is* the bot-vs-idle edge (idle arm = 0% by construction).
- **fUST_a30** (86 months): bot-vs-idle median 0.6905%/mo (annualized 9.91%); worst-month 0.0285%; idle 0.00%; deflated-Sharpe 1.0000.
- **fUST_p2** (86 months): bot-vs-idle median 0.6590%/mo (annualized 10.05%); worst-month 0.0260%; idle 0.00%; deflated-Sharpe 1.0000.
- **fUSD_a30** (115 months): bot-vs-idle median 0.8569%/mo (annualized 15.38%); worst-month 0.1476%; idle 0.00%; deflated-Sharpe 0.9995.
- **fUSD_p2** (115 months): bot-vs-idle median 0.7423%/mo (annualized 14.81%); worst-month 0.1261%; idle 0.00%; deflated-Sharpe 1.0000.
- MR timing alpha (active vs AlwaysMarketRate) is a **secondary diagnostic** — near 0 means MR timing adds little over always-lending; it does NOT gate the bot-vs-idle headline. See per-cell sections.

## OOS honesty caveat

Deployed params were chosen by a sweep over this same 2022-2026 history, so these per-month returns are **in-sample to the parameter-selection process** — an **optimistic** estimate, not pristine OOS. The deflated-Sharpe section quantifies the selection-bias haircut. The only true out-of-sample test is the live canary itself.

- A positive bot-vs-idle backtest asserts the bot beats an idle balance (earns the market rate on the filled portion of budget — see idle rate / mean fill rate per cell), NOT that MR timing beats always-lending — that is the separate MR timing alpha diagnostic below.

## Cell fUST_a30

| Metric | Strategy (= bot-vs-idle) | Baseline (AlwaysMarketRate) |
|---|---|---|
| median monthly % | 0.6905 | 0.5187 |
| p25 monthly % | 0.5124 | 0.4054 |
| worst month % | 0.0285 | 0.0545 |
| best month % | 2.6403 | 1.5645 |
| annualized % | 9.91 | 6.74 |
| Sortino (monthly) | Infinity | Infinity |
| idle rate | 0.00% | 0.00% |
| mean fill rate | 1.0000 | 1.0000 |

**bot-vs-idle median monthly 95% CI (bootstrap):** [0.6194%, 0.8090%]

### MR timing alpha vs passive (secondary diagnostic)

- Diagnostic only — does NOT gate the bot-vs-idle headline. Near 0 means MR timing adds little over always-lending; the product value is bot-vs-idle.

- median active: 0.1123%/mo; mean active: 0.2461%/mo
- information ratio: 0.5316580790942680342264849040
- months strategy > baseline: 74.42%

### Selection bias

- configs tried (n_trials): 4; observations: 86
- **deflated Sharpe: 1.0000** (>0.95 = edge survives selection-bias deflation)

## Cell fUST_p2

| Metric | Strategy (= bot-vs-idle) | Baseline (AlwaysMarketRate) |
|---|---|---|
| median monthly % | 0.6590 | 0.5178 |
| p25 monthly % | 0.4725 | 0.3988 |
| worst month % | 0.0260 | 0.0545 |
| best month % | 2.8095 | 1.9073 |
| annualized % | 10.05 | 6.81 |
| Sortino (monthly) | Infinity | Infinity |
| idle rate | 0.00% | 0.00% |
| mean fill rate | 1.0000 | 1.0000 |

**bot-vs-idle median monthly 95% CI (bootstrap):** [0.6051%, 0.7979%]

### MR timing alpha vs passive (secondary diagnostic)

- Diagnostic only — does NOT gate the bot-vs-idle headline. Near 0 means MR timing adds little over always-lending; the product value is bot-vs-idle.

- median active: 0.1064%/mo; mean active: 0.2523%/mo
- information ratio: 0.5118623583803289975368354532
- months strategy > baseline: 76.74%

### Selection bias

- configs tried (n_trials): 4; observations: 86
- **deflated Sharpe: 1.0000** (>0.95 = edge survives selection-bias deflation)

## Cell fUSD_a30

| Metric | Strategy (= bot-vs-idle) | Baseline (AlwaysMarketRate) |
|---|---|---|
| median monthly % | 0.8569 | 0.5540 |
| p25 monthly % | 0.5368 | 0.4014 |
| worst month % | 0.1476 | 0.1027 |
| best month % | 14.7642 | 3.8230 |
| annualized % | 15.38 | 9.63 |
| Sortino (monthly) | Infinity | Infinity |
| idle rate | 0.00% | 0.00% |
| mean fill rate | 1.0000 | 1.0000 |

**bot-vs-idle median monthly 95% CI (bootstrap):** [0.7140%, 1.0532%]

### MR timing alpha vs passive (secondary diagnostic)

- Diagnostic only — does NOT gate the bot-vs-idle headline. Near 0 means MR timing adds little over always-lending; the product value is bot-vs-idle.

- median active: 0.1117%/mo; mean active: 0.4384%/mo
- information ratio: 0.3724677630373218121096385060
- months strategy > baseline: 73.04%

### Selection bias

- configs tried (n_trials): 4; observations: 115
- **deflated Sharpe: 0.9995** (>0.95 = edge survives selection-bias deflation)

## Cell fUSD_p2

| Metric | Strategy (= bot-vs-idle) | Baseline (AlwaysMarketRate) |
|---|---|---|
| median monthly % | 0.7423 | 0.5410 |
| p25 monthly % | 0.5043 | 0.3969 |
| worst month % | 0.1261 | 0.0983 |
| best month % | 8.1136 | 3.8233 |
| annualized % | 14.81 | 9.47 |
| Sortino (monthly) | Infinity | Infinity |
| idle rate | 0.00% | 0.00% |
| mean fill rate | 1.0000 | 1.0000 |

**bot-vs-idle median monthly 95% CI (bootstrap):** [0.6553%, 1.0025%]

### MR timing alpha vs passive (secondary diagnostic)

- Diagnostic only — does NOT gate the bot-vs-idle headline. Near 0 means MR timing adds little over always-lending; the product value is bot-vs-idle.

- median active: 0.0973%/mo; mean active: 0.4051%/mo
- information ratio: 0.4760050536042753086038184613
- months strategy > baseline: 67.83%

### Selection bias

- configs tried (n_trials): 4; observations: 115
- **deflated Sharpe: 1.0000** (>0.95 = edge survives selection-bias deflation)

## Non-backtestable risk register

The catastrophic risk for a lending bot is **platform/credit/liquidity tail** (Bitfinex insolvency, socialized loss, Tether risk). No backtest rigor addresses it — it is mitigated by the **position cap ($450)** and not lending the full balance, NOT by this report. Treat these numbers as alpha characterization only.
