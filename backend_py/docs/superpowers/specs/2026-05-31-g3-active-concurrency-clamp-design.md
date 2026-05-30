# G3 Stage 2 — Active-arm concurrent-principal clamp

Date: 2026-05-31
Branch: `feat/g3-active-concurrency-clamp`
Follows: G3 Stage 1 (passive baseline → `funding_candles.close`, merged `50282cd`)

## Problem

The active arm of the G3 live validation
(`modules/live_validation/live_attribution.py` + `scripts/_g3_loaders.py`)
sums realized interest as `Σ(size_i · rate_i · duration_i)` with **no ceiling
on concurrently-open principal**. The passive (AlwaysMarketRate) arm, in
contrast, is normalized to a fixed budget `C` (the canary cap, 570):
`passive_return = mean(market_rate) · window_days` (capital cancels).

Live canary state: 4 fills accumulate ~863 concurrent principal > 570 cap. The
active arm therefore "deploys" more than the budget the passive arm assumes,
making the two arms different capital bases — the +0.0095% headline spread is
inflated.

A second, adjacent latent bug surfaced while specing this:
`_compute_verdict` computes `total_capital_days = Σ(size·duration) / capital`
(units: *days*, ≈2 for one full-budget 2-day fill), but `decide_verdict`'s
contract — proven by its own unit test (`total_capital_days=4000` vs
`min_capital_days=3990`) and by `min_capital_days = capital · 7` (=3990) — is
**USDT·days**. The `/ capital` is a unit bug, currently masked by the
`n_windows < 8` gate firing first. Stage 2 fixes it as part of the same change.

## Approach

Clamp instantaneous open principal to the cap `C` via a pure **sweep-line**
integration. The strategy physically cannot deploy more than `C`, so
over-budget concurrency (a data artifact or a transient cap breach) is not
credited beyond `C` in the **budget-normalized return comparison**.

### Clamp semantics (decided)

1. **Proportional scaling** when over budget. At each instant `t` with total
   open principal `S(t) > C`, every open fill's instantaneous contribution is
   scaled by `C / S(t)`. Rate-neutral, deterministic, fair to all fills.
   Rejected: greedy/priority caps (arbitrary which fills survive).

2. **Anchors stay RAW (un-clamped).** `attributed_interest` (NAV anchor) and
   `attributed_deployed` (deployment anchor) compare against *venue reality*
   (position_state / NAV reflect actual deployed principal even if it exceeded
   the cap). The clamp serves ONLY the active-vs-passive budget-normalized
   return comparison. Clamping the anchors would mis-flag a real over-deploy as
   model drift.

3. **Cross-window fills split.** The sweep clips interest to window bounds, so
   interest accrues in the window where the time passes. `n_trades` /
   `fill_rate` stay bucketed by `fill_ts` (diagnostics only).

## Components

### New pure primitive — `live_attribution.py`

```python
@dataclass(frozen=True)
class ClampedWindow:
    interest: Decimal        # budget-clamped realized interest (∫ scale·Σ(size·rate) dt)
    capital_days: Decimal    # budget-clamped capital-days (∫ min(S(t), C) dt), USDT·days
    raw_interest: Decimal    # un-clamped Σ(size·rate·duration) — for over-deploy diagnostic
    peak_concurrent: Decimal # max instantaneous open principal — for over-deploy diagnostic

def clamp_active_window(
    fills: list[FillRecord], *, cap: Decimal, lo: int, hi: int
) -> ClampedWindow:
    """Sweep-line over [lo, hi). Each fill's interest-accruing interval is
    [fill_ts, fill_ts + _fill_duration_days·MS_PER_DAY), clipped to [lo, hi).
    At each sub-interval: S = Σ open sizes; scale = min(1, cap/S) (0 if S==0);
    interest += scale·Σ(size·rate)·dt/day; capital_days += min(S, cap)·dt/day.
    Accumulate sub-interval durations as integer ms; divide by MS_PER_DAY once
    so the no-clamp case is bit-exact with the legacy Σ(size·rate·duration)."""
```

### `attribute_active` (changed)

Per window: `net_monthly = clamp_active_window(fills, cap=capital, lo, hi).interest / capital · 100`.
`n_trades` / `fill_rate` unchanged (still bucketed by `fill_ts`).

### `_compute_verdict` (`scripts/_g3_loaders.py`, changed)

- `total_capital_days` = `clamp_active_window(fills, cap=capital, min_ts, max_ts).capital_days`
  (USDT·days; **drop the `/ capital`**). Naturally ≤ cap × span_days.
- `attributed_interest` stays RAW: `Σ(size·rate·_fill_duration_days)` (NAV anchor).
- `attributed_deployed` stays RAW: `open_principal_at(fills, max_ts)` (deployment anchor).
- Build an over-deploy diagnostic from the full-span `ClampedWindow`
  (`raw_interest`, `interest`, `peak_concurrent`, `cap`) and return it.

### Report plumbing

- `_compute_verdict` / `build_verdict_from_neon` return an extra
  `ClampDiagnostic | None` (4th tuple element).
- `render_markdown` prints an honesty-caveat line **only when over-deploy
  detected** (`peak_concurrent > cap`), e.g.:
  `Active arm clamped to budget C=570: raw concurrent principal peaked at 863
  (1.51x cap) → 0.0061% over-deploy excess removed.`

## Data flow

```
event_log fills + funding_candles.close + position_state
   │
   ├─ attribute_active ── per-window net_monthly (clamped) ─┐
   ├─ attribute_passive ── per-window net_monthly ──────────┼─ paired CI / headline
   ├─ clamp_active_window(full span) ── capital_days (USDT·days, clamped)
   │                                  └─ over-deploy diagnostic ── render line
   ├─ open_principal_at(max_ts) ── attributed_deployed (RAW) ── deployment anchor
   └─ Σ(size·rate·dur) ── attributed_interest (RAW) ── NAV anchor
```

## Verdict impact

**None on state.** Still INSUFFICIENT_DATA at the current 1 window (< 8). The
clamped `total_capital_days` ≤ cap × window-days < `min_capital_days` (=3990).
Only the diagnostic headline drops (cleaner, lower) and the new honesty line
appears.

## Testing

New tests (`test_live_attribution.py`):
- `clamp_active_window`: no-overlap == legacy formula (bit-exact, USDT·days);
  two overlapping fills under cap (no clamp); overlap exceeding cap (proportional
  scale, interest & capital_days reflect cap); cross-window split; released-early
  duration; peak_concurrent + raw_interest correctness; empty fills.
- `attribute_active`: existing exact-value tests stay green; new overlapping
  >cap test asserts clamped net_monthly.

New tests (`test_g3_loaders.py`):
- `_compute_verdict`: `total_capital_days` now USDT·days (≈ cap×days, not /capital);
  over-deploy diagnostic populated when concurrent > cap; absent when ≤ cap.
- existing state assertions (INSUFFICIENT/UNRELIABLE) stay green.

Gate: `cd backend_py && uv run pytest -m "not integration"` + `uv run mypy src/`
+ `uv run ruff check` all green before commit. (`scripts/` not in default mypy
gate; two pre-existing scripts errors are tech debt — do not touch.)

## Out of scope

- Fill held-to-term duration stays `p2 = 2 days` (do NOT use `avg_period` — that
  was the C4/C5 trap that fabricates 12x interest).
- Passive baseline unchanged (Stage 1).
- `decide_verdict` decision table unchanged.
- The verdict will not flip to PASS/FAIL until ≥8 weekly windows accrue
  (independent of this change).

## Risks / verify adversarially (post-implementation review)

- Confirm `total_capital_days` unit fix matches `decide_verdict`'s contract and
  doesn't silently change the gate once n_windows ≥ 8 in some plausible future.
- Confirm proportional clamp + cross-window split keep existing exact-value
  tests bit-exact (Decimal precision).
- Confirm anchors-raw vs return-clamped split is the right semantic boundary.
