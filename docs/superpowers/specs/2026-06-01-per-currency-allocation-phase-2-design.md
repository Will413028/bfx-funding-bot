# Per-Currency Allocation — Phase 2 (Per-Symbol Values, Routing & Config Maps) — Design

**Date:** 2026-06-01
**Status:** Approved design → ready for implementation plan
**Parent epic:** `2026-05-31-per-currency-native-allocation-design.md` (approved epic design;
native-unit per-currency, USD overlay deferred, `cap=0` disables a currency). This Phase 2
spec implements the epic's §4.7–4.10 **and closes two money-path blind spots the epic's §8
P1–P4 decomposition omitted** (executor routing + tracker/reconciler partition) plus a
divergent-default landmine. Phase 1 (`merge 6b94b71`, migration `b7c1d2e3f4a5`, merged main,
UNDEPLOYED except the fUST-only canary) already shipped the epic's §4.1–4.6.

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
route, and the per-symbol CONFIG maps.** Concretely:

| Epic §4 | Gap Phase 2 closes | Verified site |
|---|---|---|
| §4.7 | `AllocationCapGuard` compares per-symbol exposure against ONE global `ctx.allocation_cap_usdt` | `hard_guards.py` (guard reads `decision.symbol`; only VALUE source is global) |
| §4.8 | `BuyingPowerGuard` uses ONE global buffer; reconciler headroom on `cells[0].symbol` | `hard_guards.py`, `reconciler.py` |
| §4.10 | safety config schema has no caps/buffers maps | `safety/config.py:32-34` (`_AllocationCapCfg` = `extra='forbid'` + `enabled` only) ✅ verified |
| **blind spot 1** | `live_executor.submit()` builds the venue POST with `self._symbol` (bound from `cells[0].symbol`), **ignores `decision.symbol`** → 2nd-currency offer mis-routes real money | `live_executor.py:209` (`symbol=self._symbol`), `:195`, `:184`; `daemon.py:803` (`build_executor(symbol=first_cell.symbol)`), `:777` ✅ verified |
| **blind spot 2** | `tracker.reconcile_to_total` does `sum(self._deployed.values())` across ALL cells → one cross-currency rescale pool; reconciler uses `cells[0].symbol` for one global gap pool | `tracker.py:56` ✅ verified; `reconciler.py:101` (panel-verified, confirm at impl) |
| **landmine** | events/`DecisionPayload` default `symbol='fUSD'` while store `DEFAULT_RECONCILE_SYMBOL='fUST'` → a missing symbol silently mis-attributes exposure | `schemas.py:114`, `store.py:45`, store tail-fold unfiltered `store.py:404` (panel-verified, confirm at impl) |

**Why the blind spots matter and why they are currently latent:** today the canary is
fUST-only, so `cells[0].symbol == fUST` and every per-symbol value collapses to the one active
symbol — behaviourally correct. The bugs only bite when a *second* currency is active with
`cap > 0`. Phase 2's `cap=0` mechanism (§3) means fUSD/fADA ship dark and these paths never
execute for them — but the code must be correct-or-loud before any currency's cap is opened,
so Phase 2 fixes them now, in the pre-launch window, as a one-shot.

## 2. Architecture decision — key-partitioned, not structural carve

**Verdict: Paradigm A (single-component, key-partitioned). Reject Paradigm B (per-currency
LendingUnit + Supervisor).** Rationale (panel critic, concurred):

1. **Phase 1 already paid for the data-attribute shape.** Every stateful surface is already
   `dict[symbol]`; guards already thread `decision.symbol`; NAV already keys `_nav_by_symbol`;
   `boot_recovery` already loops symbols. The only wrongness is a few values + one route.
   Fixing those completes the partition. Paradigm B adds ~120 LOC of `LendingUnit`/`Supervisor`
   scaffolding **on top of** that same value-fix work (B's own estimate).
2. **Shared-resource reality forbids B's payoff.** One Bitfinex account, one WS connection, one
   writer-advisory-lock, one REST/nonce sequence — all singletons. B keeps every singleton and
   steps units sequentially, so it buys **zero** isolation (no parallel cadence, no
   per-currency fault isolation, no separate rate budget) while adding a supervisor whose
   "sequential stepping" invariant is itself a new silent-race landmine (a future
   `asyncio.gather` would reintroduce concurrent submits on the shared client + lock).
3. **N=1 funded currency, fUSD/fADA locked at `cap=0`.** A per-currency object graph for
   currencies that ship dark and may never fund is textbook YAGNI — the exact gold-plating to
   avoid pre-launch.
4. **B's headline benefit is over-sold.** "Mis-route structurally impossible by construction"
   is achieved by Paradigm A at the single choke point with `symbol=decision.symbol` + a
   fail-fast invariant + a regression test, and the venue itself already segregates by symbol.

**Borrow exactly one thing from the structural paradigm:** a **fail-fast invariant at the
executor boundary** — reject any submit whose `decision.symbol` is empty or not in the
configured-symbols set. That is the cheap part of "structural" (route is correct-or-loud, never
silently wrong) without the unit scaffolding.

**Documented forcing function for a future carve (so the deferral is conscious):** flip to
Paradigm B only if a concrete need appears — independent per-currency submit cadence,
per-currency fault isolation, or separate per-currency rate-limit budgets. Pre-launch, none
exist.

## 3. Scope decisions (locked by operator, 2026-06-01)

- **NAV/loss/drawdown per-symbol split → DEFER.** The three NAV-based guards
  (`realized_loss_24h`, `drawdown_from_peak`, `divergence_rate`) are `enabled=false` on the
  canary (✅ verified `safety.yaml:21/24/27`) AND only fUST trades, so the global NAV sum ==
  per-symbol today (verified `nav_pnl_source.py` global sum). The split is inert now; activate
  before fADA (volatile) funds real money, because a cross-currency sum would let profitable
  fUST mask an fADA bleed. `_nav_by_symbol` is already keyed, so this stays purely additive.
- **Native-unit rename (`size_usdt`→`amount`, `position_state` cols, config keys) → DO NOW**
  (epic §4.1). Pre-launch is the cheapest window (no prod data to migrate); `*_usdt` naming is
  a lie for fADA. Land it in the same cutover as the migration.
- **fADA depth → config entry only, NO cell.** Phase 2 adds `caps:{fADA:0}` so fADA exists in
  config and is blocked by `cap=0`; it does NOT add an fADA cell. fADA cells need WFO
  qualification + native min-offer floor + ADA decimal precision — a separate epic.
- **fUST cap into the map (value `3000`), not env-only.** The map is the single source of
  truth; `BFX_ALLOCATION_CAP_USDT` is retained only as a back-compat single-currency fallback.
  `3000` matches the current live canary ceiling (env `BFX_ALLOCATION_CAP_USDT=3000`); value
  unchanged at cutover, behaviour equivalent. (The %-of-NAV loss limiter from `0b90763` is
  independent of the cap map.)
- **Caps precedence = strict.** Boot asserts every *configured cell's* symbol has an explicit
  `caps[symbol]` entry; `default_cap`/`BFX_ALLOCATION_CAP_USDT` serve only the single-currency
  back-compat path. An unfunded currency can never be silently enabled by a stray
  `default_cap > 0`.

## 4. Components & changes (ordered; do in this sequence — money-route first)

Grouped A–D. Each step lists the site, the change, and the test that locks it. Line numbers
marked ✅ are verified in this session; others are panel-verified and confirmed at
implementation (TDD).

### A. Money-route fix (epic §8 blind spot — highest priority)

**A1. `external/bitfinex/live_executor.py` — route by `decision.symbol`.**
Delete the `self._symbol` field and the `symbol` `__init__` param. In `submit()`, call
`build_offer_payload(symbol=decision.symbol, ...)` (was `symbol=self._symbol`, `:209` ✅).
Update the two warning log lines (`:238`, `:246`) to use `decision.symbol`/`payload['symbol']`.
**Fail-fast:** if `decision.symbol` is falsy or not in the configured-symbols set → raise
`InvariantViolation` (do not submit). *Test:* submit an fUST decision through the now
currency-agnostic executor; assert the POST body `symbol == decision.symbol` and
`!= cells[0].symbol`; assert an empty/unknown symbol raises and submits nothing.

**A2. `modules/marketfeed/daemon.py` + `modules/execution/registry.build_executor` — drop the
binding.** Remove `symbol=first_cell.symbol` from the `build_executor` call (`daemon.py:803` ✅)
and the `symbol` param from `build_executor`/`BitfinexLiveExecutor`. Pass the configured-symbol
**set** for A1's fail-fast validation only. Executor stays a singleton, currency-agnostic. The
`cell` label may remain for the emit envelope or also move per-decision.

### B. Per-symbol caps/buffers config (epic §4.7–4.10)

**B1. `modules/execution/safety/config.py` + `configs/safety.yaml` — config maps.**
Extend `_AllocationCapCfg` with `caps: dict[str, Decimal]` + `default_cap: Decimal`; add a
`_BuyingPowerCfg` with `buffers: dict[str, Decimal]` + `default_buffer: Decimal` (relax
`extra='forbid'` for exactly these fields; `:33-34` ✅). YAML:
```yaml
hard_guards:
  allocation_cap:
    enabled: true
    caps: {fUSD: 0, fUST: 3000, fADA: 0}   # native units; unlisted → default_cap
    default_cap: 0
  buying_power:
    enabled: true
    buffers: {fUSD: 3, fUST: 3, fADA: 0}   # native units; unlisted → default_buffer
    default_buffer: 0
```
Boot assert: every configured cell's `symbol` has an explicit `caps[symbol]` entry (§3 strict).
Retain `BFX_ALLOCATION_CAP_USDT`/`BFX_BALANCE_BUFFER_USDT` as documented single-currency env
fallback. *Test:* map parsing; `default_cap`/`default_buffer` fallback; boot assert fires when
a configured cell's symbol is missing from `caps`; env back-compat path.

**B2. `modules/execution/safety/hard_guards.py` — guards read per-symbol values.**
`AllocationCapGuard.evaluate` reads `caps[decision.symbol]` (→ `default_cap` → env fallback)
instead of `ctx.allocation_cap_usdt`; `BuyingPowerGuard.evaluate` reads
`buffers[decision.symbol]`. Wire the maps from YAML into the guards at build time
(`daemon.py`). Guard logic is otherwise untouched (already reads `decision.symbol`). Error
message names the currency. `cap=0` ⇒ `exposure(0)+offer > 0` ⇒ blocks every POST for that
currency. *Test:* an fUSD POST at fUSD's cap is blocked while an fUST POST with room is allowed
(independence); `cap=0` blocks all of a currency; error names the currency.

### C. Per-symbol gap/sizing

**C1. `modules/execution/deployment/tracker.py` — partition the rescale pool by symbol.**
`reconcile_to_total` currently `sum(self._deployed.values())` across all cells (`:56` ✅).
Either key `_deployed` by `(symbol, cell_id)` and rescale each symbol's sub-pool to that
symbol's `reserved_total`, or operate only on the passed symbol's cell subset. *Test:* a
2-symbol × 2-cell scenario asserts each symbol's gap/rescale is independent (no cross-currency
contamination).

**C2. `modules/execution/deployment/reconciler.py` — per-symbol gap loop.**
Replace the single `symbol=self._cell_symbol[cells[0].cell_id]` (`:101`) with a loop over
`configured_symbols(self._cells)`: per symbol read
`current_exposure/reserved_exposure/available_balance(symbol)`, compute gap/headroom against
`caps[symbol]`/`buffers[symbol]`, `allocate_gap` over that symbol's active cells, and
`tracker.reconcile_to_total` per symbol. The submit loop (already builds per-cell
`symbol=self._cell_symbol[cell_id]`) runs inside. *Test:* two symbols each get an independent
gap pool; single-active-symbol path equals current behaviour.

### D. Correctness prerequisites

**D1. `modules/execution/event_store/store.py` — symbol-filter the tail-fold.**
Add `if (r.payload or {}).get('symbol') != symbol: continue` before the
`rebuild_snapshot_from_log` fold (`:404`; the in-code comment at `:401-403` already prescribes
this exact line). *Test:* an event_log with two symbols rebuilds into two correct
`position_state` rows, not one mixed row.

**D2. Make `symbol` mandatory on the event write path.** Drop the `symbol='fUSD'` default on
`DecisionPayload`/events (`schemas.py:114`) and align/remove the divergent
`DEFAULT_RECONCILE_SYMBOL='fUST'` (`store.py:45`) so no event can be written without an explicit
symbol; fail-fast on absence at the write boundary. *Test:* writing an event without a symbol
raises; no code path relies on a default.

**D3. Native-unit rename (epic §4.1; scope decision §3).** `size_usdt`→`amount` on events;
`position_state` columns `reserved_usdt`/`realized_usdt`→`reserved`/`realized` (already in
migration `b7c1d2e3f4a5`); `PositionReconciled` `*_usdt`→native + `symbol`; config keys lose
`_usdt` where they become native maps. Mechanically heavy (events, tables, ledger, guards, and
their tests) but the correct shape and cheapest pre-launch. *Test:* existing suites updated;
no `*_usdt` remains on the native hot path.

**D4. Apply migration `b7c1d2e3f4a5` at cutover.** PK → `(account_id, deployment_environment,
symbol)`, native column rename, no backfill (pre-launch clean recreate). *Test:* `alembic
upgrade head` creates the per-symbol PK; `alembic check` no drift (after Neon cred refresh).
Evaluate during planning whether `reconcile_observation` (no symbol column today) and
`offer_claims` (global `venue_offer_id`-keyed FSM diff) must become per-symbol now — DEFER
unless the single-active-symbol behaviour is incorrect (it is not, while fUST-only).

## 5. Data flow (multi-symbol, after Phase 2)

```
WS funding frame (symbol) ─▶ executor.submit(decision.symbol)  ─▶ venue POST {symbol=decision.symbol}
                                              │                         (fail-fast if symbol unknown)
reconciler.deploy: for symbol in configured_symbols:
    gap = caps[symbol] - exposure(symbol);  headroom = available(symbol) - buffers[symbol]
    allocate_gap(symbol's cells);  tracker.reconcile_to_total(symbol, reserved_total[symbol])
guards: AllocationCapGuard  caps[symbol]  vs exposure(symbol)
        BuyingPowerGuard    available(symbol) - buffers[symbol] vs offer
ledger: _reserved[symbol]/_realized[symbol]/_available[symbol]   (Phase 1, unchanged)
store.rebuild: fold filtered by payload['symbol'] == symbol  ─▶ one position_state row per symbol
```

## 6. Error handling / edge cases

- Offer for an unconfigured symbol → `default_cap = 0` blocks; executor fail-fast also rejects.
- `decision.symbol` missing → executor raises `InvariantViolation`, submits nothing (D2 makes
  this unreachable on the write path; the executor guard is defense-in-depth).
- `_available[symbol]` defaults to 0 until the first per-symbol reconcile → `BuyingPowerGuard`
  blocks that symbol until its balance is known (fail-safe).
- Back-compat: a deploy with no `caps`/`buffers` maps keeps working via the single-currency env
  fallback (mapping to the sole configured currency).
- `cap=0` currency: the reconciler MAY skip cap=0 symbols before sizing to avoid per-tick
  guard-block log noise, OR let the guard block each POST — implementation choice (prefer skip
  for cleanliness; document either way).

## 7. Cutover (live VM canary)

Migration `b7c1d2e3f4a5` + per-symbol caps must land **atomically** at one cutover:
1. Refresh stale Neon credential (`.env`, Neon MCP `get_connection_string`) — known landmine.
2. `cd backend_py && uv run alembic upgrade head` (transactional; the env.py advisory-lock +
   atomic pattern from `ca52eb2` applies).
3. Deploy the Phase 2 build with `caps:{fUST:3000, fUSD:0, fADA:0}` — fUST cap unchanged.
4. Verify `position_state` rebuilds the live fUST credit row (the ~273 fUST credit) before
   re-enabling writes; confirm `/healthz` 200 and the routing regression behaviour in logs.
5. fUSD/fADA remain `cap=0` (dark) — no offers attempted; no funding moved.

Rollback: `BFX_KILL_SWITCH` + recreate; or `deploy-vm.sh shadow`; DB via Neon PITR. (Koyeb
stays paused — never two prod writers.)

## 8. Out of scope (deferred)

- **Per-symbol NAV/loss/drawdown split** (epic §4.9) — inert today (guards disabled + fUST
  only); activate before fADA funds. `_nav_by_symbol` already keyed so it stays additive.
- **Per-currency structural carve (Paradigm B)** — deferred; forcing function recorded in §2.
- **fADA cell** (strategy params, ADA precision, native min-offer floor) — separate epic;
  Phase 2 only adds the `caps:{fADA:0}` config entry.
- **Unified USD-equivalent exposure overlay / cross-currency ceiling** — epic §7; needs a price
  feed; explicitly deferred (no price feed in the hard-guard hot path).
- **WS `wu` wallet-frame parsing** — balance stays REST-polled per ~90s reconcile.
- **`reconcile_observation`/`offer_claims` per-symbol columns** — defer unless single-active
  behaviour is wrong (it is not while fUST-only); revisit when a 2nd currency funds.

## 9. Testing (TDD)

- **Executor routing (A):** POST body `symbol == decision.symbol` and `!= cells[0].symbol`;
  empty/unknown symbol raises and submits nothing. This is the regression lock for the §8
  blind spot.
- **Config (B1):** `caps`/`buffers` map parsing; `default_cap`/`default_buffer` fallback; boot
  assert on a configured cell missing from `caps`; env back-compat path.
- **Guards (B2):** per-symbol independence (fUSD at cap blocked, fUST with room allowed);
  `cap=0` blocks all of a currency; error names the currency; live-only `BuyingPowerGuard`
  exemption preserved.
- **Tracker/reconciler (C):** 2-symbol × 2-cell gap pools independent; single-active-symbol
  path equals current behaviour.
- **Store (D1):** multi-symbol fold → one row per symbol.
- **Mandatory symbol (D2):** event without symbol raises.
- **Migration (D4):** `alembic upgrade head` creates the per-symbol PK; `alembic check` no
  drift (after Neon cred refresh).
- Gate: `cd backend_py && uv run pytest -m "not integration"` + `mypy src/` + `ruff check`.

## 10. Implementation decomposition (for writing-plans)

Roughly four bite-sized TDD slices, money-route first so the highest-severity fix lands and is
regression-locked before the rest:
1. **Route fix** — A1, A2, routing regression test.
2. **Config maps + guards** — B1, B2.
3. **Per-symbol gap/sizing** — C1, C2.
4. **Correctness prerequisites + cutover** — D1, D2, D3 (rename), D4 (migration), §7 runbook.
