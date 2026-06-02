# fUSD-live enablement — remaining hard-gate code prereqs

- **Date:** 2026-06-02
- **Status:** draft (awaiting operator review)
- **Author:** Will + Claude
- **Supersedes / extends:** `2026-05-31-per-currency-native-allocation-design.md` §8 (deferred prereqs); ADR `2026-06-01-per-currency-allocation-phase-2.md` Followup. NAV-split D4 already landed (`2c0b7cc`).
- **Alembic head at authoring:** `b7c1d2e3f4a5` (single head, clean).

## 1. Context & Goal

The bot trades a single funding currency live today (fUST, cap 3000 on the VM canary; fUSD is **dark**, cap=0). The per-currency allocation epic (Phase 1 `6b94b71` + Phase 2 `228fa85`) made every *stateful* surface per-symbol and NAV-split D4 (`2c0b7cc`) made the loss/drawdown limiters per-symbol. What remains before a **second** currency (fUSD) can trade are a handful of **symbol-blind seams** that are latent today (everything collapses to the one active symbol) but become real-money correctness bugs the instant a 2nd cell trades.

This spec closes the **code** prereqs. The **external** prereq — funding a real USD balance on Bitfinex — is operator-owned and out of scope here.

**Goal:** make the four remaining symbol-blind seams correct-or-loud, so that flipping fUSD live becomes a pure config + funding step with no latent mis-attribution.

## 2. Verified current state (the four seams)

All line numbers verified against the tree at authoring time.

### P1 — `reconcile_observation` is symbol-blind
- `ReconcileObservationRow` (tables.py:117-141) has **no `symbol` column**. Index is `(account_id, deployment_environment, id)`.
- `PostgresEventStore.set_position_snapshot` writes a checkpoint row (store.py:327-336) **without** symbol, even though it already receives a `symbol` param (called per-symbol from boot_recovery.py:322-331).
- `rebuild_snapshot_from_log` (store.py:382-387) selects the **latest** checkpoint `ORDER BY id DESC LIMIT 1` with **no symbol filter** → when rebuilding fUST it can seed from fUSD's `reserved`/`realized` base. The tail-fold (store.py:417-424) *does* filter `payload["symbol"]==symbol`, so only the checkpoint **base** is contaminated.
- **Latency:** `rebuild_snapshot_from_log` has **no production caller** (grep: defined store.py:343, called only from tests; live boot uses `set_position_snapshot` per-symbol). The NOTE at store.py:375-381 documents this. So P1's bug is *latent* (test/admin-rebuild only) — but it is still a prereq to close before relying on rebuild with >1 symbol.

### P2 — `offer_claims` is symbol-blind
- `OfferClaimRow` (tables.py:65-84) has **no `symbol` column**. cid-keyed composite PK `(account_id, deployment_environment, cid)`.
- The boot reconcile missing-claim diff (`compute_recovery_actions`, called boot_recovery.py:311-317) stamps **`symbol=self._symbols[0]`** — a single hardcoded primary symbol — on every `ReservationReleased`/`ReservationFailed` it emits. Comment at boot_recovery.py:308-310 confirms "LocalClaim has no per-claim symbol yet (Phase 2: offer_claims.symbol)".
- The FSM diff is intentionally **global** (boot_recovery.py:276-281): venue_offer_id is globally unique on Bitfinex, so venue offers are unioned across symbols before diffing — correct. But the **release action** then needs the *per-claim* symbol, which it cannot recover because offer_claims carries none.
- **Blast radius:** `ReservationReleased` decrements `reserved[symbol]` (events.py:178-199). A wrong symbol decrements the **wrong currency's ledger bucket**, corrupting the per-currency exposure the AllocationCap/BuyingPower guards read. This fires on the **live boot/reconcile path every restart** → **riskiest seam in the batch** (see §10).
- **Distinct gate:** this seam is armed the instant fUSD enters `_symbols` (boot_recovery.py:254-265), which is a *different* gate than the cell-config flip (P4). An operator could add fUSD to the symbols list while believing "no fUSD cell yet → safe".

### P3 — `ReservationIntent` / `ReservationFailed` are symbol-less and amount-less
- Verified: events.py:80-98 (`ReservationIntent`) and events.py:100-116 (`ReservationFailed`) carry `cid, size_usdt, signal_correlation_id, account_id, is_simulated, …` but **no `symbol`, no `amount`, no `__post_init__`**. They are the *only two* position-lifecycle events still in this shape; the other four (`ReservationClaimed`, `OrderFilled`, `ReservationReleased`, `PositionReconciled`) already carry mandatory `symbol` + canonical `amount` via `_resolve_amount` (events.py:119-199).
- `store.append`'s position projection (store.py:104-108) stamps these two with `getattr(_ev,"symbol",None) or DEFAULT_RECONCILE_SYMBOL` (`= "fUST"`, store.py:50).
- `_project_offer_claims` (store.py:159-160) reads `claim_size = amount if not None else size_usdt`, with a breadcrumb (store.py:156-158): "making symbol mandatory must add amount+symbol to those two events FIRST, after which this size_usdt fallback can be dropped."
- **P3 is a prerequisite for P2**, not independent: `_project_offer_claims` creates the **PENDING** offer_claims row from `RESERVATION_INTENT`. Once offer_claims has a symbol column (P2), that row's symbol must come from the event — and `_upsert_claim`'s `on_conflict_do_update` updates only `state/venue_offer_id/last_updated_ms` (store.py:205), so the symbol set at **INSERT** time (the INTENT event) is what persists. Therefore `ReservationIntent` must carry the correct symbol (P3) before P2's offer_claims.symbol is trustworthy. P2 and P3 also share the boot_recovery emission site (both `ReservationReleased` and `ReservationFailed` need the per-claim symbol).

### P4 — fUSD cell in `cells.canary.yaml`
- Config-only. cells.canary.yaml:8-12 documents the gate (single global pool → must wait for per-currency allocation + USD funding). The fUSD cell **definitions already exist** in `cells.yaml` (master) — the canary file simply excludes them.
- Gated on: P1+P2+P3 merged & applied · per-currency cap maps live (already merged `228fa85`) · NAV-split live (already merged `2c0b7cc`) · **external USD funding** (operator).
- **Not implemented this session.** Delivered as a checklist (§7.4).

## 3. Scope

**In scope (code, this session):** P1, P3, P2 — landed on `main`, **UNDEPLOYED** (same as NAV-split D4). Two additive DB migrations authored + locally/CI-tested.

**Out of scope:**
- P4 cell flip + deploy + USD funding (external; checklist only).
- Applying the migrations to the live shared Neon DB (done at the next coordinated canary deploy — see §8).
- Per-symbol threshold maps, portfolio-level NAV backstop, peak-HWM persistence across restarts (deliberately deferred in NAV-split spec — do **not** add here).
- Structural per-currency carve, fADA cell (epic decisions D1/D3 — rejected/dropped).
- The `ClaimRecord.symbol='fUSD'` dataclass default (registry_offers.py:70) is left as-is; from_snapshot will always set it explicitly, making the default dead but harmless.

## 4. Decisions

| # | Decision | Ruling | Rationale |
|---|----------|--------|-----------|
| D1 | Spec/plan structure | **1 spec + 3 plans (P1, P3, P2) + P4 checklist** | Single goal/invariant ratified once; disjoint file sets get independent per-task 3-lens review (the pattern that caught latent bugs in D4/Phase-2). |
| D2 | Migration files | **Two separate migrations, sequenced linearly** (P1 `down_revision=b7c1d2e3f4a5`, then P2 `down_revision=<P1 rev>`), applied in the same cutover | Independent tables → independent downgrade + clear blame; linear chain avoids multi-head; one maintenance window. |
| D3 | Apply timing | **Author-now / apply-at-cutover** (Phase-1 pattern) | Both columns are additive + `server_default` → forward-compatible with old code (old daemon keeps writing, column gets default; new daemon reads the filter). *Safer* than Phase-1's destructive PK swap; removes "exact-instant coordination" pressure. |
| D4 | Backfill default | **`server_default='fUST'` for BOTH tables** | Canary is fUST-only (fact, not preference). The DB stamps the one/few existing rows automatically; semantics unambiguous. (Rejects the P2-reader's `'fUSD'` suggestion, which was anchored to the stale `symbols[0]`/`ClaimRecord` default and would mis-stamp every existing live row to a never-traded currency.) |
| D5 | `DEFAULT_RECONCILE_SYMBOL` (Q2, best-practice ruling) | **Upcaster pattern.** Symbol mandatory on the live event shape (no silent default). Old symbol-less event_log rows are upcast at the **deserialize boundary** (inject the historically-correct `"fUST"`). Constant re-homed to `events.py` (serialization.py can't import store.py — cycle). store.py:107 live fallback → fail-loud assert. **Never mutate the append-only event_log.** | Event-sourcing best practice = schema evolution lives in the read/deserialize layer (event upcasting); rewriting history destroys the audit guarantee and is an anti-pattern even pre-launch. This deserialize path is production-uncalled (test/admin-rebuild) → upcaster is cheap insurance. |
| D6 | Checkpoint index (P1) | **Add `(account_id, deployment_environment, symbol, id)` index in the P1 migration** | The new `rebuild_snapshot_from_log` filters all three + orders by id desc; the existing `…_acct_env_id` index won't serve it. Cheap, future-proof. |
| D7 | Missing-claim symbol resolution (Q1, best-practice ruling) | **Make the bad state unrepresentable.** `offer_claims.symbol` NOT NULL + stamped at claim creation → no runtime "missing symbol" case. **Delete `symbols[0]`**, read `claim.symbol`. **No runtime fallback.** Add a fail-loud assert "released symbol ∈ configured set", reusing the executor-boundary fail-fast invariant (epic D1) rather than inventing boot-specific handling. | Strongest form: unrepresentable-bad-state > checked-bad-state. On a pre-launch single-currency canary the assert never fires; post-funding it is a structural guard. Fail-loud on a genuine invariant violation, not a silent degrade that re-introduces the mis-attribution P2 exists to remove. |
| D8 | Acceptance gate | **fUST byte-identical + per-symbol isolation + `alembic check` clean**, asserted in tests, before P4 enters scope | The project's established safety contract (`3db7a3b`, `2c0b7cc`). |

## 5. Dependency graph & decomposition

```
 Phase 1+2 + NAV-split D4 (DONE): position_state PK has symbol; 4 events carry symbol+amount
        │
        ├──────────────── P1 (reconcile_observation.symbol) ─── independent, parallel ───┐
        │                                                                                 │
        └── P3 (Intent/Failed symbol+amount) ──→ P2 (offer_claims.symbol + recovery) ─────┤
              (events + producers + upcaster)      (table + projection + boot_recovery)    │
                                                                                           ▼
                                          P4 (fUSD cell flip) ── checklist, gated on all + EXTERNAL USD funding
```

- **P1 is independent** (separate table, separate code path) → can be authored in parallel with P3/P2.
- **P3 → P2 is a hard order** (§2 P3 rationale): P2's offer_claims.symbol and boot_recovery recovery actions depend on the events carrying symbol.
- **P4 depends on P1+P2+P3** applied + external funding.

**Execution:** subagent-driven-development, one task at a time per plan (TDD → spec-compliance review → code-quality review → commit), mirroring D4/Phase-2. P1 and (P3→P2) may run as parallel subagent streams; P4 is a manual checklist.

## 6. Per-prereq design

### 6.1 P1 — checkpoint symbol (independent)

**Migration** (`…_add_symbol_to_reconcile_observation`, `down_revision=b7c1d2e3f4a5`):
- `ADD COLUMN symbol TEXT NOT NULL DEFAULT 'fUST'` (server_default backfills the single live row).
- `CREATE INDEX … ON reconcile_observation (account_id, deployment_environment, symbol, id)` (D6).
- `downgrade()`: drop index + column.

**Code:**
- tables.py: add `symbol: Mapped[str]` to `ReconcileObservationRow` (after `deployment_environment`), `server_default=text("'fUST'")`; add the new `Index`.
- store.py `set_position_snapshot` (:327): add `symbol=symbol,` to the `ReconcileObservationRow(...)` construction (the param is already in scope).
- store.py `rebuild_snapshot_from_log` (:382-387): add `.where(ReconcileObservationRow.symbol == symbol)`; update the NOTE comment (:375-381) to reflect the fix.

**Tests:**
- Two-symbol rebuild: write fUST checkpoint (fence=50, realized=1000) then fUSD checkpoint (fence=52, realized=200); `rebuild_snapshot_from_log("fUST")` must seed from the **fUST** checkpoint, not the later fUSD one. (This is the regression that proves the bug fixed.)
- `set_position_snapshot` writes `symbol` into the checkpoint row.
- Integration (testcontainers, `-m integration`): full alembic chain up through the new revision; assert column + index present.

### 6.2 P3 — symbol + amount on the two claim-lifecycle events (before P2)

**Events** (events.py — mirror the `ReservationClaimed` shape exactly):
- `ReservationIntent` / `ReservationFailed`: add `symbol: str` as the **first** field (frozen+slots: non-default precedes defaulted); add `amount: Decimal | None = None`; change `size_usdt: Decimal` → `size_usdt: Decimal | None = None` (transitional alias); add `def __post_init__(self): _resolve_amount(self)`.
- Add the upcast constant to events.py, e.g. `LEGACY_SYMBOL_UPCAST = "fUST"` (re-home of `DEFAULT_RECONCILE_SYMBOL`'s value, importable by serialization.py without a cycle).

**Producers** (decision.symbol available at every site):
- reservation_emitting.py:76-80 (`ReservationIntent`): add `symbol=decision.symbol`. `amount` auto-mirrors from `size_usdt` via `_resolve_amount` (matches the sibling `ReservationClaimed` at :86-90, which passes `size_usdt=size`).
- reservation_emitting.py:112-116 (`ReservationFailed`): add `symbol=decision.symbol`.
- boot_recovery.py `compute_recovery_actions` `ReservationFailed` emission: symbol comes from `LocalClaim.symbol` (delivered by P2 — this is the P3↔P2 join; sequence P3 dataclass/producer first, wire the boot_recovery symbol source in P2).

**Deserialize upcaster** (serialization.py `deserialize_event` :55-60):
- After building kwargs, for `cls in (ReservationIntent, ReservationFailed)`: if `kwargs.get("symbol") is None`, set `kwargs["symbol"] = LEGACY_SYMBOL_UPCAST`. (`amount`/`size_usdt` are reconciled by `_resolve_amount` in `__post_init__`.) Document this as a schema-evolution upcast for pre-symbol rows.

**store.py fallback removal:**
- :104-108: drop `or DEFAULT_RECONCILE_SYMBOL`; the live events now always carry symbol → assert present, fail-loud otherwise.
- :159-160: drop the `size_usdt` fallback in `_project_offer_claims`; use `_ev.amount` directly (now populated on both events).
- Remove/retire the store.py `DEFAULT_RECONCILE_SYMBOL` live use; the only legitimate use (deserialize upcast) now lives in serialization.py via the events.py constant.

**Tests:**
- Both events round-trip serialize→deserialize with symbol+amount.
- Deserializing a **legacy** payload (no symbol) upcasts to `"fUST"`.
- Producers stamp `decision.symbol`.
- `store.append` persists symbol; projection fails loud if a (hypothetical) symbol-less live event is appended.
- fUST byte-identical: a fUST ReservationIntent/Failed produces the same offer_claims/position_state effect as before.

### 6.3 P2 — offer_claims symbol + recovery (after P3)

**Migration** (`…_add_symbol_to_offer_claims`, `down_revision=<P1 rev>`):
- `ADD COLUMN symbol TEXT NOT NULL DEFAULT 'fUST'` (D4 — backfills active CLAIMED rows; fUST-only canary makes this correct; self-heals on next reconcile regardless).
- `downgrade()`: drop column.

**Code:**
- tables.py: add `symbol: Mapped[str]` to `OfferClaimRow` (`server_default=text("'fUST'")`).
- store.py `_project_offer_claims` (:159-167): extract `symbol = _ev.symbol` (always present post-P3) and pass to `_upsert_claim`.
- store.py `_upsert_claim` (:173-207): add `symbol` param; add `"symbol": symbol` to `values`; **include `symbol` in the `on_conflict_do_update` set_** (defensive — a cid's currency is invariant, but the CLAIMED event reasserts it).
- boot_recovery.py `_load_local_claims`: read `offer_claims.symbol` → populate `LocalClaim.symbol` (new field).
- boot_recovery.py `compute_recovery_actions` (:311-317 caller + the function): drop the single `symbol=self._symbols[0]` arg; stamp each `ReservationReleased`/`ReservationFailed` with its **claim's** `claim.symbol`; add fail-loud assert `claim.symbol in configured_symbols` (D7).
- registry_offers.py `from_snapshot`: populate `ClaimRecord.symbol` from `offer_claims.symbol`.

**Tests:**
- All three release paths emit the correct per-claim symbol: (a) boot_recovery missing-claim, (b) fill_tracker offer-disappearance, (c) ws_dispatcher foc CANCELED/EXPIRED.
- Multi-symbol reconcile: fUST + fUSD claims, remove one from venue, assert the release stamps the **true** symbol and the ledger buckets stay isolated (the L2 regression).
- `_project_offer_claims` persists symbol; `from_snapshot` round-trips it.
- Fail-loud assert fires on a symbol ∉ configured set.
- Integration (testcontainers): full alembic chain through the P2 revision; column present.

### 6.4 P4 — fUSD cell flip (checklist, not implemented)

Pre-conditions (all must hold):
1. P1 + P3 + P2 merged to main and the two migrations applied at a coordinated deploy.
2. Per-currency cap maps live (✅ `228fa85`) and NAV-split live (✅ `2c0b7cc`) — confirm on the running VM.
3. **External: real USD funded** on the Bitfinex account.
4. fUSD cap set >0 in BOTH `safety.canary.yaml` and the caps map (cutover invariant `assert_caps_invariant`).

Action: un-gate cells.canary.yaml:8-12 by adding the fUSD cell(s) from cells.yaml; deploy via `deploy-vm.sh canary` under KILL_SWITCH gate; verify `effective_cap_per_symbol` shows fUST + fUSD, caps_invariant passes, per-symbol NAV guards active.

### 6.5 Cutover runbook notes (from the whole-branch final review — IMPLEMENTED on main `008146a`)

P1+P3+P2 are merged to main (`008146a`, `--no-ff`), **UNDEPLOYED**. The 3-lens adversarial review returned READY_TO_MERGE (fUST byte-identical, migrations forward-compatible, no blocker). Carry these to the fUSD cutover:

1. **Apply migrations in chain order at cutover:** `cd backend_py && uv run alembic upgrade head` brings the DB `b7c1d2e3f4a5 → c9d0e1f2a3b4` (reconcile_observation.symbol + index) `→ dac1e2f3a4b5` (offer_claims.symbol). Both are `ADD COLUMN NOT NULL DEFAULT 'fUST'`, no table rewrite. Via `deploy-vm.sh canary`, never MCP/raw SQL. Run `alembic check` **after** apply (it shows drift until applied — that is expected pre-apply).
2. **Coexistence is safe but directional:** old binary + upgraded schema works (old INSERTs get `server_default='fUST'`; the fUST-only canary attributes them correctly). The reverse (`alembic downgrade` while a new-code daemon runs) breaks new code — do **not** downgrade once cut over.
3. **Deploy the per-symbol binary BEFORE enabling the fUSD cell.** If a fUSD cell is live while old code still runs, old code writes fUSD claims stamped `server_default='fUST'` → silent capital mis-attribution. Order: deploy binary (P1+P2+P3) → apply migrations → then add the fUSD config cell.
4. **New boot-abort failure mode (intended):** `boot_recovery.compute_recovery_actions` now hard-aborts (`ValueError`) on every reconcile tick (boot + periodic) if any released/failed claim's `symbol ∉ configured_symbols` (boot_recovery.py ~155/169). Never fires on the fUST-only canary. After enabling fUSD, the config cell must list `{fUST, fUSD}` **before** any fUSD claim can be recovered, else boot/periodic reconcile fail-loud and halt (correct fail-fast — abort > corrupt; watch the Loki boot/`bfx-submit-fail` alerts on first fUSD boot).
5. **Legacy event_log upcast is read-path only:** symbol-less historical INTENT/FAILED rows upcast to `'fUST'` at deserialize (serialization.py); the append-only event_log is never rewritten and needs no data migration.

## 7. Migration strategy (shared)

- **Generate sequentially:** P1 first (`alembic revision --autogenerate` → `down_revision=b7c1d2e3f4a5`), then P2 (`down_revision=<P1 rev>`) so the chain is linear (no fork/multi-head).
- **Local/CI only this session:** verify with `cd backend_py && uv run alembic upgrade head` against local sqlite/Postgres and the testcontainers integration tests (`-m integration`, Docker-only — runs in CI/cutover, not on this machine). **Never** touch the live Neon DB; **never** use MCP/raw SQL.
- **Forward-compatibility:** both are `ADD COLUMN … NOT NULL DEFAULT 'fUST'` → old code on the shared DB keeps writing (column auto-defaults), new code reads the filter. The alembic env.py advisory lock + timeouts already serialize concurrent upgrades.
- **Apply at the next coordinated canary deploy** (with the eventual fUSD cutover), exactly as Phase-1's `b7c1d2e3f4a5` was applied at cutover, not on push.

## 8. Acceptance criteria

- `cd backend_py && uv run pytest -m "not integration"` green (was 1104 at NAV-split).
- `uv run mypy src/` + `uv run ruff check` clean.
- `uv run alembic check` clean (metadata ↔ migrations no drift); `alembic upgrade head` then `downgrade` then `upgrade` idempotent on a scratch DB.
- New regression tests: P1 two-symbol checkpoint base; P2 multi-symbol release isolation (L2); P3 upcaster + producer symbol.
- **fUST byte-identical** asserted: single-symbol paths produce identical offer_claims/position_state/event effects as before this batch.

## 9. Risks & landmines

- **L1 (resolved by D4):** the P2-reader recommended `server_default='fUSD'`; that would mis-stamp every existing live row to a never-traded currency. Ruling: **`'fUST'`** for both tables.
- **L2 (riskiest — see §10):** the `symbols[0]` hardcode is a *live boot-path* mis-attribution armed by adding fUSD to `_symbols`, a gate distinct from the cell flip. Closed by D7.
- **Synthesis misread, corrected:** an exploration agent claimed "P3 already done" by misreading `ReservationClaimed`/`OrderFilled`/`ReservationReleased` (which do carry symbol+amount) as the Intent/Failed events. Verified false — Intent/Failed genuinely lack both. P3 is real work.
- **Circular import:** the upcast constant must live in events.py (serialization.py imports events; store.py imports both). Do not leave it in store.py for the deserialize path.
- **Multi-head:** generating both migrations off the same head forks the chain. Generate P1 then P2 so P2 chains off P1.

## 10. Riskiest real-money concern

**P2's `symbol=self._symbols[0]` in the missing-claim release path (boot_recovery.py:311-317).** It (a) mutates the capital ledger directly (`reserved[wrong_symbol]`), (b) fires on the production boot/reconcile path every restart (unlike P1, which has no live caller, and unlike P3, which has no ledger effect), and (c) is armed the instant fUSD enters `_symbols` — a gate separate from the visible cell-config flip. Mitigation = D4 (`'fUST'` default) + D7 (read `claim.symbol`, no fallback, fail-loud on ∉-configured) + treat "add fUSD to `_symbols`" as gated on Plan B (P2) merge, distinct from the cell flip.

## 11. Related

- ADR `2026-06-01-per-currency-allocation-phase-2.md`; spec `2026-05-31-per-currency-native-allocation-design.md` §8; NAV-split spec `2026-06-02-nav-split-per-symbol-loss-drawdown-design.md`.
- Memory `per-currency-native-allocation`, `best-practice-decision-protocol`, `canary-currency-state`.
