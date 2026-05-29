# Balance-Aware Cap Gate — Design

**Status:** approved (brainstorming) — pending implementation plan
**Date:** 2026-05-30
**Related:** [credit-aware reconcile](2026-05-29-credit-aware-reconcile-design.md), deployment reconciler ADR (`wiki/projects/bfx-funding-bot/decisions/2026-05-29-deployment-reconciler.md`)

## The incident (evidence)

Live canary, 2026-05-29 ~15:31 UTC onward. A ~$150 credit matured and returned to the
deposit wallet, dropping `realized` 556.89 → **406.89** (2 credits). The deployment
reconciler then entered a **90-second failed-submit loop**:

```
reconcile_complete venue_offers=0 venue_credits=2 reserved=0.00 realized=406.89
→ submit fUST amount=163.10526316
→ HTTP 500  ["error",10001,"Invalid offer: not enough UST balance available in deposit wallet"]
→ deployment_submit_rejected cell=fUST_a30 status=failed
```

`163.10526316 = 570 (cap) − 406.89 (exposure)`. The reconciler wanted to fill the gap to
the allocation cap, but the deposit wallet only had ~$150 free (< 163.11) → every tick the
venue rejected the submit. Real money was safe (failed submits place nothing), but ~$150 sat
idle and the logs spammed 10001 every 90 s.

Stopgap deployed same day (`3a2c731`, Koyeb `9c8d631c`): cap 570 → 550 so
`gap = 143.11 < min_fill(153)` → `allocate_gap` returns `{}` → reconciler sleeps. Verified:
3 reconcile cycles, 0 submits, 0×10001, state intact. **This spec replaces that stopgap with
the real fix and restores cap to 570.**

## Root cause

`DeploymentReconciler.deploy()` sizes the gap purely from policy:

```python
# reconciler.py:98
fills = allocate_gap(target=self._ctx.allocation_cap_usdt, current_exposure=e_total, ...)
# sizing.py:37
gap = target - current_exposure          # = cap − exposure, NEVER consults wallet balance
```

The bitfinex client (`external/bitfinex/`) has **no wallet-balance read at all** — sizing
cannot know what is actually deployable. When `cap > actual funding-wallet total` (570 vs
~557 here), the computed gap can exceed available funds, and the reconciler submits an offer
the venue must reject. Relying on the venue's 10001 rejection as the only backstop is exactly
what produces the noisy loop.

## Industry best practice (the principles this design enforces)

1. **Venue truth is the source of truth for hard constraints.** Available balance is part of
   venue truth and belongs in the same reconcile observation pass that already reads
   offers/credits — not a side-channel call. (Consistent with the existing
   reconcile-as-correctness-backbone philosophy.)
2. **Separate policy limits from physical constraints.** Allocation cap and per-cell
   concentration express *risk intent* (operator-chosen, upper bounds on intended exposure).
   Available balance is *what we can physically deploy now*. Deployable
   `= min(policy_target − exposure, available − buffer)`. Clamp the **action size**, never the
   risk intent → concentration `cap_per_cell` stays bound to the policy cap.
3. **Declarative target-state reconciliation (controller pattern).** The deployment reconciler
   is already a k8s-style controller reconciling to `target_exposure`. Available funds is one
   more dimension of observed *actual state* fed into sizing.
4. **Pre-trade buying-power check is a distinct gate.** Institutional layering:
   strategy → sizing → **pre-trade risk/buying-power** → OMS → venue. The check lives in
   sizing (buying-power-aware position sizing) and as an independent guard — not inside the
   order-submit primitive, and not delegated to venue rejection.
5. **Fail-closed on money movement.** On uncertainty (cannot read balance), do nothing and
   self-heal next tick.

## Architecture

Two layers of enforcement against one bound (`available − buffer`):

- **Primary (correctness):** buying-power-aware sizing. `allocate_gap` clamps the cumulative
  gap to `available − buffer`. This alone fixes the bug.
- **Defense-in-depth:** `BuyingPowerGuard` in the safety chain — a per-offer sanity backstop
  so an over-balance submit never leaves the process even if the sizing snapshot is stale.

`available` is fetched once per reconcile tick (alongside offers/credits), flows through
`ReconcileResult` → `PositionReconciled` event → the in-memory ledger, and is read from the
ledger by both the sizing clamp and the guard — mirroring how `AllocationCapGuard` already
reads `ledger.current_exposure()`.

## Architecture Decisions (resolved 2026-05-30)

- **Buffer:** fixed small amount, `BFX_BALANCE_BUFFER_USDT` default **3** (covers
  rounding/precision/fees; venue `available` is already net of holds). Not a percentage — 2%
  on ~$550 would waste ~$11 of yield. Distinct from `min_offer_buffer_pct` (which sizes the
  venue minimum, `effective_min = 153`).
- **No double-counting:** the clamp and the guard use the **same** bound `available − buffer`.
  The clamp controls the *cumulative* per-tick total (`sum(fills) ≤ available − buffer`); the
  guard checks each *single* offer (`offer ≤ available − buffer`). Buffer is reserved once.
- **`available` is in-memory only, not persisted to PG.** It flows through the
  `PositionReconciled` event into the ledger; the `position_state` table is unchanged
  (no migration). At boot the ledger defaults `available = 0` → reconciler deploys nothing
  until the first reconcile (~within 90 s, and a boot reconcile runs) — fail-closed and safe.
- **Fetch failure → skip the deploy tick.** If `_fetch_wallets()` (3-attempt backoff, same as
  offers/credits) still fails, the reconcile tick does not call `deploy()`; next tick
  self-heals. Available is never advanced on a failed read.
- **Concentration cap unchanged:** `cap_per_cell = concentration_pct * allocation_cap_usdt`
  (policy cap), independent of available balance.
- **Wallet currency derived from cell symbol:** `fUST → UST`, `fUSD → USD` (strip leading
  `f`). v1 wires the canary's single currency.

## Components & changes

| Change | File | Detail |
|---|---|---|
| **New** `FundingWallet` + `parse_wallets()` + `get_wallets(ctx, currency)` | `external/bitfinex/auth_rest.py` | POST `/v2/auth/r/wallets` (signed), reuse offers/credits signing pattern; filter `type=="funding" and currency==…`; return available |
| **New** `_fetch_wallets()` + sum available | `modules/execution/boot_recovery.py` | mirror `_fetch_offers`/`_fetch_credits` (3-attempt backoff); `available_usdt = funding-wallet available for the symbol's currency` |
| **Extend** `ReconcileResult` | `boot_recovery.py:51` (defn; returned by `BootRecovery.run()`) | add `available_usdt: Decimal = Decimal("0")` |
| **Extend** `PositionReconciled` event | events module | add `available_usdt: Decimal` |
| **Extend** ledger | `modules/execution/ledger.py` | add `_available: Decimal`; set in `on_position_reconciled`; getter `available_balance() -> Decimal`. No PG column. |
| **Change** `allocate_gap` | `modules/execution/deployment/sizing.py` | add param `available_headroom: Decimal`; `gap = min(target − current_exposure, available_headroom)`. Pure. |
| **Change** `deploy()` | `modules/execution/deployment/reconciler.py` | `headroom = max(0, ledger.available_balance() − buffer)`; pass to `allocate_gap`; take `buffer` via constructor (env-injected) |
| **New** `BuyingPowerGuard` | `modules/execution/safety/hard_guards.py` | mirror `AllocationCapGuard(ledger=…)`; block if `offer_amount_usdt > available_balance − buffer` |
| **Wire** guard + buffer | `modules/marketfeed/daemon.py` | append guard after `AllocationCapGuard`; inject `BFX_BALANCE_BUFFER_USDT` into reconciler + guard |
| **Restore** cap | `scripts/deploy-koyeb.sh` | `BFX_ALLOCATION_CAP_USDT` 550 → 570; remove STOPGAP comment |

## Data flow

```
PeriodicReconcile._tick()
  ├ BootRecovery.run()
  │   ├ get_active_funding_offers   → reserved_usdt
  │   ├ get_active_funding_credits  → realized_usdt
  │   ├ [NEW] get_wallets(currency) → available_usdt
  │   ├ set_position_snapshot(reserved, realized)        # PG, unchanged
  │   └ publish PositionReconciled{reserved, realized, available}
  │         → ledger.on_position_reconciled → {_reserved,_realized,_available}
  └ [on success only] DeploymentReconciler.deploy()
        ├ exposure  = ledger.current_exposure()
        ├ available = ledger.available_balance()
        ├ headroom  = max(0, available − buffer)
        ├ fills = allocate_gap(target=cap, current_exposure=exposure,
        │                      available_headroom=headroom, …)   # gap = min(cap−exposure, headroom)
        └ per fill → safety_chain.evaluate (… → BuyingPowerGuard backstop) → submit
```

Worked example (cap restored to 570, the incident state): exposure 406.89, available 150,
buffer 3 → headroom 147 → `gap = min(163.11, 147) = 147 < min_fill 153` → `{}` → sleep.
When a maturity/interest grows available past `min_fill + buffer`, the same logic deploys
automatically without operator action.

## Error handling

- **`get_wallets` transient/HTTP error:** `_fetch_wallets()` retries (3 attempts, transient
  backoff — same policy as offers/credits). Persistent failure raises out of
  `BootRecovery.run()`; `PeriodicReconcile._tick()` records the failure and **does not** call
  `deploy()` this tick (fail-closed). This is the **existing** reconcile-failure control flow —
  wallets simply joins offers/credits as a fetch that can abort the tick; no new branch.
  `available` is not advanced.
- **Malformed wallets response:** `parse_wallets` raises `BitfinexShapeError` → same skip path.
- **No funding wallet for the currency** (e.g., zero balance, wallet absent): treat as
  `available = 0` → headroom 0 → no deploy (safe).
- **Boot:** ledger `_available` defaults 0 until first reconcile populates it → no premature
  deploy.

## Simulation (paper/shadow) — resolved

Confirmed live-only: `daemon.py` constructs `BootRecovery`, `DeploymentReconciler`, and
`PeriodicReconcile` only under `if not spec.is_simulated` (lines 810, 893); the simulated path
leaves `periodic_reconcile = None`. The balance-aware gate therefore touches **only the live
path** — simulation drives no deployment reconciler and needs no simulated balance source.
(The earlier "open item" is closed.)

## Testing (TDD)

- **`sizing.py` (pure):** `available_headroom` < gap (clamp binds), ≥ gap (cap binds),
  < min_fill (sleep), 0 / negative (sleep, no negative fills), multi-cell sum ≤ headroom.
- **`auth_rest`:** `parse_wallets` shape (valid row, malformed, short row) using real-fixture
  methodology; `get_wallets` filters by `type`/`currency`; signing headers built.
- **`BuyingPowerGuard`:** block when `offer > available − buffer`; allow at/under; SKIP/CANCEL
  bypass; missing `offer_amount_usdt` → block.
- **ledger:** `on_position_reconciled` sets `_available`; `available_balance()` getter; boot
  default 0.
- **reconciler integration:** stub ledger with `available_balance` → assert clamped fills;
  fetch-failure path → `deploy()` not called.
- Gate: `cd backend_py && uv run pytest -m "not integration"` green; `mypy src/`; `ruff check`.

## Rollout

1. Implement + tests green (TDD).
2. Restore cap to 570 in the same change.
3. Deploy canary via `scripts/deploy-koyeb.sh canary`; verify logs: with `available < gap`,
   reconciler sleeps (no submit, no 10001); when `available` is sufficient, a single clean
   `deployment_submitted`. Confirm no regression in reserved/realized reconcile.
4. Update memory + wiki: stopgap retired, balance-aware gate live.

## Non-goals / deferred

- **USDT/USD ticker fetch** for `effective_min` (still static `153`; separate deferred item).
- **Per-currency allocation** — `get_wallets` is currency-parameterized, but multi-currency
  *allocation* across cells is out of scope (see canary fUST-only state).
- **Persisting `available` to PG** — in-memory only; revisit only if a warm-start use emerges.
- **Precise multi-cell in-tick guard accounting** — the sizing clamp is the precise cumulative
  control; the guard is a per-offer backstop. Single-cell canary is unaffected; documented
  limitation for the multi-cell phase.
