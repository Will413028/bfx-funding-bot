# Venue Reconcile Pre-Live Verification — Design

**Date:** 2026-05-25
**Status:** Approved (brainstorming)
**Context:** Plan 3 (PG event-store SoT migration) done. Before flipping to
`BFX_EXECUTOR=bitfinex_live`, the boot-time venue reconcile path
(`modules/execution/boot_recovery.py`) has only ever run against stubs and
hand-authored mocks. The one thing those cannot cover — the **signed
`POST /v2/auth/r/funding/offers/{symbol}` round-trip** (HMAC-SHA384 signature
accepted by the venue, real response parses into `ActiveFundingOffer`) — must be
exercised against a real Bitfinex account.

## Goal

Close the only untested gap before going live: the **wire/auth round-trip**.
Establish two test layers around the Bitfinex funding-offers response so that:

1. Every commit verifies our parser against a **real captured response shape**
   (fast, deterministic, no network).
2. A **gated live test** can re-validate the real signed round-trip on demand
   (auth still accepted, venue wire format has not drifted from the fixture).

## Non-Goals

- **Reconcile branch logic** (orphan / missing / stale-PENDING) is already fully
  unit-tested in `tests/modules/execution/test_boot_recovery.py`
  (`test_orphan_at_venue_is_claimed`, `test_local_claimed_missing_from_venue_is_released`,
  `test_stale_pending_converges_to_failed`, plus noop/grace/retry/4xx). It is
  pure logic, independent of the wire — **do not re-test it here**.
- No PG writes, no bus publish, no offer placement/cancellation, no DB. The live
  test only issues the read-only signed offers-query.
- No bespoke verification script — the work lives entirely in pytest, using the
  repo's existing `respx` + `integration`-marker conventions.
- No periodic reconciliation, no divergence alerting, no multi-symbol
  auto-discovery (anti-gold-plating; deferred).

## Why this design (industry context)

Testing an external integration splits into two layers that must never collapse
into one:

| Layer | Hits real API? | Runs in default CI? | Data source | Catches |
|---|---|---|---|---|
| Fast contract | No (MockTransport) | Yes (every commit) | **captured real fixture** | parse/layout regressions in our code |
| Live contract (gated) | **Yes** | No (`-m "not integration"` excludes) | live | venue wire drift; auth still valid |

- **Do not make the default suite hit the live API:** it would be flaky
  (network, rate limits, account state, venue uptime), require secrets in CI,
  couple the build to a third party, and be non-reproducible. CI must be
  deterministic.
- **The root-cause fix** for this repo's recurring wire bugs (APL dot-notation,
  columnar union null-bleed) is that the fast-layer mock *data* must be
  **captured from a real response**, not hand-authored assumptions. Mocks never
  caught those bugs because the hand-written data already embodied the wrong
  assumption.
- **No funding sandbox exists** (Bitfinex paper trading does not replicate
  funding — only TESTBTC/TESTUSD exchange securities), so the live layer must run
  against the real account; there is no testnet fallback.
- The repo's `integration` marker is explicitly "marks tests requiring
  network/live services" and is deselected by default — the slot for a live test
  already exists; it has simply never held a real-network test until now.

## Deliverables

| Artifact | Layer |
|---|---|
| `backend_py/tests/external/bitfinex/fixtures/funding_offers_real.json` | captured real response (golden file) |
| `backend_py/tests/external/bitfinex/test_funding_offers_wire.py::test_fixture_parses` | fast contract (default CI) |
| `backend_py/tests/external/bitfinex/test_funding_offers_wire.py::test_live_contract` | gated live contract (`@pytest.mark.integration`) |

## Architecture / behaviour

Reuse production code — no logic duplication. The relevant parser:
`parse_active_funding_offers(raw)` — positional-array parser; indices `[0]`=offer_id,
`[1]`=symbol, `[2]`=mts_created, `[4]`=amount, `[10]`=status, `[14]`=rate,
`[15]`=period; `_MIN_ROW_LEN=16`; raises `BitfinexShapeError` on malformed rows.
Produces `ActiveFundingOffer(venue_offer_id, symbol, amount, rate, period_days,
mts_created, status)`.

### `test_fixture_parses` (fast, default suite)

1. Load `funding_offers_real.json`.
2. `parse_active_funding_offers(raw)` must not raise.
3. Assert positional-layout invariants hold: each row is a list with
   `len >= _MIN_ROW_LEN`; parsed fields have the expected types
   (`venue_offer_id: str`, `amount: Decimal >= 0`, `period_days: int`,
   `status: str`). This is the permanent regression guard against our parser
   drifting from the real shape.

### `test_live_contract` (gated, `@pytest.mark.integration`)

Env-gated on `BFX_API_KEY` / `BFX_API_SECRET`; `pytest.skip` with a clear message
if absent (so it is safe to invoke without keys).

1. Build a real `httpx.AsyncClient` + `BitfinexAuthREST` + `Credentials` /
   `AccountContext` from env (mirroring `daemon.py`).
2. `await get_active_funding_offers(ctx, symbol)` against the real
   `api.bitfinex.com` — the signed round-trip. Must return 2xx and parse without
   `BitfinexShapeError` (signature accepted + wire format valid).
3. **Golden-file mode** (standard snapshot pattern):
   - If `BFX_UPDATE_FIXTURE=1` **or** the fixture file does not exist → write the
     raw response JSON to `funding_offers_real.json` (capture/update mode).
   - Otherwise → drift-compare: assert the live response still satisfies the same
     positional-layout invariants as the committed fixture (row shape / indices),
     surfacing venue wire drift. (Compare structure, not values — offers change.)
4. With `--capture=no`, print the parsed offers so the user can eyeball their
   real account state during the pre-live run.

## Error handling

- Missing keys → `pytest.skip` (the live test is opt-in).
- `BitfinexAPIError` (4xx/transport) from the real call → test fails with status +
  body (real finding: auth/signature/connectivity broken).
- `BitfinexShapeError` or layout-invariant mismatch → test fails, printing the
  offending row (wire-format drift — exactly what we want surfaced).

## Testing & CI behaviour

- `test_fixture_parses` runs in the default `-m "not integration"` gate (the
  commit gate) and in CI on every push.
- `test_live_contract` is excluded by default; it runs only when explicitly
  selected with real keys, or in a future scheduled canary.
- The fixture is seeded with a representative sample matching the documented
  Bitfinex layout so `test_fixture_parses` is green from day one; the user's first
  live capture (`BFX_UPDATE_FIXTURE=1`) replaces it with real data, which is then
  committed.

## The actual (B) deliverable — manual pre-live run

```bash
cd backend_py && \
  BFX_API_KEY=... BFX_API_SECRET=... BFX_UPDATE_FIXTURE=1 \
  uv run pytest -m integration -k funding_offers --capture=no
```

Run against the real account (read-scoped key recommended). Expected: PASS,
real offers printed, `funding_offers_real.json` written. Then commit the captured
fixture — from then on `test_fixture_parses` guards the real shape in CI and
`test_live_contract` (without `BFX_UPDATE_FIXTURE`) detects future venue drift.

## Deferred (anti-gold-plating)

- Periodic reconciliation beyond boot-time.
- Divergence metrics / alerting on orphan/missing counts.
- Multi-symbol auto-discovery (test takes a symbol, default `fUSD`).
- Scheduled canary wiring for `test_live_contract`.
