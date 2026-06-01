# NAV-split — per-symbol loss/drawdown limiters (fUSD-live hard gate D4)

Date: 2026-06-02
Scope: split the L2 loss-limiter (`RealizedLossGuard`) and drawdown-limiter
(`DrawdownGuard`) from a single GLOBAL-summed NAV metric to PER-SYMBOL metrics,
so a profitable currency can no longer mask a losing one. This is the D4
hard-gate from the per-currency Phase 2 ADR (`2026-06-01-per-currency-allocation-phase-2.md`)
— it MUST land before a 2nd currency (fUSD) trades.

OUT OF SCOPE (deliberately, see §Out of scope): per-symbol threshold *values*
(map), a portfolio/account-level backstop guard, peak high-water-mark
persistence across restart, and the DivergenceRateGuard (still a constant-0
stub). Threshold stays a single shared scalar per the design decision below.

## Problem

`ReconcileNavTracker` (`modules/execution/safety/nav_pnl_source.py`) is the
in-memory NAV source feeding both L2 calibrated guards. NAV (account equity) for
a currency = `available + reserved + realized`, sampled from each per-symbol
`PositionReconciled` event. The tracker ALREADY buckets NAV per symbol
(`_nav_by_symbol: dict[str, Decimal]`, line 60) — but its two public metrics
**collapse all buckets into one global series**:

```python
self._nav_by_symbol[event.symbol] = available + reserved + realized   # per-symbol ✓
nav = sum(self._nav_by_symbol.values(), Decimal("0"))                 # GLOBAL Σ ✗
self._samples.append((event.occurred_at_ms, nav))                     # one global deque
if self._peak is None or nav > self._peak: self._peak = nav           # one global peak
```

`realized_loss_pct_24h()` and `drawdown_pct()` (no symbol arg) read that single
global deque/peak. The two guards (`calibrated_guards.py` L51, L80) call them
argument-free and compare against scalar thresholds from `safety.canary.yaml`
(5.0% / 10.0%).

**The bug class this closes (per-currency masking):** with a 2nd active currency,
a profitable fUST inflates the summed NAV and hides a losing fUSD. Example: fUST
+8%, fUSD −15% → summed NAV may be down only ~2%, under both thresholds → the L2
breaker never fires while fUSD bleeds. "Netting a losing book against a winning
book for *limit* purposes" is the classic risk-management failure; limits must
live at the granularity where risk independently arises. Today this is *latent*
(fUST is the sole live symbol, so global ≡ fUST), which is exactly why it must be
fixed in the pre-launch window before fUSD opens.

## Approach — per-symbol metrics; shared scalar threshold; drop the global metric

Three risk-engineering levers, each resolved to the best-practice answer for
this instrument class (homogeneous stable funding currencies, no price exposure):

1. **Granularity = per-symbol (do it).** Each currency that can fail
   independently gets its own loss/drawdown metric, measured against its OWN 24h
   window-high and OWN all-time peak. Mirrors the established per-symbol guard
   pattern (`AllocationCapGuard` reads `decision.symbol`; `resolve_for_symbol`).

2. **Threshold = single shared scalar, applied per-symbol (NOT a per-symbol map).**
   The threshold is already a *percentage of that currency's own NAV*, so it
   auto-scales per currency: fUSD −5% means the same as fUST −5%. Per-symbol
   threshold *values* would exist to calibrate against differing volatility/VaR —
   but funding lending has no price exposure (lend USDT, get USDT + interest;
   principal returns to `available` on maturity unchanged), so NAV only drops on
   operational/catastrophic events (bug, platform socialised loss, withdrawal),
   which are identical in nature across stable funding currencies. The one
   volatile asset that could justify per-symbol thresholds (fADA) was dropped in
   Phase 2. In a real-money kill path, every extra knob is a misconfiguration
   path → keep the minimal correct config. ⇒ **No config schema change**;
   `safety.canary.yaml` stays 5.0 / 10.0.

3. **Portfolio/account-level layer = remove the global metric, do NOT add an
   account guard now.** A portfolio-level limit only adds control distinct from
   per-symbol if set *tighter* than the per-book limit (a deliberate "portfolio
   appetite < book appetite" policy). At the *same* %, an account-level guard is
   near-redundant given per-symbol guards, and an un-calibrated same-% global
   guard on a real-money kill path is false comfort (gives safety feeling without
   catching anything per-symbol misses, and risks false halts). A single-operator
   2-stablecoin canary has no data to calibrate a distinct portfolio appetite. ⇒
   delete the now-redundant global Σ series; re-add an account-level backstop
   later as a *deliberate, separately-calibrated* control if the book mix becomes
   heterogeneous or loss data warrants it.

**Safety invariant — fUST-only byte-identical.** With a single active symbol,
`realized_loss_pct_24h('fUST')` and `drawdown_pct('fUST')` MUST return exactly
what the current global metrics return (the live canary is fUST-only with both
guards ENABLED). This is the load-bearing regression gate, mirroring the
per-currency Phase 2 "byte-identical for fUST-only" discipline.

## Components

### A. `ReconcileNavTracker` → per-symbol peak + window

(`modules/execution/safety/nav_pnl_source.py`)

Replace the three scalar/global fields with per-symbol structures keyed by symbol:

- `_nav_by_symbol: dict[str, Decimal]` — keep (latest NAV per symbol).
- `_samples_by_symbol: dict[str, deque[tuple[int, Decimal]]]` — replaces the
  single global `_samples`; one 24h window per symbol, each trimmed against its
  OWN latest `occurred_at_ms` (still wall-clock-free, deterministic from the
  event stream).
- `_peak_by_symbol: dict[str, Decimal]` — replaces the single global `_peak`;
  one all-time high-water-mark per symbol.

`on_position_reconciled` updates ONLY `event.symbol`'s bucket/window/peak — no
cross-symbol sum. Keep the `account_id` early-return filter unchanged.

Metric methods gain a `symbol` parameter and read only that symbol's series:

- `realized_loss_pct_24h(symbol)` = `(window_high − latest) / window_high × 100`
  over `_samples_by_symbol[symbol]`; **0.0 if that symbol has no samples**
  (per-symbol cold-start permissive).
- `drawdown_pct(symbol)` = `(peak − latest) / peak × 100` over
  `_peak_by_symbol[symbol]` / that symbol's latest; **0.0 if no peak for symbol**.

Native-unit isolation: a per-symbol NAV is computed and compared only within one
currency — fUST and fUSD magnitudes are NEVER mixed into one peak/window.

Preserve per-symbol: cold-start permissive (0.0 before a symbol's first
reconcile), 24h-window-high (loss) vs all-time-peak (drawdown) divergence,
in-memory-only (no persistence — rebuilds ~90s after first reconcile).

### B. Protocol + guards thread `decision.symbol`

(`modules/execution/safety/calibrated_guards.py`)

- `_PnLSourceProtocol` (L22-24): both methods gain `symbol: str`.
- `RealizedLossGuard.evaluate` (L51): `self.source.realized_loss_pct_24h(decision.symbol)`.
- `DrawdownGuard.evaluate` (L80): `self.source.drawdown_pct(decision.symbol)`.
- `enabled=False` short-circuit stays BEFORE any source call (L48 / L77).
- Block-reason strings (L55, L84) gain the symbol for diagnosability and Loki
  alert disambiguation, e.g. `realized_loss_pct_24h[fUSD]=… > threshold_pct=…`.

`decision.symbol` is mandatory (`schemas.py` L114) and already threaded by
`SafetyGuardChain.evaluate(decision, ctx)` to every guard — no chain change.
`AccountContext` carries no symbol, so symbol MUST come from `decision`.

The single shared tracker instance feeding both guards (`daemon.py` L783, L903,
L909) is unchanged — per-symbol state lives inside that one instance.

### C. Test-double signature updates (lockstep, or mypy/Protocol breaks)

The two metric methods change signature, so every `_PnLSourceProtocol`
implementer updates together:

- `_FakePnLSource` (`tests/.../safety/test_calibrated_guards.py` L37-49) — add
  `symbol`; make it return DIFFERENT values per symbol so the routing is actually
  exercised (today a symbol-blind stub would pass even if guards picked the wrong
  bucket).
- `_StubPnL` (`tests/.../test_integration_path_a.py` L52-57, `integration`-marked
  — excluded from the default gate but breaks under mypy) — add `symbol`.

### Config — UNCHANGED

`safety/config.py` `_RealizedLossCfg` / `_DrawdownCfg` keep the scalar
`threshold_pct`; `safety.canary.yaml` (5.0 / 10.0) and `safety.yaml` (disabled)
are untouched. `assert_canary_guard_invariant` checks only the `.enabled` flags
(guard names `realized_loss_24h` / `drawdown_from_peak`) — unaffected.

## Data flow (unchanged shape, per-symbol read)

```
PositionReconciled(symbol, available, reserved, realized, occurred_at_ms)   ← one event PER SYMBOL
   └─ tracker.on_position_reconciled → update _samples_by_symbol[symbol], _peak_by_symbol[symbol]   (no Σ)

per submit:  DeploymentReconciler.deploy builds DecisionPayload(symbol=…)
   └─ SafetyGuardChain.evaluate(decision, ctx)
        ├─ RealizedLossGuard → source.realized_loss_pct_24h(decision.symbol) > 5.0 ?  block
        └─ DrawdownGuard     → source.drawdown_pct(decision.symbol)        > 10.0 ? block
```

A drawdown in symbol A blocks only symbol A's POSTs; symbol B is unaffected.

## Testing (TDD)

**Rewrite (encodes the OLD global-sum behaviour being inverted):**
`test_nav_pnl_source.py::test_two_symbols_global_nav_is_sum_of_latest_per_symbol`
(L183-202) → `test_two_symbols_isolated`: fUST 100→70 reports `realized_loss_pct_24h('fUST')`
= 30% and `drawdown_pct('fUST')` = 30%, while fUSD (unchanged 100) reports 0% —
NO cross-symbol summing. This is the clearest encoding of the behaviour change; it
must be replaced, not left green.

**New (the headline isolation guarantee, currently untested end-to-end):** a
guard-level test that a per-symbol drawdown in symbol A does NOT block a POST for
symbol B. Use a `_FakePnLSource` returning per-symbol values; assert
`RealizedLossGuard`/`DrawdownGuard` block `decision.symbol=A` and allow
`decision.symbol=B`.

**Must stay green, adapted to pass `'fUST'` (byte-identical fUST path):**
- `test_single_symbol_global_api_identical_to_pre_per_symbol` (L167-180) — the
  byte-identical lock; same 10%/10% for one symbol.
- cold-start zero, single-symbol drop/rise, 24h-window-evicts-old-high-but-peak-is-all-time,
  recovery, new-peak-reset, ignore-other-account (all single-symbol).
- `test_daemon_pnl_wiring.py`: fUST 100→40 via the bus still trips BOTH guards;
  `dd_guard.source is loss_guard.source` (single shared instance) still holds.
- `test_canary_invariant.py`, `test_daemon_phase42.py` — config/enabled only,
  unaffected.

**Docs updated in the same change:**
- `ARCHITECTURE.md` L299 — drop the already-stale "接 stub source 回 0" claim and
  state the metric is per-symbol NAV (not summed-across-symbols).
- `nav_pnl_source.py` module docstring — rewrite the "global NAV sample is the
  SUM of latest per-symbol NAV" paragraphs to per-symbol semantics.
- `safety.canary.yaml` / `safety.yaml` header comments that mention the global sum.

Gate: `cd backend_py && uv run pytest -m "not integration"` + `uv run mypy src/`
+ `uv run ruff check` all green before commit.

## Invariants to preserve

1. **fUST-only byte-identical**: single symbol ⇒ per-symbol metric ≡ current
   global metric, to the same float; same 5.0/10.0 thresholds; canary still blocks.
2. **Single shared `ReconcileNavTracker`** feeds both guards (one instance,
   internally bucketed) — not two trackers.
3. **Per-symbol cold-start permissive**: a never-reconciled symbol returns 0.0
   (never blocks a freshly-funded 2nd currency before its first snapshot); the
   cold-start check is per-bucket, not a shared global `if not self._samples`.
4. **account_id filter** preserved (ignore other-account events).
5. **Determinism from the event stream only** — each per-symbol 24h window trims
   against that symbol's own latest `occurred_at_ms`; no wall-clock.
6. **Native units never cross-summed** across currencies.
7. **Canary invariant** (config `.enabled` flags) unchanged.

## Out of scope (deferred / explicitly not this change)

- **Per-symbol threshold map (Option B)**: clean additive follow-up via the
  caps/buffers `resolve_for_symbol` precedent IF a heterogeneous-risk currency is
  ever added. Not now (homogeneous stable currencies).
- **Account/portfolio-level backstop guard**: a *deliberately tighter*
  portfolio-appetite limit, added later with its own calibration. Not an
  un-calibrated same-% global guard now.
- **Peak high-water-mark persistence across restart**: a real best-practice gap
  (a restart currently resets the peak and silently widens the effective drawdown
  limit), but it is a separate concern (touches DB/migration) — tracked as its own
  item, NOT folded into this in-memory refactor.
- **DivergenceRateGuard / `_DivergenceSourceProtocol`**: still a constant-0 stub
  with no real source; symbol-blind by virtue of being constant, so no
  cross-symbol sum exists to fix. Leave a `TODO` noting it will need the same
  per-symbol treatment when a real source lands.

## Risks / verify adversarially (post-implementation)

- **Silent no-op split**: if a metric goes per-symbol but a guard still calls it
  without `decision.symbol` (or the stub ignores the arg), the split does nothing
  while looking done. The new per-symbol guard isolation test + a per-symbol-valued
  `_FakePnLSource` are the guard against this — verify they FAIL before the guard
  edit and PASS after.
- **Naive bucketing**: bucketing only `_nav_by_symbol` while leaving `_peak` /
  `_samples` global would still cross-contaminate drawdown. Confirm BOTH `_peak`
  and `_samples` are per-symbol.
- **fUST byte-identical drift**: if the fUST per-symbol peak/window is seeded
  differently from the old global one, the live canary's NAV history could read a
  different drawdown and spuriously block/allow real-money offers. Prove
  byte-identical via the single-symbol identity test + the wiring test.
- **fUSD-dark transition**: fUSD is reconciled while dark (cap=0), so the tracker
  receives fUSD events and builds a fUSD peak/window — harmless while no fUSD POST
  consults it. At fUSD go-live, a deposit-then-partial-use pattern reads as a fUSD
  drawdown that (correctly) could halt fUSD; confirm cold-start (first reconcile
  establishes the peak) so the very first fUSD offer isn't falsely blocked.
- **Block-reason string change**: per-symbol reason strings change the format Loki
  alerts may parse; confirm the alert regex tolerates the `[symbol]` addition.
