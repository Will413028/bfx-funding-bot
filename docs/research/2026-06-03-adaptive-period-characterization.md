# Adaptive-Period Strategy — OOS Characterization

**Run date**: 2026-06-03
**Strategy**: `AdaptivePeriodStrategy` (Phase 3c) — lends at `candle.close` every cycle, varies the lock `period_days` from the rate's deviation-from-EMA (at/below trend → 2d floor; mildly above → 7d; spike `>band2` → 30d).
**Config**: `backend_py/configs/cells.experimental.yaml` (fUST/fUSD × a30/p2; `ema_span=24, t1=0.5, t2=1.5, p_mid=7, p_long=30`; `ratio_sigma` borrowed from canary, strategy-independent).
**Raw reports**: `2026-06-03-adaptive-period-recent.md` (2022→now, 50 windows/cell) and `2026-06-03-adaptive-period-fullhistory.md` (fUST 86 / fUSD 115 windows; manual `START_MTS=2016` override, reverted, NOT committed).
**Fill model**: linear, mean fill rate = 1.0 (100% fill at market) — the key optimism caveat, see §4.

> ⚠️ **Template artifact**: both raw reports carry the static renderer title "fUST MeanReversion (a30, p2)" and `Config: cells.canary.yaml` / the "2022-2026 in-sample" caveat. For these runs the strategy is **AdaptivePeriod** over `cells.experimental.yaml`. Numbers and per-cell sections are correct; only those two static strings are stale (same renderer limitation noted in the MR full-history report).

## TL;DR

Adaptive period adds a **real, regime-robust edge over always-2d lending**, but the headline annualized number is **fat-tail-inflated** by rare spike-capture months. The honest read:

- **bot-vs-idle ranking (annualized):** AdaptivePeriod **>** MeanReversion **>** AlwaysMarketRate, in both windows and all 4 cells.
- **Median** period alpha (vs always-2d) ≈ **+1.2–1.4%/yr** (full history) — modest, robust, won 68–77% of months, deflated-Sharpe ≈ 1.0. This is comparable-to-better than MR's timing alpha (+0.9–1.2%/yr) and is the **defensible** estimate.
- **Mean / headline** period alpha ≈ **+3–6%/yr** — driven by a handful of fat-tail months (fUSD_a30 best month **14.76%** vs baseline 3.82%), i.e. locking 30 days into a 2017/2021-style funding spike. Real in backtest, but the **most fill-model-sensitive** part of the edge (see §4) → expect heavy live haircut.
- **Verdict**: promising enough to pursue, but do NOT size up on the headline. Size on the median period alpha; treat spike-capture as uncertain upside pending live confirmation. Recommended next step is a **`p_long` sensitivity sweep** before any deploy wiring (§6).

## 1. bot-vs-idle (annualized %) — three strategies, two windows

Sources: AdaptivePeriod = this run; MeanReversion = `2026-06-02-oos-full-history-profitability.md` (full) and `2026-06-03-oos-recent-window.md` (recent); AlwaysMarketRate = the baseline column of each AdaptivePeriod report (period=2, identical baseline definition).

### Full history (fUST 2018→, fUSD 2016→)

| Cell | AdaptivePeriod | MeanReversion | AlwaysMarketRate |
|---|---|---|---|
| fUST_a30 | **9.91** | 7.70 | 6.74 |
| fUST_p2 | **10.05** | 7.70 | 6.81 |
| fUSD_a30 | **15.38** | 10.66 | 9.63 |
| fUSD_p2 | **14.81** | 10.32 | 9.47 |

### Recent window (2022→now)

| Cell | AdaptivePeriod | MeanReversion | AlwaysMarketRate |
|---|---|---|---|
| fUST_a30 | **9.94** | 7.12 | 6.20 |
| fUST_p2 | **9.38** | 7.22 | 6.07 |
| fUSD_a30 | **11.94** | 7.17 | 5.94 |
| fUSD_p2 | **10.56** | 6.66 | 5.58 |

AdaptivePeriod is the top bot-vs-idle in every cell and both windows. Worst month stays **positive** in all cells (full history: fUST ~0.026–0.029%, fUSD ~0.13–0.15%) — it never produces a losing month.

## 2. Period alpha (vs AlwaysMarketRate period=2) — headline vs median

The period alpha is the active return of AdaptivePeriod over the always-2d baseline at the same market rate — it isolates the value of *varying the lock duration*.

| Cell | Period α (ann, full) | Period α (ann, recent) | median active %/mo (full) | mean active %/mo (full) | IR (full) | win% (full) |
|---|---|---|---|---|---|---|
| fUST_a30 | +3.17 | +3.74 | 0.1123 (~1.35%/yr) | 0.2461 | 0.53 | 74 |
| fUST_p2 | +3.24 | +3.31 | 0.1064 (~1.28%/yr) | 0.2523 | 0.51 | 77 |
| fUSD_a30 | +5.75 | +6.00 | 0.1117 (~1.34%/yr) | **0.4384** | 0.37 | 73 |
| fUSD_p2 | +5.34 | +4.98 | 0.0973 (~1.17%/yr) | **0.4051** | 0.48 | 68 |

**For comparison, MeanReversion's timing alpha** (vs the same baseline) was +0.85–1.03%/yr (full) / +0.92–1.23%/yr (recent), median active ~0.035–0.044%/mo, IR 0.39–0.65, win 74–80%.

So on a **median** basis AdaptivePeriod's period alpha (~0.10–0.11%/mo, ~+1.2–1.4%/yr) is **modestly larger** than MR's timing alpha (~0.035–0.044%/mo) and roughly as consistent (win 68–77% vs 74–80%). The big divergence is in the **mean**: AdaptivePeriod's mean active is 2–4× its median (fUSD: 0.10 median vs 0.44 mean), whereas MR's mean/median gap is small. That gap is the fat tail.

## 3. The fat tail — where the headline comes from

The annualized headline (+3–6%/yr) is much larger than the median period alpha (+~1.3%/yr) because a small number of months dominate:

- **fUSD_a30 best month = 14.76%** (baseline best month: 3.82%). fUSD_p2 best = 8.11% (baseline 3.82%). fUST best ~2.6–2.8% (baseline 1.56–1.91%).
- Mechanism: when the rate is well above its EMA (a funding spike — frequent in 2017 and 2021), the strategy locks `period_days=30` at that spike rate. The engine compounds `(1 + rate·30)`, so a 30-day lock at a high daily rate produces an outsized single-window return. The 2016–2021 fUSD history has the fattest spikes, which is why fUSD's headline (15.4%) and best-month (14.8%) blow out while its median (0.86%/mo) is far more pedestrian.
- IR is *lower* full-history (0.37–0.53) than recent (0.65–0.87) precisely because those fat-tail spike months add variance to the active-return series.

**Interpretation**: the strategy has two distinct edges layered together — (a) a steady ~+1.3%/yr median duration edge (lock-the-locally-high-rate, robust across regimes), and (b) a convex spike-capture option (lock 30d into a funding spike). (a) is reliable; (b) is lottery-like and regime-dependent (fat in 2017/2021, thin in calm years).

## 4. Caveats (what live will not reproduce)

- **100% fill assumption is most violated exactly where the headline lives.** The fat-tail months require a 30-day offer filling fully at the spike rate. In live: (i) funding spikes are often short-lived, so a 30-day offer posted at the spike may sit unfilled as the rate reverts; (ii) borrowers prefer shorter/flexible terms, so long offers at elevated rates fill slowly and partially; (iii) at a spike, lending supply surges (everyone posts), pushing fill probability down. So **live will haircut the spike-capture (mean-driven) portion heavily** — the median (~+1.3%/yr) is the more trustworthy live estimate.
- **In-sample-ish params.** `ema_span`/`ratio_sigma` come from the same 2022-2026 EDA used for MR; the tier thresholds were chosen, not swept here. deflated-Sharpe ≈ 1.0 (n_trials=4) says the bot-vs-idle headline survives selection deflation, but the period-tier choice (2/7/30) is unexamined — see §6.
- **Duration tail risk.** A 30-day lock commits capital for up to a month — marginally more platform/credit/liquidity tail exposure (Bitfinex/Tether) per the standing risk register, and less responsiveness if conditions deteriorate mid-lock. Bounded by position caps, not this strategy.
- **Lumpy returns.** With `p_long=30`, a single lock can span almost an entire 1-month OOS window → few decisions per window and lumpy month-to-month returns. Real, not a bug, but it widens the live confidence band.

## 5. Period-tier firing distribution (instrumentation gap)

The OOS report does not expose how often each tier (2 / 7 / 30) fired. The engine-order integration tests (`test_integrated_*`) confirm all three tiers fire under realistic `ema_span=24`, and the fat-tail months are direct evidence that `p_long=30` fires during spikes. But the exact tier-occupancy (and therefore how concentrated the edge is) is not quantified here. **Follow-up**: add per-tier decision counts to the OOS report (or a one-off instrumentation pass) so the spike-capture concentration is measurable.

## 6. Recommendation / next steps

1. **`p_long` sensitivity sweep (do this first).** Re-characterize with `p_long ∈ {7, 14, 30}` (and maybe `p_mid ∈ {4, 7}`). This directly answers the central risk: how much of the median edge survives if we cap the lock shorter, trading away fat-tail spike-capture for far lower fill risk? If `p_long=14` keeps most of the median +1.3%/yr with a much smaller (more fill-robust) tail, that's the better live candidate. Cheap — just edit `cells.experimental.yaml` / `param_grid_for_cell` and re-run.
2. **Do NOT size up on the headline.** Treat the median period alpha (~+1%/yr) as the bonus; treat spike-capture as uncertain, fill-gated upside.
3. **Deployment stays gated.** Wiring `adaptive_period` into `derive_cells --write/--check` + adding canary cells + the `divergence_reporter` period-boundary handling (spec §7) remain deferred until (a) a favorable sweep and (b) the live MR G3 verdict clears (≥8 windows). The backtest's optimism (in-sample params + 100% fill, concentrated in the fat tail) is exactly why live is the only honest test.
4. **Optional fill-model stress.** A conservative re-run that down-weights fill probability for long-duration offers would bound the live haircut on the spike-capture portion. Lower priority than the `p_long` sweep.

## 7. Status

v1 implementation complete and fully unit-tested (`tests/modules/backtest/strategies/test_adaptive_period.py`, 22 tests; full suite 1153 green, mypy clean, ruff clean). Strategy is wired into the shared `build_strategy` factory, so it runs in both backtest and (when deployed) live with no harness changes. The live variable-period plumbing is confirmed end-to-end (`LendDecision.period_days` → signal_engine → StandingQuote → reconciler → Bitfinex submit `"period"`). No live deployment performed; `cells.canary.yaml` untouched.
