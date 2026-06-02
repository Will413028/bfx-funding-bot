# Canary OOS Profitability — RECENT WINDOW (fUST + fUSD MeanReversion a30/p2)

**Run date**: 2026-06-03 (clock skew: machine UTC stamped 2026-06-02T16:08; real date 2026-06-03)
**Data window**: 2022-01 .. now (50 monthly OOS windows/cell — the script default)
**Config**: `configs/cells.canary.yaml` (deployed params, fixed — not re-swept)
**Fill model**: linear (deterministic) — see methodology.

> ⚠️ **Scope note (recent-window run, 2026-06-03):** This is the script-default `START_MTS=2022-01-01` run, the companion to the manual full-history run (`2026-06-02-oos-full-history-profitability.md`, `START_MTS` temporarily → 2016, since reverted). Two static template strings below are template artifacts: the section title's "fUST (a30, p2)" — this run **also covers fUSD** (see fUSD cells); the auto-generated title above has been corrected. Unlike the full-history run, here the OOS-honesty caveat **is literally true**: params were selected by a sweep over 2022-2026, so these are **in-sample to parameter selection** — an optimistic estimate, not pristine OOS. **Headline reversal vs the hypothesis: the recent regime did NOT thin the MR timing alpha — it thinned the bot-vs-idle *level* (esp. fUSD).** See the comparison section.

## TL;DR

- Primary metric = **bot-vs-idle**: the strategy's absolute monthly return on budget. An idle balance earns 0%, so this return *is* the bot-vs-idle edge (idle arm = 0% by construction).
- **fUST_a30** (50 months): bot-vs-idle median 0.5502%/mo (annualized 7.12%); worst-month 0.2404%; idle 0.00%; deflated-Sharpe 1.0000.
- **fUST_p2** (50 months): bot-vs-idle median 0.5386%/mo (annualized 7.22%); worst-month 0.2678%; idle 0.00%; deflated-Sharpe 1.0000.
- **fUSD_a30** (50 months): bot-vs-idle median 0.5147%/mo (annualized 7.17%); worst-month 0.2669%; idle 0.00%; deflated-Sharpe 1.0000.
- **fUSD_p2** (50 months): bot-vs-idle median 0.4853%/mo (annualized 6.66%); worst-month 0.2669%; idle 0.00%; deflated-Sharpe 1.0000.
- MR timing alpha (active vs AlwaysMarketRate) is a **secondary diagnostic** — near 0 means MR timing adds little over always-lending; it does NOT gate the bot-vs-idle headline. See per-cell sections.

## Recent (2022-26) vs full-history (2016-26) vs live G3

The motivating question was "is the recent-regime MR alpha thinner than the full-history +~1%/yr, and does that explain the live G3 ≈0?" **The data answers no on both counts.** Two things move *in opposite directions* between the windows:

### What thinned: the bot-vs-idle *level* (market rate), not the alpha

| Cell | bot-vs-idle ann% — full (2016-26) | bot-vs-idle ann% — recent (2022-26) | Δ |
|---|---|---|---|
| fUST_a30 | 7.70 | 7.12 | −0.58 |
| fUST_p2 | 7.70 | 7.22 | −0.48 |
| fUSD_a30 | 10.66 | 7.17 | **−3.49** |
| fUSD_p2 | 10.32 | 6.66 | **−3.66** |

The recent window lacks the fat 2017/2021 funding-rate spikes — fUSD best-month collapses 4.30%→1.23%, fUST 1.99%→1.14%. fUSD compresses hard toward fUST (both ~7%/yr now). This is a **regime fact about the market rate**, and it is the thing that actually faded. The earnings floor stayed positive though: **worst month is still positive in every cell** (recent worst ~0.24-0.27%/mo, *higher* than full-history's 0.06-0.14% — fewer fat months also means fewer thin ones).

### What did NOT thin: the MR timing alpha (it tightened)

| Cell | MR alpha %/yr full → recent | IR full → recent | win% full → recent |
|---|---|---|---|
| fUST_a30 | +0.96 → +0.92 | 0.51 → **1.01** | 80 → 86 |
| fUST_p2 | +0.89 → **+1.15** | 0.39 → 0.73 | 74 → 84 |
| fUSD_a30 | +1.03 → **+1.23** | 0.65 → 0.77 | 75 → 82 |
| fUSD_p2 | +0.85 → **+1.08** | 0.59 → 0.66 | 78 → 86 |

Annualized alpha held at **+0.9 to +1.2%/yr** (comparable-to-higher), and on a *risk-adjusted* basis the recent window is uniformly **better**: information ratio and win-rate rose in all four cells (fUST_a30 IR doubled 0.51→1.01, win 80%→86%). Intuition: in a calmer rate regime the small mean-reversion edge is more *consistent* (less drowned by huge passive moves), even though the absolute dollars are smaller. **There is no "alpha decay" story in the backtest.**

### So why is live G3 MR-alpha ≈ +0.0055%/mo (≈0) when backtest says +0.06%/mo?

Not regime fade (just shown). The gap is **measurement, not market**:

1. **Insufficient data dominates.** Live G3 verdict is still `INSUFFICIENT_DATA` (<8 weekly windows; the verdict gate needs ≈2 months). A handful of weeks of live data has a noise band that easily swallows a +0.06%/mo signal — the point estimate is not yet distinguishable from 0. This is the leading explanation; the live number is a thin-sample artifact, not a measured fade.
2. **Backtest assumes mean fill rate = 1.0.** Every cell above fills 100% of budget at the market rate instantly. Live can't: the bot posts a 2-day offer (`period_days=2`, hardcoded), so capital turns over slowly, sits in the book unfilled, and only fills at queue position. The MR *timing* edge — being in the book at the right moment — is precisely what a partial/slow fill erodes, so **live MR-alpha ≤ backtest MR-alpha by construction**, independent of regime. (bot-vs-idle is more robust to this: even slow fills still earn the market rate on the filled portion.)
3. **Different baseline framing.** Backtest active-return is vs `AlwaysMarketRate` (a continuously-filled passive arm); live G3's primary gate is now bot-vs-idle, with MR-alpha demoted to a non-gating diagnostic ([[g3-passive-baseline-frr-bug]] Stage 3 reframe). Comparing the live MR-alpha *diagnostic* to the backtest active return is apples-to-oranges on the denominator.

### Takeaway for sizing-up decisions

- **Durable value = bot-vs-idle (market-rate capture).** It is regime-sensitive in *level* (recent ~7%/yr for both currencies, down from full-history fUSD ~10%), but stays positive every month and survives selection-bias deflation (deflated-Sharpe 1.0). This is what the product sells and what scales with capital.
- **MR timing alpha = a regime-dependent bonus, ~+1%/yr in backtest and not decaying** — but live has not yet *confirmed* it (insufficient data) and the 100%-fill assumption means the realized live bonus will be smaller than the +1%/yr headline. Do **not** size up on the timing alpha; size up on bot-vs-idle, treat any live timing alpha as upside.
- The gate for adding real money remains the **live G3 ≥8-window verdict**, not the backtest — the backtest's optimism (in-sample params + 100% fill) is exactly why live is the only honest OOS.

## OOS honesty caveat

Deployed params were chosen by a sweep over this same 2022-2026 history, so these per-month returns are **in-sample to the parameter-selection process** — an **optimistic** estimate, not pristine OOS. The deflated-Sharpe section quantifies the selection-bias haircut. The only true out-of-sample test is the live canary itself.

- A positive bot-vs-idle backtest asserts the bot beats an idle balance (earns the market rate on the filled portion of budget — see idle rate / mean fill rate per cell), NOT that MR timing beats always-lending — that is the separate MR timing alpha diagnostic below.

## Cell fUST_a30

| Metric | Strategy (= bot-vs-idle) | Baseline (AlwaysMarketRate) |
|---|---|---|
| median monthly % | 0.5502 | 0.4949 |
| p25 monthly % | 0.4579 | 0.4079 |
| worst month % | 0.2404 | 0.1687 |
| best month % | 1.1371 | 0.8520 |
| annualized % | 7.12 | 6.20 |
| Sortino (monthly) | Infinity | Infinity |
| idle rate | 0.00% | 0.00% |
| mean fill rate | 1.0000 | 1.0000 |

**bot-vs-idle median monthly 95% CI (bootstrap):** [0.5036%, 0.5999%]

### MR timing alpha vs passive (secondary diagnostic)

- Diagnostic only — does NOT gate the bot-vs-idle headline. Near 0 means MR timing adds little over always-lending; the product value is bot-vs-idle.

- median active: 0.0613%/mo; mean active: 0.0729%/mo
- information ratio: 1.011983739397408317893112067
- months strategy > baseline: 86.00%

### Selection bias

- configs tried (n_trials): 9; observations: 50
- **deflated Sharpe: 1.0000** (>0.95 = edge survives selection-bias deflation)

## Cell fUST_p2

| Metric | Strategy (= bot-vs-idle) | Baseline (AlwaysMarketRate) |
|---|---|---|
| median monthly % | 0.5386 | 0.4789 |
| p25 monthly % | 0.4583 | 0.4026 |
| worst month % | 0.2678 | 0.2526 |
| best month % | 1.1402 | 0.8550 |
| annualized % | 7.22 | 6.07 |
| Sortino (monthly) | Infinity | Infinity |
| idle rate | 0.00% | 0.00% |
| mean fill rate | 1.0000 | 1.0000 |

**bot-vs-idle median monthly 95% CI (bootstrap):** [0.4869%, 0.5747%]

### MR timing alpha vs passive (secondary diagnostic)

- Diagnostic only — does NOT gate the bot-vs-idle headline. Near 0 means MR timing adds little over always-lending; the product value is bot-vs-idle.

- median active: 0.0654%/mo; mean active: 0.0908%/mo
- information ratio: 0.7307229307960330465354333317
- months strategy > baseline: 84.00%

### Selection bias

- configs tried (n_trials): 9; observations: 50
- **deflated Sharpe: 1.0000** (>0.95 = edge survives selection-bias deflation)

## Cell fUSD_a30

| Metric | Strategy (= bot-vs-idle) | Baseline (AlwaysMarketRate) |
|---|---|---|
| median monthly % | 0.5147 | 0.4657 |
| p25 monthly % | 0.4365 | 0.3831 |
| worst month % | 0.2669 | 0.2549 |
| best month % | 1.2332 | 0.8874 |
| annualized % | 7.17 | 5.94 |
| Sortino (monthly) | Infinity | Infinity |
| idle rate | 0.00% | 0.00% |
| mean fill rate | 1.0000 | 1.0000 |

**bot-vs-idle median monthly 95% CI (bootstrap):** [0.4814%, 0.6041%]

### MR timing alpha vs passive (secondary diagnostic)

- Diagnostic only — does NOT gate the bot-vs-idle headline. Near 0 means MR timing adds little over always-lending; the product value is bot-vs-idle.

- median active: 0.0603%/mo; mean active: 0.0975%/mo
- information ratio: 0.7682637967303896293116539926
- months strategy > baseline: 82.00%

### Selection bias

- configs tried (n_trials): 9; observations: 50
- **deflated Sharpe: 1.0000** (>0.95 = edge survives selection-bias deflation)

## Cell fUSD_p2

| Metric | Strategy (= bot-vs-idle) | Baseline (AlwaysMarketRate) |
|---|---|---|
| median monthly % | 0.4853 | 0.4422 |
| p25 monthly % | 0.4090 | 0.3868 |
| worst month % | 0.2669 | 0.2489 |
| best month % | 1.2048 | 0.7483 |
| annualized % | 6.66 | 5.58 |
| Sortino (monthly) | Infinity | Infinity |
| idle rate | 0.00% | 0.00% |
| mean fill rate | 1.0000 | 1.0000 |

**bot-vs-idle median monthly 95% CI (bootstrap):** [0.4489%, 0.5451%]

### MR timing alpha vs passive (secondary diagnostic)

- Diagnostic only — does NOT gate the bot-vs-idle headline. Near 0 means MR timing adds little over always-lending; the product value is bot-vs-idle.

- median active: 0.0443%/mo; mean active: 0.0857%/mo
- information ratio: 0.6584498375782478968810011087
- months strategy > baseline: 86.00%

### Selection bias

- configs tried (n_trials): 9; observations: 50
- **deflated Sharpe: 1.0000** (>0.95 = edge survives selection-bias deflation)

## Non-backtestable risk register

The catastrophic risk for a lending bot is **platform/credit/liquidity tail** (Bitfinex insolvency, socialized loss, Tether risk). No backtest rigor addresses it — it is mitigated by the **position cap ($450)** and not lending the full balance, NOT by this report. Treat these numbers as alpha characterization only.
