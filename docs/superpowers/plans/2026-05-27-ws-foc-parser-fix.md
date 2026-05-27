# WS `foc` Authoritative Fill Path — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Restore the WS `foc` fast path as a seconds-latency fill/cancel signal by giving the Bitfinex funding-offer wire layout a single source of truth, making `foc EXECUTED` the authoritative fill, and demoting `fcn` to informational — correctness still guaranteed by the Plan-1 reconcile backbone.

**Architecture:** Extract one shared `parse_funding_offer_row` (the only copy of the funding-offer array indices); both REST `parse_active_funding_offers` and WS `_parse_foc` build their dataclasses from it, so the two paths can never drift again. `foc EXECUTED → OrderFilled(credit_id=None)`; `fcn → no-op`; remove the OOO staging buffer (the reconciler is the safety net). Fixtures are real-derived + a gated-live WS contract test.

**Tech Stack:** Python 3.13, `uv`, pytest, `websockets`, dataclasses, `httpx`. All commands run from `backend_py/`.

**Spec:** `docs/superpowers/specs/2026-05-27-ws-foc-parser-fix-design.md`

**Conventions (read once):**
- Every command runs from `backend_py/`: `cd backend_py && uv run <cmd>`.
- Commit gate (must pass before every commit): `uv run pytest -m "not integration"` green **and** `uv run mypy src/` **and** `uv run ruff check`.
- Commit messages use this repo's emoji prefixes (`✨ Feat:`, `✅ Test:`, `♻️ Refactor:`, `🐛 Fix:`, `🔥 Remove:`).

---

## File structure

| File | Responsibility | Task |
|---|---|---|
| `src/bfx_funding_bot/external/bitfinex/funding_offer_row.py` (**new**) | Single source of truth for the funding-offer wire array layout + tolerant parser | 1 |
| `tests/external/bitfinex/test_funding_offer_row.py` (**new**) | Unit tests for the shared parser | 1 |
| `src/bfx_funding_bot/external/bitfinex/auth_rest.py` | REST funding-offers parser → delegates layout to shared parser | 2 |
| `tests/external/bitfinex/fixtures/foc_executed.json`, `foc_canceled.json` | Corrected real-layout WS `foc` fixtures | 3 |
| `src/bfx_funding_bot/external/bitfinex/auth_ws.py` | WS `_parse_foc` via shared parser; drop `fcn.offer_id_meta` | 3, 5 |
| `tests/external/bitfinex/test_auth_ws_parse_frame.py` | WS parser tests asserting corrected layout | 3 |
| `src/bfx_funding_bot/external/bitfinex/ws_dispatcher.py` | `foc EXECUTED → OrderFilled`; `fcn → no-op`; remove OOO staging | 4, 5, 6 |
| `tests/external/bitfinex/test_ws_dispatcher_translate.py` | Dispatcher translate unit tests | 4, 5 |
| `tests/external/bitfinex/test_ws_dispatcher_integration.py` | Dispatcher integration test (fill via `foc`) | 5 |
| `tests/external/bitfinex/test_funding_offer_ws_wire.py` (**new**) | Gated-live WS layout contract + fast replay of captured golden | 7 |

---

## Task 1: Shared funding-offer row parser (single source of truth)

**Files:**
- Create: `src/bfx_funding_bot/external/bitfinex/funding_offer_row.py`
- Test: `tests/external/bitfinex/test_funding_offer_row.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/external/bitfinex/test_funding_offer_row.py`:

```python
from decimal import Decimal

import pytest

from bfx_funding_bot.external.bitfinex.errors import BitfinexShapeError
from bfx_funding_bot.external.bitfinex.funding_offer_row import (
    parse_funding_offer_row,
)

# Real funding-offer layout (matches fixtures/funding_offers_real.json rows):
#   [0]=id [1]=symbol [2]=mts_create [3]=mts_update [4]=amount(signed)
#   [10]=status [14]=rate [15]=period
_ROW = [998001, "fUSD", 1716595200000, 1716595260000, -120.5, -120.5,
        "LIMIT", None, None, 0, "ACTIVE", None, None, None, 0.00028, 2,
        0, 0, None, 0, None]


def test_parse_row_extracts_fields_at_correct_indices() -> None:
    r = parse_funding_offer_row(_ROW)
    assert r.venue_offer_id == "998001"
    assert r.symbol == "fUSD"
    assert r.mts_create == 1716595200000
    assert r.mts_update == 1716595260000
    assert r.amount == Decimal("120.5")  # abs of signed -120.5
    assert r.status == "ACTIVE"
    assert r.rate == 0.00028
    assert r.period_days == 2


def test_parse_row_tolerates_none_rate_and_period() -> None:
    row = list(_ROW)
    row[14] = None
    row[15] = None
    r = parse_funding_offer_row(row)
    assert r.rate is None
    assert r.period_days is None
    assert r.status == "ACTIVE"  # correctness fields still parsed


def test_parse_row_raises_on_short_row() -> None:
    with pytest.raises(BitfinexShapeError):
        parse_funding_offer_row([1, "fUSD", 2, 3])


def test_parse_row_raises_on_non_list() -> None:
    with pytest.raises(BitfinexShapeError):
        parse_funding_offer_row("not-a-list")  # type: ignore[arg-type]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd backend_py && uv run pytest tests/external/bitfinex/test_funding_offer_row.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named '...funding_offer_row'`.

- [ ] **Step 3: Write minimal implementation**

Create `src/bfx_funding_bot/external/bitfinex/funding_offer_row.py`:

```python
"""Single source of truth for the Bitfinex funding-offer wire array layout.

The same positional array is delivered by the REST `auth/r/funding/offers`
endpoint and by the WS user channel (`fos`/`fon`/`fou`/`foc`). Parsing it in
two places with divergent index assumptions was the 2026-05-26 incident root
cause. Both REST and WS MUST build their dataclasses from this one parser.

Layout (0-indexed):
  [0]=id [1]=symbol [2]=mts_create [3]=mts_update [4]=amount(signed)
  [10]=status [14]=rate [15]=period
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from bfx_funding_bot.external.bitfinex.errors import BitfinexShapeError

_MIN_ROW_LEN = 16


@dataclass(frozen=True, slots=True)
class FundingOfferRow:
    venue_offer_id: str
    symbol: str
    mts_create: int
    mts_update: int
    amount: Decimal        # absolute size (venue signs offers negative)
    status: str
    rate: float | None     # display/audit only; None on at-market/placeholder rows
    period_days: int | None


def parse_funding_offer_row(o: Any) -> FundingOfferRow:
    """Parse one funding-offer positional array. Raises on layout drift.

    Strict on correctness fields (id/symbol/status/amount); tolerant None-guard
    on rate/period (display-only, absent on at-market/placeholder rows).
    """
    if not isinstance(o, list) or len(o) < _MIN_ROW_LEN:
        raise BitfinexShapeError(f"funding offer row malformed: {o!r}")
    rate = o[14]
    period = o[15]
    return FundingOfferRow(
        venue_offer_id=str(o[0]),
        symbol=str(o[1]),
        mts_create=int(o[2]),
        mts_update=int(o[3]),
        amount=abs(Decimal(str(o[4]))),
        status=str(o[10]),
        rate=float(rate) if rate is not None else None,
        period_days=int(period) if period is not None else None,
    )
```

- [ ] **Step 4: Run tests + gate**

Run: `cd backend_py && uv run pytest tests/external/bitfinex/test_funding_offer_row.py -v && uv run mypy src/ && uv run ruff check`
Expected: 4 passed; mypy + ruff clean.

- [ ] **Step 5: Commit**

```bash
git add backend_py/src/bfx_funding_bot/external/bitfinex/funding_offer_row.py \
        backend_py/tests/external/bitfinex/test_funding_offer_row.py
git commit -m "✨ Feat: single-source funding-offer wire row parser

One parse_funding_offer_row owns the funding-offer array layout shared by REST
and WS — prevents the dual-parser index drift that caused the 2026-05-26 incident."
```

---

## Task 2: REST parser delegates to the shared row parser (behavior-preserving)

**Files:**
- Modify: `src/bfx_funding_bot/external/bitfinex/auth_rest.py:23-57`

- [ ] **Step 1: Confirm the regression test exists and is green**

Run: `cd backend_py && uv run pytest tests/external/bitfinex/test_funding_offers_wire.py::test_fixture_parses -v`
Expected: PASS (this is the behavior-preservation guard; it must stay green after the refactor).

- [ ] **Step 2: Refactor `parse_active_funding_offers` to use the shared parser**

In `src/bfx_funding_bot/external/bitfinex/auth_rest.py`, delete the local index
constant (lines 23-24):

```python
# Funding-offer array indices (Bitfinex docs). No cid field exists.
_MIN_ROW_LEN = 16
```

Add the import (next to the existing `errors` import near line 20):

```python
from bfx_funding_bot.external.bitfinex.funding_offer_row import parse_funding_offer_row
```

Replace the body of `parse_active_funding_offers` (lines 38-57) with:

```python
def parse_active_funding_offers(raw: Any) -> list[ActiveFundingOffer]:
    """Parse Bitfinex auth funding-offers response -> list[ActiveFundingOffer].

    Layout is owned by funding_offer_row.parse_funding_offer_row (shared with WS).
    """
    if not isinstance(raw, list):
        raise BitfinexShapeError(
            f"expected list of funding offers, got {type(raw).__name__}: {raw!r}"
        )
    out: list[ActiveFundingOffer] = []
    for o in raw:
        row = parse_funding_offer_row(o)
        out.append(ActiveFundingOffer(
            venue_offer_id=row.venue_offer_id,
            symbol=row.symbol,
            amount=row.amount,
            rate=row.rate if row.rate is not None else 0.0,
            period_days=row.period_days if row.period_days is not None else 0,
            mts_created=row.mts_create,
            status=row.status,
        ))
    return out
```

(`ActiveFundingOffer` keeps its non-null `rate: float`/`period_days: int` contract;
active offers always carry both — the golden proves it — so the `or` coercion
never triggers in practice but keeps mypy happy.)

- [ ] **Step 3: Run the regression test + gate**

Run: `cd backend_py && uv run pytest tests/external/bitfinex/test_funding_offers_wire.py -m "not integration" -v && uv run mypy src/ && uv run ruff check`
Expected: `test_fixture_parses` PASS unchanged; mypy + ruff clean.

- [ ] **Step 4: Commit**

```bash
git add backend_py/src/bfx_funding_bot/external/bitfinex/auth_rest.py
git commit -m "♻️ Refactor: REST funding-offer parse via shared row parser

Behavior-preserving (funding_offers_real.json golden still green); REST and WS
now share one layout source."
```

---

## Task 3: Fix WS `foc` fixtures + `_parse_foc` via shared parser

**Files:**
- Modify: `tests/external/bitfinex/fixtures/foc_executed.json`, `foc_canceled.json`
- Modify: `src/bfx_funding_bot/external/bitfinex/auth_ws.py:209-221`
- Modify: `tests/external/bitfinex/test_auth_ws_parse_frame.py:37-51`

- [ ] **Step 1: Rewrite the fixtures to the real funding-offer layout**

Overwrite `tests/external/bitfinex/fixtures/foc_executed.json` with (single line):

```json
[0,"foc",[789012,"fUSD",1716383500000,1716383600000,-100.0,-100.0,"LIMIT",null,null,0,"EXECUTED @ 0.0005 (100.0)",null,null,null,0.0005,2,0,0,null,0,null]]
```

Overwrite `tests/external/bitfinex/fixtures/foc_canceled.json` with (single line):

```json
[0,"foc",[789012,"fUSD",1716383500000,1716383600000,-100.0,-100.0,"LIMIT",null,null,0,"CANCELED",null,null,null,0.0005,2,0,0,null,0,null]]
```

(These mirror `funding_offers_real.json` rows: `[1]=symbol [2]=mts_create
[3]=mts_update [4]=amount [10]=status [14]=rate [15]=period`, with `status` set
to the close reason. 22 elements ≥ the 16-element minimum.)

- [ ] **Step 2: Update the parser tests to assert the previously-broken fields**

In `tests/external/bitfinex/test_auth_ws_parse_frame.py`, replace
`test_parse_foc_canceled` and `test_parse_foc_executed` (lines 37-51) with:

```python
def test_parse_foc_canceled() -> None:
    raw = _load("foc_canceled.json")
    result = parse_frame(raw)
    assert isinstance(result, FocEvent)
    assert result.venue_offer_id == "789012"
    assert result.symbol == "fUSD"
    assert result.mts_create == 1716383500000
    assert result.mts_update == 1716383600000
    assert result.status == "CANCELED"
    assert result.rate == 0.0005
    assert result.period_days == 2


def test_parse_foc_executed() -> None:
    raw = _load("foc_executed.json")
    result = parse_frame(raw)
    assert isinstance(result, FocEvent)
    assert result.venue_offer_id == "789012"
    assert result.symbol == "fUSD"
    assert result.status.startswith("EXECUTED")
    assert result.rate == 0.0005
    assert result.period_days == 2


def test_parse_foc_malformed_row_is_dropped_not_raised() -> None:
    """A short/malformed foc array → parse_frame drops it (returns None), never
    raises (BitfinexShapeError must be caught like the other parse errors)."""
    raw = json.dumps([0, "foc", [789012, "fUSD", 1, 2]])  # < 16 elements
    assert parse_frame(raw) is None
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `cd backend_py && uv run pytest tests/external/bitfinex/test_auth_ws_parse_frame.py -k foc -v`
Expected: FAIL — the old `_parse_foc` reads `symbol=d[2]`/`rate=float(d[11])`, so against the corrected fixtures it raises `TypeError`/`ValueError` (parse_frame catches → returns None → `isinstance` assert fails), or yields wrong `symbol`.

- [ ] **Step 4: Rewrite `_parse_foc` to use the shared parser**

In `src/bfx_funding_bot/external/bitfinex/auth_ws.py`, add the imports next to the
existing `protocols` import near line 31:

```python
from bfx_funding_bot.external.bitfinex.errors import BitfinexShapeError
from bfx_funding_bot.external.bitfinex.funding_offer_row import parse_funding_offer_row
```

Widen the parse-failure `except` in `_parse_channel_msg` (line 169) so a malformed
offer row is dropped + WARNed rather than crashing `parse_frame` (`BitfinexShapeError`
is **not** a subclass of `ValueError`, so it must be named explicitly — otherwise
the "parse_frame never raises" contract and the hypothesis fuzz test break):

```python
    except (IndexError, TypeError, ValueError, BitfinexShapeError) as e:
        log.warning("bfx_ws_parse_failed type=%s err=%r", msg_type, e)
        return None
```

Replace `_parse_foc` (lines 209-221) with:

```python
def _parse_foc(d: list[Any], raw_seq: int | None) -> FocEvent:
    # foc is a funding-offer array — layout owned by parse_funding_offer_row,
    # shared with the REST parser so the two can never drift.
    row = parse_funding_offer_row(d)
    return FocEvent(
        venue_offer_id=row.venue_offer_id,
        symbol=row.symbol,
        mts_create=row.mts_create,
        mts_update=row.mts_update,
        amount=row.amount,
        status=row.status,
        rate=row.rate if row.rate is not None else 0.0,
        period_days=row.period_days if row.period_days is not None else 0,
        raw_seq=raw_seq,
        raw=d,
    )
```

(`FocEvent` keeps its non-null `rate: float`/`period_days: int` types; the shared
parser's tolerance is what prevents the `float(None)` crash, and `0.0`/`0` are
acceptable display sentinels for the rare placeholder row.)

- [ ] **Step 5: Run tests + gate**

Run: `cd backend_py && uv run pytest tests/external/bitfinex/test_auth_ws_parse_frame.py -v && uv run mypy src/ && uv run ruff check`
Expected: all parse-frame tests PASS (including the hypothesis fuzz test); mypy + ruff clean.

- [ ] **Step 6: Commit**

```bash
git add backend_py/src/bfx_funding_bot/external/bitfinex/auth_ws.py \
        backend_py/tests/external/bitfinex/test_auth_ws_parse_frame.py \
        backend_py/tests/external/bitfinex/fixtures/foc_executed.json \
        backend_py/tests/external/bitfinex/fixtures/foc_canceled.json
git commit -m "🐛 Fix: WS _parse_foc layout via shared parser + real-derived fixtures

foc now parses the real funding-offer array ([1]=symbol [10]=status [14]=rate
[15]=period); fixtures rebuilt from the verified layout (were hand-authored to
the buggy indices that crashed prod on 2026-05-26)."
```

---

## Task 4: Dispatcher — `foc EXECUTED → OrderFilled`

**Files:**
- Modify: `src/bfx_funding_bot/external/bitfinex/ws_dispatcher.py:131-139`
- Modify: `tests/external/bitfinex/test_ws_dispatcher_translate.py:107-116`

- [ ] **Step 1: Replace the no-op test with a fill-emitting test**

In `tests/external/bitfinex/test_ws_dispatcher_translate.py`, replace
`test_foc_executed_on_claimed_is_no_op_with_diag` (lines 107-116) with:

```python
def test_foc_executed_on_claimed_emits_orderfilled() -> None:
    """foc EXECUTED is the authoritative fill signal (carries venue_offer_id)."""
    snapshot = {"v1": _claim_record("v1")}
    events, mutations, _diags = translate_bfx_event(
        _foc("v1", status="EXECUTED @ 0.0005 (100.0)"), snapshot, {}, now_ms=2500,
    )
    assert len(events) == 1
    assert isinstance(events[0], OrderFilled)
    assert events[0].credit_id is None        # foc carries no credit id
    assert events[0].venue_offer_id == "v1"
    assert events[0].fill_rate == 0.0005
    assert events[0].venue_seq == 7
    assert events[0].occurred_at_ms == 2000   # _foc mts_update
    assert len(mutations) == 1
    assert mutations[0].new_state == RegistryState.RELEASED
```

- [ ] **Step 2: Run it to verify it fails**

Run: `cd backend_py && uv run pytest tests/external/bitfinex/test_ws_dispatcher_translate.py::test_foc_executed_on_claimed_emits_orderfilled -v`
Expected: FAIL — current `_translate_foc` returns `([], [], [diag])` for EXECUTED, so `len(events) == 1` fails.

- [ ] **Step 3: Implement `foc EXECUTED → OrderFilled`**

In `src/bfx_funding_bot/external/bitfinex/ws_dispatcher.py`, replace the EXECUTED
no-op block (lines 131-139) with:

```python
    status_upper = foc.status.upper()

    # EXECUTED is the authoritative fill: foc carries venue_offer_id (fcn does not).
    if "EXECUTED" in status_upper:
        fill = OrderFilled(
            cid=claim.cid,
            venue_offer_id=voi,
            credit_id=None,
            size_usdt=claim.size_usdt,
            fill_rate=foc.rate,
            signal_correlation_id=claim.signal_correlation_id,
            account_id=claim.account_id,
            is_simulated=False,
            venue_seq=foc.raw_seq,
            occurred_at_ms=foc.mts_update,
        )
        return [fill], [RegistryMutation(
            venue_offer_id=voi,
            new_state=RegistryState.RELEASED,
            occurred_at_ms=foc.mts_update,
        )], []
```

(Leave the CANCELED/EXPIRED block that follows unchanged.)

- [ ] **Step 4: Run the full dispatcher translate suite + gate**

Run: `cd backend_py && uv run pytest tests/external/bitfinex/test_ws_dispatcher_translate.py -v && uv run mypy src/ && uv run ruff check`
Expected: all PASS (the existing fcn/cancel/expire tests are untouched and still green); mypy + ruff clean.

- [ ] **Step 5: Commit**

```bash
git add backend_py/src/bfx_funding_bot/external/bitfinex/ws_dispatcher.py \
        backend_py/tests/external/bitfinex/test_ws_dispatcher_translate.py
git commit -m "✨ Feat: foc EXECUTED → OrderFilled (authoritative fill signal)

foc carries venue_offer_id; OrderFilled.credit_id is None (foc has none). Replaces
the 'EXECUTED is redundant, fcn handles it' no-op."
```

---

## Task 5: `fcn` → informational (drop `offer_id_meta`)

**Files:**
- Modify: `src/bfx_funding_bot/external/bitfinex/auth_ws.py:41-54, 176-193`
- Modify: `src/bfx_funding_bot/external/bitfinex/ws_dispatcher.py:62-110`
- Modify: `tests/external/bitfinex/test_ws_dispatcher_translate.py:24-31, 43-67`
- Modify: `tests/external/bitfinex/test_ws_dispatcher_integration.py:40-84`

- [ ] **Step 1: Verify no other code references `offer_id_meta`**

Run: `cd backend_py && grep -rn "offer_id_meta" src/ tests/`
Expected: matches only in `auth_ws.py` (FcnEvent field + `_parse_fcn`),
`ws_dispatcher.py` (`_translate_fcn`), and the two test files listed above. If any
other file matches, stop and update it in this task.

- [ ] **Step 2: Rewrite the dispatcher + integration tests for the new behavior**

In `tests/external/bitfinex/test_ws_dispatcher_translate.py`:

Replace the `_fcn` helper (lines 24-31) — drop the `offer_id_meta` kwarg:

```python
def _fcn(voi: str = "v1", credit_id: int = 999, raw_seq: int = 5) -> FcnEvent:
    return FcnEvent(
        credit_id=credit_id, symbol="fUSD", side=1,
        mts_create=2000, mts_update=2000,
        amount=Decimal("100"), rate=0.0005, period_days=2,
        raw_seq=raw_seq, raw=[],
    )
```

Replace `test_fcn_on_claimed_emits_orderfilled_and_release_mutation` and
`test_fcn_on_empty_emits_no_event_with_diag` (lines 43-67) with a single no-op test:

```python
def test_fcn_is_informational_no_op() -> None:
    """fcn no longer drives the lifecycle (it carries no offer id). foc EXECUTED
    is the fill signal; reconcile is the backbone. fcn → no domain effect."""
    snapshot = {"v1": _claim_record("v1")}
    events, mutations, _diags = translate_bfx_event(
        _fcn("v1", credit_id=999), snapshot, recent_cancels={}, now_ms=2500,
    )
    assert events == []
    assert mutations == []
```

In `tests/external/bitfinex/test_ws_dispatcher_integration.py`, replace
`test_dispatcher_publishes_orderfilled_on_fcn` (lines 40-84) with a foc-driven version:

```python
@pytest.mark.asyncio
async def test_dispatcher_publishes_orderfilled_on_foc_executed() -> None:
    bus = DomainEventBus(clock=lambda: 5000)
    registry = OfferRegistry(clock=lambda: 5000)
    bus.subscribe(ReservationClaimed, registry.handle)
    bus.subscribe(OrderFilled, registry.handle)
    bus.subscribe(ReservationReleased, registry.handle)

    await bus.publish(ReservationClaimed(
        cid=42, venue_offer_id="42", size_usdt=Decimal("100"),
        signal_correlation_id=uuid4(), account_id="default", is_simulated=False,
        occurred_at_ms=1000,
    ))

    foc = FocEvent(
        venue_offer_id="42", symbol="fUSD",
        mts_create=1000, mts_update=2000,
        amount=Decimal("100"), status="EXECUTED @ 0.0005 (100.0)",
        rate=0.0005, period_days=2, raw_seq=5, raw=[],
    )
    fake_ws = _FakeWSClient([foc])

    captured: list = []

    async def capture(ev: OrderFilled) -> None:
        captured.append(ev)
    bus.subscribe(OrderFilled, capture)

    dispatcher = BitfinexLiveWSDispatcher(
        ws_client=fake_ws, registry=registry, bus=bus,
        event_sink=_EventCapture(), clock=lambda: 5000, queue_max=100,
    )

    stop = asyncio.Event()
    task = asyncio.create_task(dispatcher.run(stop))
    await asyncio.sleep(0.3)
    stop.set()
    try:
        await asyncio.wait_for(task, timeout=2.0)
    except (TimeoutError, asyncio.CancelledError):
        task.cancel()

    assert len(captured) == 1
    assert captured[0].credit_id is None
    assert captured[0].venue_offer_id == "42"
    assert captured[0].fill_rate == 0.0005
```

(`FcnEvent` is no longer imported by the integration test — remove it from the
import on line 9, leaving `from ...auth_ws import BfxWSEvent, FocEvent`.)

- [ ] **Step 3: Run the tests to verify they fail**

Run: `cd backend_py && uv run pytest tests/external/bitfinex/test_ws_dispatcher_translate.py tests/external/bitfinex/test_ws_dispatcher_integration.py -v`
Expected: FAIL — `_fcn`/`FcnEvent(...)` still require/accept `offer_id_meta` (the field
removal in Step 4 hasn't happened), and `test_fcn_is_informational_no_op` fails because
the current `_translate_fcn` still emits `OrderFilled`.

- [ ] **Step 4: Drop `offer_id_meta` from `FcnEvent` and make `_translate_fcn` a no-op**

In `src/bfx_funding_bot/external/bitfinex/auth_ws.py`, remove the `offer_id_meta`
field from `FcnEvent` (delete line 52):

```python
    offer_id_meta: int | None  # cross-map to our venue_offer_id (TBV vs real API)
```

In `_parse_fcn`, remove the `offer_id_meta=...` argument (delete line 190):

```python
        offer_id_meta=int(d[14]) if len(d) > 14 and d[14] is not None else None,
```

In `src/bfx_funding_bot/external/bitfinex/ws_dispatcher.py`, replace the
`FcnEvent` branch of `translate_bfx_event` (line 62-63) so it short-circuits, and
delete the entire `_translate_fcn` function (lines 71-110):

```python
    if isinstance(bfx_event, FcnEvent):
        return [], [], []  # informational only — no offer id; foc EXECUTED is the fill signal
```

- [ ] **Step 5: Run tests + gate**

Run: `cd backend_py && uv run pytest -m "not integration" && uv run mypy src/ && uv run ruff check`
Expected: full suite PASS; mypy + ruff clean. (If ruff flags an unused `OrderFilled`
import in `ws_dispatcher.py`, it is still used by `_translate_foc` from Task 4 —
no change needed; if it flags any genuinely unused import left by deleting
`_translate_fcn`, remove that import.)

- [ ] **Step 6: Commit**

```bash
git add backend_py/src/bfx_funding_bot/external/bitfinex/auth_ws.py \
        backend_py/src/bfx_funding_bot/external/bitfinex/ws_dispatcher.py \
        backend_py/tests/external/bitfinex/test_ws_dispatcher_translate.py \
        backend_py/tests/external/bitfinex/test_ws_dispatcher_integration.py
git commit -m "♻️ Refactor: fcn → informational; drop unsound offer_id_meta mapping

Bitfinex credit events carry no originating offer id (d[14] is mts_opening), so
credit→offer mapping was impossible. fcn is now a no-op; foc EXECUTED is the fill."
```

---

## Task 6: Remove the OOO staging buffer

**Files:**
- Modify: `src/bfx_funding_bot/external/bitfinex/ws_dispatcher.py:191, 211-215, 248-266, 284-299`

- [ ] **Step 1: Check for tests that depend on the staging buffer**

Run: `cd backend_py && grep -rn "staging\|OOO_STAGING\|_staging_buffer\|ooo_drop\|_background_tasks" src/ tests/`
Expected: matches only inside `ws_dispatcher.py`. If a test references staging,
stop and update it here (none is expected — the only staging-named test was
removed in Task 5).

- [ ] **Step 2: Remove the staging constant + state**

In `src/bfx_funding_bot/external/bitfinex/ws_dispatcher.py`:

Remove the `OOO_STAGING_TTL_MS` class constant (line 191):

```python
    OOO_STAGING_TTL_MS = 200
```

Remove the staging + background-task fields from `__init__` (lines 212, 215):

```python
        self._staging_buffer: dict[str, tuple[BfxWSEvent, int]] = {}
```
```python
        self._background_tasks: set[asyncio.Task[None]] = set()
```

- [ ] **Step 3: Remove the staging branch in `_process`**

Replace the diagnostics loop in `_process` (lines 254-263) with just the logging
(drop the `if d.venue_offer_id and "stage" in ...` staging block):

```python
        for d in diags:
            (log.warning if d.level == "warn" else log.info)(
                "ws_dispatcher_diag voi=%s msg=%s",
                d.venue_offer_id, d.message,
            )
```

- [ ] **Step 4: Remove the staging drain from `_tick_maintenance`**

Replace `_tick_maintenance` (lines 284-311) with the version that keeps only
`recent_cancels` cleanup + queue-depth emit:

```python
    def _tick_maintenance(self) -> None:
        now_ms = self._clock()
        # Cleanup expired recent_cancels
        cancel_cutoff = now_ms - self.RECENT_CANCELS_TTL_MS
        self._recent_cancels = {
            voi: ts for voi, ts in self._recent_cancels.items() if ts >= cancel_cutoff
        }

        # Emit queue depth health every 30s
        if now_ms - self._last_depth_emit_ms > 30_000:
            self._last_depth_emit_ms = now_ms
            depth = self._queue.qsize()
            log.info("ws_dispatcher_queue_depth depth=%d max=%d", depth, self._queue_max)
```

- [ ] **Step 5: Run tests + gate**

Run: `cd backend_py && uv run pytest -m "not integration" && uv run mypy src/ && uv run ruff check`
Expected: full suite PASS (the two dispatcher integration tests — fill via foc,
cancel tracking — still pass without staging); mypy + ruff clean. (Remove any
now-unused import ruff flags, e.g. `contextlib`/`asyncio` only if genuinely
unused — note `run()` still uses both, so they should remain.)

- [ ] **Step 6: Commit**

```bash
git add backend_py/src/bfx_funding_bot/external/bitfinex/ws_dispatcher.py
git commit -m "🔥 Remove: OOO staging buffer (reconciler is the safety net)

claim-before-foc is structurally guaranteed (submit writes CLAIMED before any
foc can arrive); rare reordering is converged by the Plan-1 reconcile backbone.
Drops staging state, its TTL, and the ooo_drop latent-bug error path."
```

---

## Task 7: Gated-live WS layout contract test

**Files:**
- Create: `tests/external/bitfinex/test_funding_offer_ws_wire.py`

- [ ] **Step 1: Write the gated contract test + fast replay**

Create `tests/external/bitfinex/test_funding_offer_ws_wire.py`:

```python
"""Wire-format contract test for the Bitfinex funding-offer WS array.

WS sibling of test_funding_offers_wire.py. Proves the WS funding-offer array
(fos/fon/fou/foc) shares the REST layout owned by parse_funding_offer_row — the
2026-05-26 incident was a WS-vs-REST layout drift.

  - test_ws_funding_offer_fixture_parses_if_present: fast; replays a captured
    golden through the shared parser (skips until a golden exists).
  - test_ws_funding_offer_layout_contract: gated (@pytest.mark.integration);
    connects the real auth WS, captures fos/fon/foc offer rows, validates each
    through parse_funding_offer_row. fos (snapshot) arrives free on connect for
    any account with active funding offers — no fill needed. BFX_UPDATE_FIXTURE=1
    writes the captured raw frames as the golden.

    Capture run:
        BFX_API_KEY=... BFX_API_SECRET=... BFX_UPDATE_FIXTURE=1 \\
          uv run pytest -m integration -k ws_funding_offer --capture=no
"""
import asyncio
import json
import os
import time
from pathlib import Path

import pytest
import websockets

from bfx_funding_bot.external.bitfinex.auth_ws import build_auth_payload
from bfx_funding_bot.external.bitfinex.funding_offer_row import parse_funding_offer_row

_FIXTURE = Path(__file__).parent / "fixtures" / "foc_real.json"
_FUNDING_OFFER_MSG_TYPES = {"fos", "fon", "fou", "foc"}


def _extract_offer_rows(msg: object) -> list[list]:
    """A funding-offer channel msg is [chan, type, payload, (seq)].
    fos payload is a list of offer arrays; fon/fou/foc payload is one offer array.
    """
    if (not isinstance(msg, list) or len(msg) < 3
            or msg[1] not in _FUNDING_OFFER_MSG_TYPES):
        return []
    payload = msg[2]
    if not isinstance(payload, list) or not payload:
        return []
    if isinstance(payload[0], list):   # fos snapshot: list-of-rows
        return [r for r in payload if isinstance(r, list)]
    return [payload]                   # single-offer event


def test_ws_funding_offer_fixture_parses_if_present() -> None:
    if not _FIXTURE.exists():
        pytest.skip("no captured WS golden yet (run the gated test with BFX_UPDATE_FIXTURE=1)")
    frames = json.loads(_FIXTURE.read_text())
    rows = [r for msg in frames for r in _extract_offer_rows(msg)]
    assert rows, "captured golden must contain at least one funding-offer row"
    for r in rows:
        parse_funding_offer_row(r)  # raises BitfinexShapeError on layout drift


@pytest.mark.integration
@pytest.mark.asyncio
async def test_ws_funding_offer_layout_contract() -> None:
    key = os.environ.get("BFX_API_KEY")
    secret = os.environ.get("BFX_API_SECRET")
    if not key or not secret:
        pytest.skip("requires BFX_API_KEY / BFX_API_SECRET for the live WS round-trip")

    captured: list[list] = []
    rows: list[list] = []
    deadline = time.monotonic() + 15.0
    async with websockets.connect("wss://api.bitfinex.com/ws/2", max_size=2**20) as ws:
        await ws.send(json.dumps(build_auth_payload(
            api_key=key, api_secret=secret, nonce_ms=int(time.time() * 1000),
        )))
        while time.monotonic() < deadline and not rows:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=2.0)
            except TimeoutError:
                continue
            msg = json.loads(raw)
            extracted = _extract_offer_rows(msg)
            if extracted:
                captured.append(msg)
                rows.extend(extracted)

    if not rows:
        pytest.skip("no active funding offers on this account during the capture window")

    for r in rows:
        parsed = parse_funding_offer_row(r)  # raises on layout drift
        assert parsed.venue_offer_id
        assert parsed.symbol
        assert parsed.status
        print(f"[ws_contract] voi={parsed.venue_offer_id} symbol={parsed.symbol} "
              f"status={parsed.status} rate={parsed.rate} period={parsed.period_days}")

    if os.environ.get("BFX_UPDATE_FIXTURE") == "1":
        _FIXTURE.write_text(json.dumps(captured, indent=2) + "\n")
        print(f"[ws_contract] wrote {_FIXTURE} ({len(captured)} frames, {len(rows)} rows)")
```

- [ ] **Step 2: Run the fast layer (gated test must be collected but skipped)**

Run: `cd backend_py && uv run pytest tests/external/bitfinex/test_funding_offer_ws_wire.py -m "not integration" -v`
Expected: `test_ws_funding_offer_fixture_parses_if_present` SKIPPED (no golden yet);
gated test deselected. No collection errors.

- [ ] **Step 3: Full gate**

Run: `cd backend_py && uv run pytest -m "not integration" && uv run mypy src/ && uv run ruff check`
Expected: full suite PASS; mypy + ruff clean.

- [ ] **Step 4: Commit**

```bash
git add backend_py/tests/external/bitfinex/test_funding_offer_ws_wire.py
git commit -m "✅ Test: gated-live WS funding-offer layout contract

Connects the real auth WS, captures fos/foc rows, validates via the shared
parse_funding_offer_row; fast layer replays the captured golden when present.
Extends the 2026-05-25 capture-and-replay methodology to the WS path."
```

---

## Final verification

- [ ] **Run the full non-integration suite + types + lint one more time**

Run: `cd backend_py && uv run pytest -m "not integration" && uv run mypy src/ && uv run ruff check`
Expected: all green.

- [ ] **Confirm the single-source invariant holds**

Run: `cd backend_py && grep -rn "d\[14\]\|d\[11\]\|o\[14\]\|d\[7\]" src/bfx_funding_bot/external/bitfinex/auth_ws.py src/bfx_funding_bot/external/bitfinex/auth_rest.py`
Expected: **no matches** — neither `auth_ws._parse_foc` nor `auth_rest.parse_active_funding_offers` indexes the funding-offer array directly anymore; both go through `parse_funding_offer_row`. (`_parse_fcn`/`_parse_fcu` still index the *credit* array `d[12]`/`d[13]` — that is a different wire object and is expected.)

---

## Rollout (post-merge, manual)

Not part of this plan's commits — for the deploy session:

1. (Opportunistic) capture the real WS golden when keys + a canary fill window are available:
   `BFX_API_KEY=... BFX_API_SECRET=... BFX_UPDATE_FIXTURE=1 uv run pytest -m integration -k ws_funding_offer --capture=no`
2. Deploy canary via the **manual** `scripts/deploy-koyeb.sh` (`no_deploy_on_push=true`). No new env vars.
3. Verify a canary fill produces an `ORDER_FILL` in `event_log` within seconds (not only at the ~90 s reconcile interval), and that reconcile divergence WARNs stop firing for fills the WS now catches.

## Out of scope (separate plans — see spec Non-goals)

- **(d)** WS reconnect / `venue_seq` gap → immediate reconcile trigger.
- **(e)** `position_state` snapshot table staleness (`reserved`/`updated_at` stuck at 06:00).
- Credit / interest / earnings tracking via `fcn`/`fcu` amounts.
