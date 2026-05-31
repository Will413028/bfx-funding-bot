# Per-Currency Native-Unit Allocation (fUSD / fUST / fADA …) — Design

**Date:** 2026-05-31
**Status:** Approved design → ready for implementation plan
**Supersedes:** `2026-05-26-per-currency-allocation-design.md` (USDT-denominated, balance
deferred). This version is grounded in code as of HEAD `798a765` and reflects three
features that shipped after the original spec: credit-aware reconcile v2 (2026-05-29),
the balance-aware cap gate (2026-05-30), and the NAV-based loss/drawdown tracker
(2026-05-30).

**Trigger:** Operator wants to support **fUSD, fUST, and fADA** (two stablecoins plus a
volatile crypto). The bot currently treats all currencies against one global, scalar,
USDT-denominated cap and balance gate — wrong for independent wallets, and the
USDT-denomination breaks for a non-stablecoin whose value floats.

**Two locked product decisions (operator, 2026-05-31):**
1. **Scope = build the foundation; do not real-money-deploy fUSD/fADA yet.** New
   currencies ship at `cap = 0` (disabled) until funded. Canary keeps running
   single-currency fUST. "Pre-launch is the window to refactor to the correct shape."
2. **A unified cross-currency USD-equivalent exposure ceiling is DEFERRED.** Only
   per-currency native caps + native balance gates are in scope now. No price feed
   enters the hard-guard hot path.

---

## 1. Problem

Allocation exposure, policy caps, and the physical balance gate are all **global,
scalar, and USDT-denominated**:

- `AllocationCapGuard` (`hard_guards.py:111-146`) blocks a POST when
  `ledger.current_exposure() + offer > ctx.allocation_cap_usdt` — a single global
  ceiling from `BFX_ALLOCATION_CAP_USDT`.
- `ledger.current_exposure()` (`ledger.py:135`) returns `self._reserved +
  self._realized` — scalars summed across **all** currencies (`ledger.py:38-39`).
- `BuyingPowerGuard` (`hard_guards.py:153-191`) blocks when `offer >
  ledger.available_balance() - buffer`, where `_available` (`ledger.py:40`) is one
  scalar sourced from a single-currency REST wallet read.

This is correct only for a single-currency deployment. For multiple currencies it is
wrong on two axes:

- **Independence:** fUSD, fUST, fADA are separate wallets. Lending one must not consume
  another's budget, and the bot must not attempt to lend a currency it does not hold.
- **Denomination:** a USDT-denominated cap/balance is fine for stablecoins (≈1:1 USD)
  but **incorrect for fADA**, whose USD value floats. A USDT cap on an ADA position
  would require an ADA/USD conversion, dragging price-feed volatility and staleness into
  the real-money write path.

A third gap blocks any per-currency guard: **the guard cannot see which currency it is
evaluating.** `DecisionPayload` (`schemas.py:101-128`) and `AccountContext`
(`protocols.py:29-34`) carry no `cell`/`symbol`; the reconciler knows `cell_id`
(`reconciler.py:165`) but does not thread it into `safety.evaluate`.

## 2. Current state (verified against HEAD 798a765)

- **Ledger** (`modules/execution/ledger.py`): `_reserved`/`_realized` scalar `Decimal`
  (`:38-39`); `_available` scalar (`:40`). `on_reservation_claimed` (`:79`),
  `on_order_filled` (`:98`), `on_reservation_released` (`:108-109`) add/subtract
  `event.size_usdt`, no symbol. `on_position_reconciled` (`:118-129`) **absolute-sets**
  `_realized = event.realized_usdt` and `_available` (single-writer, every ~90s
  reconcile tick — credit-aware reconcile v2). `current_exposure()` → `_reserved +
  _realized` (`:135`); `available_balance()` → `_available` (`:151-157`).
- **Events** (`modules/execution/events.py`): `ReservationClaimed` (`:66-80`),
  `OrderFilled` (`:83-102`), `ReservationReleased` (`:105-121`) carry `cid`,
  `venue_offer_id`, `size_usdt`, … but **no `symbol`**. `PositionReconciled` (`:141-160`)
  is a single-currency global snapshot (`realized_usdt`/`available_usdt`/`reserved_usdt`,
  no symbol).
- **position_state** (`tables.py:87-106`): PK `(account_id, deployment_environment)`
  (`:106`); scalar `reserved_usdt`/`realized_usdt` (`:94-95`). Migrations
  `93c9ff214a61`, `a3ae9a60862a` added `last_reconciled_at`, `n_credits`, and the
  separate `reconcile_observation` table — none touched the PK or added a symbol.
- **Guards** (`hard_guards.py`): `AllocationCapGuard.evaluate` (`:111-146`);
  `BuyingPowerGuard` (`:153-191`, `name="buying_power"`, live-only — skipped for
  simulated at `daemon.py:775`). Wired as a pair after the allocation cap
  (`daemon.py:767-776`). Shared buffer `BFX_BALANCE_BUFFER_USDT` default 3
  (`daemon.py:732`).
- **Balance source**: REST only. `BootRecovery._fetch_available()` →
  `auth_rest.get_funding_available()` → POST `/v2/auth/r/wallets`
  (`auth_rest.py:236-271`) → `parse_wallets()` sums `available` for funding rows of one
  currency (`auth_rest.py:112-132`). No WS `wu` parsing (`auth_ws.py` handles
  `fcn`/`fcu`/`foc` only). Polled every ~90s; `ledger._available` is in-memory only,
  not persisted.
- **Reconciler** (`reconciler.py`): `headroom = max(0, available_balance() -
  balance_buffer)` (`:97-98`) → `available_headroom` for `allocate_gap()`. `deploy()`
  builds `DecisionPayload` with no cell/symbol (`:169-176`); cell is only the loop var
  `cell_id` (`:165`).
- **NAV** (`nav_pnl_source.py`): `ReconcileNavTracker` subscribed to `PositionReconciled`
  (`daemon.py:888`); `nav = available_usdt + reserved_usdt + realized_usdt` (`:56`),
  global. Exposes `realized_loss_24h()` / `drawdown_pct()` (`:66-77`) consumed by
  `RealizedLossGuard` / `DrawdownGuard` via the `pnl_source` protocol.
- **Boot recovery** (`boot_recovery.py:221-226`): `_symbol = symbol`, default `"fUSD"`;
  one symbol per reconcile cycle.

## 3. Approach: native-unit per-currency, USD overlay deferred

Make exposure, caps, and the balance gate **per symbol, in that symbol's native units**.
The ledger's source of truth becomes a per-symbol native amount; nothing in the hard
path sums across symbols or converts to USD. Each currency has its own native cap and
native balance gate; `cap = 0` disables a currency (the guard blocks every POST:
`exposure(0) + offer > 0`), so no offers are attempted for unfunded currencies.

**Why native units (best practice for a multi-currency lending system):** the venue
denominates offers, credits, and wallet balances in native currency; keeping the
settlement ledger and hard guards native makes them **price-feed-independent and correct
for crypto**, and confines any FX conversion to a risk/reporting overlay. That overlay (a
unified USD-equivalent portfolio view / cross-currency ceiling) is **out of scope /
deferred** (§7) — YAGNI while crypto is `cap = 0` pre-launch. The per-symbol interfaces
are shaped so the overlay can be added later without restructuring.

## 4. Components & changes

### 4.1 Native-unit renaming (correctness, pre-launch)

The pervasive `*_usdt` naming asserts a USDT denomination that is false for fADA. Rename
to native-neutral names as part of this work (pre-launch, no production data to migrate):

- Event field `size_usdt` → `amount` (the offer/credit amount in the event's currency).
- `position_state` columns `reserved_usdt`/`realized_usdt` → `reserved`/`realized`
  (native; free to rename because the migration recreates the table for the new PK).
- `PositionReconciled` fields `realized_usdt`/`available_usdt`/`reserved_usdt` →
  `realized`/`available`/`reserved`, plus a new `symbol`.
- Ledger internal scalars → per-symbol dicts (§4.3); public methods take `symbol`.
- Config keys lose the `_usdt` suffix where they become native per-currency maps (§4.8);
  the old env vars are retained as documented single-currency fallbacks.

This is mechanically heavy (touches events, tables, ledger, guards, and their tests) but
is the correct shape and the cheapest time to do it.

### 4.2 Events carry `symbol`

Add `symbol: str` (e.g. `"fUSD"`, `"fUST"`, `"fADA"`) to `ReservationClaimed`,
`OrderFilled`, `ReservationReleased`. The executor knows the cell/symbol at submit time;
thread it from the executor/WS-parse layer into the events. `PositionReconciled` also
gains `symbol` (one event per symbol per reconcile cycle — §4.5).

### 4.3 Ledger tracks exposure & balance per symbol

Replace the three scalars with `dict[str, Decimal]` maps:

- `current_exposure(symbol) -> Decimal` = `_reserved[symbol] + _realized[symbol]`.
- `available_balance(symbol) -> Decimal` = `_available[symbol]`.
- `on_reservation_claimed`/`on_order_filled`/`on_reservation_released` mutate the
  `event.symbol` bucket (default 0 for an unseen symbol).
- `on_position_reconciled` absolute-sets `_realized[event.symbol]` and
  `_available[event.symbol]` for that symbol only (single-writer per symbol).
- **No method sums across symbols in the hot path.** A global-sum helper is added only
  if a non-hot-path consumer needs it (audit during planning); it is explicitly not used
  by any guard.

### 4.4 `position_state` per symbol (hand-written Alembic migration)

Change PK to `(account_id, deployment_environment, symbol)` so each currency's
reserved/realized persists independently; rename columns to `reserved`/`realized`
(§4.1). Hand-write the migration (autogenerate is unreliable for PK changes); pre-launch
the table is tiny and the realm is moving to `prod`, so **no data backfill** — any stale
scalar row is discarded. Evaluate during planning whether `reconcile_observation`
(`n_credits`, checkpoints) must also become per-symbol; if the reconcile fires per-symbol
events (§4.5), its observation rows should key by symbol too.

### 4.5 Per-symbol reconcile

The reconcile loop iterates the **configured symbols** (currently just fUST), querying
venue offers/credits filtered by symbol and that currency's wallet `available`, and fires
one `PositionReconciled` **per symbol**. `BootRecovery` generalizes from a single
`_symbol` to looping configured symbols, rebuilding the per-symbol ledger from snapshot
rows + open `offer_claims`, and reading `available` per currency from
`/v2/auth/r/wallets`. With one active symbol this is behaviourally identical to today.

### 4.6 Symbol threading to the guards

Add `symbol: str` to the per-offer decision object the guards read (the `DecisionPayload`
constructed in `reconciler.deploy()`, `reconciler.py:169-176`), sourced from the
reconciler's `cell_id`. `AllocationCapGuard.evaluate` and `BuyingPowerGuard.evaluate` read
`decision.symbol`. (`AccountContext` stays the account singleton; the per-offer symbol
rides on the decision, not the context.)

### 4.7 `AllocationCapGuard` per symbol (native)

`evaluate` resolves `decision.symbol`, looks up the native `caps[symbol]` (or
`default_cap`), and `ledger.current_exposure(symbol)`, and blocks when
`exposure(symbol) + offer > cap(symbol)`. Error message names the currency. `cap = 0`
disables the currency.

### 4.8 `BuyingPowerGuard` per symbol (native) + reconciler headroom

`evaluate` blocks when `offer > ledger.available_balance(symbol) - buffer(symbol)`, with
a per-currency native `buffers[symbol]` (fallback `default_buffer`). The reconciler's
`headroom = max(0, available_balance(symbol) - buffer(symbol))` is computed per cell's
symbol and passed to that cell's `allocate_gap()`. Live-only exemption unchanged.

### 4.9 NAV / loss / drawdown per symbol

`ReconcileNavTracker` keys NAV/peak/loss-window state by symbol (driven by the per-symbol
`PositionReconciled`). `RealizedLossGuard` / `DrawdownGuard` evaluate per symbol and trip
if **any** symbol breaches its native threshold; thresholds become per-currency native
config with a single-currency env fallback. With only fUST active, identical to today.

### 4.10 Config (native per-currency maps + back-compat)

In the safety config YAML where the guards already live:

```yaml
hard_guards:
  allocation_cap:
    enabled: true
    caps: {fUSD: 0, fUST: 570, fADA: 0}   # native units; unlisted → default_cap
    default_cap: 0
  buying_power:
    enabled: true
    buffers: {fUSD: 3, fUST: 3, fADA: 0}  # native units; unlisted → default_buffer
    default_buffer: 0
```

Retain `BFX_ALLOCATION_CAP_USDT` and `BFX_BALANCE_BUFFER_USDT` as documented
single-currency fallbacks (mapping to the sole configured currency) so the current fUST
deploy keeps working until the YAML maps are populated. Per-currency loss/drawdown
thresholds follow the same map+fallback shape.

## 5. Data flow

```
WS funding frame (symbol) ─▶ executor ─▶ domain event (+symbol)
                                              │
                              ledger.apply_*  → _reserved[symbol]/_realized[symbol]
reconcile (per symbol) ─▶ PositionReconciled(symbol) ─▶ _realized[symbol]/_available[symbol]
                                              │                      └▶ NAV[symbol]
DecisionPayload(symbol) ─▶ AllocationCapGuard: cap(symbol) vs exposure(symbol)
                        └▶ BuyingPowerGuard:  available(symbol) - buffer(symbol) vs offer
position_state (per-symbol rows) ◀─ snapshot / boot recovery (loop symbols)
```

## 6. Error handling / edge cases

- Offer for an unconfigured symbol → `default_cap = 0` → blocked.
- Symbol missing from a domain event (should not happen post-change) → reject the POST
  defensively rather than mis-attribute exposure.
- Migration: pre-launch the account is clean (no open offers/positions) and the realm is
  `prod`; the hand-written migration recreates `position_state` with the per-symbol PK
  and needs no backfill (stale scalar rows discarded).
- Back-compat: a deploy with no `caps`/`buffers` maps keeps working via the
  `BFX_ALLOCATION_CAP_USDT` / `BFX_BALANCE_BUFFER_USDT` single-currency fallbacks.
- `_available[symbol]` defaults to 0 until the first per-symbol reconcile populates it →
  `BuyingPowerGuard` blocks that symbol until balance is known (fail-safe).

## 7. Out of scope (deferred)

- **Unified USD-equivalent exposure overlay / cross-currency ceiling** — the risk/
  reporting layer that marks all currencies to USD. Needs a price feed; explicitly
  deferred (operator decision). Per-symbol interfaces are shaped to accept it later.
- **WS `wu` wallet-frame parsing** — balance stays REST-polled per ~90s reconcile (the
  existing, working path); no new WS parsing.
- **Cross-currency rebalancing / FX execution.**
- **Per-currency strategy parameters** beyond what `cells.yaml` already encodes.

## 8. Implementation dependency & decomposition

- The `position_state` migration is applied via `cd backend_py && uv run alembic upgrade
  head`, which fails on a stale local Neon credential — refresh `.env` (Neon MCP
  `get_connection_string`) before applying/deploying. Unit tests (sqlite `create_all`)
  are unaffected and come first.
- Components 4.1–4.9 are tightly coupled (making the ledger per-symbol forces
  position_state, reconcile, and NAV to change together), so this is **one spec**. The
  implementation plan should phase it into bite-sized TDD tasks, roughly:
  **(P1)** events `+symbol` + rename + per-symbol ledger + position_state migration + boot
  recovery; **(P2)** symbol threading + `AllocationCapGuard` per-symbol native + config;
  **(P3)** `BuyingPowerGuard`/reconciler headroom per-symbol; **(P4)** per-symbol reconcile
  events + NAV/loss/drawdown per-symbol. The USD overlay (§7) is the clean deferred edge.

## 9. Testing (TDD)

- **Ledger:** per-symbol `apply_*` and `current_exposure(symbol)` / `available_balance(symbol)`
  isolation — an fUST fill/reconcile does not change fUSD or fADA buckets; no method sums
  across symbols.
- **AllocationCapGuard:** blocks an fUSD POST when fUSD is at its native cap while an fUST
  POST with room is still allowed (independence); `cap = 0` blocks all of a currency;
  error names the currency.
- **BuyingPowerGuard:** per-symbol native `available - buffer(symbol)`; an unknown-balance
  symbol (0) blocks; live-only exemption preserved.
- **Config:** `caps`/`buffers` map parsing; `default_cap`/`default_buffer` fallback;
  back-compat `BFX_ALLOCATION_CAP_USDT` / `BFX_BALANCE_BUFFER_USDT` single-currency paths.
- **Reconcile / boot recovery:** rebuilds per-symbol exposure from multi-symbol snapshot
  rows; fires one `PositionReconciled` per configured symbol; single-active-symbol path
  equals current behaviour.
- **NAV / guards:** per-symbol NAV; loss/drawdown trips on any symbol breaching its native
  threshold; single-currency fallback equals today.
- **Migration:** `alembic upgrade head` creates the per-symbol PK; `alembic check` no
  drift (once the Neon credential is refreshed).
- Gate: `cd backend_py && uv run pytest -m "not integration"` + `mypy src/` + `ruff check`.
