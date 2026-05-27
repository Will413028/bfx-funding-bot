# WS `foc` Authoritative Fill Path — Design (Plan 2)

**Date:** 2026-05-27
**Status:** Approved (brainstorming)
**Follows:** `2026-05-27-live-venue-reconcile-backbone-design.md` (Plan 1, the
periodic reconcile backbone — merged + canary-deployed). This is the deferred
Layer-1 (WS fast path) half of that work.

## Context

Plan 1 made the bot correct regardless of the WS stream: periodic REST reconcile
converges the ledger within `BFX_RECONCILE_INTERVAL_S` (≈90 s). That is the
correctness backbone. Plan 2 is a **pure latency optimization**: restore the WS
`foc` fast path so fills/cancels are reflected in seconds instead of ≤90 s.

Per the Plan 1 best-practice table: **the stream is a latency optimization that
is allowed to fail.** Correctness never depends on the WS path; reconcile is the
authoritative net. Plan 2 therefore carries no correctness risk — a regression in
the WS parser degrades to Plan-1 latency, it does not lose money or drift.

## Root cause (the real one is structural)

The 2026-05-26 incident dropped every `foc` event with
`float() argument ... not 'NoneType'`. The proximate cause was wrong array
indices in `auth_ws._parse_foc`. The **structural** cause:

> The same Bitfinex funding-offer wire array is parsed in **two places** with
> **divergent index assumptions**.

- `auth_rest.parse_active_funding_offers` (`auth_rest.py:38-57`) — **correct**:
  `[0]=id [1]=symbol [2]=mts_create [4]=amount [10]=status [14]=rate [15]=period`.
  Validated against the real captured golden `fixtures/funding_offers_real.json`.
- `auth_ws._parse_foc` (`auth_ws.py:209-221`) — **wrong**: `[2]=symbol [7]=status
  [11]=rate [12]=period`. The hand-authored `fixtures/foc_*.json` were built to
  match these wrong indices, so unit tests were green while prod crashed on the
  real wire (`d[11]` is a placeholder `None` → `float(None)`).

Fixing only the WS indices (the literal Plan-1 spec line) leaves two independent
parsers that will drift again the next time either side needs another field. The
best-practice fix is to give the wire layout **a single source of truth**.

Secondary: `fcn` (funding credit) carried an unsound `offer_id_meta=d[14]`
mapping (`d[14]` is `mts_opening`, not an offer id — Bitfinex credit events do
not carry the originating offer id, confirmed `fill_tracker.py:8-9`). So fills
could never be mapped credit→offer via `fcn`. The authoritative fill signal is
`foc EXECUTED` (which *does* carry `venue_offer_id`).

## Design

### 1. Single source of truth for the funding-offer wire layout

New neutral module `external/bitfinex/funding_offer_row.py` owns the **only**
copy of the funding-offer array index layout:

```
[0]=id  [1]=symbol  [2]=mts_create  [3]=mts_update  [4]=amount
[10]=status  [14]=rate  [15]=period
```

```python
@dataclass(frozen=True, slots=True)
class FundingOfferRow:
    venue_offer_id: str
    symbol: str
    mts_create: int
    mts_update: int
    amount: Decimal        # signed at venue; consumers abs() as needed
    status: str
    rate: float | None     # tolerant: at-market / placeholder rows carry None
    period_days: int | None

def parse_funding_offer_row(o: list[Any]) -> FundingOfferRow: ...
```

- **Strict** on `venue_offer_id`, `symbol`, `status`, `amount` (correctness
  fields). **Tolerant** None-guard on `rate`/`period_days` (display/audit only;
  `rate` is explicitly never used in financial arithmetic per
  `auth_rest.py:32`).
- `auth_rest.parse_active_funding_offers` builds `ActiveFundingOffer` from a
  `FundingOfferRow` (its dataclass and `mts_created=row.mts_create` projection
  unchanged → **reconcile consumers see zero type change**). It keeps its
  non-null `rate: float` contract by coercing `row.rate or 0.0` (active offers
  always carry a rate; the golden proves it).
- `auth_ws._parse_foc` builds `FocEvent` from the same `FundingOfferRow`
  (uses both `mts_create` and `mts_update`).
- One golden validates the layout; the two paths are now **structurally unable
  to drift**.

Module placement: neutral 3rd module (not in `auth_ws`/`auth_rest`) so neither
import direction deepens — `auth_rest` already imports `sign_request` from
`auth_ws`; both will import `funding_offer_row`.

### 2. `fcn` → informational

`auth_ws._parse_fcn`: **remove the `offer_id_meta` field** from `FcnEvent`
entirely (it was always wrong). Keep parsing `credit_id`/`symbol`/`amount`/`rate`
(cheap; reserved for future earnings tracking — a non-goal here).
`ws_dispatcher._translate_fcn` becomes a no-op returning `([], [], [])`. The
funding-credit array is a *different* wire object (not a funding offer), so it is
**not** covered by `parse_funding_offer_row`.

### 3. dispatcher: `foc EXECUTED` is the fill signal

`ws_dispatcher._translate_foc`:

- `EXECUTED` → `OrderFilled(credit_id=None, fill_rate=foc.rate, ...)`. `foc`
  carries the authoritative `venue_offer_id`; `OrderFilled.credit_id` is already
  `str | None` (`events.py:91`) so `None` is valid and the ledger effect
  (`reserved -= size; realized += size`) does not depend on it. Remove the
  current "EXECUTED is redundant, fcn handles it" no-op (`ws_dispatcher.py:133-139`).
- `CANCELED`/`EXPIRED` → `ReservationReleased` — **unchanged** (incl. the
  `recent_cancels` user_cancel-vs-venue_cancel window).
- Status matched by substring (`"EXECUTED" in status.upper()`), so robust to the
  venue's status-string format (`"EXECUTED @ 0.0005 (100.0)"`).

### 4. Remove the OOO staging buffer

Delete `_staging_buffer`, `OOO_STAGING_TTL_MS`, and the staging drain in
`_tick_maintenance` (`ws_dispatcher.py:211-212, 259-299`). Rationale (industry
OMS layering):

- **claim-before-foc is structurally guaranteed**: submit synchronously writes
  `CLAIMED`, and a `foc EXECUTED` can only arrive *after* the offer is live and
  matched (i.e. after submit returned). The race the buffer guarded existed only
  for the now-removed `fcn` mapping.
- **Plan 1 reconcile is the out-of-band safety net.** With an authoritative
  reconciler, an in-band reordering buffer is belt-and-suspenders that adds
  state, a TTL, and a latent bug surface (the existing
  `ws_dispatcher_ooo_drop ... _realized may be wrong` error path).
- `foc` for an unknown `voi` → info diag + drop → reconcile converges it.

`recent_cancels` tracking + its TTL cleanup in `_tick_maintenance` **stay**
(still needed for user_cancel classification).

### 5. Fixtures — capture-and-replay (VCR), never hand-authored

This is the methodology the incident proved necessary (hand-authored fixtures
embodied the wrong layout).

- **Land deterministically now:** rebuild `foc_*.json` / `fcn_*.json` *derived
  from the real layout* validated by `funding_offers_real.json` (real-derived —
  not invented from docs). The corrected `foc` array matches the funding-offer
  layout with `status` set to `EXECUTED`/`CANCELED`.
- **Gated-live-contract test** (extends the `test_funding_offers_wire.py`
  pattern): with `BFX_API_KEY`/`SECRET`, connect the auth WS, capture a real
  frame, and `BFX_UPDATE_FIXTURE=1` regenerates the golden + drift-compares the
  layout. `fos` (funding-offer snapshot) arrives **free on connect** (same array
  layout, no fill needed) → proves the WS≡REST layout assumption at zero cost. A
  real `foc EXECUTED` golden is captured opportunistically when the canary
  naturally fills (its absence is not blocking: substring status match + shared
  layout make the EXECUTED case deterministic from `fos`/REST data).

## Data flow

```
PENDING (reserved+=) → submit → CLAIMED
   ├── WS  foc EXECUTED            → OrderFilled            (seconds; OPTIONAL)
   ├── WS  foc CANCELED/EXPIRED    → ReservationReleased    (seconds; OPTIONAL)
   └── periodic reconcile (Plan 1) → ReservationReleased    (≤ interval; BACKBONE)
```

Both paths idempotent (registry RELEASED dedup + `venue_seq` dedup +
deterministic synthetic cids) → WS and reconcile may overlap safely.

## Error handling

- Tolerant parse: strict only on `voi`/`status`/`amount`; `rate`/`period`
  None-guarded. Parse failure → drop + WARN; correctness unaffected (backbone).
- `foc` unknown `voi` → info diag + drop; reconcile converges.
- WS fully dead → reconcile is sole convergence path (Plan 1 behaviour preserved).

## Testing (real-fixture methodology, per 2026-05-25)

| Layer | Cases |
|---|---|
| unit — shared row parser | real-layout field/type extraction; `rate`/`period` None tolerated; malformed/short row → raises |
| unit — WS parser | `foc`/`fcn` fixtures (real-derived) fast-contract (parses, fields/types correct); gated-live-contract (`@pytest.mark.integration`, venue drift via `BFX_UPDATE_FIXTURE`) |
| unit — dispatcher | `foc EXECUTED → OrderFilled(credit_id=None)`; `foc CANCELED/EXPIRED → ReservationReleased` (reasons incl. user_cancel window); `fcn → no-op` |
| regression | `parse_active_funding_offers` still green against existing `funding_offers_real.json` golden (proves the refactor is behaviour-preserving) |

Commit gate: `cd backend_py && uv run pytest -m "not integration"` green + mypy +
ruff. Live-contract tests excluded by default.

## Rollout

1. Land code; commit gate green.
2. (Opportunistic) capture real WS golden via the gated live test when keys +
   a canary fill window are available.
3. Deploy canary via the **manual** `scripts/deploy-koyeb.sh` (auto-deploy is
   off — `no_deploy_on_push=true`). No new env vars.
4. Verify: a canary fill produces an `ORDER_FILL` in `event_log` within seconds
   (not only at the reconcile interval); reconcile divergence WARN stops firing
   for fills the WS now catches.

## Non-goals / deferred (anti-gold-plating)

- **(d)** WS reconnect / `venue_seq` gap → immediate reconcile trigger — separate
  plan (touches `periodic_reconcile` + ws client, not the parser).
- **(e)** `position_state` snapshot table staleness (`reserved=450`/`updated_at`
  stuck at 06:00; runtime + `event_log` SoT remain correct) — unrelated
  projection bug, separate investigation.
- Credit / interest / earnings tracking (`fcn`/`fcu` amounts) — keep parsing
  `FcnEvent` but no consumer yet.
- Enabling `RestPollingFillTracker` as a faster detection layer.
