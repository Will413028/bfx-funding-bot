# Per-Currency Allocation — Phase 2 (Per-Symbol Values, Routing & Config Maps) — Design

**Date:** 2026-06-01
**Status:** Approved design → ready for implementation plan
**Parent epic:** `2026-05-31-per-currency-native-allocation-design.md` (approved epic design;
native-unit per-currency, USD overlay deferred, `cap=0` disables a currency). This Phase 2
spec implements the epic's §4.7–4.10 **and closes two money-path blind spots the epic's §8
P1–P4 decomposition omitted** (executor routing + reconciler/tracker partition) plus a
divergent-default landmine. Phase 1 (`merge 6b94b71`, migration `b7c1d2e3f4a5`, merged main,
UNDEPLOYED except the fUST-only canary) already shipped the epic's §4.1–4.6.

**Review note (2026-06-01):** this is v2, incorporating an adversarial spec review (4
reviewers: claim-verifier / self-review / completeness / canary-safety). Key corrections vs
v1: (a) the cap/buffer authority is **two** sites, not one — the `DeploymentReconciler` sizing
path reads the global cap in 3 places, the guards in others; both must move to the per-symbol
map; (b) the live canary loads `configs/safety.canary.yaml` (not `safety.yaml`), and on that
file `realized_loss_24h`+`drawdown_from_peak` are **enabled** — so the NAV-split deferral rests
on "single active currency", not "guards disabled"; (c) env↔map precedence pinned; (d) symbol
defaults live on 5+ sites incl. the ledger READ side; (e) cutover needs a real write-gate.

**Architecture verdict (design panel, 3 architects + adversarial critic, 2026-06-01):**
keep the **single-component, key-partitioned** shape (one executor / one reconciler / one
tracker, every per-symbol VALUE made correct on Phase 1's existing `dict[symbol]` KEY shape).
Do **not** carve per-currency execution units (Paradigm B). See §2.

---

## 1. Phase boundary — what Phase 1 shipped vs what Phase 2 finishes

Phase 1 turned every **stateful** surface per-symbol via `dict[symbol]` keys: ledger buckets,
`position_state` PK `(account_id, deployment_environment, symbol)`, events carry `symbol`,
`BootRecovery` loops configured symbols emitting one `PositionReconciled` each, guards already
read `decision.symbol`, NAV already keys `_nav_by_symbol`. **What remains wrong is not
structure — it is a handful of VALUES that still collapse across symbols, one constructor-bound
route, and the per-symbol CONFIG maps.** Concretely (line numbers ✅ = verified this session):

| Epic §4 | Gap Phase 2 closes | Verified site |
|---|---|---|
| §4.7 | `AllocationCapGuard` compares per-symbol exposure against ONE global `ctx.allocation_cap_usdt`; **the reconciler SIZING reads the same global cap in 3 places** | `hard_guards.py` (guard reads `decision.symbol`); `reconciler.py:112` (`cap_per_cell`), `:133` (`target`), `:144` (`gap`) ✅ |
| §4.8 | `BuyingPowerGuard` uses ONE global buffer; reconciler headroom uses one `self._balance_buffer` | `hard_guards.py`; `reconciler.py:83/:106` ✅ |
| §4.10 | safety config has NO caps/buffers maps and NO `buying_power` block at all | `safety/config.py:32-34` (`_AllocationCapCfg` = `extra='forbid'` + `enabled` only) ✅; `HardGuardsCfg:37-42` has no `buying_power` key ✅; buffer is env-only |
| **blind spot 1** | `live_executor.submit()` builds the venue POST with `self._symbol` (bound from `cells[0].symbol`), **ignores `decision.symbol`** → 2nd-currency offer mis-routes real money | `live_executor.py:209` (`symbol=self._symbol`), `:195`, `:184`; `daemon.py:803` (`build_executor(symbol=first_cell.symbol)`), `:777` ✅ |
| **blind spot 2** | `tracker.reconcile_to_total` does `sum(self._deployed.values())` across ALL cells; reconciler derives the gap pool from `self._cell_symbol[self._cells[0].cell_id]` | `tracker.py:56` ✅; `reconciler.py:101` ✅ |
| **landmine** | `symbol` defaults to `'fUSD'` on `DecisionPayload` + the domain events, while store `DEFAULT_RECONCILE_SYMBOL='fUST'`; the ledger READ API also silently sums when `symbol=None` | `schemas.py:114`, `events.py:136/165/190/240`, `store.py:45`, `ledger.py:158/164/169` ✅ |

**Why the blind spots are currently latent:** the canary is fUST-only, so `cells[0].symbol ==
fUST` and every per-symbol value collapses to the one active symbol — behaviourally correct.
The bugs only bite when a *second* currency is active with `cap > 0`. Phase 2's `cap=0`
mechanism means fUSD/fADA ship dark and these paths never execute for them — but the code must
be correct-or-loud before any currency's cap is opened, so Phase 2 fixes them now, in the
pre-launch window, as a one-shot.

## 2. Architecture decision — key-partitioned, not structural carve

**Verdict: Paradigm A (single-component, key-partitioned). Reject Paradigm B (per-currency
LendingUnit + Supervisor).** Rationale (panel critic, concurred):

1. **Phase 1 already paid for the data-attribute shape.** Every stateful surface is already
   `dict[symbol]`; guards already thread `decision.symbol`; NAV already keys `_nav_by_symbol`;
   `boot_recovery` already loops symbols. The only wrongness is a few values + one route.
   Paradigm B adds ~120 LOC of `LendingUnit`/`Supervisor` scaffolding **on top of** that same
   value-fix work (B's own estimate).
2. **Shared-resource reality forbids B's payoff.** One Bitfinex account, one WS connection, one
   writer-advisory-lock, one REST/nonce sequence — all singletons. B keeps every singleton and
   steps units sequentially, so it buys **zero** isolation while adding a supervisor whose
   "sequential stepping" invariant is itself a new silent-race landmine.
3. **N=1 funded currency, fUSD/fADA locked at `cap=0`.** A per-currency object graph for
   currencies that ship dark and may never fund is textbook YAGNI — the exact gold-plating to
   avoid pre-launch.
4. **B's headline benefit is over-sold.** "Mis-route structurally impossible" is achieved at
   the single choke point with `symbol=decision.symbol` + a fail-fast invariant + a regression
   test, and the venue itself already segregates by symbol.

**Borrow exactly one thing from the structural paradigm:** a **fail-fast invariant at the
executor boundary** — reject any submit whose `decision.symbol` is empty or not in the
configured-symbols set. That is the cheap part of "structural" (route correct-or-loud) without
the unit scaffolding.

**Documented forcing function for a future carve:** flip to Paradigm B only if a concrete need
appears — independent per-currency submit cadence, per-currency fault isolation, or separate
per-currency rate-limit budgets. Pre-launch, none exist.

## 3. Scope decisions (locked by operator, 2026-06-01)

- **NAV/loss/drawdown per-symbol split → DEFER.** Correct deferral rationale (v2): the live
  canary loads `configs/safety.canary.yaml`, where `realized_loss_24h` + `drawdown_from_peak`
  are **enabled** (real-money guards; `divergence_rate` disabled). They are nonetheless inert
  today because **only fUST is an active currency**, so the `ReconcileNavTracker` global NAV
  sum has exactly one component → `global sum ≡ per-symbol fUST NAV`; the enabled guards behave
  correctly under one currency. **HARD GATE:** the per-symbol split MUST land before a *second*
  currency becomes active (cap>0 **and** a cell actually trading), because the enabled
  loss/drawdown guards would otherwise evaluate a summed-across-currency NAV where profitable
  fUST masks a losing fADA (most dangerous for the volatile asset). `_nav_by_symbol` is already
  keyed, so the split stays purely additive when it lands.
- **Native-unit rename (`size_usdt`→`amount`, `position_state` cols, config keys) → DO NOW**
  (epic §4.1). Pre-launch is the cheapest window (no prod data to migrate); `*_usdt` naming is
  a lie for fADA. Must be atomic with the store-layer reads that consume `size_usdt` (§4 D1/D3).
- **fADA depth → config entry only, NO cell.** Phase 2 adds `caps:{fADA:0}` so fADA exists in
  config and is blocked by `cap=0`; it does NOT add an fADA cell (needs WFO qualification +
  native min-offer floor + ADA decimal precision — a separate epic). A symbol with a caps entry
  but no cell must NOT enter `configured_symbols` (which drives BootRecovery / reconcile / NAV)
  — `configured_symbols` is derived from cells, not from the caps map (§4 A2/C2).
- **fUST cap into the map (value `3000`), not env-only.** `caps:{fUST:3000,fUSD:0,fADA:0}`.
- **env↔map precedence (PINNED):** `caps[symbol]` WINS whenever present. The env
  `BFX_ALLOCATION_CAP_USDT` / `BFX_BALANCE_BUFFER_USDT` is consulted ONLY for a symbol with no
  `caps[symbol]` entry AND no `default_cap`. At cutover BOTH the map and the env will be present
  (deploy-vm.sh:23 requires `BFX_ALLOCATION_CAP_USDT`); the map must win deterministically. Boot
  logs the **effective cap per symbol** so the operator can confirm 3000 took.
- **Caps strictness (PINNED):** boot asserts every *configured cell's* symbol has an explicit
  `caps[symbol]` entry; under `BFX_PHASE=canary` it further asserts `caps[symbol] > 0` for each
  configured-cell symbol (a typo'd/forgotten `caps:{fUST:3000}` falling through to
  `default_cap=0` would silently halt fUST lending). Fail-fast config-fatal `ValueError` (same
  shape as `assert_canary_guard_invariant`, daemon.py:637). `default_cap`/env serve only the
  single-currency back-compat path.

## 4. Components & changes (ordered; money-route first)

Grouped A–D. Each step: site, change, test. ✅ = line verified this session; others
panel-verified, confirm at implementation (TDD).

### A. Money-route fix (epic §8 blind spot — highest priority)

**A1. `external/bitfinex/live_executor.py` — route by `decision.symbol`.**
Delete the `self._symbol` field and `symbol` `__init__` param. In `submit()`, call
`build_offer_payload(symbol=decision.symbol, ...)` (was `symbol=self._symbol`, `:209` ✅).
Update the two warning log lines (`:238`, `:246`) to use `decision.symbol`/`payload['symbol']`.
**Fail-fast:** if `decision.symbol` is falsy or not in the configured-symbols set → raise
`InvariantViolation`, submit nothing. The configured-symbols set is the SAME
`configured_symbols(config.cells)` already used at daemon.py:911/932 (proven non-empty for
canary = `{fUST}`), constructed at **build time** and injected, never re-derived at runtime
(so a legit fUST submit can never be rejected by an empty set). *Test:* POST body
`symbol == decision.symbol` and `!= cells[0].symbol`; empty/unknown symbol raises and submits
nothing; the canary `{fUST}` set admits a real fUST decision.

**A2. `daemon.py` + `registry.build_executor` — drop the binding (all call sites).**
Remove `symbol=first_cell.symbol` from `build_executor` and the `symbol` param from
`build_executor`/`BitfinexLiveExecutor`. Pass the configured-symbol **set** for A1's validation.
**`build_executor` is called at daemon.py:803 ✅ and also ~881/944/1031 (paper/shadow/smoke
paths) — update ALL of them.** Executor stays a singleton, currency-agnostic; the `cell` label
may remain for the emit envelope.

### B. Per-symbol caps/buffers config (epic §4.7–4.10)

**B1. `safety/config.py` + `configs/safety.yaml` + `configs/safety.canary.yaml` — config maps.**
Extend `_AllocationCapCfg` with `caps: dict[str, Decimal]` + `default_cap: Decimal`; **add a new
`_BuyingPowerCfg` (`buffers: dict[str,Decimal]` + `default_buffer`) AND add `buying_power:
_BuyingPowerCfg` to `HardGuardsCfg` (config.py:42) — it does not exist today** (relax
`extra='forbid'` only for the new fields). Edit **BOTH** YAML files (the dev/base `safety.yaml`
AND the live-canary `safety.canary.yaml` selected via `BFX_SAFETY_CONFIG=...safety.canary.yaml`,
canary.env:9):
```yaml
hard_guards:
  allocation_cap:
    enabled: true
    caps: {fUSD: 0, fUST: 3000, fADA: 0}   # native units; unlisted → default_cap
    default_cap: 0
  buying_power:                              # NEW block (did not exist)
    enabled: true
    buffers: {fUSD: 3, fUST: 3, fADA: 0}    # native units; unlisted → default_buffer
    default_buffer: 0
```
Boot assert per §3 (explicit entry for every configured-cell symbol; `>0` under canary). Retain
`BFX_ALLOCATION_CAP_USDT`/`BFX_BALANCE_BUFFER_USDT` as the single-currency env fallback per the
§3 precedence rule. *Test:* map parsing; `default_cap`/`default_buffer` fallback; precedence
(map present → env ignored); boot assert fires on missing entry and on `caps[fUST]==0` under
canary; env back-compat path for a single configured symbol.

**B2. `safety/hard_guards.py` — guards read per-symbol values.**
`AllocationCapGuard.evaluate` reads `caps[decision.symbol]` (→ `default_cap` → env fallback)
instead of `ctx.allocation_cap_usdt`; `BuyingPowerGuard.evaluate` reads `buffers[decision.symbol]`.
Wire maps from YAML into the guards at build time (`daemon.py`). Guard logic otherwise untouched
(already reads `decision.symbol`). Error message names the currency. *Test:* fUSD POST at cap
blocked while fUST POST with room allowed; `cap=0` blocks all of a currency; error names it;
live-only `BuyingPowerGuard` exemption preserved.

### C. Per-symbol gap/sizing — ALL cap/buffer consumers, not just guards

**C1. `deployment/reconciler.py` — per-symbol sizing loop (THE blocker v1 missed).**
`deploy()` reads the global cap/buffer at four sites: `cap_per_cell = concentration_pct *
ctx.allocation_cap_usdt` (`:112` ✅), `allocate_gap(target=ctx.allocation_cap_usdt)` (`:133` ✅),
`gap = ctx.allocation_cap_usdt - e_total` (`:144` ✅), and `headroom = available_balance(symbol)
- self._balance_buffer` (`:106` ✅). Restructure `deploy()` to loop `configured_symbols(self._cells)`
(reuse the helper at daemon.py:154) and, per symbol, route `target`/`gap`/`cap_per_cell` through
`caps[symbol]` and `headroom` through `buffers[symbol]` — the **same source as the guard** (so
the sizing authority and the gate agree). Decide `AccountContext.allocation_cap_usdt`'s fate in
planning: keep as the single-currency env-fallback value the map falls back to, or remove and
inject the caps map into the reconciler. The submit loop (already builds per-cell
`symbol=self._cell_symbol[cell_id]`) runs inside the per-symbol iteration. *Test:* two symbols
get independent gap pools sized by their own cap; single-active-symbol path equals current
behaviour; a `cap=0` symbol sizes to zero (no offer).

**C2. `deployment/tracker.py` — partition the rescale pool by symbol.**
`reconcile_to_total` currently `sum(self._deployed.values())` across all cells (`:56` ✅).
Either key `_deployed` by `(symbol, cell_id)` and rescale each symbol's sub-pool to that
symbol's `reserved_total`, or operate only on the passed symbol's cell subset (called once per
symbol from C1). *Test:* a 2-symbol × 2-cell scenario asserts each symbol's rescale is
independent (no cross-currency contamination).

### D. Correctness prerequisites

**D1+D3 (atomic). Native rename `size_usdt`→`amount` across events AND the store reads that
consume it.** These MUST land in one slice or the store breaks: `serialize_event` persists the
event payload, and `rebuild_snapshot_from_log`'s tail-fold reads `payload['size_usdt']`
(`store.py:404` ✅), while `_project_position_state` / offer_claims upsert read `_ev.size_usdt`
(`store.py:100/150/206` ✅). In the same change: rename the event field (`events.py` +
`_resolve_amount` transitional shim at events.py:26-47), update the three store reads to
`amount`, update `serialize_event` (serialization.py:45 already emits `symbol`), and **add the
symbol filter to the fold** — `if (r.payload or {}).get('symbol') != symbol: continue` before
`store.py:404` (the in-code comment at `:400-403` prescribes exactly this). Also rename
`position_state` cols (in migration `b7c1d2e3f4a5` already) and `DecisionPayload.offer_amount_usdt`.
*Test:* a 2-symbol event_log rebuilds into two correct rows; no `size_usdt` read remains on the
hot path; serialize/deserialize round-trips `amount`+`symbol`.

**D2. Make `symbol` mandatory — write side AND read side.** Remove the `symbol='fUSD'` default
from `DecisionPayload` (`schemas.py:114`), the three domain events + `PositionReconciled`
(`events.py:136/165/190/240`), and the store kwarg/`DEFAULT_RECONCILE_SYMBOL='fUST'` defaults
(`store.py:45` + kwarg defaults). **Also remove the ledger READ-side global-sum backdoor:**
`current_exposure`/`reserved_exposure`/`available_balance` default `symbol=None` and silently
`sum()` across all symbols (`ledger.py:158/164/169` ✅) — drop the `None` branch (make `symbol`
required), OR move the global sum to an explicitly-named non-hot-path helper
(`total_exposure_all_symbols()`) that NO guard/reconciler calls (epic §4.3: "no method sums
across symbols in the hot path"). Otherwise A1's fail-fast and the mandatory write-side symbol
leave a silent cross-currency backdoor on the read path. *Test:* writing/reading without a
symbol raises; no guard/reconciler path reaches a cross-symbol sum.

**D4. Apply migration `b7c1d2e3f4a5` at cutover (already authored in Phase 1 — apply only).**
PK → `(account_id, deployment_environment, symbol)`, native column rename, no backfill. *Test:*
`alembic upgrade head` creates the per-symbol PK; `alembic check` no drift (after Neon cred
refresh). **Deferred-with-condition:** `rebuild_snapshot_from_log`'s checkpoint base also
selects the latest `reconcile_observation` by `id DESC LIMIT 1` with NO symbol filter
(`store.py:358-363`), and `reconcile_observation` has no symbol column. This is safe ONLY while
single-active-symbol; §8 records that rebuild is NOT multi-symbol-safe until
`reconcile_observation` gains a symbol column. Do not enable a 2nd trading currency before that.

## 5. Data flow (multi-symbol, after Phase 2)

```
WS funding frame (symbol) ─▶ executor.submit(decision.symbol) ─▶ venue POST {symbol=decision.symbol}
                                              │                       (fail-fast if symbol ∉ configured set)
reconciler.deploy: for symbol in configured_symbols(cells):
    cap=caps[symbol]; buf=buffers[symbol]
    gap = cap - exposure(symbol);  headroom = available(symbol) - buf
    allocate_gap(symbol's cells, target=cap, cap_per_cell=concentration*cap)
    tracker.reconcile_to_total(symbol, reserved_total[symbol])
guards: AllocationCapGuard caps[symbol] vs exposure(symbol);  BuyingPowerGuard available(symbol)-buffers[symbol] vs offer
ledger: _reserved[symbol]/_realized[symbol]/_available[symbol]  (no symbol=None sum on hot path)
store.rebuild: fold filtered by payload['symbol']==symbol  ─▶ one position_state row per symbol
```

## 6. Error handling / edge cases

- Offer for an unconfigured symbol → `default_cap=0` blocks; executor fail-fast also rejects.
- `decision.symbol` missing → executor raises `InvariantViolation`; D2 makes this unreachable
  on the write path (defense-in-depth at the executor).
- `_available[symbol]` defaults to 0 until the first per-symbol reconcile → `BuyingPowerGuard`
  blocks that symbol until balance is known (fail-safe).
- env↔map precedence: `caps[symbol]` present → env ignored for that symbol (§3); boot logs the
  effective cap per symbol.
- `cap=0` currency: **do both** — the reconciler skips `cap=0` symbols before sizing (avoids
  per-tick guard-block log noise) AND the guard blocks any POST that slips through (defense).
  *Test:* a `cap=0` symbol produces zero offers and no guard-block spam.
- Back-compat: a deploy with no `caps`/`buffers` maps keeps working via the single-currency env
  fallback (sole configured currency).

## 7. Cutover (live VM canary)

Migration `b7c1d2e3f4a5` + per-symbol caps land **atomically**, write-gated:
1. Refresh stale Neon credential (`.env`, Neon MCP `get_connection_string`) — known landmine.
2. **Write-gate:** deploy with `BFX_KILL_SWITCH=true` (or `caps:{fUST:0}`) so the reconciler
   places no offers on first boot.
3. `cd backend_py && uv run alembic upgrade head` (transactional; env.py advisory-lock + atomic
   pattern from `ca52eb2`).
4. Verify on the gated process: `position_state` rebuilt the live fUST credit row (~273 fUST);
   the boot "effective cap per symbol" log shows `fUST=3000` (map won over env); the routing
   regression behaviour appears in logs. Update `safety.canary.yaml` with the caps/buffers maps.
5. Release writes: flip `BFX_KILL_SWITCH=false` (or `caps:{fUST:3000}`) and recreate. fUSD/fADA
   stay `cap=0` (dark) — no offers, no funding moved. Loss limiter is %-of-NAV (commit
   `0b90763`), independent of the cap map — confirm it is unperturbed.

Rollback: `BFX_KILL_SWITCH` + recreate; or `deploy-vm.sh shadow`; DB via Neon PITR. (Koyeb
stays paused — never two prod writers.)

## 8. Out of scope (deferred)

- **Per-symbol NAV/loss/drawdown split** (epic §4.9) — inert while single-active-currency
  (canary loss/drawdown guards are ENABLED but see one symbol); **HARD GATE: must land before a
  2nd currency trades** (§3). `_nav_by_symbol` already keyed → additive.
- **`reconcile_observation` symbol column** — `rebuild_snapshot_from_log` checkpoint base is
  symbol-blind (`store.py:358-363`); rebuild is NOT multi-symbol-safe until this lands. Coupled
  to the NAV gate: do not enable a 2nd trading currency before both.
- **`offer_claims` symbol column** — the FSM diff is a global union on globally-unique
  `venue_offer_id`; a missing-claim release stamps `symbols[0]` (`boot_recovery.py:303-311`),
  mis-attributing a 2nd-currency release to the primary bucket. Safe specifically because the
  venue segregates offer ids AND there is no 2nd trading cell. Safety condition is **"no 2nd
  cell"**, not "no 2nd caps entry".
- **Per-currency structural carve (Paradigm B)** — forcing function recorded in §2.
- **fADA cell** (strategy params, ADA precision, native min-offer floor) — separate epic;
  Phase 2 adds only the `caps:{fADA:0}` entry (no cell, not in `configured_symbols`).
- **Unified USD-equivalent exposure overlay / cross-currency ceiling** — epic §7; needs a price
  feed; deferred. **WS `wu` wallet-frame parsing** — balance stays REST-polled.

## 9. Testing (TDD)

- **Executor routing (A1):** POST body `symbol == decision.symbol` and `!= cells[0].symbol`;
  empty/unknown raises and submits nothing; canary `{fUST}` set admits a real fUST decision.
  This is the regression lock for the §8 blind spot.
- **Config (B1):** map parsing; `default_cap`/`default_buffer` fallback; env↔map precedence;
  boot assert on missing entry AND on `caps[fUST]==0` under canary; new `buying_power` block.
- **Guards (B2):** per-symbol independence; `cap=0` blocks a currency; error names it; live-only
  buying-power exemption preserved.
- **Reconciler sizing (C1):** two symbols → independent gap pools sized by own cap; `cap=0` →
  zero offers; single-active equals current behaviour; guard and sizing use the same cap source.
- **Tracker (C2):** 2-symbol × 2-cell rescale independence.
- **Store rename+fold (D1/D3):** 2-symbol event_log → two correct rows; no `size_usdt` on the
  hot path; serialize/deserialize round-trips `amount`+`symbol`.
- **Mandatory symbol (D2):** write/read without symbol raises; no hot-path cross-symbol sum.
- **Migration (D4):** per-symbol PK created; `alembic check` no drift (after Neon cred refresh).
- Gate: `cd backend_py && uv run pytest -m "not integration"` + `mypy src/` + `ruff check`.

## 10. Implementation decomposition (for writing-plans)

Four bite-sized TDD slices, money-route first so the highest-severity fix lands and is
regression-locked before the rest:
1. **Route fix** — A1, A2, routing regression test.
2. **Config maps + all consumers** — B1, B2, C1 (reconciler sizing), C2 (tracker). Caps/buffers
   must reach the guards AND the reconciler sizing in the same slice (they are one authority).
3. **Correctness prerequisites** — D1/D3 (atomic rename + store reads + fold filter), D2
   (mandatory symbol incl. ledger read side).
4. **Cutover** — D4 (apply migration), §7 write-gated runbook.
