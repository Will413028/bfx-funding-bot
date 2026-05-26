# Per-Currency Allocation (Independent USD / USDT Limits) — Design

**Date:** 2026-05-26
**Status:** Approved design → implementation deferred (see §8 dependency)
**Trigger:** Operator funded the account with USDT and observed that the bot would
treat USD and USDT against one shared cap. USD and USDT are independent wallets;
allocation exposure and limits must be judged independently per currency.

## 1. Problem

The `AllocationCapGuard` enforces a single **global** ceiling: it blocks a POST
when `ledger.current_exposure() + offer_amount > BFX_ALLOCATION_CAP_USDT`. But
`ledger.current_exposure()` (`modules/execution/ledger.py:118`) returns
`self._reserved + self._realized` — **scalars summed across all currencies**. So
fUSD and fUST share one pooled cap: lending USDT consumes the same budget as USD,
and the bot would attempt to lend a currency it may not hold (rejected by venue).

This is correct only for a single-currency deployment (where the global cap *is*
that currency's cap). For multi-currency it is wrong — currencies are not
independent.

## 2. Current state (verified)

- `ledger.py`: `_reserved` / `_realized` are scalar `Decimal`s; `apply_*` methods
  add `event.size_usdt` with no symbol awareness.
- Domain events `ReservationClaimed` / `OrderFilled` / `ReservationReleased`
  (`modules/execution/events.py`) carry `cid`, `venue_offer_id`, `size_usdt`, …
  but **no `symbol`**.
- `position_state` snapshot table: PK `(account_id, deployment_environment)`,
  columns `reserved_usdt` / `realized_usdt` — **one scalar row per (account, env)**,
  not per symbol.
- `boot_recovery.py` already takes a `symbol` arg (defaults `"fUSD"`).
- `AllocationCapGuard.evaluate` (`hard_guards.py:125`) compares the global exposure
  to `ctx.allocation_cap_usdt` (from `BFX_ALLOCATION_CAP_USDT`).
- No funding-wallet balance ingestion exists (auth WS handles fon/fcn offer/credit
  frames; ws/wu wallet frames are documented but not parsed).

## 3. Approach (chosen: static per-currency caps)

Make exposure and caps **per symbol** (currency). Each currency has its own
configured cap; the ledger tracks exposure per symbol; the guard compares the
offer's-symbol exposure against that symbol's cap. A currency with `cap = 0` is
effectively disabled (the guard blocks every POST: `0 + offer > 0`), so no offers
are even attempted for unfunded currencies.

**Balance-awareness (reading the funding wallet to auto-derive caps) is OUT of
scope** (§7) — but the cap interface is shaped so it can later become
`min(config_cap(symbol), wallet_balance(symbol) × fraction)` without restructuring.

## 4. Components & changes

1. **Events carry `symbol`.** Add `symbol: str` (e.g. `"fUSD"`, `"fUST"`) to
   `ReservationClaimed`, `OrderFilled`, `ReservationReleased`. Source: the executor
   knows the cell/symbol at submit time; the Bitfinex WS funding frames (fon/fcn)
   include the symbol. Thread it from the executor / WS-parse layer into the events.

2. **Ledger tracks exposure per symbol.** Replace the scalar `_reserved` /
   `_realized` with per-symbol maps (`dict[str, Decimal]`). Add
   `current_exposure(symbol: str) -> Decimal` returning that symbol's
   `reserved + realized`. (Retain a global sum helper only if an existing consumer
   needs it — audit callers; the drawdown/loss guards may want global or per-symbol,
   decide per-caller during planning.)

3. **`position_state` per symbol (Alembic migration).** Change PK to
   `(account_id, deployment_environment, symbol)` so each currency's reserved/realized
   persists independently. Boot recovery rebuilds the per-symbol ledger from these
   rows. Migration is additive in spirit but changes a PK — write it by hand
   (autogenerate is unreliable for PK changes), pre-launch tables are tiny.

4. **Boot recovery per symbol.** Generalize `boot_recovery.py` (currently
   `symbol="fUSD"` default) to recover exposure for every symbol present in the
   snapshot / open offer-claims, populating the per-symbol ledger.

5. **Per-currency cap config.** Move the cap into the safety config YAML where the
   `allocation_cap` guard already lives, as a per-currency map:
   ```yaml
   hard_guards:
     allocation_cap:
       enabled: true
       caps_usdt: {fUSD: 0, fUST: 1000}   # per-currency; unlisted symbol → default_cap
       default_cap_usdt: 0                # currencies not listed are not lent
   ```
   Keep `BFX_ALLOCATION_CAP_USDT` as a back-compat single-currency fallback if no
   `caps_usdt` map is provided (maps to the sole configured currency), so existing
   single-currency deploys keep working.

6. **`AllocationCapGuard` per currency.** `evaluate` resolves the offer's symbol
   (from `DecisionPayload` — it carries `cell`, e.g. `"fUST_a30"`; derive symbol, or
   add an explicit `symbol` field to the payload during planning), looks up
   `caps_usdt[symbol]` (or `default_cap_usdt`) and `ledger.current_exposure(symbol)`,
   and blocks when `exposure(symbol) + offer > cap(symbol)`. Error message names the
   currency.

7. **Balance-awareness hook (deferred).** The guard reads its limit via a single
   `cap(symbol)` lookup. A future feature adds funding-wallet ingestion and changes
   that lookup to `min(config_cap(symbol), available_balance(symbol) × fraction)` —
   no change to the per-symbol ledger/guard structure.

## 5. Data flow

```
WS funding frame (symbol) ─▶ executor ─▶ domain event (+symbol)
                                              │
                                   ledger.apply_* (per-symbol _reserved/_realized)
                                              │
        AllocationCapGuard.evaluate(decision) ─▶ cap(symbol) vs exposure(symbol)
                                              │
                        position_state (per-symbol rows) ◀─ snapshot / boot recovery
```

## 6. Error handling / edge cases

- Offer for a symbol with no configured cap → `default_cap_usdt` (0 → blocked).
- Symbol missing from a domain event (shouldn't happen post-change) → reject the
  POST defensively rather than mis-attribute exposure.
- Migration / `position_state`: pre-launch the account is clean (no open
  offers/positions) and the realm is moving to `prod`, so there is no real exposure
  to preserve. The hand-written migration recreates `position_state` with the
  per-symbol PK and needs **no data backfill** (any stale scalar row is discarded).
- Existing single-currency deploys (no `caps_usdt`) keep working via the
  `BFX_ALLOCATION_CAP_USDT` fallback.

## 7. Out of scope (deferred)

- **Funding-wallet balance ingestion / balance-aware caps** (ws/wu parsing,
  per-currency balance tracking, staleness handling) — separate feature; the cap
  interface (§4.7) is shaped to accept it later.
- Per-currency *strategy* parameters beyond what cells.yaml already encodes (S7 is
  separate).
- Cross-currency rebalancing / FX.

## 8. Implementation dependency

The `position_state` migration must be applied via `cd backend_py && uv run alembic
upgrade head` — which currently **fails on the stale local Neon credential** (see
the local-credential issue). Refresh the local `.env` Neon password before
implementing/deploying this feature. Unit tests (sqlite `create_all`) are
unaffected and can be written first.

## 9. Testing (TDD)

- **Ledger:** per-symbol `apply_*` and `current_exposure(symbol)` isolation (a fUST
  fill does not change fUSD exposure).
- **Guard:** blocks an fUSD POST when fUSD is at its cap while an fUST POST with room
  is still allowed (independence); `cap=0` blocks all of that currency; error names
  the currency.
- **Config:** `caps_usdt` map parsing; `default_cap_usdt` fallback; back-compat
  `BFX_ALLOCATION_CAP_USDT` single-currency path.
- **Boot recovery:** rebuilds per-symbol exposure from multi-symbol snapshot rows.
- **Migration:** `alembic upgrade head` creates the per-symbol PK; `alembic check`
  no drift (run once the Neon credential is refreshed).
- Gate: `cd backend_py && uv run pytest -m "not integration"` + `mypy src/` + `ruff`.
