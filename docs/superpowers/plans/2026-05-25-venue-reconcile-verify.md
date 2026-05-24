# Venue Reconcile Pre-Live Verification Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a fast wire-format contract test (default CI) plus a gated live contract test (real Bitfinex signed round-trip) for the active-funding-offers response, so the only untested pre-live gap — the real signed offers-query wire/auth path — is closeable on demand and guarded against drift forever.

**Architecture:** Two test layers around `parse_active_funding_offers`. The fast layer replays a captured real response fixture through the production parser on every commit. The gated layer (`@pytest.mark.integration`) hits the real API, validates parse, and uses golden-file capture/compare. A small behavior-preserving refactor exposes the raw response body so the live test can capture the fixture while still exercising real HMAC signing.

**Tech Stack:** Python 3.13, pytest (`asyncio_mode = "auto"`), httpx + `MockTransport`, existing `BitfinexAuthREST` / `parse_active_funding_offers`. No new dependencies.

**Spec:** `docs/superpowers/specs/2026-05-25-venue-reconcile-verify-design.md`

**Working dir for all commands:** `backend_py/` (`cd backend_py` first — see repo CLAUDE.md).

---

## File Structure

- **Modify** `src/bfx_funding_bot/external/bitfinex/auth_rest.py` — split `get_active_funding_offers` into `fetch_funding_offers_raw` (signed transport → raw JSON) + parse delegation. Behavior of `get_active_funding_offers` is unchanged.
- **Modify** `tests/external/bitfinex/test_auth_rest.py` — one test for the new raw method.
- **Create** `tests/external/bitfinex/fixtures/funding_offers_real.json` — seed sample in the documented 21-element layout (replaced by a real capture later).
- **Create** `tests/external/bitfinex/test_funding_offers_wire.py` — `test_fixture_parses` (fast) + `test_live_contract` (gated).

---

## Task 1: Expose raw funding-offers body (behavior-preserving refactor)

**Files:**
- Modify: `src/bfx_funding_bot/external/bitfinex/auth_rest.py` (the `get_active_funding_offers` method, currently ending at the `return parse_active_funding_offers(body)` line)
- Test: `tests/external/bitfinex/test_auth_rest.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/external/bitfinex/test_auth_rest.py` (the helpers `_row`, `_ctx` already exist in this file):

```python
@pytest.mark.asyncio
async def test_fetch_funding_offers_raw_returns_unparsed_body():
    rows = [_row(offer_id=1), _row(offer_id=2)]
    transport = httpx.MockTransport(lambda r: httpx.Response(200, json=rows))
    async with httpx.AsyncClient(transport=transport) as http:
        client = BitfinexAuthREST(http=http, nonce_provider=lambda: 1)
        raw = await client.fetch_funding_offers_raw(ctx=_ctx(), symbol="fUSD")
    # raw is the positional-array body, NOT parsed ActiveFundingOffer objects
    assert raw == rows
    assert isinstance(raw, list) and isinstance(raw[0], list)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend_py && uv run pytest tests/external/bitfinex/test_auth_rest.py::test_fetch_funding_offers_raw_returns_unparsed_body -v`
Expected: FAIL — `AttributeError: 'BitfinexAuthREST' object has no attribute 'fetch_funding_offers_raw'`.

- [ ] **Step 3: Refactor `auth_rest.py`**

Replace the existing `get_active_funding_offers` method body. Find:

```python
    async def get_active_funding_offers(
        self, *, ctx: AccountContext, symbol: str = "fUSD",
    ) -> list[ActiveFundingOffer]:
        """POST /v2/auth/r/funding/offers/{symbol} (signed). Returns active offers.

        Raises BitfinexAPIError on transport/HTTP error, BitfinexShapeError on
        malformed body.
        """
        path = f"{_FUNDING_OFFERS_PATH}/{symbol}"
        body_bytes = json.dumps({}).encode("utf-8")
        nonce = self._nonce_provider()
        headers = sign_request(
            body=body_bytes, nonce=nonce,
            api_secret=ctx.credentials.api_secret, path=path,
        )
        headers["bfx-apikey"] = ctx.credentials.api_key
        headers["Content-Type"] = "application/json"
        try:
            resp = await self._http.post(
                f"{self._base_url}/{path}", content=body_bytes, headers=headers,
                timeout=30.0,
            )
        except httpx.HTTPError as e:
            raise BitfinexAPIError(status_code=0, message=f"transport error: {e}", raw=None) from e
        if resp.status_code >= 400:
            raise BitfinexAPIError(
                status_code=resp.status_code,
                message=resp.reason_phrase or "http error", raw=resp.text,
            )
        try:
            body = resp.json()
        except json.JSONDecodeError as e:
            raise BitfinexShapeError(f"invalid JSON in funding-offers response: {e}") from e
        return parse_active_funding_offers(body)
```

Replace with:

```python
    async def fetch_funding_offers_raw(
        self, *, ctx: AccountContext, symbol: str = "fUSD",
    ) -> Any:
        """POST /v2/auth/r/funding/offers/{symbol} (signed). Returns the raw
        decoded JSON body (list of positional arrays), before parsing.

        Separated from get_active_funding_offers so callers that need the raw
        wire payload (e.g. the live contract test capturing a fixture) still go
        through the real HMAC-signed transport.

        Raises BitfinexAPIError on transport/HTTP error, BitfinexShapeError on
        invalid JSON.
        """
        path = f"{_FUNDING_OFFERS_PATH}/{symbol}"
        body_bytes = json.dumps({}).encode("utf-8")
        nonce = self._nonce_provider()
        headers = sign_request(
            body=body_bytes, nonce=nonce,
            api_secret=ctx.credentials.api_secret, path=path,
        )
        headers["bfx-apikey"] = ctx.credentials.api_key
        headers["Content-Type"] = "application/json"
        try:
            resp = await self._http.post(
                f"{self._base_url}/{path}", content=body_bytes, headers=headers,
                timeout=30.0,
            )
        except httpx.HTTPError as e:
            raise BitfinexAPIError(status_code=0, message=f"transport error: {e}", raw=None) from e
        if resp.status_code >= 400:
            raise BitfinexAPIError(
                status_code=resp.status_code,
                message=resp.reason_phrase or "http error", raw=resp.text,
            )
        try:
            return resp.json()
        except json.JSONDecodeError as e:
            raise BitfinexShapeError(f"invalid JSON in funding-offers response: {e}") from e

    async def get_active_funding_offers(
        self, *, ctx: AccountContext, symbol: str = "fUSD",
    ) -> list[ActiveFundingOffer]:
        """POST /v2/auth/r/funding/offers/{symbol} (signed). Returns parsed
        active offers. Raises BitfinexAPIError / BitfinexShapeError."""
        raw = await self.fetch_funding_offers_raw(ctx=ctx, symbol=symbol)
        return parse_active_funding_offers(raw)
```

(`Any` is already imported at the top of `auth_rest.py` — `parse_active_funding_offers(raw: Any)` uses it.)

- [ ] **Step 4: Run the new test + the existing auth_rest tests**

Run: `cd backend_py && uv run pytest tests/external/bitfinex/test_auth_rest.py -v`
Expected: PASS — the new test plus `test_get_active_funding_offers_signs_and_parses` and `test_get_active_funding_offers_raises_on_http_error` (confirms behavior preserved).

- [ ] **Step 5: Type-check + lint**

Run: `cd backend_py && uv run mypy src/ && uv run ruff check`
Expected: clean (no new errors).

- [ ] **Step 6: Commit**

```bash
cd backend_py && git add src/bfx_funding_bot/external/bitfinex/auth_rest.py tests/external/bitfinex/test_auth_rest.py
git commit -m "♻️ Refactor: split fetch_funding_offers_raw from get_active_funding_offers

Exposes the raw signed-query body so the live contract test can capture a
real fixture while still exercising real HMAC signing. get_active_funding_offers
behavior unchanged (delegates to the new method + parse).

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 2: Seed fixture + fast contract test

**Files:**
- Create: `tests/external/bitfinex/fixtures/funding_offers_real.json`
- Create: `tests/external/bitfinex/test_funding_offers_wire.py`

- [ ] **Step 1: Create the seed fixture**

Create `tests/external/bitfinex/fixtures/funding_offers_real.json` with two offers in the documented 21-element Bitfinex funding-offer layout (indices: `[0]`=id, `[1]`=symbol, `[2]`=mts_created, `[4]`=amount [negative for an offer], `[10]`=status, `[14]`=rate, `[15]`=period). This makes the fast test green from day one; the user's first live capture replaces it.

```json
[
  [998001, "fUSD", 1716595200000, 1716595200000, -120.5, -120.5, "LIMIT", null, null, 0, "ACTIVE", null, null, null, 0.00028, 2, 0, 0, null, 0, null],
  [998002, "fUSD", 1716595260000, 1716595260000, -500.0, -500.0, "LIMIT", null, null, 0, "ACTIVE", null, null, null, 0.00035, 30, 0, 0, null, 0, null]
]
```

- [ ] **Step 2: Write the failing test**

Create `tests/external/bitfinex/test_funding_offers_wire.py`:

```python
"""Wire-format contract tests for Bitfinex active funding offers.

Two layers (see specs/2026-05-25-venue-reconcile-verify-design.md):
  - test_fixture_parses: fast, runs in the default CI gate. Replays a captured
    real response fixture through the production parser; guards the
    positional-array layout against parser drift.
  - test_live_contract: gated (@pytest.mark.integration). Real signed
    round-trip against api.bitfinex.com; golden-file capture / drift-compare.
"""
import json
import os
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from bfx_funding_bot.external.bitfinex.auth_rest import (
    ActiveFundingOffer,
    BitfinexAuthREST,
    parse_active_funding_offers,
)
from bfx_funding_bot.modules.execution.protocols import AccountContext, Credentials

_FIXTURE = Path(__file__).parent / "fixtures" / "funding_offers_real.json"


def _assert_offer_fields(o: ActiveFundingOffer) -> None:
    """Per-offer invariants implied by the positional-array contract."""
    assert isinstance(o.venue_offer_id, str) and o.venue_offer_id
    assert isinstance(o.amount, Decimal) and o.amount >= 0
    assert isinstance(o.period_days, int) and o.period_days > 0
    assert isinstance(o.status, str) and o.status


def test_fixture_parses():
    raw = json.loads(_FIXTURE.read_text())
    offers = parse_active_funding_offers(raw)  # raises BitfinexShapeError on layout drift
    assert offers, "seed fixture must contain at least one offer"
    for o in offers:
        _assert_offer_fields(o)
```

- [ ] **Step 3: Run test to verify it passes**

Run: `cd backend_py && uv run pytest tests/external/bitfinex/test_funding_offers_wire.py::test_fixture_parses -v`
Expected: PASS (fixture parses, 2 offers, fields sane).

- [ ] **Step 4: Sanity-check it actually guards layout — temporarily break the fixture**

Edit the fixture: shorten the first row to 3 elements `[998001, "fUSD", 1716595200000]`. Run the test again.
Expected: FAIL with `BitfinexShapeError` (row len < 16). **Then revert the fixture to the full 2-row version from Step 1.** Re-run: PASS.

- [ ] **Step 5: Lint**

Run: `cd backend_py && uv run ruff check tests/external/bitfinex/test_funding_offers_wire.py`
Expected: clean.

- [ ] **Step 6: Commit**

```bash
cd backend_py && git add tests/external/bitfinex/fixtures/funding_offers_real.json tests/external/bitfinex/test_funding_offers_wire.py
git commit -m "✅ Test: add fast wire-format contract test for funding offers

Replays a captured-shape fixture through parse_active_funding_offers on every
commit, guarding the Bitfinex positional-array layout against parser drift.
Seed fixture matches the documented 21-element layout; replaced by a real
capture after the live run.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 3: Gated live contract test

**Files:**
- Modify: `tests/external/bitfinex/test_funding_offers_wire.py` (append the gated test)

- [ ] **Step 1: Append the live contract test**

Add to the end of `tests/external/bitfinex/test_funding_offers_wire.py`:

```python
@pytest.mark.integration
@pytest.mark.asyncio
async def test_live_contract():
    """Real signed round-trip against api.bitfinex.com.

    Gated: skipped unless BFX_API_KEY / BFX_API_SECRET are set. Validates that
    the venue accepts our HMAC signature and the live response parses. Golden-file
    mode: with BFX_UPDATE_FIXTURE=1 (or a missing fixture) it writes the captured
    response; otherwise it drift-checks the live layout (structure, not values).

    Pre-live run (the actual (B) deliverable):
        BFX_API_KEY=... BFX_API_SECRET=... BFX_UPDATE_FIXTURE=1 \\
          uv run pytest -m integration -k funding_offers --capture=no
    """
    key = os.environ.get("BFX_API_KEY")
    secret = os.environ.get("BFX_API_SECRET")
    if not key or not secret:
        pytest.skip("requires BFX_API_KEY / BFX_API_SECRET for the live signed round-trip")
    symbol = os.environ.get("BFX_VERIFY_SYMBOL", "fUSD")

    ctx = AccountContext(
        account_id="verify",
        credentials=Credentials(api_key=key, api_secret=secret),
        allocation_cap_usdt=Decimal("0"),
    )
    async with httpx.AsyncClient() as http:
        client = BitfinexAuthREST(http=http)
        raw = await client.fetch_funding_offers_raw(ctx=ctx, symbol=symbol)

    # Real wire format must parse through production code (raises on drift).
    offers = parse_active_funding_offers(raw)
    print(f"\n[live_contract] symbol={symbol} offers={len(offers)}")
    for o in offers:
        print(f"  voi={o.venue_offer_id} amount={o.amount} rate={o.rate} "
              f"period={o.period_days}d status={o.status}")

    if os.environ.get("BFX_UPDATE_FIXTURE") == "1" or not _FIXTURE.exists():
        _FIXTURE.parent.mkdir(parents=True, exist_ok=True)
        _FIXTURE.write_text(json.dumps(raw, indent=2) + "\n")
        print(f"[live_contract] wrote fixture -> {_FIXTURE}")
    else:
        # Drift-compare: empty offers is a valid live state (account has none);
        # the round-trip + parse already validated the wire. Only check field
        # invariants on whatever offers exist.
        for o in offers:
            _assert_offer_fields(o)
```

- [ ] **Step 2: Verify it SKIPS cleanly without keys**

Run (no keys in env): `cd backend_py && env -u BFX_API_KEY -u BFX_API_SECRET uv run pytest tests/external/bitfinex/test_funding_offers_wire.py -m integration -v`
Expected: `test_live_contract` SKIPPED with the keys-required reason; `test_fixture_parses` deselected (not integration).

- [ ] **Step 3: Verify default gate excludes the live test**

Run: `cd backend_py && uv run pytest tests/external/bitfinex/test_funding_offers_wire.py -m "not integration" -v`
Expected: only `test_fixture_parses` runs and PASSES; `test_live_contract` deselected.

- [ ] **Step 4: Lint**

Run: `cd backend_py && uv run ruff check tests/external/bitfinex/test_funding_offers_wire.py`
Expected: clean.

- [ ] **Step 5: Commit**

```bash
cd backend_py && git add tests/external/bitfinex/test_funding_offers_wire.py
git commit -m "✅ Test: add gated live contract test for funding offers round-trip

@pytest.mark.integration (excluded from the default gate). Hits the real signed
offers-query, validates parse, and does golden-file capture (BFX_UPDATE_FIXTURE)
or drift-compare. This is the pre-live (B) verification, runnable on demand with
real keys; the captured fixture then guards the wire shape in CI forever.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Final verification

- [ ] **Run the full default gate (commit gate) to confirm nothing regressed**

Run: `cd backend_py && uv run pytest -m "not integration" -q`
Expected: all pass (previous 630 + the new `test_fixture_parses` + `test_fetch_funding_offers_raw_returns_unparsed_body`), 0 failed. `test_live_contract` deselected.

- [ ] **Confirm mypy + ruff clean**

Run: `cd backend_py && uv run mypy src/ && uv run ruff check`
Expected: clean.

---

## Manual step (post-merge, user-run — the actual (B) deliverable)

Not part of the automated plan. With a real read-scoped Bitfinex key:

```bash
cd backend_py && \
  BFX_API_KEY=... BFX_API_SECRET=... BFX_UPDATE_FIXTURE=1 \
  uv run pytest -m integration -k funding_offers --capture=no
```

Expected: PASS, real offers printed, `funding_offers_real.json` overwritten with the real response. Then `git add` + commit the captured fixture. From then on `test_fixture_parses` guards the real shape in CI, and `test_live_contract` (without `BFX_UPDATE_FIXTURE`) detects future venue drift.

---

## Notes / scope

- **Production change is intentional and minimal:** Task 1 splits one method in `auth_rest.py`; `get_active_funding_offers` behavior is unchanged (guarded by existing tests). The spec assumed pure-test work; this small refactor is the cleanest way to capture the raw wire body without duplicating the signed-POST glue.
- **3-path reconcile logic is NOT re-tested** — already covered by `tests/modules/execution/test_boot_recovery.py`.
- **Deferred (anti-gold-plating):** periodic reconciliation, divergence alerting, multi-symbol auto-discovery, scheduled canary wiring for `test_live_contract`.
