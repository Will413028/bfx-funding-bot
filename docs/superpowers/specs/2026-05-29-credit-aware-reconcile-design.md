# Credit-Aware Snapshot Reconcile — Design

**Date:** 2026-05-29
**Status:** Approved (design agreed 2026-05-29)
**Context:** First funded canary run surfaced that the runtime reconcile
under-counts deployed capital. The wallet holds $450, fully lent by the bot
across 3 funding credits, but `position_state.realized_usdt = 300` (2 credits).
The bot believes it has $150 of free cap headroom, retries an offer every hour,
and the venue rejects it (`10001 not enough UST balance`). No financial danger
(the venue back-stops the over-lend attempt) but the ledger is wrong, and the
credit-return path is equally broken.

This spec **extends and corrects** `2026-05-27-live-venue-reconcile-backbone-design.md`.
That spec made periodic reconcile the correctness backbone for **offers** and
explicitly deferred credit tracking as a non-goal (its line: "Credit / interest /
earnings tracking — separate feature"). That deferral is the root cause of the
new bug. This spec promotes **credit-awareness from non-goal to core** — but only
the *aggregate exposure* dimension, not interest/earnings attribution (still
deferred).

## The incident (evidence)

Canary, account `default`, env `prod`. Reconstructed from `event_log` +
`position_state` + Bitfinex wallet (operator-observed $450 fully lent):

5 offers were `RESERVATION_CLAIMED`; their fates:

| venue_offer_id | claimed (UTC) | recorded outcome |
|---|---|---|
| 4960024777 | 05-26 07:00 | `RESERVATION_RELEASED` 05-26 23:49 `reason=missing_from_venue` |
| 4960024785 | 05-26 07:00 | `RESERVATION_RELEASED` 05-26 23:49 `reason=missing_from_venue` |
| 4960063836 | 05-26 08:00 | `ORDER_FILL` 05-27 14:30 ✅ |
| 4960815610 | 05-27 00:00 | `ORDER_FILL` 05-27 19:30 ✅ |
| 4960815615 | 05-27 00:00 | `RESERVATION_RELEASED` 05-27 05:11 `reason=missing_from_venue` |

Bot ledger: 2 fills → `realized=$300`, `reserved=$0`. Venue truth: $450 lent (3
credits). The two `05-26 23:49` releases landed **1 minute after deployment
`fd2f603a` (23:48 restart)** → boot reconcile. At least one of the three
"released" offers actually **matched into a still-active credit**, but was
recorded as a release (capital freed) instead of a fill (capital still lent).

`reconcile_complete venue_offers=0` repeats every ~90 s; the running daemon
**never polls `/funding/credits`** (grep of live logs for "credit" → zero hits).
The bot has no visibility into its own deployed capital.

## Root cause

`boot_recovery.py::compute_recovery_actions` resolves an offer that is in the
local `CLAIMED` set but absent from the venue `/funding/offers` snapshot by
emitting `ReservationReleased(reason="missing_from_venue")`:

```python
# missing: local CLAIMED, venue gone -> release (reserved -= size)
for voi, claim in claimed_by_voi.items():
    if voi in venue_by_voi:        # venue_by_voi is built from ACTIVE OFFERS only
        continue
    actions.append(ReservationReleased(... reason="missing_from_venue" ...))
```

An offer leaves `/offers` for **two** reasons: (a) cancelled/expired — capital
returned, release correct; (b) **matched into a credit** — capital still lent,
release **wrong** (should be a fill that *keeps* the capital as `realized`).
The reconcile cannot distinguish them because it never looks at `/credits`.

The 2026-05-27 backbone fix solved the opposite failure (reserved stuck *high* at
cap because releases never fired) by releasing on absence — but **over-corrected**:
it now releases offers that filled, under-counting `realized`. The architecture
**infers a state transition from absence**, which is fundamentally ambiguous.
The deeper cause is the same one that spec named: *correctness must not depend on
inferring what the stream missed.* It only fixed it for one dimension (offers),
not the other (credits).

## Industry best practice (the principles this design enforces)

| Principle | This design |
|---|---|
| Venue = source of truth for **the full position** (offers **and** credits **and** balance), not a subset | Reconcile fetches `/funding/offers` + `/funding/credits` + funding wallet available |
| **Converge to the snapshot; never infer a transition from absence** | Each reconcile **sets** `reserved = Σ(active offers)`, `realized = Σ(active credits)` — idempotent, self-healing, indifferent to which events were missed |
| Stream is a latency optimization allowed to fail | WS `foc`/`fcn` apply optimistic deltas *between* reconciles; the periodic snapshot set is authoritative and overwrites any drift |
| Separate the **correctness ledger** from the **attribution ledger** | Exposure is venue-derived (exact); `event_log` per-offer events are demoted to audit / P&L attribution (best-effort, may miss the offer→credit link) |
| Pre-trade risk is balance-aware | Submit path consults venue available balance; never sends an offer it knows will be rejected |
| Fail-safe on uncertainty | Venue-fetch failure (offers *or* credits) → executor `DOWN` → block new offers (unchanged from backbone) |

Institutional analogue: an OMS reconciles **positions**, not just open orders,
against the venue's authoritative copy (FIX drop-copy + Request-For-Positions).
"Stream for speed, snapshot for truth" — applied to the *complete* state.

## Architecture

```
                 Bitfinex (truth: which offers + which credits + free balance)
        WS stream (fast, fallible)        │  REST snapshot (authoritative)
                 │                         ▼
                 ▼            PERIODIC reconcile (every BFX_RECONCILE_INTERVAL_S
   foc EXECUTED → realized+=               + on WS reconnect / seq gap):
   foc CANCEL  → reserved-=        offers  = get_active_funding_offers()
   (optimistic deltas)            credits  = get_active_funding_credits()  ← NEW
                 │                 set reserved = Σ offers
                 │                 set realized = Σ credits                ← NEW
                 └───────────────────────────┬───────────────────────────┘
                                             ▼
            PositionReconciled (absolute set; persisted for audit)  ← NEW
                                             ▼
            ledger {reserved, realized} → current_exposure → AllocationCapGuard
                                             ▼
                  submit path also gates on venue available balance ← NEW
```

`reserved`/`realized` become a **cache of last-reconciled venue truth** plus WS
deltas in between; each reconcile re-establishes truth. The per-offer
`compute_recovery_actions` claim/release events stay for the WS-gap audit trail,
but **exposure no longer depends on accumulating them correctly** — the snapshot
set is the backstop.

## Components & changes

| Component | File | Change |
|---|---|---|
| **Active credits query (NEW)** | `external/bitfinex/auth_rest.py` (+ `_ActiveCreditsQuery` Protocol) | `get_active_funding_credits(ctx, symbol) -> list[ActiveFundingCredit]` hitting `/v2/auth/r/funding/credits/{symbol}`. Reuse the verified funding-array layout + retry/transient classification already used for offers. |
| **`PositionReconciled` event (NEW)** | `modules/execution/events.py` | Carries `reserved_usdt`, `realized_usdt`, `account_id`, `n_offers`, `n_credits`, `occurred_at_ms`. Audit + drives the absolute ledger set. |
| **Ledger absolute set (NEW handler)** | `modules/execution/ledger.py` | `on_position_reconciled`: set `self._reserved`, `self._realized` to the snapshot values (not delta). `current_exposure` unchanged (`reserved + realized`). Persisted via snapshot so it survives restart. |
| **Reconcile convergence** | `boot_recovery.py::run` | After fetching offers, also fetch credits; compute `Σ offers`, `Σ credits`; append one `PositionReconciled` per run. Keep `compute_recovery_actions` for the per-offer audit events, but it is **no longer the sole exposure authority**. Drop the implicit "missing offer ⇒ capital freed" assumption from the *exposure* path. |
| **`ReconcileResult` extension** | `boot_recovery.py` | Add `reserved_usdt`, `realized_usdt`, `n_credits` so `PeriodicReconcile` can log/health-signal drift on the realized dimension too (divergence = snapshot set differed from prior ledger). |
| **Pre-trade balance gate (NEW)** | submit path (`live_executor` / safety chain) | Before submit, require venue funding available ≥ offer size; else skip with a structured log (no doomed venue call). Available comes from the reconcile snapshot (or a cheap wallet read), not a per-submit extra round-trip where avoidable. |
| **daemon wiring** | `daemon.py` | `BootRecovery` / `PeriodicReconcile` gain the credits query dep; subscribe ledger `on_position_reconciled`. No new sub-task (reuses the existing reconcile loop). |

Account is bot-only and single-currency (fUST), so **`realized = Σ(all active
fUST credits)` needs no credit→offer mapping** — it side-steps the Bitfinex
"credits carry no originating offer id" problem entirely. This invariant is what
makes the snapshot set exact; it is also why **sub-account isolation is the
declared next step** (it keeps the invariant true if manual lending ever shares
the account).

## Data flow (corrected)

- **Placed → filled:** PENDING (`reserved+=`) → CLAIMED. On match: WS `foc
  EXECUTED → reserved-=, realized+=` (optimistic), **and/or** next reconcile sets
  `reserved=Σoffers`, `realized=Σcredits`. Capital stays counted as `realized`
  while the credit is open. ← the bug fix.
- **Credit matures / returns:** credit leaves `/credits` → next reconcile sets
  `realized` down by that amount → capital is now free → redeployed next tick.
  ← previously impossible (realized never decremented).
- **Cancel/expire (unfilled):** offer leaves `/offers`, no new credit →
  `reserved=Σoffers` drops, `realized` unchanged. Correct.
- **WS fully dead:** reconcile is the sole convergence path; both dimensions
  still converge at interval latency.
- **Venue unreachable:** fetch fails → executor `DOWN` → new offers blocked.

## Bootstrap (self-heals the current $150)

First reconcile after deploy: fetches 3 credits → `Σcredits=$450` →
`PositionReconciled(realized=450, reserved=0)` → `current_exposure=$450`. With the
cap aligned to funded capital ($450), `AllocationCapGuard` now blocks the hourly
offer → `10001` noise stops. No manual DB surgery. **Confirm `BFX_ALLOCATION_CAP_USDT`
= 450** (the `safety.canary.yaml` comment still says 150 — stale; reconcile to the
real funded amount before deploy).

## Error handling

- Credits fetch: same transient-retry → fail-safe path as offers
  (`_fetch_offers`). Either fetch failing fails the run → degrade.
- Idempotency: `PositionReconciled` is an absolute set; replaying it is a no-op.
  Safe to overlap with WS deltas (the next set corrects any interim drift).
- Grace: a freshly-placed offer not yet visible in the snapshot must not collapse
  `reserved` — keep the existing `action_grace_ms` window concept on the offer
  dimension; for the absolute set, exclude in-flight PENDING from the diff or run
  the set on a short grace so a just-submitted offer isn't briefly dropped.

## Testing (TDD; real-fixture methodology per 2026-05-25)

| Layer | Cases |
|---|---|
| **unit — reproduce the bug (RED first)** | claimed offer matched into a credit + WS foc missed → reconcile keeps `realized = credit size` (NOT released to 0). Asserts `current_exposure` = offers+credits. |
| unit — credit fetch/parse | `/funding/credits` array → `ActiveFundingCredit`; real-payload fast-contract + gated-live-contract |
| unit — ledger | `on_position_reconciled` sets reserved/realized absolutely; survives `from_snapshot` round-trip |
| unit — convergence | Σcredits up (fill) → realized up; credit gone (matured) → realized down; offer gone unfilled → reserved down, realized flat |
| unit — pre-trade gate | available < size → submit skipped (no venue call); available ≥ size → proceeds |
| **integration (reproduces incident)** | venue: 0 offers + N credits; ledger drifted low → one reconcile → `realized=Σcredits`, exposure hits cap, hourly over-lend stops |

Commit gate: `cd backend_py && uv run pytest -m "not integration"` green + mypy +
ruff. Live-contract excluded by default.

## Rollout

1. Land code; commit gate green.
2. Align `BFX_ALLOCATION_CAP_USDT=450` on Koyeb canary.
3. Deploy via manual `scripts/deploy-koyeb.sh canary` (auto-deploy disabled).
4. First reconcile: `realized` → $450, exposure = cap, `10001` noise stops; verify
   `event_log` shows `PositionReconciled(realized=450)` and `position_state.realized_usdt=450`.
5. Watch the first credit maturity: `realized` decrements, bot redeploys the freed
   capital next tick (the previously-broken path).

## Non-goals / deferred

- **Sub-account isolation** — the declared **next step**. Keeps "all venue credits
  = bot's" rigorously true so the snapshot set stays exact; also cleans P&L.
- Interest / earnings / P&L attribution (`fcu` amounts, per-credit yield) — analytics
  feature; correctness does not depend on it.
- Per-offer signal→credit attribution — Bitfinex credits lack the offer id; accept
  best-effort via WS/event_log, never a correctness dependency.
- WS `foc`/`fcn` rewrite beyond the 2026-05-27 fixes — the stream stays an
  optimization.
