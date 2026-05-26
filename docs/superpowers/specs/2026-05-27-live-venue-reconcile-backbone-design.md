# Live Venue Reconcile Backbone — Design

**Date:** 2026-05-27
**Status:** Approved (brainstorming)
**Context:** First real-money canary run (2026-05-26) surfaced that the live
post-submit lifecycle never converges. Offers placed at the venue are recorded
`CLAIMED` but their fills/cancels are never reflected back into the ledger, so
`position_state.reserved` is permanently stuck at the allocation cap and
`AllocationCapGuard` blocks all further offers — capital sits idle while the bot
believes it is fully deployed.

This follows directly from `2026-05-25-venue-reconcile-verify-design.md` (which
hardened the *boot-time* REST reconcile + the "capture real fixtures" testing
methodology). That spec correctly deferred *periodic* reconcile as
anti-gold-plating — there was no evidence it was needed. There now is: a 7-hour
silent stuck-at-cap incident. This spec makes reconcile the runtime correctness
backbone.

## The incident (evidence)

Canary, account `default`, env `prod`, 2026-05-26:

- 07:00 — bot placed 2× $150 fUST offers (voi `4960024777`, `4960024785`).
- **07:10** — both matched → became funding credits `457328790`, `457328791`.
  The WS events that should have recorded this **both failed**:
  - `bfx_ws_parse_failed type=foc err=TypeError("float() argument ... not 'NoneType'")`
  - `ws_dispatcher_diag voi=None msg=fcn ... missing offer_id_meta — cannot map back`
- 08:00 — bot placed a 3rd $150 offer (voi `4960063836`). `reserved` now $450 = cap.
- 08:00 → now — `AllocationCapGuard` blocks every tick; no new offers. The 2
  matched offers' credits later closed and returned to the wallet, but the
  ledger still shows `reserved=$450, realized=$0`. Net: ~$300 idle inside the
  cap, bot will never redeploy it.

`event_log` (prod) contains only `RESERVATION_INTENT / CLAIMED / FAILED` — **zero**
`ORDER_FILL` or `RESERVATION_RELEASED`. `ORDER_FILL` (415 rows) exists only in
`shadow`. The live post-submit lifecycle has produced nothing.

## Root causes

1. **WS `foc` parser index mismatch** (`auth_ws.py::_parse_foc`). Uses
   `symbol=d[2]`, `status=d[7]`, `rate=float(d[11])`, `period=d[12]`. The real
   Bitfinex funding-offer array is `symbol=d[1]`, `status=d[10]`, `rate=d[14]`,
   `period=d[15]` (the *correct* layout is already documented for the REST
   parser in the 2026-05-25 spec: `parse_active_funding_offers`). On the real
   wire `d[11]` is a placeholder (`None`) → `float(None)` → `TypeError` → the
   whole `foc` event is dropped → offer close/cancel/execute never reaches the
   dispatcher → reservation never released.

2. **WS `fcn` → offer mapping is unsound** (`auth_ws.py::_parse_fcn`,
   `offer_id_meta=int(d[14])`). Bitfinex funding-credit events do **not** carry
   the originating offer id (confirmed in `fill_tracker.py:8-9`); `d[14]` is
   `mts_opening`. So a fill can never be mapped credit→offer via `fcn` →
   `OrderFilled` is never emitted.

3. **Test fixtures encode the same wrong layout.** `fixtures/foc_canceled.json`
   etc. were hand-authored to match the buggy indices, so unit tests are green
   while prod crashes — exactly the failure mode the 2026-05-25 spec named
   ("hand-authored mock data already embodies the wrong assumption").

4. **Reconcile is boot-only.** `BootRecovery` runs once at
   `Daemon.run()` startup and would have caught the drift (local `CLAIMED` vs
   venue snapshot → `missing` → `ReservationReleased`). But the daemon has not
   restarted since before 07:00, so it never re-ran. There is **no periodic
   convergence** — the single architectural gap that turned two WS parse bugs
   into a 7-hour silent stuck state.

The deeper cause: **correctness depended on the WS stream.** When the stream
broke, nothing converged. Best practice forbids this.

## Industry best practice (the principles this design enforces)

| Principle | This design |
|---|---|
| Venue = source of truth; local ledger = projection that must converge | Periodic REST snapshot reconcile is the backbone |
| **Stream is a latency optimization that is allowed to fail** | WS `foc` path is secondary; correctness never depends on it |
| Snapshot + incremental sync; re-sync on **seq gap / reconnect**, not only a timer | Timer-driven reconcile **plus** WS-reconnect/seq-gap-triggered reconcile |
| Fail-safe (fail-closed) on uncertainty | Repeated venue-fetch failure → executor DEGRADED → block new offers |
| Divergence observability | Each reconcile emits released/claimed counts; non-zero on a periodic run → DEGRADED + WARN |
| Reconcile must not race live placement | Grace window on reconcile actions (see below) |
| Idempotent application | registry RELEASED dedup + `venue_seq` dedup + deterministic synthetic cids |

Institutional analogue: FIX **drop-copy reconciliation** — an OMS never trusts
its own fill stream; it reconciles against the venue's authoritative copy.

## Architecture — three reconcile layers

```
                    Bitfinex (truth for which offers exist)
                       │ WS stream (fast, fallible)   │ REST snapshot (authoritative)
                       ▼                                ▼
  Layer 1: WS fast path        Layer 2: PERIODIC reconcile      Layer 3: boot reconcile
  auth_ws + ws_dispatcher      (NEW) interval + gap-triggered    (existing) BootRecovery
  foc → fill/release           compute_recovery_actions          runs once at startup
  seconds; OPTIONAL            ≤ interval; CORRECTNESS BACKBONE   cross-restart backstop
                       └───────────────┬────────────────────────┘
                                       ▼
            ReservationReleased / OrderFilled  (idempotent; registry + seq dedup)
                                       ▼
            PG event_log (SoT) → ledger projection → AllocationCapGuard
```

**Priority / sequencing:** Layer 2 is built, tested, and shipped **first** — it
makes the bot correct regardless of WS. Layer 1 (WS `foc` fix) is a follow-on
latency optimization, explicitly allowed to fail. For a canary whose cells act
hourly, a ~90 s reconcile interval already dominates the latency requirement, so
the WS fix is not on the critical path.

## Components & changes

| Component | File | Change |
|---|---|---|
| **Periodic reconcile (NEW)** | new `modules/execution/periodic_reconcile.py` (or daemon sub-task) | Interval loop reusing `BootRecovery` fetch/persist/publish + `compute_recovery_actions`. Iterates the **distinct symbols across all cells** (canary: `fUST`). Triggers: (a) every `BFX_RECONCILE_INTERVAL_S`; (b) immediate on WS reconnect / `venue_seq` gap. |
| **Grace on reconcile actions** | `boot_recovery.py::compute_recovery_actions` | Add a per-direction grace param applied to `orphan→ReservationClaimed` and `missing→ReservationReleased` (only act on offers stable at venue / claims older than `grace_ms`). `PENDING→FAILED` already grace-guarded. **Boot passes `action_grace_ms=0`** (no concurrent placement → reconcile fully and immediately, current behaviour preserved); **periodic runtime passes `action_grace_ms>0`** so it never races an offer mid-placement. |
| **Runtime fail-safe** | periodic reconcile + executor/safety chain | Consecutive venue-fetch failures → emit executor `DEGRADED` health → `AllocationCapGuard`/safety chain blocks new offers until reconcile recovers. Mirrors boot fail-safe (which fails startup). |
| **Divergence signal** | periodic reconcile | Structured log per run (`released`, `orphans_claimed`, `pending_failed`). A non-zero release on a **periodic** (non-boot) run means the WS stream silently missed an event → health `DEGRADED` + WARN. |
| **daemon wiring** | `daemon.py` (TaskGroup, ~line 188 / 800-1073) | Register periodic reconcile as a live-only sub-task alongside `ws_dispatcher`. New env `BFX_RECONCILE_INTERVAL_S` (default 90). |
| **WS `foc` parser fix** | `auth_ws.py::_parse_foc` | Align indices to verified layout: `symbol=d[1]`, `mts_create=d[2]`, `mts_update=d[3]`, `amount=d[4]`, `status=d[10]`, `rate=d[14]`, `period=d[15]`; tolerant None-guard on `rate`/`period`. |
| **WS `fcn` parser** | `auth_ws.py::_parse_fcn` | Drop the unsound `offer_id_meta=d[14]` mapping; `fcn` becomes informational (lifecycle no longer depends on it). |
| **dispatcher** | `ws_dispatcher.py::_translate_foc` | `foc EXECUTED → OrderFilled` (authoritative fill signal — `foc` carries `venue_offer_id`); remove the "EXECUTED is redundant, fcn handles it" no-op. CANCELED/EXPIRED → `ReservationReleased` unchanged. |
| **WS fixtures** | `tests/external/bitfinex/fixtures/foc_*.json`, `fcn_*.json` | Rebuild from **real captured payloads** (current ones are hand-authored wrong layout); fast-contract + gated-live-contract per the 2026-05-25 methodology. |

`RestPollingFillTracker` stays as-is (gated off). Layer 2 deliberately uses the
**full registry-vs-venue snapshot diff** (`compute_recovery_actions`), not the
fill_tracker's `last_state` diff — only the snapshot diff can release offers that
disappeared *before* the reconciler started observing (e.g. the currently-stuck
$300). Enabling fill_tracker as a faster detection layer is a deferred option.

## Data flow

- **Placed → filled:** PENDING (`reserved+=`) → submit → CLAIMED. Then either WS
  `foc EXECUTED → OrderFilled` (seconds) **or** periodic reconcile sees voi
  missing → `ReservationReleased` (≤ interval). Ledger converges; freed capital
  redeployed next tick.
- **Cancel/expire:** WS `foc CANCELED/EXPIRED` → Released, or reconcile missing →
  Released.
- **WS fully dead (the incident):** reconcile is the sole convergence path →
  still correct, interval-latency; divergence signal fires.
- **Venue unreachable:** reconcile fetch fails repeatedly → executor DEGRADED →
  new offers blocked (fail-safe).

## Error handling

- Tolerant WS parsing: only required fields (`voi`, `status`, `amount`) are
  strict; `rate`/`period` None-guarded. Parse failure → drop + WARN; correctness
  unaffected (backbone).
- Reconcile fetch: transient (timeout / 5xx / 429) → bounded retry+backoff
  (existing `BootRecovery._fetch_offers`); persistent → runtime fail-safe degrade
  (NEW; boot already fails startup).
- In-flight race: grace window on reconcile actions (above).
- Idempotency: registry RELEASED dedup + `venue_seq` dedup + deterministic
  synthetic cids → WS and periodic reconcile may overlap safely.

## Testing (real-fixture methodology, per 2026-05-25)

| Layer | Cases |
|---|---|
| unit — reconcile | existing `compute_recovery_actions` tests + **grace both directions** (orphan within grace not claimed; missing past grace released; release of long-stale claim) |
| unit — WS parser | rebuild `foc`/`fcn` fixtures from **real payloads**; fast-contract (parses, fields/types correct) + gated-live-contract (`@pytest.mark.integration`, venue drift) |
| unit — dispatcher | `foc EXECUTED → OrderFilled` |
| unit — periodic loop | timer fires reconcile; consecutive failure → DEGRADED; WS reconnect / seq-gap → immediate reconcile (fakes) |
| **integration (reproduces incident)** | **WS silent (no events) → periodic reconcile converges ledger**; reserved drops from cap, new offers resume |

Commit gate: `cd backend_py && uv run pytest -m "not integration"` green + mypy +
ruff. Live-contract tests excluded by default.

## Rollout (the fix self-unsticks the stuck capital)

1. Land code; commit gate green.
2. (Optional) capture real WS fixtures via the gated live test (needs `BFX_API_KEY`/`SECRET`).
3. Deploy canary via the **manual** `scripts/deploy-koyeb.sh` (auto-deploy is
   disabled — `no_deploy_on_push=true`). Set `BFX_RECONCILE_INTERVAL_S`.
4. First periodic reconcile after deploy: local `CLAIMED {3}` vs venue `{1}` → 2×
   `ReservationReleased(missing_from_venue)` → `reserved` → $150 → bot resumes
   placing within cap. **No manual DB surgery.**
5. Verify: `event_log` shows 2 `RESERVATION_RELEASED`; `position_state.reserved`
   = $150; subsequent ticks place new offers; divergence WARN observed (expected,
   since this first convergence is real drift).

## Non-goals / deferred (anti-gold-plating)

- Credit / interest / earnings tracking (`fcn`/`fcu` amounts) — separate feature.
- Enabling `RestPollingFillTracker` as a faster detection layer.
- Per-currency multi-symbol reconcile beyond iterating current cell symbols
  (only relevant once per-currency allocation lands).
- Divergence alerting infra beyond a health-status + structured log.
- WS path rewrite — dispatch table / OOO staging / dedup logic is sound; keep it.
