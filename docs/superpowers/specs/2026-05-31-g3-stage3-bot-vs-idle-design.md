# G3 Stage 3 — bot-vs-idle reframe

Date: 2026-05-31
Status: approved (brainstorm)
Branch: `feat/g3-stage3-bot-vs-idle`

## Problem

The deployed canary's G3 live-validation gate measures **active-vs-passive**:
does the MeanReversion timing module beat an `AlwaysMarketRate` arm
(full-budget lending at the market funding rate)? Both OOS and live signals now
point to MR timing alpha ≈ 0. With the current gate, the canary is accruing
toward a verdict that — in ~8 weekly windows — would read FAIL and imply "cut
MR", when in fact the *product's* value proposition was never MR timing alpha.

The product is: **put the retail user's otherwise-idle balance to work earning
the market funding rate.** The success criterion is therefore **bot-vs-idle**
(earn the market rate vs sit at 0), not MR-vs-AlwaysMarketRate.

## Decision summary

Two design forks, both resolved by best practice (system is pre-launch, so we
refactor to the right shape rather than bolt on):

1. **Verdict semantics → primary/secondary layering.** Quant validation reports
   both "absolute return vs cash floor" and "alpha vs naive benchmark", but
   declares which is primary. For this product the primary objective is
   unambiguously absolute return vs idle. So **bot-vs-idle becomes the primary
   PASS/FAIL gate; active-vs-passive (MR alpha) is kept as a reported secondary
   diagnostic — not deleted, not gating.** Deleting MR-alpha would throw away the
   signal that decides whether to keep/simplify the MR module; a dual verdict
   with no declared primary leaves the operator without a success criterion.

2. **Scope → live G3 reframe only (staged).** Best practice is for OOS and live
   to measure the same metric. Key insight: because idle return ≡ 0,
   bot-vs-idle = the bot's own absolute return, which the backtest's
   `summarize_oos` (`median_monthly` / `annualized_pct`) already computes — so
   OOS↔live comparison is possible today with no new backtest arm. Combined with
   the canary urgency (live is accruing toward the wrong gate) and the value of a
   small reviewable unit, the backtest report headline reframe is logged as an
   **optional follow-up**; this round changes the live G3 path only.

## The math (why this is clean)

The idle arm lends nothing, so its per-window `net_monthly ≡ 0`. The paired
per-window difference is therefore:

    bot_vs_idle[w] = active.net_monthly[w] - 0 = active.net_monthly[w]

The active arm's `net_monthly = clamped_interest / capital * 100` already bakes
in idle drag: capital not deployed earns 0 within the budget. So bot-vs-idle is
exactly "what did the retail user's full allocation earn vs leaving it idle",
with no double-counting. The CI is the bootstrap CI over the active arm's
per-window returns.

`active.net_monthly ≥ 0` in normal operation (positive rates, positive sizes),
so PASS becomes near-certain once enough windows accrue — and that is *correct
and meaningful*: it asserts "the product reliably beats idle". The discriminating
outcomes move to **UNRELIABLE** (anchor divergence catches the attribution bugs
that plagued the canary) and the **MR-alpha diagnostic** (does MR add value).
FAIL (bot-vs-idle CI entirely < 0) is retained for completeness — reachable only
under negative-rate regimes or realized losses.

## Approach (selected: explicit idle zero-arm + decoupled primary verdict)

Rejected alternatives:
- *Compute bot-vs-idle directly from active net_monthly, no idle arm* — fewer
  lines but breaks the symmetric "two arms → `paired_active_returns`" pattern and
  the report can't honestly show the idle baseline. Marginal.
- *Relabel only, keep gate on active-vs-passive* — cosmetic; does not fix the
  "canary accruing toward the wrong gate" problem. Rejected.

## Design

### 1. `live_attribution.py`

- **New** `attribute_idle(window_bounds) -> list[WindowOutcome]`: one
  `WindowOutcome` per window with `net_monthly=0`, `n_trades=0`, `fill_rate=0`.
  Pure, mirrors `attribute_passive`'s shape so it feeds `paired_active_returns`.
- **`G3Verdict`**: rename `headline_active_spread` → `headline_bot_vs_idle`
  (pre-launch, no external API consumers — only the report files read it; rename
  for clarity). Add secondary MR-alpha diagnostic fields:
  - `mr_alpha_spread: Decimal` — single-span active − passive headline.
  - `mr_alpha_ci_lo: Decimal`, `mr_alpha_ci_hi: Decimal` — bootstrap CI of paired
    active − passive (the old gating numbers, now diagnostic).
  - `mr_alpha_available: bool` — False when market-rate coverage/band makes the
    passive arm untrustworthy; the diagnostic is then reported as unavailable
    rather than misleading.
- **`decide_verdict`**: the primary gate now keys off the bot-vs-idle CI
  (`ci_lo`/`ci_hi` carry the idle-spread CI; `headline_bot_vs_idle` the absolute
  return). PASS = CI entirely > 0; FAIL = CI entirely < 0; straddles 0 →
  INSUFFICIENT_DATA. The `n_windows` / `total_capital_days` minimum gates are
  unchanged. The anchor checks (`deployment_anchor`, `nav_anchor`) still force
  UNRELIABLE — they verify attribution integrity, independent of which arm is
  primary. MR-alpha fields are carried through but never change the state.

### 2. `_g3_loaders.py` (`_compute_verdict`)

- Build three arms: `attribute_active` (strat), `attribute_passive` (market
  rate), `attribute_idle` (zeros).
- **Primary**: `paired_active_returns(strat, idle)` → `bootstrap_ci` → drives the
  verdict. `headline_bot_vs_idle` = single-span active net_monthly (passive not
  subtracted).
- **Secondary**: `paired_active_returns(strat, passive)` → mean + `bootstrap_ci`
  → `mr_alpha_spread` / `mr_alpha_ci_*`.
- **Decouple guards from primary verdict (correctness change):**
  - The market-rate **coverage guard** (no candle coverage in window) and the
    **band guard** (wrong-scale market series) currently degrade the *whole*
    verdict to INSUFFICIENT_DATA / UNRELIABLE. After the reframe they affect only
    the **MR-alpha diagnostic**: set `mr_alpha_available = False` and surface the
    reason as a caveat. The primary bot-vs-idle verdict does **not** require
    market-rate coverage (idle ≡ 0 needs no candles) and is no longer blocked by
    passive-arm data quality.
  - The anchor-divergence UNRELIABLE path is unchanged — those reflect
    attribution-vs-venue truth, which still gates the primary verdict.

### 3. `run_g3_live_validation.py` (`render_markdown`, `_verdict_to_json`)

- TL;DR leads with bot-vs-idle: `Verdict`, `Headline bot-vs-idle (absolute
  return since inception)`, `bot-vs-idle 95% CI`.
- New **"MR timing alpha (secondary diagnostic)"** section: `mr_alpha_spread`,
  `mr_alpha_ci`, or "unavailable — <reason>" when `mr_alpha_available` is False.
  Explicit note: *idle arm = 0% by construction; the headline is the bot's own
  realized return on the allocated budget.*
- Honesty caveats: add the bot-vs-idle framing note (PASS asserts "beats idle",
  not "beats always-lend"); keep the existing clamp / held-to-term caveats.
- Recommendation block reworded for bot-vs-idle PASS/FAIL/INSUFFICIENT/UNRELIABLE.
- JSON: add an `mr_alpha` block (`spread`, `ci_lo`, `ci_hi`, `available`);
  rename `headline_active_spread` → `headline_bot_vs_idle`.

### 4. Tests (TDD — written first)

- `tests/modules/live_validation/test_live_attribution.py`:
  - `attribute_idle` returns zero-arm outcomes aligned 1:1 with `window_bounds`.
  - `decide_verdict` reframed: PASS/FAIL/straddle on bot-vs-idle CI; anchor
    divergence → UNRELIABLE regardless of MR-alpha; MR-alpha fields carried but
    non-gating.
- `tests/scripts/test_g3_loaders.py`:
  - `_compute_verdict` computes both paired series; primary verdict independent of
    market-rate coverage; coverage/band only flips `mr_alpha_available` + caveat,
    not the primary state; anchor divergence still UNRELIABLE.
- `tests/scripts/test_run_g3_live_validation.py`:
  - `render_markdown` leads with bot-vs-idle headline; MR-alpha section renders
    available and unavailable variants; JSON shape includes `mr_alpha` + renamed
    headline key.

## Out of scope / follow-ups

- Backtest OOS report headline reframe to lead with bot-vs-idle (consistency —
  optional, separate spec; the OOS absolute return already exists via
  `summarize_oos`).
- G3 Gate #4 verdict still needs ≥ 8 weekly windows (~2 months) to leave
  INSUFFICIENT_DATA — unchanged by this reframe.
