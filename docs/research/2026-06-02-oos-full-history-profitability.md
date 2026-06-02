# Canary OOS Profitability — FULL HISTORY (fUST + fUSD MeanReversion a30/p2)

**Run date**: 2026-06-02T10:21:27.937091+00:00
**Data window**: 2016-01 .. now
**Config**: `configs/cells.canary.yaml` (deployed params, fixed — not re-swept)
**Fill model**: linear (deterministic) — see methodology.

> ⚠️ **Scope note (manual full-history run, 2026-06-02):** `START_MTS` was temporarily overridden from the script default `2022-01-01` → `2016-01-01` (each cell uses its real data start: fUSD 2016-07, fUST 2018-12; the override was reverted, NOT committed — the script default stays 2022). Two static template strings below are therefore inaccurate for THIS run: the section title says "fUST" (this run also covers fUSD — see the fUSD cells), and the OOS-honesty caveat says "this same 2022-2026 history" — in fact **params were selected on 2022-2026 but evaluated here over 2016-2026, so the 2016-2021 portion is genuinely out-of-sample to parameter selection** (a stronger result than the caveat implies). Window counts: **fUST 86 / fUSD 115** monthly OOS windows. Headline: bot-vs-idle ann. **fUST ~7.70% / fUSD ~10.3-10.7%**, worst month always positive, deflated-Sharpe 1.0; MR timing alpha vs passive **+0.85-1.03%/yr** (win 74-80%, IR 0.39-0.65) — small but positive (NOT the earlier "≈0", which was the pre-fix inert config). Caveat that only live resolves: **mean fill rate = 1.0 (assumes 100% fill at market rate) → live ≤ backtest**; and the alpha is likely regime-dependent (fatter in 2017/2021 volatility).

## TL;DR

- Primary metric = **bot-vs-idle**: the strategy's absolute monthly return on budget. An idle balance earns 0%, so this return *is* the bot-vs-idle edge (idle arm = 0% by construction).
- **fUST_a30** (86 months): bot-vs-idle median 0.5765%/mo (annualized 7.70%); worst-month 0.0614%; idle 0.00%; deflated-Sharpe 1.0000.
- **fUST_p2** (86 months): bot-vs-idle median 0.5687%/mo (annualized 7.70%); worst-month 0.0614%; idle 0.00%; deflated-Sharpe 1.0000.
- **fUSD_a30** (115 months): bot-vs-idle median 0.6231%/mo (annualized 10.66%); worst-month 0.1384%; idle 0.00%; deflated-Sharpe 1.0000.
- **fUSD_p2** (115 months): bot-vs-idle median 0.6256%/mo (annualized 10.32%); worst-month 0.1101%; idle 0.00%; deflated-Sharpe 1.0000.
- MR timing alpha (active vs AlwaysMarketRate) is a **secondary diagnostic** — near 0 means MR timing adds little over always-lending; it does NOT gate the bot-vs-idle headline. See per-cell sections.

## OOS honesty caveat

Deployed params were chosen by a sweep over this same 2022-2026 history, so these per-month returns are **in-sample to the parameter-selection process** — an **optimistic** estimate, not pristine OOS. The deflated-Sharpe section quantifies the selection-bias haircut. The only true out-of-sample test is the live canary itself.

- A positive bot-vs-idle backtest asserts the bot beats an idle balance (earns the market rate on the filled portion of budget — see idle rate / mean fill rate per cell), NOT that MR timing beats always-lending — that is the separate MR timing alpha diagnostic below.

## Cell fUST_a30

| Metric | Strategy (= bot-vs-idle) | Baseline (AlwaysMarketRate) |
|---|---|---|
| median monthly % | 0.5765 | 0.5187 |
| p25 monthly % | 0.4523 | 0.4054 |
| worst month % | 0.0614 | 0.0545 |
| best month % | 1.5883 | 1.5645 |
| annualized % | 7.70 | 6.74 |
| Sortino (monthly) | Infinity | Infinity |
| idle rate | 0.00% | 0.00% |
| mean fill rate | 1.0000 | 1.0000 |

**bot-vs-idle median monthly 95% CI (bootstrap):** [0.5339%, 0.6332%]

### MR timing alpha vs passive (secondary diagnostic)

- Diagnostic only — does NOT gate the bot-vs-idle headline. Near 0 means MR timing adds little over always-lending; the product value is bot-vs-idle.

- median active: 0.0441%/mo; mean active: 0.0752%/mo
- information ratio: 0.5101339261654407720213088063
- months strategy > baseline: 80.23%

### Selection bias

- configs tried (n_trials): 9; observations: 86
- **deflated Sharpe: 1.0000** (>0.95 = edge survives selection-bias deflation)

## Cell fUST_p2

| Metric | Strategy (= bot-vs-idle) | Baseline (AlwaysMarketRate) |
|---|---|---|
| median monthly % | 0.5687 | 0.5178 |
| p25 monthly % | 0.4566 | 0.3988 |
| worst month % | 0.0614 | 0.0545 |
| best month % | 1.9717 | 1.9073 |
| annualized % | 7.70 | 6.81 |
| Sortino (monthly) | Infinity | Infinity |
| idle rate | 0.00% | 0.00% |
| mean fill rate | 1.0000 | 1.0000 |

**bot-vs-idle median monthly 95% CI (bootstrap):** [0.5119%, 0.6041%]

### MR timing alpha vs passive (secondary diagnostic)

- Diagnostic only — does NOT gate the bot-vs-idle headline. Near 0 means MR timing adds little over always-lending; the product value is bot-vs-idle.

- median active: 0.0347%/mo; mean active: 0.0703%/mo
- information ratio: 0.3902864555565405229936832402
- months strategy > baseline: 74.42%

### Selection bias

- configs tried (n_trials): 9; observations: 86
- **deflated Sharpe: 1.0000** (>0.95 = edge survives selection-bias deflation)

## Cell fUSD_a30

| Metric | Strategy (= bot-vs-idle) | Baseline (AlwaysMarketRate) |
|---|---|---|
| median monthly % | 0.6231 | 0.5540 |
| p25 monthly % | 0.4555 | 0.4014 |
| worst month % | 0.1384 | 0.1027 |
| best month % | 4.3009 | 3.8230 |
| annualized % | 10.66 | 9.63 |
| Sortino (monthly) | Infinity | Infinity |
| idle rate | 0.00% | 0.00% |
| mean fill rate | 1.0000 | 1.0000 |

**bot-vs-idle median monthly 95% CI (bootstrap):** [0.5778%, 0.7055%]

### MR timing alpha vs passive (secondary diagnostic)

- Diagnostic only — does NOT gate the bot-vs-idle headline. Near 0 means MR timing adds little over always-lending; the product value is bot-vs-idle.

- median active: 0.0420%/mo; mean active: 0.0788%/mo
- information ratio: 0.6545348590744296882341545308
- months strategy > baseline: 74.78%

### Selection bias

- configs tried (n_trials): 9; observations: 115
- **deflated Sharpe: 1.0000** (>0.95 = edge survives selection-bias deflation)

## Cell fUSD_p2

| Metric | Strategy (= bot-vs-idle) | Baseline (AlwaysMarketRate) |
|---|---|---|
| median monthly % | 0.6256 | 0.5410 |
| p25 monthly % | 0.4368 | 0.3969 |
| worst month % | 0.1101 | 0.0983 |
| best month % | 3.8233 | 3.8233 |
| annualized % | 10.32 | 9.47 |
| Sortino (monthly) | Infinity | Infinity |
| idle rate | 0.00% | 0.00% |
| mean fill rate | 1.0000 | 1.0000 |

**bot-vs-idle median monthly 95% CI (bootstrap):** [0.5108%, 0.6858%]

### MR timing alpha vs passive (secondary diagnostic)

- Diagnostic only — does NOT gate the bot-vs-idle headline. Near 0 means MR timing adds little over always-lending; the product value is bot-vs-idle.

- median active: 0.0352%/mo; mean active: 0.0647%/mo
- information ratio: 0.5862275397042527081616190815
- months strategy > baseline: 78.26%

### Selection bias

- configs tried (n_trials): 9; observations: 115
- **deflated Sharpe: 1.0000** (>0.95 = edge survives selection-bias deflation)

## Non-backtestable risk register

The catastrophic risk for a lending bot is **platform/credit/liquidity tail** (Bitfinex insolvency, socialized loss, Tether risk). No backtest rigor addresses it — it is mitigated by the **position cap ($450)** and not lending the full balance, NOT by this report. Treat these numbers as alpha characterization only.
