# Balance-Aware Cap Gate Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the deployment reconciler clamp how much it deploys to the venue's actual available funding-wallet balance, so it never submits an offer the venue must reject for insufficient funds (the 2026-05-29 cap>balance 90s failed-submit loop).

**Architecture:** Fetch wallet `available` in the same reconcile pass that already reads offers/credits → flow it through `ReconcileResult` → `PositionReconciled` event → the in-memory ledger. The reconciler reads `ledger.available_balance()` and clamps the deployable gap to `min(cap − exposure, available − buffer)` (primary, correctness). A `BuyingPowerGuard` in the safety chain is a per-offer defense-in-depth backstop. `available` is in-memory only — no PG column, no migration. Concentration cap stays bound to the policy cap.

**Tech Stack:** Python 3.13, asyncio, httpx, SQLAlchemy 2.0 async, pytest (`-m "not integration"`), Decimal arithmetic.

**Spec:** `docs/superpowers/specs/2026-05-30-balance-aware-cap-gate-design.md`

**Run all commands from** `backend_py/`: `cd /Users/will/second-brain/projects/startup/bfx-funding-bot/backend_py`. Tests: `uv run pytest ...`. Final gate: `uv run pytest -m "not integration"` + `uv run mypy src/` + `uv run ruff check`.

**Decisions locked in spec:** buffer fixed `BFX_BALANCE_BUFFER_USDT` default `3`; fetch failure → reconcile tick raises → `deploy()` not called (existing fail-closed path); `available` in-memory only; concentration cap unchanged; wallet currency = symbol without leading `f` (`fUST`→`UST`); gate is **live-only** (`DeploymentReconciler` built only under `if not spec.is_simulated`).

---

## File Structure

| File | Responsibility | Change |
|---|---|---|
| `src/bfx_funding_bot/external/bitfinex/auth_rest.py` | venue REST reads | + `FundingWallet`, `parse_wallets`, `BitfinexAuthREST.get_funding_available` |
| `src/bfx_funding_bot/modules/execution/deployment/sizing.py` | pure gap sizing | + `available_headroom` param to `allocate_gap` |
| `src/bfx_funding_bot/modules/execution/events.py` | domain events | + `available_usdt` on `PositionReconciled` |
| `src/bfx_funding_bot/modules/execution/ledger.py` | in-mem venue-truth mirror | + `_available`, set in `on_position_reconciled`, getter `available_balance()` |
| `src/bfx_funding_bot/modules/execution/boot_recovery.py` | reconcile observation pass | + `_WalletsQuery` protocol, `_fetch_wallets`, wire into `run()`, `ReconcileResult.available_usdt` |
| `src/bfx_funding_bot/modules/execution/deployment/reconciler.py` | single-writer controller | read `available_balance()`, clamp gap, `balance_buffer_usdt` ctor param |
| `src/bfx_funding_bot/modules/execution/safety/hard_guards.py` | L1 guards | + `BuyingPowerGuard` |
| `src/bfx_funding_bot/modules/marketfeed/daemon.py` | wiring | read buffer env, pass to reconciler, append guard |
| `scripts/deploy-koyeb.sh` | canary deploy | restore cap 550 → 570, drop stopgap comment |

**Task order (dependencies):** 1 (auth_rest) and 2 (sizing) are independent. 3 (event+ledger) precedes 4, 5, 6. 4 (boot_recovery) needs 1+3. 5 (reconciler) needs 2+3. 6 (guard) needs 3. 7 (daemon) needs 5+6. 8 (cap) independent. 9 verifies all.

---

## Task 1: `get_funding_available` venue read

**Files:**
- Modify: `src/bfx_funding_bot/external/bitfinex/auth_rest.py`
- Test: `tests/external/bitfinex/test_auth_rest_wallets.py` (create)

Bitfinex `POST /v2/auth/r/wallets` returns positional rows:
`[0]=WALLET_TYPE [1]=CURRENCY [2]=BALANCE [3]=UNSETTLED_INTEREST [4]=AVAILABLE_BALANCE …`.
**`available` is index 4**, not 3. It may be `null` (not yet calculated) → treat as `0` (conservative, fail-closed). Funding wallet currency for `fUST` is `UST`.

- [ ] **Step 1: Write the failing test** — create `tests/external/bitfinex/test_auth_rest_wallets.py`:

```python
"""FundingWallet parse + get_funding_available REST tests.

Parallels test_auth_rest_credits.py. Bitfinex /v2/auth/r/wallets positional layout:
  [0]=WALLET_TYPE [1]=CURRENCY [2]=BALANCE [3]=UNSETTLED_INTEREST [4]=AVAILABLE_BALANCE ...
"""
from __future__ import annotations

from decimal import Decimal

import httpx
import pytest

from bfx_funding_bot.external.bitfinex.auth_rest import (
    BitfinexAuthREST,
    FundingWallet,
    parse_wallets,
)
from bfx_funding_bot.external.bitfinex.errors import BitfinexAPIError, BitfinexShapeError
from bfx_funding_bot.modules.execution.protocols import AccountContext, Credentials


def _wallet_row(
    wallet_type: str = "funding",
    currency: str = "UST",
    balance: float = 550.0,
    available: object = 147.5,
) -> list:
    return [wallet_type, currency, balance, None, available, None, None]


def _ctx() -> AccountContext:
    return AccountContext(
        account_id="default",
        credentials=Credentials(api_key="KEY", api_secret="SECRET"),
        allocation_cap_usdt=Decimal("570"),
    )


# ── parse_wallets ─────────────────────────────────────────────────────────────


def test_parse_wallets_happy_path():
    wallets = parse_wallets([_wallet_row()])
    assert wallets == [
        FundingWallet(
            wallet_type="funding", currency="UST",
            balance=Decimal("550.0"), available=Decimal("147.5"),
        )
    ]


def test_parse_wallets_null_available_is_zero():
    wallets = parse_wallets([_wallet_row(available=None)])
    assert wallets[0].available == Decimal("0")


def test_parse_wallets_rejects_non_list():
    with pytest.raises(BitfinexShapeError):
        parse_wallets({"not": "a list"})


def test_parse_wallets_rejects_short_row():
    with pytest.raises(BitfinexShapeError):
        parse_wallets([["funding", "UST", 550.0]])  # len < 5, no AVAILABLE_BALANCE


# ── BitfinexAuthREST.get_funding_available ────────────────────────────────────


@pytest.mark.asyncio
async def test_get_funding_available_sums_funding_currency_only():
    rows = [
        _wallet_row(wallet_type="funding", currency="UST", available=147.5),
        _wallet_row(wallet_type="exchange", currency="UST", available=999.0),  # excluded: not funding
        _wallet_row(wallet_type="funding", currency="USD", available=50.0),    # excluded: wrong currency
    ]
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["method"] = request.method
        captured["headers"] = dict(request.headers)
        return httpx.Response(200, json=rows)

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http:
        client = BitfinexAuthREST(http=http, nonce_provider=lambda: 111)
        available = await client.get_funding_available(ctx=_ctx(), currency="UST")

    assert available == Decimal("147.5")
    assert captured["method"] == "POST"
    assert captured["url"] == "https://api.bitfinex.com/v2/auth/r/wallets"
    headers = captured["headers"]
    assert headers["bfx-apikey"] == "KEY"
    assert "bfx-signature" in headers


@pytest.mark.asyncio
async def test_get_funding_available_zero_when_no_funding_wallet():
    transport = httpx.MockTransport(
        lambda r: httpx.Response(200, json=[_wallet_row(wallet_type="exchange")])
    )
    async with httpx.AsyncClient(transport=transport) as http:
        client = BitfinexAuthREST(http=http, nonce_provider=lambda: 1)
        available = await client.get_funding_available(ctx=_ctx(), currency="UST")
    assert available == Decimal("0")


@pytest.mark.asyncio
async def test_get_funding_available_raises_on_http_error():
    transport = httpx.MockTransport(lambda r: httpx.Response(500, text="boom"))
    async with httpx.AsyncClient(transport=transport) as http:
        client = BitfinexAuthREST(http=http, nonce_provider=lambda: 1)
        with pytest.raises(BitfinexAPIError):
            await client.get_funding_available(ctx=_ctx(), currency="UST")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/external/bitfinex/test_auth_rest_wallets.py -v`
Expected: FAIL — `ImportError: cannot import name 'FundingWallet'` / `parse_wallets`.

- [ ] **Step 3: Implement** — in `auth_rest.py`, after `parse_active_funding_credits` (line 98) add the dataclass + parser; after `_FUNDING_CREDITS_PATH` (line 103) add the path const; inside `BitfinexAuthREST` (after `get_active_funding_credits`, line 197) add the method:

```python
@dataclass(frozen=True, slots=True)
class FundingWallet:
    wallet_type: str   # "funding" / "exchange" / "margin"
    currency: str      # "UST" / "USD" / ...
    balance: Decimal
    available: Decimal  # AVAILABLE_BALANCE; deposit-wallet free portion


_WALLET_MIN_ROW_LEN = 5  # AVAILABLE_BALANCE at index 4


def parse_wallets(raw: Any) -> list[FundingWallet]:
    """Parse Bitfinex /v2/auth/r/wallets response -> list[FundingWallet].

    Layout: [0]=WALLET_TYPE [1]=CURRENCY [2]=BALANCE [3]=UNSETTLED_INTEREST
            [4]=AVAILABLE_BALANCE ... AVAILABLE_BALANCE may be null (venue has
    not computed it) -> treated as 0 (conservative: never deploy on unknown funds).
    """
    if not isinstance(raw, list):
        raise BitfinexShapeError(
            f"expected list of wallets, got {type(raw).__name__}: {raw!r}"
        )
    out: list[FundingWallet] = []
    for w in raw:
        if not isinstance(w, list) or len(w) < _WALLET_MIN_ROW_LEN:
            raise BitfinexShapeError(f"wallet row malformed: {w!r}")
        available_raw = w[4]
        out.append(FundingWallet(
            wallet_type=str(w[0]),
            currency=str(w[1]),
            balance=Decimal(str(w[2])),
            available=Decimal(str(available_raw)) if available_raw is not None else Decimal("0"),
        ))
    return out
```

Add path constant next to the others:

```python
_WALLETS_PATH = "v2/auth/r/wallets"  # no /{symbol}; sign_request prepends /api/
```

Add the method inside `BitfinexAuthREST`:

```python
    async def get_funding_available(
        self, *, ctx: AccountContext, currency: str,
    ) -> Decimal:
        """POST /v2/auth/r/wallets (signed). Returns Σ available of FUNDING
        wallets for `currency` (0 if none). Same error contract as offers/credits:
        raises BitfinexAPIError on transport/HTTP error, BitfinexShapeError on
        invalid JSON / shape."""
        path = _WALLETS_PATH
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
            raw = resp.json()
        except json.JSONDecodeError as e:
            raise BitfinexShapeError(f"invalid JSON in wallets response: {e}") from e
        wallets = parse_wallets(raw)
        return sum(
            (w.available for w in wallets
             if w.wallet_type == "funding" and w.currency == currency),
            Decimal("0"),
        )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/external/bitfinex/test_auth_rest_wallets.py -v`
Expected: PASS (7 tests).

- [ ] **Step 5: Commit**

```bash
git add src/bfx_funding_bot/external/bitfinex/auth_rest.py tests/external/bitfinex/test_auth_rest_wallets.py
git commit -m "✨ Feat: BitfinexAuthREST.get_funding_available (wallet balance read)"
```

---

## Task 2: `allocate_gap` balance clamp (pure sizing)

**Files:**
- Modify: `src/bfx_funding_bot/modules/execution/deployment/sizing.py:21-54`
- Test: `tests/modules/execution/deployment/test_sizing.py`

`available_headroom` defaults to `Decimal("Infinity")` (no balance constraint) so existing
sizing tests — which assert policy-cap behavior — keep passing unchanged. The real caller
(reconciler, Task 5) always passes a finite value.

- [ ] **Step 1: Write the failing test** — append to `tests/modules/execution/deployment/test_sizing.py`:

```python
def test_allocate_gap_clamps_to_available_headroom():
    # cap gap = 600 - 400 = 200, but only 140 deployable → gap clamps to 140.
    fills = allocate_gap(
        target=Decimal("600"),
        current_exposure=Decimal("400"),
        available_headroom=Decimal("140"),
        deployed={},
        active_cells=["fUST_a30"],
        concentration_pct=Decimal("0.70"),
        min_fill=Decimal("153"),
    )
    # 140 < min_fill(153) → nothing deployed (sleep).
    assert fills == {}


def test_allocate_gap_deploys_when_headroom_allows():
    fills = allocate_gap(
        target=Decimal("600"),
        current_exposure=Decimal("400"),
        available_headroom=Decimal("200"),   # >= cap gap 200
        deployed={},
        active_cells=["fUST_a30"],
        concentration_pct=Decimal("0.70"),
        min_fill=Decimal("153"),
    )
    assert fills == {"fUST_a30": Decimal("200")}


def test_allocate_gap_headroom_binds_below_cap_gap():
    # cap gap = 300, headroom 160 → deploy 160 (headroom binds, >= min_fill).
    fills = allocate_gap(
        target=Decimal("700"),
        current_exposure=Decimal("400"),
        available_headroom=Decimal("160"),
        deployed={},
        active_cells=["fUST_a30"],
        concentration_pct=Decimal("0.70"),
        min_fill=Decimal("153"),
    )
    assert fills == {"fUST_a30": Decimal("160")}


def test_allocate_gap_negative_headroom_sleeps():
    fills = allocate_gap(
        target=Decimal("600"),
        current_exposure=Decimal("400"),
        available_headroom=Decimal("-5"),
        deployed={},
        active_cells=["fUST_a30"],
        concentration_pct=Decimal("0.70"),
        min_fill=Decimal("153"),
    )
    assert fills == {}


def test_allocate_gap_default_headroom_is_unbounded():
    # No available_headroom passed → behaves as before (cap gap only).
    fills = allocate_gap(
        target=Decimal("600"),
        current_exposure=Decimal("400"),
        deployed={},
        active_cells=["fUST_a30"],
        concentration_pct=Decimal("0.70"),
        min_fill=Decimal("153"),
    )
    assert fills == {"fUST_a30": Decimal("200")}
```

(If `allocate_gap` is not already imported at the top of the test file, add
`from bfx_funding_bot.modules.execution.deployment.sizing import allocate_gap`.)

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/modules/execution/deployment/test_sizing.py -k "headroom or unbounded" -v`
Expected: FAIL — `allocate_gap() got an unexpected keyword argument 'available_headroom'`.

- [ ] **Step 3: Implement** — modify `allocate_gap` signature and the `gap` line in `sizing.py`:

```python
def allocate_gap(
    *,
    target: Decimal,
    current_exposure: Decimal,
    deployed: dict[str, Decimal],
    active_cells: list[str],
    concentration_pct: Decimal,
    min_fill: Decimal,
    available_headroom: Decimal = Decimal("Infinity"),
) -> dict[str, Decimal]:
    """Distribute the funding gap across active cells.

    Greedy emptiest-first (balances per-cell deployment over time), each cell
    capped at concentration_pct * target. Fills below min_fill are dropped to
    avoid sub-minimum dust (would be rejected by the venue minimum anyway).
    Total allocated <= gap, so the global allocation cap is never exceeded.

    The gap is clamped to available_headroom (= venue free balance − buffer) so
    the reconciler never sizes an offer larger than the funds physically present;
    default Infinity = no balance constraint (only the policy cap binds).
    cap_per_cell stays bound to the POLICY target, not the balance-clamped gap.
    """
    gap = min(target - current_exposure, available_headroom)
    if gap < min_fill or not active_cells:
        return {}

    cap_per_cell = concentration_pct * target
    ordered = sorted(active_cells, key=lambda c: (deployed.get(c, Decimal("0")), c))

    fills: dict[str, Decimal] = {}
    remaining = gap
    for cell in ordered:
        if remaining < min_fill:
            break
        headroom = cap_per_cell - deployed.get(cell, Decimal("0"))
        fill = min(remaining, headroom)
        if fill >= min_fill:
            fills[cell] = fill
            remaining -= fill
    return fills
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/modules/execution/deployment/test_sizing.py -v`
Expected: PASS (new + all existing sizing tests).

- [ ] **Step 5: Commit**

```bash
git add src/bfx_funding_bot/modules/execution/deployment/sizing.py tests/modules/execution/deployment/test_sizing.py
git commit -m "✨ Feat: allocate_gap clamps gap to available_headroom (balance-aware sizing)"
```

---

## Task 3: `available` on `PositionReconciled` event + ledger

**Files:**
- Modify: `src/bfx_funding_bot/modules/execution/events.py:140-157`
- Modify: `src/bfx_funding_bot/modules/execution/ledger.py`
- Test: `tests/modules/execution/test_ledger.py`

- [ ] **Step 1: Write the failing test** — append to `tests/modules/execution/test_ledger.py`:

```python
async def test_available_balance_default_zero():
    led = PaperPositionLedger(account_id="default")
    assert led.available_balance() == Decimal("0")


async def test_on_position_reconciled_sets_available():
    led = PaperPositionLedger(account_id="default")
    await led.on_position_reconciled(PositionReconciled(
        account_id="default",
        reserved_usdt=Decimal("0"),
        realized_usdt=Decimal("406.89"),
        available_usdt=Decimal("147.5"),
        n_offers=0,
        n_credits=2,
        occurred_at_ms=1_000,
    ))
    assert led.available_balance() == Decimal("147.5")
    assert led.current_exposure() == Decimal("406.89")


async def test_on_position_reconciled_other_account_ignored():
    led = PaperPositionLedger(account_id="default")
    await led.on_position_reconciled(PositionReconciled(
        account_id="other",
        reserved_usdt=Decimal("1"), realized_usdt=Decimal("1"),
        available_usdt=Decimal("99"), n_offers=1, n_credits=1, occurred_at_ms=1,
    ))
    assert led.available_balance() == Decimal("0")
```

(Ensure the test module imports `PositionReconciled` and `PaperPositionLedger`; add
`from bfx_funding_bot.modules.execution.events import PositionReconciled` and
`from bfx_funding_bot.modules.execution.ledger import PaperPositionLedger` if absent.)

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/modules/execution/test_ledger.py -k "available" -v`
Expected: FAIL — `PositionReconciled.__init__() got an unexpected keyword argument 'available_usdt'`.

- [ ] **Step 3a: Implement event field** — in `events.py`, add `available_usdt` to `PositionReconciled` (place after `realized_usdt`, before `n_offers`; update the docstring line):

```python
@dataclass(frozen=True, slots=True)
class PositionReconciled:
    """Periodic venue snapshot result — in-process pub/sub signal ONLY.

    NOT persisted to event_log. Emitted by BootRecovery / PeriodicReconcile
    after fetching /funding/offers, /funding/credits and /wallets. Drives the
    absolute set in PaperPositionLedger.on_position_reconciled(); the
    store.set_position_snapshot() direct write persists reserved/realized to
    position_state (available is in-memory only — not persisted).

    reserved_usdt  = Σ(active offers)  — venue snapshot, not event accumulation.
    realized_usdt  = Σ(active credits) — venue snapshot.
    available_usdt = funding-wallet available balance (deposit-wallet free funds).
    """
    account_id: str
    reserved_usdt: Decimal
    realized_usdt: Decimal
    available_usdt: Decimal
    n_offers: int
    n_credits: int
    occurred_at_ms: int
```

- [ ] **Step 3b: Implement ledger field + getter** — in `ledger.py`: add `self._available = Decimal("0")` in `__init__` (after `self._realized`); set it in `on_position_reconciled`; add the getter after `realized_exposure`:

```python
    # in __init__, after self._realized = Decimal("0"):
        self._available = Decimal("0")
```

```python
    # in on_position_reconciled, after self._realized = event.realized_usdt:
        self._available = event.available_usdt
```

```python
    # new getter after realized_exposure():
    def available_balance(self) -> Decimal:
        """Funding-wallet available balance from the last reconcile (in-memory;
        not persisted). 0 until the first reconcile populates it — fail-closed
        (the reconciler deploys nothing on unknown funds). Read by the
        DeploymentReconciler balance clamp and BuyingPowerGuard."""
        return self._available
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/modules/execution/test_ledger.py -v`
Expected: PASS (new + existing ledger tests). NOTE: this change makes `available_usdt` a
**required** field on `PositionReconciled`; Task 4 updates the producer. Other unit tests that
construct `PositionReconciled` directly must add `available_usdt=…` — fix any that fail here.

- [ ] **Step 5: Commit**

```bash
git add src/bfx_funding_bot/modules/execution/events.py src/bfx_funding_bot/modules/execution/ledger.py tests/modules/execution/test_ledger.py
git commit -m "✨ Feat: PositionReconciled carries available_usdt; ledger.available_balance()"
```

---

## Task 4: Fetch `available` in the reconcile pass

**Files:**
- Modify: `src/bfx_funding_bot/modules/execution/boot_recovery.py`
- Test: `tests/modules/execution/test_boot_recovery.py`

Add a `_WalletsQuery` protocol so `_AuthRestQuery` requires `get_funding_available`; add
`_fetch_wallets()` mirroring `_fetch_offers`; thread `available_usdt` through `run()` into
both `ReconcileResult` and the published `PositionReconciled`. A persistent fetch failure
raises out of `run()` (same as offers/credits) → `PeriodicReconcile._tick` skips `deploy()`.

- [ ] **Step 1: Write the failing test** — the file already has `_StubStore`, `_StubSessionFactory`, `_StubBus`, `_StubAuthRest`, and `_full_boot_recovery(auth, store, session_factory, bus, **kw)` (used by `test_run_returns_reconcile_result_for_orphan_claim`). Two edits:

(a) Extend `_StubAuthRest` (currently offers+credits only) to support a configurable available — replace its body with:

```python
class _StubAuthRest:
    """Offers+credits+wallet-available stub for run() tests."""
    def __init__(self, offers, credits=None, available=Decimal("0")):
        self._offers = offers
        self._credits = credits if credits is not None else []
        self._available = available

    async def get_active_funding_offers(self, *, ctx, symbol="fUSD"):
        return self._offers

    async def get_active_funding_credits(self, *, ctx, symbol="fUSD"):
        return self._credits

    async def get_funding_available(self, *, ctx, currency):
        return self._available
```

(b) Ensure `PositionReconciled` is imported (add to the events import block at the top:
`from bfx_funding_bot.modules.execution.events import (..., PositionReconciled, ...)`), then append:

```python
@pytest.mark.asyncio
async def test_run_populates_available_from_wallets():
    """run() threads wallet available into ReconcileResult + published event."""
    store = _StubStore()
    bus = _StubBus()
    auth = _StubAuthRest(offers=[], credits=[], available=Decimal("147.5"))
    rec = _full_boot_recovery(auth, store, _StubSessionFactory(), bus)

    result = await rec.run()

    assert result.available_usdt == Decimal("147.5")
    published = [e for e in bus.published if isinstance(e, PositionReconciled)]
    assert published and published[-1].available_usdt == Decimal("147.5")
    # available is NOT persisted: set_position_snapshot has no available_usdt kwarg.
    assert "available_usdt" not in store.snapshot_calls[-1]


@pytest.mark.asyncio
async def test_fetch_available_does_not_retry_4xx():
    class _FailingWallets:
        def __init__(self):
            self.calls = 0
        async def get_active_funding_offers(self, *, ctx, symbol="fUSD"):
            return []
        async def get_active_funding_credits(self, *, ctx, symbol="fUSD"):
            return []
        async def get_funding_available(self, *, ctx, currency):
            self.calls += 1
            raise BitfinexAPIError(status_code=401, message="boom")

    auth = _FailingWallets()
    rec = _boot_recovery(auth)
    with pytest.raises(BitfinexAPIError):
        await rec._fetch_available()
    assert auth.calls == 1  # no retry on 4xx (fail-closed, same as offers/credits)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/modules/execution/test_boot_recovery.py -k "available" -v`
Expected: FAIL — `ReconcileResult` has no `available_usdt` / fake `auth_rest` lacks `get_funding_available`.

- [ ] **Step 3a: Add wallets protocol** — in `boot_recovery.py`, near `_ActiveOffersQuery` / `_ActiveCreditsQuery` (around line 159-179) add and compose:

```python
class _WalletsQuery(Protocol):
    async def get_funding_available(
        self, *, ctx: AccountContext, currency: str,
    ) -> Decimal: ...


class _AuthRestQuery(_ActiveOffersQuery, _ActiveCreditsQuery, _WalletsQuery, Protocol):
    """Combined protocol: offers + credits + wallet-available queries."""
```

- [ ] **Step 3b: Add `_fetch_wallets`** — in `boot_recovery.py`, after `_fetch_credits` (line 361) add (mirrors the retry/backoff pattern exactly):

```python
    async def _fetch_available(self) -> Decimal:
        """Fetch funding-wallet available balance with bounded retry on TRANSIENT
        failures only. 4xx re-raises immediately; transient exhaustion re-raises.
        Same fail-safe contract as offers/credits: a persistent failure aborts the
        reconcile tick, so deploy() is skipped (never size against unknown funds).
        Currency = symbol minus the leading 'f' (fUST -> UST)."""
        currency = self._symbol[1:] if self._symbol.startswith("f") else self._symbol
        last_exc: BitfinexAPIError | None = None
        for attempt in range(self._max_attempts):
            try:
                return await self._auth_rest.get_funding_available(
                    ctx=self._ctx, currency=currency,
                )
            except BitfinexAPIError as e:
                if not _is_transient_status(e.status_code):
                    log.error(
                        "boot_recovery_wallets_fetch_fatal status=%d err=%r — failing reconcile",
                        e.status_code, e,
                    )
                    raise
                last_exc = e
                if attempt + 1 < self._max_attempts:
                    backoff = self._backoff_base_s * (2 ** attempt)
                    log.warning(
                        "boot_recovery_wallets_fetch_transient attempt=%d/%d status=%d backoff=%.1fs",
                        attempt + 1, self._max_attempts, e.status_code, backoff,
                    )
                    await asyncio.sleep(backoff)
        log.error("boot_recovery_wallets_unreachable after %d attempts — failing reconcile", self._max_attempts)
        assert last_exc is not None
        raise last_exc
```

- [ ] **Step 3c: Thread through `run()`** — in `run()`: fetch available after credits (line 241), include it in the log, the `PositionReconciled` event (line 269), and the returned `ReconcileResult` (line 297). Also add the field to the `ReconcileResult` dataclass (line 51-60).

`ReconcileResult` (add field after `realized_usdt`):

```python
@dataclass(frozen=True, slots=True)
class ReconcileResult:
    n_claimed: int
    n_released: int
    n_failed: int
    reserved_usdt: Decimal = Decimal("0")
    realized_usdt: Decimal = Decimal("0")
    available_usdt: Decimal = Decimal("0")
    n_credits: int = 0
    reserved_drift_usdt: Decimal = Decimal("0")
    realized_drift_usdt: Decimal = Decimal("0")
```

In `run()`, after `venue_credits = await self._fetch_credits()` (line 241):

```python
        venue_offers = await self._fetch_offers()
        venue_credits = await self._fetch_credits()
        available_usdt = await self._fetch_available()
```

Update the `PositionReconciled(...)` construction (line 269) to pass `available_usdt=available_usdt`:

```python
        position_reconciled = PositionReconciled(
            account_id=self._ctx.account_id,
            reserved_usdt=reserved_usdt,
            realized_usdt=realized_usdt,
            available_usdt=available_usdt,
            n_offers=len(venue_offers),
            n_credits=len(venue_credits),
            occurred_at_ms=now_ms,
        )
```

Update the `reconcile_complete` log to include available (optional but useful — append `available=%.2f`):

```python
        log.info(
            "reconcile_complete venue_offers=%d venue_credits=%d "
            "reserved=%.2f realized=%.2f available=%.2f "
            "orphans_claimed=%d released=%d pending_failed=%d",
            len(venue_offers), len(venue_credits),
            float(reserved_usdt), float(realized_usdt), float(available_usdt),
            n_claim, n_release, n_fail,
        )
```

Update the returned `ReconcileResult` (line 297) to pass `available_usdt=available_usdt`:

```python
        return ReconcileResult(
            n_claimed=n_claim, n_released=n_release, n_failed=n_fail,
            reserved_usdt=reserved_usdt, realized_usdt=realized_usdt,
            available_usdt=available_usdt,
            n_credits=len(venue_credits),
            reserved_drift_usdt=drift.reserved_drift,
            realized_drift_usdt=drift.realized_drift,
        )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/modules/execution/test_boot_recovery.py -v`
Expected: PASS. Fix any existing test in this file that builds a fake `auth_rest` without
`get_funding_available` (add the async method returning `Decimal("0")` or a configured value).

- [ ] **Step 5: Commit**

```bash
git add src/bfx_funding_bot/modules/execution/boot_recovery.py tests/modules/execution/test_boot_recovery.py
git commit -m "✨ Feat: reconcile pass fetches funding-wallet available; flows to ReconcileResult + event"
```

---

## Task 5: Reconciler clamps gap to available balance

**Files:**
- Modify: `src/bfx_funding_bot/modules/execution/deployment/reconciler.py`
- Test: `tests/modules/execution/deployment/test_reconciler.py`

The reconciler's `_LedgerProtocol` gains `available_balance()`; `deploy()` computes
`headroom = max(0, available − buffer)` and passes it to `allocate_gap`. A new
`balance_buffer_usdt` constructor param carries the buffer.

- [ ] **Step 1: Write the failing test** — in `tests/modules/execution/deployment/test_reconciler.py`, first extend `_FakeLedger` and `_build` (so existing tests keep deploying — default available is large), then add the clamp test:

```python
# Replace _FakeLedger with:
class _FakeLedger:
    def __init__(
        self, exposure: Decimal, reserved: Decimal | None = None,
        available: Decimal | None = None,
    ) -> None:
        self._e = exposure
        self._reserved = reserved if reserved is not None else exposure
        # Default: effectively unbounded so existing cap-driven tests are unaffected.
        self._available = available if available is not None else Decimal("1000000")

    def current_exposure(self) -> Decimal:
        return self._e

    def reserved_exposure(self) -> Decimal:
        return self._reserved

    def available_balance(self) -> Decimal:
        return self._available
```

```python
# In _build(...), add an `available=None` kwarg and pass buffer + available through:
def _build(*, exposure, quotes, safety_allowed=True, executor=None, safety=None,
           available=None):
    cells = [_cell("fUST", "a30"), _cell("fUST", "p2")]
    store = StandingQuoteStore(ttl_ms=3_900_000)
    for q in quotes:
        store.update(q)
    tracker = CellDeploymentTracker()
    ex = executor or _FakeExecutor()
    safety = safety if safety is not None else _FakeSafety(allowed=safety_allowed)
    rec = DeploymentReconciler(
        store=store, tracker=tracker, ledger=_FakeLedger(exposure, available=available),
        safety_chain=safety, executor=ex, account_ctx=_ctx(), cells=cells,
        venue_floor_usd=D("150"), min_offer_buffer_pct=D("0.02"),
        concentration_pct=D("0.70"), balance_buffer_usdt=D("3"),
        clock=lambda: 1_000,
    )
    return rec, ex, tracker, safety
```

```python
# New tests:
async def test_clamps_deploy_to_available_minus_buffer():
    # cap gap = 570 - 406.89 = 163.11; available 150, buffer 3 -> headroom 147
    # < min_fill 153 -> sleep (the incident scenario).
    rec, ex, tracker, _ = _build(
        exposure=D("406.89"), quotes=[_post_quote("fUST_a30")], available=D("150"),
    )
    await rec.deploy()
    assert ex.submitted == []
    assert tracker.deployed("fUST_a30") == D("0")


async def test_deploys_when_available_sufficient():
    # cap gap = 570 - 370 = 200; available 250, buffer 3 -> headroom 247 >= 200
    # -> deploy full cap gap 200.
    rec, ex, tracker, _ = _build(
        exposure=D("370"), quotes=[_post_quote("fUST_a30")], available=D("250"),
    )
    await rec.deploy()
    assert len(ex.submitted) == 1
    assert ex.submitted[0].offer_amount_usdt == 200.0


async def test_available_headroom_binds_below_cap_gap():
    # cap gap = 570 - 200 = 370; available 320, buffer 3 -> headroom 317 -> deploy 317.
    rec, ex, tracker, _ = _build(
        exposure=D("200"), quotes=[_post_quote("fUST_a30")], available=D("320"),
    )
    await rec.deploy()
    assert len(ex.submitted) == 1
    assert ex.submitted[0].offer_amount_usdt == 317.0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/modules/execution/deployment/test_reconciler.py -k "available or clamps" -v`
Expected: FAIL — `DeploymentReconciler.__init__() got an unexpected keyword argument 'balance_buffer_usdt'`.

- [ ] **Step 3a: Add ledger protocol method** — in `reconciler.py`, extend `_LedgerProtocol` (line 32-34):

```python
class _LedgerProtocol(Protocol):
    def current_exposure(self) -> Decimal: ...
    def reserved_exposure(self) -> Decimal: ...
    def available_balance(self) -> Decimal: ...
```

- [ ] **Step 3b: Add constructor param** — in `__init__` (line 44-68) add `balance_buffer_usdt: Decimal` (after `min_offer_buffer_pct`) and store it:

```python
        concentration_pct: Decimal,
        balance_buffer_usdt: Decimal,
        clock: Callable[[], int],
    ) -> None:
        ...
        self._concentration_pct = concentration_pct
        self._balance_buffer = balance_buffer_usdt
        self._clock = clock
```

- [ ] **Step 3c: Clamp in `deploy()`** — in `deploy()`, after `e_total = self._ledger.current_exposure()` (line 72), compute headroom and pass it to `allocate_gap` (line 98):

```python
        e_total = self._ledger.current_exposure()
        headroom = max(Decimal("0"), self._ledger.available_balance() - self._balance_buffer)
        ...
        fills = allocate_gap(
            target=self._ctx.allocation_cap_usdt,
            current_exposure=e_total,
            available_headroom=headroom,
            deployed=self._tracker.snapshot(),
            active_cells=active,
            concentration_pct=self._concentration_pct,
            min_fill=self._min_fill,
        )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/modules/execution/deployment/test_reconciler.py -v`
Expected: PASS (new + all existing reconciler tests, which now use default-large available).

- [ ] **Step 5: Commit**

```bash
git add src/bfx_funding_bot/modules/execution/deployment/reconciler.py tests/modules/execution/deployment/test_reconciler.py
git commit -m "✨ Feat: DeploymentReconciler clamps gap to available balance − buffer"
```

---

## Task 6: `BuyingPowerGuard` (defense-in-depth)

**Files:**
- Modify: `src/bfx_funding_bot/modules/execution/safety/hard_guards.py`
- Test: `tests/modules/execution/safety/test_hard_guards.py`

Mirrors `AllocationCapGuard`. Reads `ledger.available_balance()`; blocks a POST whose
`offer_amount_usdt > available − buffer`. SKIP/CANCEL bypass; missing amount blocks.

- [ ] **Step 1: Write the failing test** — append to `tests/modules/execution/safety/test_hard_guards.py` (match the file's existing fixtures for `DecisionPayload` / `AccountContext`; a minimal self-contained fake ledger is shown):

```python
class _FakeBalanceLedger:
    def __init__(self, available: Decimal) -> None:
        self._a = available

    def available_balance(self) -> Decimal:
        return self._a


def _post_decision(amount: float | None) -> DecisionPayload:
    return DecisionPayload(
        decision_outcome=DecisionOutcome.POST,
        signal_correlation_id=uuid4(),
        offer_rate=0.00012,
        offer_amount_usdt=amount,
        offer_duration_days=2,
    )


def _bp_ctx() -> AccountContext:
    return AccountContext(
        account_id="default",
        credentials=Credentials(api_key="k", api_secret="s"),
        allocation_cap_usdt=Decimal("570"),
    )


async def test_buying_power_blocks_over_available():
    guard = BuyingPowerGuard(ledger=_FakeBalanceLedger(Decimal("150")), buffer_usdt=Decimal("3"))
    # deployable = 150 - 3 = 147; offer 160 > 147 -> block
    res = await guard.evaluate(_post_decision(160.0), _bp_ctx())
    assert res.allowed is False
    assert res.guard_name == "buying_power"


async def test_buying_power_allows_within_available():
    guard = BuyingPowerGuard(ledger=_FakeBalanceLedger(Decimal("250")), buffer_usdt=Decimal("3"))
    res = await guard.evaluate(_post_decision(200.0), _bp_ctx())
    assert res.allowed is True


async def test_buying_power_skip_bypasses():
    guard = BuyingPowerGuard(ledger=_FakeBalanceLedger(Decimal("0")), buffer_usdt=Decimal("3"))
    decision = DecisionPayload(
        decision_outcome=DecisionOutcome.SKIP,
        signal_correlation_id=uuid4(),
        offer_rate=None, offer_amount_usdt=None, offer_duration_days=None,
    )
    res = await guard.evaluate(decision, _bp_ctx())
    assert res.allowed is True


async def test_buying_power_missing_amount_blocks():
    guard = BuyingPowerGuard(ledger=_FakeBalanceLedger(Decimal("250")), buffer_usdt=Decimal("3"))
    res = await guard.evaluate(_post_decision(None), _bp_ctx())
    assert res.allowed is False
```

(Add imports as needed at the top of the test file: `from uuid import uuid4`,
`from decimal import Decimal`, `BuyingPowerGuard` from hard_guards,
`Credentials`/`AccountContext` from protocols, `DecisionOutcome`/`DecisionPayload` from schemas.)

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/modules/execution/safety/test_hard_guards.py -k "buying_power" -v`
Expected: FAIL — `ImportError: cannot import name 'BuyingPowerGuard'`.

- [ ] **Step 3: Implement** — in `hard_guards.py`, after `AllocationCapGuard` (line 146) add the protocol + guard:

```python
class _BalanceLedgerProtocol(Protocol):
    def available_balance(self) -> Decimal: ...


class BuyingPowerGuard:
    """Block POST when offer_amount > available funding-wallet balance − buffer.

    Physical-funds twin of AllocationCapGuard (which enforces the policy cap).
    Defense-in-depth: the DeploymentReconciler's sizing clamp is the precise
    cumulative control; this is a per-offer backstop so an over-balance offer
    never leaves the process (avoids relying on the venue's 10001 rejection).
    SKIP/CANCEL bypass; exactly-at-(available−buffer) allows.
    """

    name = "buying_power"
    is_calibrated = False

    def __init__(self, *, ledger: _BalanceLedgerProtocol, buffer_usdt: Decimal) -> None:
        self.ledger = ledger
        self.buffer_usdt = buffer_usdt

    async def evaluate(
        self, decision: DecisionPayload, ctx: AccountContext,
    ) -> GuardResult:
        if decision.decision_outcome != DecisionOutcome.POST:
            return GuardResult(allowed=True, guard_name=self.name)
        if decision.offer_amount_usdt is None:
            return GuardResult(
                allowed=False, guard_name=self.name,
                reason="POST decision missing offer_amount_usdt",
            )
        available = self.ledger.available_balance()
        deployable = available - self.buffer_usdt
        offer = Decimal(str(decision.offer_amount_usdt))
        if offer > deployable:
            return GuardResult(
                allowed=False, guard_name=self.name,
                reason=(
                    f"offer={offer} > available={available}−buffer={self.buffer_usdt}"
                    f"={deployable}"
                ),
            )
        return GuardResult(allowed=True, guard_name=self.name)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/modules/execution/safety/test_hard_guards.py -v`
Expected: PASS (new + existing guard tests).

- [ ] **Step 5: Commit**

```bash
git add src/bfx_funding_bot/modules/execution/safety/hard_guards.py tests/modules/execution/safety/test_hard_guards.py
git commit -m "✨ Feat: BuyingPowerGuard — pre-trade physical-funds backstop"
```

---

## Task 7: Wire buffer + guard in daemon

**Files:**
- Modify: `src/bfx_funding_bot/modules/marketfeed/daemon.py`

No unit test for wiring (covered by the full suite + a daemon-build smoke if present, and by
mypy). Wire: read the buffer env once, append `BuyingPowerGuard` right after `AllocationCapGuard`,
pass `balance_buffer_usdt` to `DeploymentReconciler`. Import `BuyingPowerGuard`.

- [ ] **Step 1: Add import** — in `daemon.py`, add `BuyingPowerGuard` to the hard_guards import (find the existing `from ...safety.hard_guards import (... AllocationCapGuard ...)` and add `BuyingPowerGuard`).

- [ ] **Step 2: Read buffer env once** — before the guards block (around line 740, before `if hg.heartbeat.enabled:`), add:

```python
    balance_buffer_usdt = Decimal(os.environ.get("BFX_BALANCE_BUFFER_USDT", "3"))
```

- [ ] **Step 3: Append the guard** — in the guards block, right after `AllocationCapGuard` (line 752-753), add it under the same enable flag (buying-power is the physical complement of the policy cap):

```python
    if hg.allocation_cap.enabled:
        guards.append(AllocationCapGuard(ledger=ledger))
        guards.append(BuyingPowerGuard(ledger=ledger, buffer_usdt=balance_buffer_usdt))
```

- [ ] **Step 4: Pass buffer to reconciler** — in the `DeploymentReconciler(...)` construction (line 894-906), add `balance_buffer_usdt=balance_buffer_usdt` after `concentration_pct=...`:

```python
        deployment_reconciler = DeploymentReconciler(
            store=quote_store,
            tracker=CellDeploymentTracker(),
            ledger=ledger,
            safety_chain=safety_chain,
            executor=wrapped_executor,
            account_ctx=account_ctx,
            cells=config.cells,
            venue_floor_usd=Decimal(os.environ.get("BFX_VENUE_FLOOR_USD", "150")),
            min_offer_buffer_pct=Decimal(os.environ.get("BFX_MIN_OFFER_BUFFER_PCT", "0.02")),
            concentration_pct=Decimal(os.environ.get("BFX_CONCENTRATION_PCT", "0.70")),
            balance_buffer_usdt=balance_buffer_usdt,
            clock=lambda: int(time.time() * 1000),
        )
```

- [ ] **Step 5: Verify build + types**

Run: `uv run mypy src/bfx_funding_bot/modules/marketfeed/daemon.py`
Expected: no new errors.
Run: `uv run pytest -m "not integration" -k "daemon" -q`
Expected: PASS (daemon-build/smoke tests, if any).

- [ ] **Step 6: Commit**

```bash
git add src/bfx_funding_bot/modules/marketfeed/daemon.py
git commit -m "🔧 Chore: wire BuyingPowerGuard + balance buffer into daemon (live path)"
```

---

## Task 8: Restore canary cap 550 → 570

**Files:**
- Modify: `scripts/deploy-koyeb.sh` (line ~7 comment, ~70 warn, ~158 env + STOPGAP comment block)

The gate now makes 570 safe (the reconciler clamps to available regardless of cap). Revert the
2026-05-29 stopgap so the policy cap returns to its intended value.

- [ ] **Step 1: Revert the cap env + drop the STOPGAP comment** — replace the STOPGAP comment block + `--env "BFX_ALLOCATION_CAP_USDT=550"` with:

```bash
      --env "BFX_ALLOCATION_CAP_USDT=570"
```

- [ ] **Step 2: Restore the two cosmetic references** — `$550 cap` → `$570 cap` on the usage comment (line ~7) and the `warn "CANARY = REAL MONEY ... ($550 cap ...)"` (line ~70).

- [ ] **Step 3: Verify no stray 550** —

Run: `grep -n "550\|570\|STOPGAP" scripts/deploy-koyeb.sh`
Expected: only `BFX_ALLOCATION_CAP_USDT=570` and the two `$570 cap` comments; no `550`, no `STOPGAP`.

- [ ] **Step 4: Commit**

```bash
git add scripts/deploy-koyeb.sh
git commit -m "🩹 Patch: restore canary cap 550→570 (balance-aware gate supersedes stopgap)"
```

---

## Task 9: Full verification + canary rollout

**Files:** none (verification + deploy).

- [ ] **Step 1: Full unit suite + types + lint**

Run: `uv run pytest -m "not integration" -q`
Expected: all green (was 855; now +~20 new tests).
Run: `uv run mypy src/`
Expected: clean (119+ files).
Run: `uv run ruff check`
Expected: clean.

- [ ] **Step 2: Push main** (canary deploy builds origin/main HEAD)

```bash
gh auth switch --user Will413028
git push origin main
git rev-parse --short HEAD; git ls-remote origin -h refs/heads/main
```
Expected: remote main == local HEAD.

- [ ] **Step 3: Deploy canary** (real money — confirm with the operator before running)

```bash
BFX_CANARY_CONFIRM=yes bash scripts/deploy-koyeb.sh canary
```

- [ ] **Step 4: Verify live behavior** — wait for the new deployment HEALTHY, then tail runtime logs:

```bash
koyeb deployments list | head -3
koyeb service logs fee91ed2 -t runtime --start-time <deploy-UTC> | grep -iE 'reconcile_complete|available|10001|deployment_submit'
```
Expected: `reconcile_complete … available=…` present; with `available < cap gap`, **no** `offer/submit` and **no** `10001`; when available is sufficient, a single clean `deployment_submitted`. Confirm reserved/realized unchanged from pre-deploy (no spurious release).

- [ ] **Step 5: Update memory + wiki**

- Mark the cap=550 stopgap retired; balance-aware gate live.
- Update `[[deployment-reconciler-merged-undeployed]]` memory and wiki `bfx-funding-bot` Pending.

---

## Notes for the implementer

- **Decimal Infinity:** `Decimal("Infinity")` is valid; `min(Decimal("200"), Decimal("Infinity")) == Decimal("200")`. Never compare/than-arith it into a real offer (the clamp only ever *reduces* the gap).
- **No migration:** `available` is in-memory only. Do NOT add a `position_state` column or alembic revision. If you feel tempted, re-read the spec "Architecture Decisions".
- **Currency mapping** lives only in `_fetch_available` (`fUST`→`UST`); `get_funding_available` takes an explicit `currency`.
- **Single buffer:** clamp (cumulative) and guard (per-offer) share the one `available − buffer` bound — no double subtraction.
- **Live-only:** none of this runs in paper/shadow (`DeploymentReconciler` is built only under `if not spec.is_simulated`). Do not add a simulated balance source.
- If any existing test constructs `PositionReconciled(...)` positionally or without `available_usdt`, update it (Task 3 makes the field required).
