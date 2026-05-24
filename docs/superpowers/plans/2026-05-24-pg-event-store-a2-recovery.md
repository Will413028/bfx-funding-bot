# PG Event-Store 3a-recovery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a signed Bitfinex active-offers query, a boot-time venue reconciliation that resolves crash-mid-flight `PENDING` intents and orphan/missing offers, and complete the synchronous SoT-write path so live WS/fill-tracker `OrderFilled` + `ReservationReleased` events land in Postgres.

**Architecture:** The venue (Bitfinex) is the ultimate truth for which funding offers exist; the PG `event_log` + snapshot is the SoT for *our* intent + accounting. At boot we reconcile local snapshot against venue truth **by `venue_offer_id`** (the only stable shared key — Bitfinex funding offers carry no client cid). Crash-mid-flight `PENDING` intents that can't be matched converge to `FAILED` (capital-neutral); any real offer they created is independently captured by orphan-claim. The 3a-write `EventStorePersister` (persist-then-publish, synchronous, in-command-path) is reused at the WS/fill-tracker source so every money-mutating event is durable before in-memory projection.

**Tech Stack:** Python 3.13, SQLAlchemy 2.0 async, httpx, pytest + pytest-asyncio, testcontainers Postgres. Run from `backend_py/`.

---

## Design Decisions (locked — 2026-05-24)

These resolve open premises in the spec. They were confirmed with the user under the explicit "pre-launch → refactor to best practice" mandate.

1. **Recovery model = Reconciliation-by-venue_offer_id (not cid-match).**
   Bitfinex *Submit Funding Offer* accepts no `cid` param and the *Active Funding Offers* response has no cid field (verified against docs; the funding-offer array places undocumented placeholders at index 20, which the old `fill_tracker.py:141` wrongly read as cid). Therefore spec §6 step 3 "對每個 state=PENDING 的 cid 向 venue 查" is **physically impossible** — our cid never reaches the venue. The industry-standard answer when a provider lacks client-id round-trip is **reconciliation against the provider's authoritative records by the provider's own id** (vs Stripe's idempotency-key world). Heuristic attribute-matching (size/rate/mts) is explicitly rejected as a fragile anti-pattern. See Task 3/4.

2. **Complete the synchronous SoT write path for WS-sourced events.**
   3a-write retired `PostgresEventSink`, which left **both** `ReservationReleased` (§13.1 item 7) **and** live `OrderFilled` (WS `fcn`) with no PG persistence — only paper synchronous fills land in PG. Both are money-mutating and must be durable. Fix = persist-then-publish at the source (`ws_dispatcher`, `fill_tracker`), reusing `EventStorePersister`. No revived bus-subscriber sink (event-store-append-in-command-path is the correct pattern). See Tasks 5–6.

3. **Signed query lives in a new `BitfinexAuthREST`, not on `BitfinexREST`.**
   Spec §13.1 item 5 names `BitfinexREST.get_active_funding_offers()`, but `BitfinexREST` is the *public* client wired to `https://api-pub.bitfinex.com` (`daemon.py:129`) with no credentials. Authenticated reads require `https://api.bitfinex.com` + HMAC-SHA384. A dedicated read client mirrors `BitfinexLiveExecutor`'s auth pattern and keeps public/auth concerns separate. See Tasks 1–2.

4. **No schema migration.** Recovery only reads/writes the existing `event_log`, `offer_claims`, `position_state` tables. `RESERVATION_FAILED → FAILED` and `RESERVATION_RELEASED → RELEASED` projection branches already exist (`store.py:32-38`, `:209-216`). `alembic check` must remain clean (verified in Task 9).

5. **Recovery runs once at the start of `Daemon.run()`, gated to live** (`not spec.is_simulated`). It persists corrections in one txn (no REST call held inside the txn — venue is fetched before opening the session), then publishes `CLAIMED`/`RELEASED` to the in-memory bus so the already-loaded `ledger`/`offer_registry` projections update (matches `ReservationEmittingMiddleware` persist-then-publish; `FAILED` is not published — no in-memory subscriber, capital untouched). Idempotent across boots: once an orphan is `CLAIMED` / a claim is `RELEASED` / a PENDING is `FAILED`, the next boot's `from_snapshot` loads the terminal state and the diff naturally excludes it.

---

## File Structure

**New files:**
- `src/bfx_funding_bot/external/bitfinex/auth_rest.py` — `ActiveFundingOffer` dataclass, pure `parse_active_funding_offers()`, `BitfinexAuthREST` I/O shell (signed POST). Responsibility: authenticated venue reads.
- `src/bfx_funding_bot/modules/execution/boot_recovery.py` — `LocalClaim` struct, synth-id helpers, pure `compute_recovery_actions()`, `BootRecovery` shell. Responsibility: boot reconciliation orchestration.
- `tests/external/bitfinex/test_auth_rest.py`
- `tests/modules/execution/test_boot_recovery.py`
- `tests/integration/test_boot_recovery_pg.py`
- `tests/external/bitfinex/test_source_persistence.py`

**Modified files:**
- `src/bfx_funding_bot/external/bitfinex/ws_dispatcher.py` — inject `persister`, persist-then-publish in `_process`.
- `src/bfx_funding_bot/external/bitfinex/fill_tracker.py` — inject `persister`, persist-then-publish in `_diff_and_emit`.
- `src/bfx_funding_bot/modules/marketfeed/daemon.py` — hoist `persister` build; thread it into `fill_tracker` + `ws_dispatcher`; build `BitfinexAuthREST` + `BootRecovery` (live-gated); add `Daemon.boot_recovery` field + `run()` call.
- `docs/superpowers/specs/2026-05-23-postgres-event-store-sot-migration-design.md` — mark §13.1 items 5 & 7 resolved, record the cid-roundtrip finding.

---

## Task 1: ActiveFundingOffer + pure parser

**Files:**
- Create: `src/bfx_funding_bot/external/bitfinex/auth_rest.py`
- Test: `tests/external/bitfinex/test_auth_rest.py`

Bitfinex funding-offer array indices (verified against docs): `[0]`=ID, `[1]`=SYMBOL, `[2]`=MTS_CREATED, `[4]`=AMOUNT, `[10]`=STATUS, `[14]`=RATE, `[15]`=PERIOD. We require `len >= 16` (need index 15). There is **no** cid field.

- [ ] **Step 1: Write the failing test**

```python
# tests/external/bitfinex/test_auth_rest.py
from decimal import Decimal

import pytest

from bfx_funding_bot.external.bitfinex.auth_rest import (
    ActiveFundingOffer,
    parse_active_funding_offers,
)
from bfx_funding_bot.external.bitfinex.errors import BitfinexShapeError


def _row(offer_id=12345, symbol="fUSD", mts=1_700_000_000_000, amount=-100.0,
         status="ACTIVE", rate=0.00031, period=2):
    # 21-element funding offer row; only documented indices are meaningful.
    row = [None] * 21
    row[0] = offer_id
    row[1] = symbol
    row[2] = mts
    row[4] = amount        # negative for an offer; we store abs
    row[5] = amount
    row[10] = status
    row[14] = rate
    row[15] = period
    return row


def test_parse_happy_path():
    offers = parse_active_funding_offers([_row()])
    assert offers == [
        ActiveFundingOffer(
            venue_offer_id="12345", symbol="fUSD", amount=Decimal("100.0"),
            rate=0.00031, period_days=2, mts_created=1_700_000_000_000,
            status="ACTIVE",
        )
    ]


def test_parse_empty():
    assert parse_active_funding_offers([]) == []


def test_parse_rejects_non_list():
    with pytest.raises(BitfinexShapeError):
        parse_active_funding_offers({"not": "a list"})


def test_parse_rejects_short_row():
    with pytest.raises(BitfinexShapeError):
        parse_active_funding_offers([[1, "fUSD", 0]])  # len < 16
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend_py && uv run pytest tests/external/bitfinex/test_auth_rest.py -q`
Expected: FAIL — `ModuleNotFoundError: bfx_funding_bot.external.bitfinex.auth_rest`

- [ ] **Step 3: Write `ActiveFundingOffer` + `parse_active_funding_offers`**

```python
# src/bfx_funding_bot/external/bitfinex/auth_rest.py
"""Authenticated Bitfinex REST reads (Phase 4.4c / 3a-recovery).

Separate from the public BitfinexREST (api-pub.bitfinex.com): authenticated
endpoints require api.bitfinex.com + HMAC-SHA384 signing (auth_ws.sign_request)
+ per-account credentials. Mirrors BitfinexLiveExecutor's auth pattern but for
read-side recovery queries.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Callable

import httpx

from bfx_funding_bot.external.bitfinex.auth_ws import sign_request
from bfx_funding_bot.external.bitfinex.errors import BitfinexAPIError, BitfinexShapeError
from bfx_funding_bot.modules.execution.protocols import AccountContext

log = logging.getLogger(__name__)

BITFINEX_AUTH_REST_BASE = "https://api.bitfinex.com"
_FUNDING_OFFERS_PATH = "v2/auth/r/funding/offers"  # /{symbol} appended; no leading slash (sign_request prepends /api/)

# Funding-offer array indices (Bitfinex docs). No cid field exists.
_MIN_ROW_LEN = 16


@dataclass(frozen=True, slots=True)
class ActiveFundingOffer:
    venue_offer_id: str
    symbol: str
    amount: Decimal     # absolute size in quote currency (offers are negative-signed at venue)
    rate: float
    period_days: int
    mts_created: int
    status: str


def parse_active_funding_offers(raw: Any) -> list[ActiveFundingOffer]:
    """Parse Bitfinex GET-auth funding-offers response → list[ActiveFundingOffer]."""
    if not isinstance(raw, list):
        raise BitfinexShapeError(
            f"expected list of funding offers, got {type(raw).__name__}: {raw!r}"
        )
    out: list[ActiveFundingOffer] = []
    for o in raw:
        if not isinstance(o, list) or len(o) < _MIN_ROW_LEN:
            raise BitfinexShapeError(f"funding offer row malformed: {o!r}")
        out.append(ActiveFundingOffer(
            venue_offer_id=str(o[0]),
            symbol=str(o[1]),
            amount=abs(Decimal(str(o[4]))),
            rate=float(o[14]),
            period_days=int(o[15]),
            mts_created=int(o[2]),
            status=str(o[10]),
        ))
    return out
```

- [ ] **Step 4: Run test to verify parser passes**

Run: `cd backend_py && uv run pytest tests/external/bitfinex/test_auth_rest.py -q`
Expected: PASS (4 tests)

- [ ] **Step 5: Commit**

```bash
git add backend_py/src/bfx_funding_bot/external/bitfinex/auth_rest.py backend_py/tests/external/bitfinex/test_auth_rest.py
git commit -m "✨ Feat: ActiveFundingOffer + pure parse_active_funding_offers (3a-recovery)"
```

---

## Task 2: BitfinexAuthREST signed query (I/O shell)

**Files:**
- Modify: `src/bfx_funding_bot/external/bitfinex/auth_rest.py`
- Test: `tests/external/bitfinex/test_auth_rest.py`

Signing contract (`auth_ws.sign_request`, verified): HMAC-SHA384 over `"/api/" + path + nonce + body`; returns `{bfx-nonce, bfx-signature}`. Caller adds `bfx-apikey` + `Content-Type`. `path` has **no** leading slash. Auth reads send body `{}`.

- [ ] **Step 1: Write the failing test (signing + request shape via MockTransport)**

```python
# append to tests/external/bitfinex/test_auth_rest.py
import httpx
import pytest

from bfx_funding_bot.external.bitfinex.auth_rest import BitfinexAuthREST
from bfx_funding_bot.modules.execution.protocols import AccountContext, Credentials


def _ctx():
    return AccountContext(
        account_id="default",
        credentials=Credentials(api_key="KEY", api_secret="SECRET"),
        allocation_cap_usdt=Decimal("1000"),
    )


@pytest.mark.asyncio
async def test_get_active_funding_offers_signs_and_parses():
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["method"] = request.method
        captured["headers"] = dict(request.headers)
        captured["content"] = request.content
        return httpx.Response(200, json=[_row()])

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http:
        client = BitfinexAuthREST(http=http, nonce_provider=lambda: 111)
        offers = await client.get_active_funding_offers(ctx=_ctx(), symbol="fUSD")

    assert offers[0].venue_offer_id == "12345"
    assert captured["method"] == "POST"
    assert captured["url"] == "https://api.bitfinex.com/v2/auth/r/funding/offers/fUSD"
    headers = captured["headers"]
    assert headers["bfx-apikey"] == "KEY"
    assert headers["bfx-nonce"] == "111"
    assert "bfx-signature" in headers and len(headers["bfx-signature"]) == 96  # sha384 hexdigest
    assert captured["content"] == b"{}"


@pytest.mark.asyncio
async def test_get_active_funding_offers_raises_on_http_error():
    transport = httpx.MockTransport(lambda r: httpx.Response(500, text="boom"))
    async with httpx.AsyncClient(transport=transport) as http:
        client = BitfinexAuthREST(http=http, nonce_provider=lambda: 1)
        with pytest.raises(BitfinexAPIError):
            await client.get_active_funding_offers(ctx=_ctx())
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend_py && uv run pytest tests/external/bitfinex/test_auth_rest.py -q -k get_active`
Expected: FAIL — `BitfinexAuthREST` has no attribute / not defined.

- [ ] **Step 3: Implement `BitfinexAuthREST`**

```python
# append to src/bfx_funding_bot/external/bitfinex/auth_rest.py
class BitfinexAuthREST:
    """Authenticated read client for Bitfinex funding endpoints."""

    def __init__(
        self,
        *,
        http: httpx.AsyncClient,
        base_url: str = BITFINEX_AUTH_REST_BASE,
        nonce_provider: Callable[[], int] | None = None,
    ) -> None:
        self._http = http
        self._base_url = base_url.rstrip("/")
        import time
        self._nonce_provider = nonce_provider or (lambda: int(time.time() * 1_000_000))

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
        return parse_active_funding_offers(resp.json())
```

Move the `import time` to module top (ruff will flag the inline import). Final top imports: add `import time`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd backend_py && uv run pytest tests/external/bitfinex/test_auth_rest.py -q`
Expected: PASS (6 tests)

- [ ] **Step 5: Lint + typecheck**

Run: `cd backend_py && uv run ruff check src/bfx_funding_bot/external/bitfinex/auth_rest.py && uv run mypy src/bfx_funding_bot/external/bitfinex/auth_rest.py`
Expected: no errors

- [ ] **Step 6: Commit**

```bash
git add backend_py/src/bfx_funding_bot/external/bitfinex/auth_rest.py backend_py/tests/external/bitfinex/test_auth_rest.py
git commit -m "✨ Feat: BitfinexAuthREST.get_active_funding_offers signed query (3a-recovery)"
```

---

## Task 3: Recovery synth helpers + pure `compute_recovery_actions`

**Files:**
- Create: `src/bfx_funding_bot/modules/execution/boot_recovery.py`
- Test: `tests/modules/execution/test_boot_recovery.py`

This is the pure decision core (mirrors `registry_offers.transition()`): given venue offers + local claims, return the ordered list of domain events to append. No I/O.

- [ ] **Step 1: Write the failing test**

```python
# tests/modules/execution/test_boot_recovery.py
from decimal import Decimal
from uuid import UUID, uuid4

from bfx_funding_bot.external.bitfinex.auth_rest import ActiveFundingOffer
from bfx_funding_bot.modules.execution.boot_recovery import (
    LocalClaim,
    compute_recovery_actions,
    synth_orphan_cid,
    synth_orphan_scid,
)
from bfx_funding_bot.modules.execution.events import (
    ReservationClaimed,
    ReservationFailed,
    ReservationReleased,
)
from bfx_funding_bot.modules.execution.registry_offers import RegistryState

_ACC = "default"
_NOW = 2_000_000


def _offer(voi="555", amount="100", rate=0.0003, period=2):
    return ActiveFundingOffer(
        venue_offer_id=voi, symbol="fUSD", amount=Decimal(amount),
        rate=rate, period_days=period, mts_created=1_000_000, status="ACTIVE",
    )


def _claim(cid, voi, state, size="100", scid=None, occurred=0):
    return LocalClaim(
        cid=cid, venue_offer_id=voi, state=state, size_usdt=Decimal(size),
        signal_correlation_id=scid or uuid4(), occurred_at_ms=occurred,
    )


def _actions(venue, local, grace_ms=120_000, now=_NOW):
    return compute_recovery_actions(
        venue_offers=venue, local_claims=local, account_id=_ACC,
        is_simulated=False, now_ms=now, grace_ms=grace_ms,
    )


def test_orphan_at_venue_is_claimed():
    acts = _actions([_offer(voi="555", amount="250")], [])
    assert len(acts) == 1
    ev = acts[0]
    assert isinstance(ev, ReservationClaimed)
    assert ev.venue_offer_id == "555"
    assert ev.size_usdt == Decimal("250")
    assert ev.cid == synth_orphan_cid("555")
    assert ev.signal_correlation_id == synth_orphan_scid("555")
    assert ev.account_id == _ACC and ev.is_simulated is False


def test_local_claimed_missing_from_venue_is_released():
    scid = uuid4()
    claim = _claim(cid=42, voi="999", state=RegistryState.CLAIMED, size="80", scid=scid)
    acts = _actions([], [claim])
    assert len(acts) == 1
    ev = acts[0]
    assert isinstance(ev, ReservationReleased)
    assert ev.cid == 42 and ev.venue_offer_id == "999"
    assert ev.size_usdt == Decimal("80") and ev.reason == "missing_from_venue"
    assert ev.signal_correlation_id == scid


def test_claimed_still_present_is_noop():
    claim = _claim(cid=42, voi="555", state=RegistryState.CLAIMED)
    assert _actions([_offer(voi="555")], [claim]) == []


def test_stale_pending_converges_to_failed():
    scid = uuid4()
    claim = _claim(cid=7, voi=None, state=RegistryState.PENDING, size="60",
                   scid=scid, occurred=_NOW - 200_000)  # older than grace
    acts = _actions([], [claim], grace_ms=120_000)
    assert len(acts) == 1
    ev = acts[0]
    assert isinstance(ev, ReservationFailed)
    assert ev.cid == 7 and ev.size_usdt == Decimal("60")
    assert ev.reason == "unresolved_at_boot" and ev.signal_correlation_id == scid


def test_recent_pending_within_grace_is_left_alone():
    claim = _claim(cid=7, voi=None, state=RegistryState.PENDING,
                   occurred=_NOW - 1_000)  # within grace
    assert _actions([], [claim], grace_ms=120_000) == []


def test_synth_cid_numeric_voi_is_int():
    assert synth_orphan_cid("12345") == 12345


def test_synth_scid_is_deterministic():
    assert synth_orphan_scid("555") == synth_orphan_scid("555")
    assert synth_orphan_scid("555") != synth_orphan_scid("556")
    assert isinstance(synth_orphan_scid("555"), UUID)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend_py && uv run pytest tests/modules/execution/test_boot_recovery.py -q`
Expected: FAIL — module not found.

- [ ] **Step 3: Implement the pure core**

```python
# src/bfx_funding_bot/modules/execution/boot_recovery.py
"""Boot-time venue reconciliation (Phase 4.4c / 3a-recovery).

The venue (Bitfinex) is the ultimate truth for which funding offers exist;
the PG event_log + snapshot is the SoT for our intent + accounting. Bitfinex
funding offers carry NO client cid (submit drops it, response lacks it), so we
reconcile by venue_offer_id — the only stable shared key.

  - orphan  (venue has voi, local has no CLAIMED row)  -> ReservationClaimed
  - missing (local CLAIMED, venue no longer has voi)   -> ReservationReleased
  - stale PENDING (write-ahead intent, unresolvable)   -> ReservationFailed

PENDING can't be matched to the venue (no cid round-trip), so it converges to
FAILED after reconcile — capital-neutral, because the actual offer (if the
submit reached the venue) is captured independently by orphan-claim.
"""
from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from decimal import Decimal
from uuid import UUID, uuid5

from bfx_funding_bot.external.bitfinex.auth_rest import ActiveFundingOffer
from bfx_funding_bot.external.bitfinex.cid import BITFINEX_CID_MAX
from bfx_funding_bot.modules.execution.events import (
    ReservationClaimed,
    ReservationFailed,
    ReservationReleased,
)
from bfx_funding_bot.modules.execution.registry_offers import RegistryState

log = logging.getLogger(__name__)

# Fixed namespace for deterministic synthetic correlation ids on reconciled
# orphans (offers with no originating local signal).
_RECOVERY_SCID_NS = UUID("3a000000-0000-4000-8000-000000000001")


@dataclass(frozen=True, slots=True)
class LocalClaim:
    cid: int
    venue_offer_id: str | None
    state: RegistryState
    size_usdt: Decimal
    signal_correlation_id: UUID
    occurred_at_ms: int


def synth_orphan_cid(venue_offer_id: str) -> int:
    """Stable synthetic cid for an orphan claim. Bitfinex offer ids are numeric;
    use directly so re-running reconcile upserts the same offer_claims row.
    Non-numeric fallback hashes the id into the int63 cid space."""
    try:
        return int(venue_offer_id)
    except ValueError:
        digest = hashlib.blake2b(venue_offer_id.encode(), digest_size=8).digest()
        return int.from_bytes(digest, "big") & BITFINEX_CID_MAX


def synth_orphan_scid(venue_offer_id: str) -> UUID:
    """Deterministic correlation id for a reconciled orphan (no local signal)."""
    return uuid5(_RECOVERY_SCID_NS, venue_offer_id)


def compute_recovery_actions(
    *,
    venue_offers: list[ActiveFundingOffer],
    local_claims: list[LocalClaim],
    account_id: str,
    is_simulated: bool,
    now_ms: int,
    grace_ms: int,
) -> list[object]:
    """Pure reconciliation: produce the ordered list of domain events to append."""
    venue_by_voi = {o.venue_offer_id: o for o in venue_offers}
    claimed_by_voi = {
        c.venue_offer_id: c
        for c in local_claims
        if c.state == RegistryState.CLAIMED and c.venue_offer_id is not None
    }
    actions: list[object] = []

    # orphan: venue has it, local CLAIMED set doesn't -> claim (reserved += size)
    for voi, offer in venue_by_voi.items():
        if voi not in claimed_by_voi:
            actions.append(ReservationClaimed(
                cid=synth_orphan_cid(voi), venue_offer_id=voi,
                size_usdt=offer.amount, signal_correlation_id=synth_orphan_scid(voi),
                account_id=account_id, is_simulated=is_simulated, occurred_at_ms=now_ms,
            ))

    # missing: local CLAIMED, venue gone -> release (reserved -= size)
    for voi, claim in claimed_by_voi.items():
        if voi not in venue_by_voi:
            actions.append(ReservationReleased(
                cid=claim.cid, venue_offer_id=voi, size_usdt=claim.size_usdt,
                reason="missing_from_venue", signal_correlation_id=claim.signal_correlation_id,
                account_id=account_id, is_simulated=is_simulated, occurred_at_ms=now_ms,
            ))

    # stale PENDING (crash-mid-flight, unmatchable) -> FAILED (capital-neutral)
    for c in local_claims:
        if c.state == RegistryState.PENDING and (now_ms - c.occurred_at_ms) >= grace_ms:
            actions.append(ReservationFailed(
                cid=c.cid, size_usdt=c.size_usdt,
                signal_correlation_id=c.signal_correlation_id,
                account_id=account_id, is_simulated=is_simulated,
                reason="unresolved_at_boot", occurred_at_ms=now_ms,
            ))

    return actions
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd backend_py && uv run pytest tests/modules/execution/test_boot_recovery.py -q`
Expected: PASS (7 tests)

- [ ] **Step 5: Commit**

```bash
git add backend_py/src/bfx_funding_bot/modules/execution/boot_recovery.py backend_py/tests/modules/execution/test_boot_recovery.py
git commit -m "✨ Feat: pure compute_recovery_actions + synth-id helpers (3a-recovery)"
```

---

## Task 4: `BootRecovery` shell (load claims, fetch+retry, persist, publish)

**Files:**
- Modify: `src/bfx_funding_bot/modules/execution/boot_recovery.py`
- Test: `tests/integration/test_boot_recovery_pg.py` (real PG — exercises store projection)

The shell: fetch venue offers (with transient retry → fail-safe on exhaustion), read `offer_claims` for the account/env, call the pure core, append each action in **one** txn via the store, then publish `CLAIMED`/`RELEASED` (not `FAILED`) to the bus for in-memory projections.

- [ ] **Step 1: Write the failing integration test**

```python
# tests/integration/test_boot_recovery_pg.py
"""Boot reconciliation integration tests — real Postgres (testcontainers).

Run: cd backend_py && uv run pytest tests/integration/test_boot_recovery_pg.py -q -m integration
"""
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import select

from bfx_funding_bot.external.bitfinex.auth_rest import ActiveFundingOffer
from bfx_funding_bot.modules.execution.boot_recovery import BootRecovery, synth_orphan_cid
from bfx_funding_bot.modules.execution.bus import DomainEventBus
from bfx_funding_bot.modules.execution.event_store.persister import EventStorePersister
from bfx_funding_bot.modules.execution.event_store.store import PostgresEventStore
from bfx_funding_bot.modules.execution.event_store.tables import (
    OfferClaimRow,
    PositionStateRow,
)
from bfx_funding_bot.modules.execution.events import (
    ReservationClaimed,
    ReservationIntent,
)
from bfx_funding_bot.modules.execution.protocols import AccountContext, Credentials

pytestmark = pytest.mark.integration
_ENV = "ci"
_ACC = "default"


class _StubAuthRest:
    def __init__(self, offers): self._offers = offers
    async def get_active_funding_offers(self, *, ctx, symbol="fUSD"):
        return self._offers


def _ctx():
    return AccountContext(
        account_id=_ACC, credentials=Credentials(api_key="k", api_secret="s"),
        allocation_cap_usdt=Decimal("10000"),
    )


def _recovery(offers, store, session_factory, *, clock=lambda: 5_000_000):
    return BootRecovery(
        store=store, session_factory=session_factory, auth_rest=_StubAuthRest(offers),
        account_ctx=_ctx(), deployment_environment=_ENV, bus=DomainEventBus(),
        is_simulated=False, grace_ms=120_000, clock=clock,
    )


async def _claims(session_factory):
    async with session_factory() as s:
        return (await s.execute(
            select(OfferClaimRow).where(OfferClaimRow.deployment_environment == _ENV)
        )).scalars().all()


async def _reserved(session_factory) -> Decimal:
    async with session_factory() as s:
        row = (await s.execute(
            select(PositionStateRow).where(PositionStateRow.deployment_environment == _ENV)
        )).scalar_one_or_none()
        return Decimal(str(row.reserved_usdt)) if row else Decimal("0")


@pytest.mark.asyncio
async def test_orphan_at_venue_is_claimed_and_reserved(pg_session_factory):
    store = PostgresEventStore(deployment_environment=_ENV)
    offers = [ActiveFundingOffer("777", "fUSD", Decimal("250"), 0.0003, 2, 1_000, "ACTIVE")]
    await _recovery(offers, store, pg_session_factory).run()

    claims = await _claims(pg_session_factory)
    assert len(claims) == 1
    assert claims[0].cid == synth_orphan_cid("777")
    assert claims[0].state == "claimed" and claims[0].venue_offer_id == "777"
    assert await _reserved(pg_session_factory) == Decimal("250")


@pytest.mark.asyncio
async def test_crash_mid_flight_pending_converges_failed(pg_session_factory):
    store = PostgresEventStore(deployment_environment=_ENV)
    persister = EventStorePersister(store=store, session_factory=pg_session_factory)
    # seed a PENDING intent (txn1 happened, crash before outcome), INTENT old enough
    await persister.persist(ReservationIntent(
        cid=7, size_usdt=Decimal("60"), signal_correlation_id=uuid4(),
        account_id=_ACC, is_simulated=False, occurred_at_ms=1_000,
    ))
    await _recovery([], store, pg_session_factory).run()

    claims = await _claims(pg_session_factory)
    assert len(claims) == 1 and claims[0].state == "failed"
    assert await _reserved(pg_session_factory) == Decimal("0")  # untouched


@pytest.mark.asyncio
async def test_missing_from_venue_releases(pg_session_factory):
    store = PostgresEventStore(deployment_environment=_ENV)
    persister = EventStorePersister(store=store, session_factory=pg_session_factory)
    scid = uuid4()
    await persister.persist(
        ReservationIntent(cid=42, size_usdt=Decimal("80"), signal_correlation_id=scid,
                          account_id=_ACC, is_simulated=False, occurred_at_ms=1_000),
        ReservationClaimed(cid=42, venue_offer_id="999", size_usdt=Decimal("80"),
                          signal_correlation_id=scid, account_id=_ACC, is_simulated=False,
                          occurred_at_ms=2_000),
    )
    assert await _reserved(pg_session_factory) == Decimal("80")

    await _recovery([], store, pg_session_factory).run()  # venue has nothing now

    claims = await _claims(pg_session_factory)
    assert claims[0].state == "released"
    assert await _reserved(pg_session_factory) == Decimal("0")


@pytest.mark.asyncio
async def test_recovery_is_idempotent(pg_session_factory):
    store = PostgresEventStore(deployment_environment=_ENV)
    offers = [ActiveFundingOffer("777", "fUSD", Decimal("250"), 0.0003, 2, 1_000, "ACTIVE")]
    await _recovery(offers, store, pg_session_factory).run()
    await _recovery(offers, store, pg_session_factory).run()  # second boot

    assert await _reserved(pg_session_factory) == Decimal("250")  # not double-counted
    assert len(await _claims(pg_session_factory)) == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend_py && uv run pytest tests/integration/test_boot_recovery_pg.py -q -m integration`
Expected: FAIL — `BootRecovery` has no `__init__`/`run` matching usage.

- [ ] **Step 3: Implement `BootRecovery`**

```python
# append to src/bfx_funding_bot/modules/execution/boot_recovery.py
import time
from typing import Any, Callable, Protocol

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bfx_funding_bot.core.db import session_scope
from bfx_funding_bot.core.errors import ExecutorTransientError
from bfx_funding_bot.modules.execution.event_store.store import PostgresEventStore
from bfx_funding_bot.modules.execution.event_store.tables import OfferClaimRow
from bfx_funding_bot.modules.execution.protocols import AccountContext
from bfx_funding_bot.modules.execution.retry import transient_retry


class _ActiveOffersQuery(Protocol):
    async def get_active_funding_offers(
        self, *, ctx: AccountContext, symbol: str = "fUSD",
    ) -> list[ActiveFundingOffer]: ...


class _Bus(Protocol):
    async def publish(self, event: Any) -> None: ...


class BootRecovery:
    """Boot orchestration: venue reconcile + resolve crash-mid-flight PENDING.

    Runs once at the start of Daemon.run(), live only. Persists corrections in
    one txn (venue is fetched BEFORE opening the session — no REST inside txn),
    then publishes CLAIMED/RELEASED to the bus for in-memory projections.
    """

    def __init__(
        self,
        *,
        store: PostgresEventStore,
        session_factory: async_sessionmaker[AsyncSession],
        auth_rest: _ActiveOffersQuery,
        account_ctx: AccountContext,
        deployment_environment: str,
        bus: _Bus,
        is_simulated: bool = False,
        symbol: str = "fUSD",
        grace_ms: int = 120_000,
        clock: Callable[[], int] | None = None,
    ) -> None:
        self._store = store
        self._session_factory = session_factory
        self._auth_rest = auth_rest
        self._ctx = account_ctx
        self._env = deployment_environment
        self._bus = bus
        self._is_simulated = is_simulated
        self._symbol = symbol
        self._grace_ms = grace_ms
        self._clock = clock or (lambda: int(time.time() * 1000))

    async def run(self) -> None:
        venue_offers = await self._fetch_offers()  # may raise -> daemon fail-safe
        async with session_scope(self._session_factory) as session:
            local_claims = await self._load_local_claims(session)
            actions = compute_recovery_actions(
                venue_offers=venue_offers, local_claims=local_claims,
                account_id=self._ctx.account_id, is_simulated=self._is_simulated,
                now_ms=self._clock(), grace_ms=self._grace_ms,
            )
            for ev in actions:
                await self._store.append(session, ev)
        # publish in-memory projection events AFTER durable commit (FAILED has no subscriber)
        n_claim = n_release = n_fail = 0
        for ev in actions:
            if isinstance(ev, ReservationClaimed):
                n_claim += 1
                await self._safe_publish(ev)
            elif isinstance(ev, ReservationReleased):
                n_release += 1
                await self._safe_publish(ev)
            elif isinstance(ev, ReservationFailed):
                n_fail += 1
        log.info(
            "boot_recovery_complete venue_offers=%d orphans_claimed=%d released=%d pending_failed=%d",
            len(venue_offers), n_claim, n_release, n_fail,
        )

    async def _fetch_offers(self) -> list[ActiveFundingOffer]:
        wrapped = transient_retry(self._auth_rest.get_active_funding_offers)
        try:
            return await wrapped(ctx=self._ctx, symbol=self._symbol)
        except ExecutorTransientError:
            log.error("boot_recovery_venue_unreachable — failing startup (cannot reconcile)")
            raise

    async def _load_local_claims(self, session: AsyncSession) -> list[LocalClaim]:
        rows = (await session.execute(
            select(OfferClaimRow).where(
                OfferClaimRow.account_id == self._ctx.account_id,
                OfferClaimRow.deployment_environment == self._env,
            )
        )).scalars().all()
        return [
            LocalClaim(
                cid=r.cid, venue_offer_id=r.venue_offer_id,
                state=RegistryState(r.state), size_usdt=Decimal(str(r.size_usdt)),
                signal_correlation_id=UUID(r.signal_correlation_id),
                occurred_at_ms=r.occurred_at_ms,
            )
            for r in rows
        ]

    async def _safe_publish(self, event: object) -> None:
        try:
            await self._bus.publish(event)
        except Exception as exc:  # noqa: BLE001
            log.critical(
                "boot_recovery_publish_failed event=%s err=%r — projection lost, SoT persisted",
                type(event).__name__, exc,
            )
```

Move `import time` + the `sqlalchemy`/typing imports to the module top with the Task 3 imports (ruff: no mid-file imports). Confirm `transient_retry` raises `ExecutorTransientError` on exhaustion (it does — see `live_executor.cancel` usage). If the retry helper expects a positional-arg callable, adapt the wrap to `lambda: self._auth_rest.get_active_funding_offers(ctx=self._ctx, symbol=self._symbol)`.

- [ ] **Step 4: Run integration tests**

Run: `cd backend_py && uv run pytest tests/integration/test_boot_recovery_pg.py -q -m integration`
Expected: PASS (4 tests)

- [ ] **Step 5: Lint + typecheck**

Run: `cd backend_py && uv run mypy src/bfx_funding_bot/modules/execution/boot_recovery.py && uv run ruff check src/bfx_funding_bot/modules/execution/boot_recovery.py`
Expected: no errors

- [ ] **Step 6: Commit**

```bash
git add backend_py/src/bfx_funding_bot/modules/execution/boot_recovery.py backend_py/tests/integration/test_boot_recovery_pg.py
git commit -m "✨ Feat: BootRecovery shell — venue reconcile + resolve PENDING (3a-recovery)"
```

---

## Task 5: ws_dispatcher persist-then-publish

**Files:**
- Modify: `src/bfx_funding_bot/external/bitfinex/ws_dispatcher.py` (`__init__` ~line 190; `_process` ~line 242)
- Test: `tests/external/bitfinex/test_source_persistence.py`

`translate_bfx_event` emits `OrderFilled` (from `fcn`) and `ReservationReleased` (from `foc`) — both money-mutating, both must be durable before in-memory fanout. Inject `persister`; persist before publish; on persist failure log critical + skip publish (avoid in-memory drifting ahead of PG; WS events carry `venue_seq` so re-delivery is deduped).

- [ ] **Step 1: Write the failing integration test**

```python
# tests/external/bitfinex/test_source_persistence.py
"""WS/fill-tracker source persistence — real Postgres (testcontainers).

Run: cd backend_py && uv run pytest tests/external/bitfinex/test_source_persistence.py -q -m integration
"""
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from bfx_funding_bot.external.bitfinex.auth_ws import FcnEvent
from bfx_funding_bot.external.bitfinex.ws_dispatcher import BitfinexLiveWSDispatcher
from bfx_funding_bot.modules.execution.bus import DomainEventBus
from bfx_funding_bot.modules.execution.event_store.persister import EventStorePersister
from bfx_funding_bot.modules.execution.event_store.store import PostgresEventStore
from bfx_funding_bot.modules.execution.event_store.tables import EventLogRow, PositionStateRow
from bfx_funding_bot.modules.execution.events import ReservationClaimed, ReservationIntent
from bfx_funding_bot.modules.execution.registry_offers import ClaimRecord, OfferRegistry, RegistryState

pytestmark = pytest.mark.integration
_ENV = "ci"
_ACC = "default"


class _StubWSClient:
    def __init__(self, events): self._events = events
    async def events(self):
        for e in self._events:
            yield e


def _registry_with_claim(voi, cid, scid, size):
    reg = OfferRegistry.__new__(OfferRegistry)
    reg._axiom_query = None  # type: ignore[attr-defined]
    reg._clock = lambda: 0
    reg._snapshot = {voi: ClaimRecord(
        venue_offer_id=voi, cid=cid, signal_correlation_id=scid,
        size_usdt=Decimal(str(size)), account_id=_ACC, state=RegistryState.CLAIMED,
        occurred_at_ms=0, last_updated_ms=0,
    )}
    return reg


class _NoopAxiom:
    async def emit(self, event): return None


@pytest.mark.asyncio
async def test_ws_fcn_orderfilled_persisted_before_publish(pg_session_factory):
    store = PostgresEventStore(deployment_environment=_ENV)
    persister = EventStorePersister(store=store, session_factory=pg_session_factory)
    # seed a CLAIMED row so the offer is reserved (reserved=100)
    scid = uuid4()
    await persister.persist(
        ReservationIntent(cid=11, size_usdt=Decimal("100"), signal_correlation_id=scid,
                         account_id=_ACC, is_simulated=False, occurred_at_ms=1),
        ReservationClaimed(cid=11, venue_offer_id="888", size_usdt=Decimal("100"),
                          signal_correlation_id=scid, account_id=_ACC, is_simulated=False,
                          occurred_at_ms=2),
    )
    fcn = FcnEvent(credit_id=1, symbol="fUSD", side=1, mts_create=10, mts_update=10,
                   amount=Decimal("100"), rate=0.0003, period_days=2,
                   offer_id_meta=888, raw_seq=99)
    import asyncio
    bus = DomainEventBus()
    dispatcher = BitfinexLiveWSDispatcher(
        ws_client=_StubWSClient([fcn]), registry=_registry_with_claim("888", 11, scid, 100),
        bus=bus, axiom=_NoopAxiom(), persister=persister,
    )
    stop = asyncio.Event()
    task = asyncio.create_task(dispatcher.run(stop))
    await asyncio.sleep(0.2)
    stop.set()
    await task

    async with pg_session_factory() as s:
        n = (await s.execute(select(func.count()).select_from(EventLogRow).where(
            EventLogRow.event_type == "ORDER_FILL"))).scalar_one()
        ps = (await s.execute(select(PositionStateRow))).scalar_one()
    assert n == 1
    assert Decimal(str(ps.reserved_usdt)) == Decimal("0")    # reserved -= 100
    assert Decimal(str(ps.realized_usdt)) == Decimal("100")  # realized += 100
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend_py && uv run pytest tests/external/bitfinex/test_source_persistence.py -q -m integration -k ws_fcn`
Expected: FAIL — `BitfinexLiveWSDispatcher.__init__` got unexpected kwarg `persister`.

- [ ] **Step 3: Inject `persister` + persist-then-publish**

In `ws_dispatcher.py`, add the import near the top:

```python
from bfx_funding_bot.modules.execution.event_store.persister import EventPersister, NoopEventPersister
```

In `__init__` (after `queue_max: int = 10_000,` param), add a param and store it:

```python
        persister: EventPersister | None = None,
```
```python
        self._persister = persister or NoopEventPersister()
```

Replace the publish loop in `_process` (currently the `for ev in events:` block, ~line 258-263) with:

```python
        for ev in events:
            await self._persist_then_publish(ev)
```

Add the helper method on the class:

```python
    async def _persist_then_publish(self, ev: Any) -> None:
        """Durable SoT write before in-memory fanout. WS events carry venue_seq
        so re-delivery is deduped by the store; on persist failure we skip
        publish to keep in-memory projections from drifting ahead of PG."""
        try:
            await self._persister.persist(ev)
        except Exception as e:  # noqa: BLE001
            log.critical("ws_dispatcher_persist_failed err=%r event=%s — SoT write lost, skipping publish",
                         e, type(ev).__name__)
            return
        try:
            await self._bus.publish(ev)
        except Exception as e:  # noqa: BLE001
            log.critical("ws_dispatcher_publish_failed err=%r event=%s", e, type(ev).__name__)
```

`NoopEventPersister` keeps every existing ws_dispatcher unit test (which constructs without a persister) green.

- [ ] **Step 4: Run new test + existing ws_dispatcher unit tests**

Run: `cd backend_py && uv run pytest tests/external/bitfinex/test_source_persistence.py -q -m integration -k ws_fcn && uv run pytest tests/external/bitfinex/ -q -m "not integration" -k dispatcher`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend_py/src/bfx_funding_bot/external/bitfinex/ws_dispatcher.py backend_py/tests/external/bitfinex/test_source_persistence.py
git commit -m "✨ Feat: ws_dispatcher persist-then-publish OrderFilled/ReservationReleased (3a-recovery)"
```

---

## Task 6: fill_tracker persist-then-publish

**Files:**
- Modify: `src/bfx_funding_bot/external/bitfinex/fill_tracker.py` (`__init__` ~line 71; `_diff_and_emit` ~line 194)
- Test: `tests/external/bitfinex/test_source_persistence.py`

The REST polling fill tracker (reconciliation backup) publishes `ReservationReleased` on offer disappearance. Persist it before publishing, same pattern.

- [ ] **Step 1: Write the failing test (append to test_source_persistence.py)**

```python
# append to tests/external/bitfinex/test_source_persistence.py
import asyncio

from bfx_funding_bot.external.bitfinex.fill_tracker import RestPollingFillTracker
from bfx_funding_bot.modules.execution.event_store.tables import EventLogRow as _ELR  # alias if needed
from bfx_funding_bot.modules.marketfeed.health_monitor import HealthProbe
from bfx_funding_bot.modules.marketfeed.schemas import Phase, StrategyName


class _OneTickHttp:
    """First /offers call returns one offer, then it disappears."""
    def __init__(self):
        self._calls = 0
    async def get(self, path):
        from unittest.mock import MagicMock
        resp = MagicMock()
        resp.status_code = 200
        if path.endswith("/credits"):
            resp.json = lambda: []
            return resp
        # offers: present on first poll, gone afterwards
        self._calls += 1
        if self._calls == 1:
            row = [None] * 21
            row[0] = 888; row[5] = -100.0; row[20] = 11  # legacy o[20]; ignored by recovery
            resp.json = lambda: [row]
        else:
            resp.json = lambda: []
        return resp


@pytest.mark.asyncio
async def test_fill_tracker_release_persisted_before_publish(pg_session_factory):
    store = PostgresEventStore(deployment_environment=_ENV)
    persister = EventStorePersister(store=store, session_factory=pg_session_factory)
    scid = uuid4()
    await persister.persist(
        ReservationIntent(cid=11, size_usdt=Decimal("100"), signal_correlation_id=scid,
                         account_id=_ACC, is_simulated=False, occurred_at_ms=1),
        ReservationClaimed(cid=11, venue_offer_id="888", size_usdt=Decimal("100"),
                          signal_correlation_id=scid, account_id=_ACC, is_simulated=False,
                          occurred_at_ms=2),
    )
    tracker = RestPollingFillTracker(
        http=_OneTickHttp(), axiom=_NoopAxiom(), probe=HealthProbe(), bus=DomainEventBus(),
        phase=Phase.PAPER, strategy=StrategyName.RATE_PERCENTILE, cell="bfx_USDT",
        account_id=_ACC, registry=_registry_with_claim("888", 11, scid, 100),
        persister=persister, poll_interval_s=0.05,
    )
    stop = asyncio.Event()
    task = asyncio.create_task(tracker.poll_loop(stop))
    await asyncio.sleep(0.25)  # let two ticks run (present -> gone)
    stop.set()
    await task

    async with pg_session_factory() as s:
        n = (await s.execute(select(func.count()).select_from(EventLogRow).where(
            EventLogRow.event_type == "RESERVATION_RELEASED"))).scalar_one()
        ps = (await s.execute(select(PositionStateRow))).scalar_one()
    assert n == 1
    assert Decimal(str(ps.reserved_usdt)) == Decimal("0")
```

(`Phase` / `StrategyName` enum member names: confirm against `marketfeed/schemas.py`; adjust `Phase.PAPER` / `StrategyName.RATE_PERCENTILE` if the actual members differ.)

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend_py && uv run pytest tests/external/bitfinex/test_source_persistence.py -q -m integration -k fill_tracker`
Expected: FAIL — `RestPollingFillTracker.__init__` got unexpected kwarg `persister`.

- [ ] **Step 3: Inject `persister` + persist-then-publish**

In `fill_tracker.py` add the import:

```python
from bfx_funding_bot.modules.execution.event_store.persister import EventPersister, NoopEventPersister
```

In `__init__` add a param (after `poll_interval_s: float = 30.0,`) and store it:

```python
        persister: EventPersister | None = None,
```
```python
        self._persister = persister or NoopEventPersister()
```

In `_diff_and_emit`, replace the `await self._bus.publish(ReservationReleased(...))` call with a build-then-persist-then-publish:

```python
            release = ReservationReleased(
                cid=claim.cid,
                venue_offer_id=venue_offer_id,
                size_usdt=claim.size_usdt,
                reason="missing_from_venue",
                signal_correlation_id=claim.signal_correlation_id,
                account_id=self.account_id,
                is_simulated=False,
                occurred_at_ms=int(time.time() * 1000),
            )
            try:
                await self._persister.persist(release)
            except Exception as e:  # noqa: BLE001
                log.critical("fill_tracker_persist_failed err=%r voi=%s — skipping publish",
                             e, venue_offer_id)
                continue
            await self._bus.publish(release)
```

- [ ] **Step 4: Run new test + existing fill_tracker tests**

Run: `cd backend_py && uv run pytest tests/external/bitfinex/test_source_persistence.py -q -m integration -k fill_tracker && uv run pytest tests/external/bitfinex/test_fill_tracker.py tests/external/bitfinex/test_fill_tracker_registry_aware.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend_py/src/bfx_funding_bot/external/bitfinex/fill_tracker.py backend_py/tests/external/bitfinex/test_source_persistence.py
git commit -m "✨ Feat: fill_tracker persist-then-publish ReservationReleased (3a-recovery)"
```

---

## Task 7: Daemon wiring — hoist persister, build recovery (live-gated), thread persister

**Files:**
- Modify: `src/bfx_funding_bot/modules/marketfeed/daemon.py`
  - `Daemon` dataclass (~line 145-174): add field
  - `Daemon.run()` (~line 176-216): call recovery before TaskGroup
  - `build_daemon` (~line 595-1049): hoist `persister`, thread into `fill_tracker` + `ws_dispatcher`, build `BitfinexAuthREST` + `BootRecovery`
- Test: `tests/integration/test_daemon_pg_cutover.py` (extend) or new daemon-boot test

- [ ] **Step 1: Add imports at the top of daemon.py**

```python
from bfx_funding_bot.external.bitfinex.auth_rest import BitfinexAuthREST
from bfx_funding_bot.modules.execution.boot_recovery import BootRecovery
```

- [ ] **Step 2: Add `boot_recovery` field to the `Daemon` dataclass**

After `ws_dispatcher: BitfinexLiveWSDispatcher | None = None` (line 170), add:

```python
    boot_recovery: BootRecovery | None = None
```

- [ ] **Step 3: Call recovery at the start of `Daemon.run()`**

In `run()`, immediately before `async with asyncio.TaskGroup() as tg:` (line 188), add:

```python
        # 3a-recovery: reconcile against venue + resolve crash-mid-flight PENDING
        # BEFORE any sub-task starts (live only; paper leaves this None).
        if self.boot_recovery is not None:
            await self.boot_recovery.run()
```

- [ ] **Step 4: Hoist `persister` construction**

Currently `persister = EventStorePersister(...)` is built at line ~815. Move it to right after `event_store = PostgresEventStore(...)` (line 687), before the `from_snapshot` block:

```python
    event_store = PostgresEventStore(deployment_environment=env_str)
    persister = EventStorePersister(store=event_store, session_factory=session_factory)
```

Delete the old `persister = EventStorePersister(...)` line near 815 (now redundant).

- [ ] **Step 5: Thread `persister` into `fill_tracker` construction**

In the `fill_tracker = RestPollingFillTracker(...)` block (line 782), add `persister=persister,` to the kwargs.

- [ ] **Step 6: Thread `persister` into `ws_dispatcher` construction**

In the `ws_dispatcher = BitfinexLiveWSDispatcher(...)` block (line 1014), add `persister=persister,` to the kwargs.

- [ ] **Step 7: Build `BitfinexAuthREST` + `BootRecovery` (live-gated)**

After `executor: ExecutorPort = spec.executor` (line 778) — where `spec` is known — add:

```python
    # 3a-recovery: live-only venue reconciliation. Paper/shadow have no real
    # venue offers (BFX_FILL_TRACKER/WS gated off) -> boot_recovery stays None.
    boot_recovery: BootRecovery | None = None
    if not spec.is_simulated:
        auth_rest = BitfinexAuthREST(http=bitfinex_http)
        boot_recovery = BootRecovery(
            store=event_store,
            session_factory=session_factory,
            auth_rest=auth_rest,
            account_ctx=account_ctx,
            deployment_environment=env_str,
            bus=bus,
            is_simulated=spec.is_simulated,
        )
```

(`bus` is created at line 766, just before `build_executor`; this block sits after both, so `bus` is in scope.)

- [ ] **Step 8: Pass `boot_recovery` to the `Daemon(...)` constructor**

In the `return Daemon(...)` block (line 1022-1049), add:

```python
        boot_recovery=boot_recovery,
```

- [ ] **Step 9: Run the daemon-boot + executor-chain integration tests**

Run: `cd backend_py && uv run pytest tests/integration/test_daemon_pg_cutover.py tests/modules/marketfeed/test_daemon_phase43_executor_chain.py -q`
Expected: PASS (paper path: `boot_recovery` is None; recovery skipped; no regression)

- [ ] **Step 10: Lint + typecheck the daemon**

Run: `cd backend_py && uv run mypy src/bfx_funding_bot/modules/marketfeed/daemon.py && uv run ruff check src/bfx_funding_bot/modules/marketfeed/daemon.py`
Expected: no errors

- [ ] **Step 11: Commit**

```bash
git add backend_py/src/bfx_funding_bot/modules/marketfeed/daemon.py
git commit -m "✅ Feat: wire BootRecovery + thread persister into ws_dispatcher/fill_tracker (3a-recovery)"
```

---

## Task 8: Full-suite verification + cross-cutting integration

**Files:**
- Test: existing suites + the new ones

- [ ] **Step 1: Run the full non-integration unit suite**

Run: `cd backend_py && uv run pytest -m "not integration" -q`
Expected: all PASS (no regressions from the ws_dispatcher/fill_tracker signature changes — `NoopEventPersister` default covers callers that don't pass one)

- [ ] **Step 2: Run the full integration suite against ephemeral docker PG**

Run: `cd backend_py && uv run pytest -m integration -q`
Expected: all PASS. New: `test_boot_recovery_pg.py` (4), `test_source_persistence.py` (2). (Only the testcontainers PG is touched — Neon is never contacted.)

- [ ] **Step 3: Confirm no schema drift**

Run: `cd backend_py && uv run alembic check`
Expected: "No new upgrade operations detected." — confirms 3a-recovery added no schema (Design Decision 4).

- [ ] **Step 4: Full typecheck + lint**

Run: `cd backend_py && uv run mypy src/ && uv run ruff check`
Expected: no errors

- [ ] **Step 5: Commit (only if any fixups were needed)**

```bash
git add -A backend_py
git commit -m "✅ Test: full suite green for 3a-recovery (unit + integration + alembic check)"
```

---

## Task 9: Update spec — mark §13.1 items 5 & 7 resolved + record cid finding

**Files:**
- Modify: `docs/superpowers/specs/2026-05-23-postgres-event-store-sot-migration-design.md`

- [ ] **Step 1: Append a resolution note to §13.1**

Add after item 7 (line ~326):

```markdown
8. **3a-recovery shipped (2026-05-24).** Resolves items 5 & 7. Key finding: Bitfinex
   funding offers carry **no client cid** (submit drops it; response has no cid field —
   index 20 is an undocumented placeholder the old `fill_tracker` misread). So §6 step 3
   "match PENDING by cid at venue" is impossible. Recovery is therefore **reconciliation
   by `venue_offer_id`** (the venue's own id, the only stable shared key):
     - orphan (venue has, local CLAIMED doesn't) → claim w/ synth cid (`int(voi)`),
       reserved += size;
     - missing (local CLAIMED, venue gone) → ReservationReleased, reserved -= size
       (this is item 7's release-side persistence);
     - stale PENDING (unmatchable) → FAILED, capital-neutral (the real offer, if any,
       is captured by orphan-claim).
   Signed query lives in a new `BitfinexAuthREST` (api.bitfinex.com + HMAC), not the
   public `BitfinexREST` (api-pub). Item 7 extended to also persist live WS `fcn` →
   OrderFilled (same sink-retirement gap); both now persist-then-publish at the source
   via `EventStorePersister`. No schema migration. Cutover §6 step 3 superseded by this
   reconciliation model.
```

- [ ] **Step 2: Update the §13.1 closing order line**

The "3a 後修訂的 Plan 3 順序" line (line ~328) — append `(done 2026-05-24)` to `3a-recovery`.

- [ ] **Step 3: Commit**

```bash
git add docs/superpowers/specs/2026-05-23-postgres-event-store-sot-migration-design.md
git commit -m "📝 Docs: record 3a-recovery cid-roundtrip finding + reconcile-by-voi model (§13.1)"
```

---

## Self-Review

**Spec coverage:**
- §6 step 3 (resolve PENDING) → Tasks 3-4 (reconcile-by-voi; PENDING→FAILED). The cid-match mechanism is replaced (Design Decision 1) — documented in Task 9.
- §6 step 4 (venue reconcile: orphan-claim + mark released) → Tasks 3-4.
- §13.1 item 5 (signed offers-query) → Tasks 1-2 (`BitfinexAuthREST`).
- §13.1 item 7 (RESERVATION_RELEASED PG persistence) → reconcile-release (Task 4) + WS/fill-tracker source persistence (Tasks 5-6). Extended to OrderFilled (Design Decision 2).
- §10 (venue unreachable at boot) → Task 4 `_fetch_offers` transient retry → fail-safe.
- Idempotency (re-boot safety) → Task 4 `test_recovery_is_idempotent`.

**Placeholder scan:** No TBD/"add error handling"/"similar to". Every code step has full code; every test step has runnable assertions.

**Type consistency:** `ActiveFundingOffer` (Task 1) consumed by `compute_recovery_actions` + `BootRecovery` (Tasks 3-4) + tests (Task 4). `LocalClaim`, `synth_orphan_cid`, `synth_orphan_scid` defined Task 3, used Tasks 3-4. `EventPersister`/`NoopEventPersister` (existing `persister.py`) injected in Tasks 5-6, threaded in Task 7. `persister` variable hoisted (Task 7 step 4) before its first use in `fill_tracker` (step 5) / `ws_dispatcher` (step 6). `boot_recovery` field (Task 7 step 2) set in step 7, consumed step 3 + step 8.

**Open verification points for the executor (confirm at implementation time, not blockers):**
- `transient_retry`'s call convention (positional vs kw) — adapt the wrap in Task 4 step 3 if it requires a zero-arg callable.
- `Phase` / `StrategyName` enum member names in `marketfeed/schemas.py` — fix the Task 6 test literals if they differ.
- `BitfinexShapeError` constructor signature in `external/bitfinex/errors.py` — Task 1 assumes a single positional message; adjust if it's keyworded.

---

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-05-24-pg-event-store-a2-recovery.md`. Two execution options:

1. **Subagent-Driven (recommended)** — dispatch a fresh subagent per task, review between tasks, fast iteration. Mechanical tasks (1, 3, 5, 6, 9) → sonnet; auth/integration/migration-adjacent tasks (2, 4, 7, 8) → capable model. Migration verified only against ephemeral docker PG; Neon untouched.
2. **Inline Execution** — execute tasks in this session using executing-plans, batch execution with checkpoints.

Which approach?
