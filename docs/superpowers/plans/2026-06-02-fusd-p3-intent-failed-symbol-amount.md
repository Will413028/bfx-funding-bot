# fUSD-live P3 — symbol + amount on ReservationIntent / ReservationFailed Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give the only two symbol-less position events (`ReservationIntent`, `ReservationFailed`) a mandatory `symbol` + canonical `amount`, wire their producers, upcast legacy rows at the deserialize boundary, and convert the `DEFAULT_RECONCILE_SYMBOL` *live* fallback into a fail-loud invariant.

**Architecture:** Mirror the `ReservationClaimed` shape exactly (symbol mandatory-first, `amount`/`size_usdt` transitional pair, `__post_init__` → `_resolve_amount`). No DB migration — events live as JSON in `event_log.payload`. Schema evolution for old symbol-less rows is an **upcaster** in `deserialize_event` (inject `"fUST"`); the append-only log is never rewritten. `DEFAULT_RECONCILE_SYMBOL` is re-homed to `events.py` and used **only** by the upcaster.

**Tech Stack:** Python 3.13 frozen dataclasses (`slots=True`), pytest (asyncio), sqlite.

**Spec:** `docs/superpowers/specs/2026-06-02-fusd-live-enablement-design.md` §6.2.

**Key correctness fact (verified):** only the 5 events in `serialization._TYPE_BY_CLASS` (Intent/Claimed/Failed/OrderFill/Released) ever reach `store.append` — `serialize_event` raises on any other type, so `CancelRequested`/`PositionReconciled` never hit `_project_position_state`. After this plan all 5 carry a mandatory `symbol`, so dropping the `or DEFAULT_RECONCILE_SYMBOL` fallback at store.py:107 is safe.

**Ordering:** Run after/with P1; **before P2**. This plan adds `symbol=symbol` (the existing global param) to the boot-recovery `ReservationFailed`; P2 later switches that and the missing-release to per-claim `claim.symbol` and deletes the param.

**Gate (run before every commit):** `cd backend_py && uv run pytest -m "not integration" && uv run mypy src/ && uv run ruff check`

---

### Task 1: Reshape the two events + wire producers + upcaster (atomic schema change)

Making `symbol` mandatory is a wide atomic change: the dataclasses, both producers, the deserialize upcaster, the store fallback removal, and every existing test constructor must land together to keep the suite green.

**Files:**
- Modify: `backend_py/src/bfx_funding_bot/modules/execution/events.py:80-117` (+ move constant)
- Modify: `backend_py/src/bfx_funding_bot/modules/execution/event_store/serialization.py:55-71`
- Modify: `backend_py/src/bfx_funding_bot/modules/execution/event_store/store.py:41-50, 104-108, 156-171`
- Modify: `backend_py/src/bfx_funding_bot/modules/execution/middleware/reservation_emitting.py:76-80, 112-116`
- Modify: `backend_py/src/bfx_funding_bot/modules/execution/boot_recovery.py` (the `ReservationFailed` in `compute_recovery_actions`)
- Test: `backend_py/tests/modules/execution/test_events.py`, `tests/modules/execution/event_store/test_serialization.py`

- [ ] **Step 1: Write the failing tests (fields + legacy upcast)**

Add to `tests/modules/execution/test_events.py` (mirror `test_reservation_claimed_has_symbol_and_amount`):

```python
def test_reservation_intent_has_symbol_and_amount() -> None:
    e = ReservationIntent(
        cid=1, size_usdt=Decimal("100"), symbol="fUST",
        signal_correlation_id=uuid4(), account_id="default", is_simulated=False)
    assert e.symbol == "fUST"
    assert e.amount == Decimal("100")      # mirrored from size_usdt
    assert e.size_usdt == Decimal("100")


def test_reservation_failed_has_symbol_and_amount() -> None:
    e = ReservationFailed(
        cid=1, size_usdt=Decimal("100"), symbol="fUST",
        signal_correlation_id=uuid4(), account_id="default", is_simulated=False,
        reason="submit_failed")
    assert e.symbol == "fUST"
    assert e.amount == Decimal("100")
```

Add to `tests/modules/execution/event_store/test_serialization.py`:

```python
def test_intent_failed_legacy_payload_upcasts_symbol() -> None:
    """Pre-symbol event_log rows have no `symbol`; deserialize injects fUST."""
    legacy = {"cid": 9, "size_usdt": "7.5",
              "signal_correlation_id": str(_SCID), "account_id": "acct",
              "is_simulated": True, "occurred_at_ms": 1000}
    ev = deserialize_event("RESERVATION_INTENT", legacy)
    assert ev.symbol == "fUST"          # type: ignore[attr-defined]
    assert ev.amount == Decimal("7.5")  # type: ignore[attr-defined]
```

- [ ] **Step 2: Run them — verify they fail**

Run: `cd backend_py && uv run pytest tests/modules/execution/test_events.py -k "intent_has_symbol or failed_has_symbol" tests/modules/execution/event_store/test_serialization.py -k "legacy_payload_upcasts" -v`
Expected: FAIL — `TypeError: __init__() got an unexpected keyword argument 'symbol'` (and the upcast test errors building the event).

- [ ] **Step 3: Reshape `ReservationIntent` and `ReservationFailed`**

In `events.py`, replace the two class bodies (mirror `ReservationClaimed`). `ReservationIntent`:

```python
@dataclass(frozen=True, slots=True)
class ReservationIntent:
    """A2 write-ahead intent — durable record BEFORE the venue REST submit.

    Persisted in txn1 so a crash between submit-call and outcome leaves a
    recoverable PENDING claim (resolved at boot in 3a-recovery). Carries no
    venue_offer_id (unknown until CLAIMED). Not published to the bus.

    `symbol` is the offer currency; MANDATORY (no default). `amount` is the
    native reserve size; `size_usdt` is the transitional alias (see
    _resolve_amount). Legacy rows predate `symbol` and are upcast to
    DEFAULT_RECONCILE_SYMBOL in deserialize_event.
    """
    symbol: str  # mandatory, FIRST (frozen+slots: non-default must precede defaulted)
    cid: int
    signal_correlation_id: UUID
    account_id: str
    is_simulated: bool
    amount: Decimal | None = None
    size_usdt: Decimal | None = None  # transitional alias; mapped to amount
    venue_seq: int | None = None
    event_seq: int | None = None
    occurred_at_ms: int | None = None
    recorded_at_ms: int | None = None

    def __post_init__(self) -> None:
        _resolve_amount(self)
```

`ReservationFailed`:

```python
@dataclass(frozen=True, slots=True)
class ReservationFailed:
    """A2 terminal outcome — venue REST submit failed; intent resolves to FAILED.

    Ledger effect: none (reserved untouched). Not published to the bus.
    `symbol`/`amount`/`size_usdt`: see ReservationIntent (symbol mandatory).
    """
    symbol: str  # mandatory, FIRST
    cid: int
    signal_correlation_id: UUID
    account_id: str
    is_simulated: bool
    reason: str  # e.g. "submit_failed"
    amount: Decimal | None = None
    size_usdt: Decimal | None = None  # transitional alias; mapped to amount
    venue_seq: int | None = None
    event_seq: int | None = None
    occurred_at_ms: int | None = None
    recorded_at_ms: int | None = None

    def __post_init__(self) -> None:
        _resolve_amount(self)
```

- [ ] **Step 4: Re-home `DEFAULT_RECONCILE_SYMBOL` into events.py**

Add near the top of `events.py` (after `__SCHEMA_VERSION__`, before `_resolve_amount`):

```python
# Schema-evolution upcast value for the two events that gained a mandatory
# `symbol` after early event_log rows were written. Used ONLY by
# serialization.deserialize_event for those legacy rows; every live event now
# carries an explicit symbol. The canary was fUST-only when those rows existed.
DEFAULT_RECONCILE_SYMBOL = "fUST"
```

- [ ] **Step 5: Add the deserialize upcaster**

In `serialization.py`, import the constant and inject it in `deserialize_event`:

```python
from bfx_funding_bot.modules.execution.events import (
    DEFAULT_RECONCILE_SYMBOL,
    OrderFilled,
    ReservationClaimed,
    ReservationFailed,
    ReservationIntent,
    ReservationReleased,
)
...
def deserialize_event(event_type: str, payload: dict[str, Any]) -> object:
    cls = _CLASS_BY_TYPE.get(event_type)
    if cls is None:
        raise ValueError(f"unknown event_type: {event_type}")
    # Upcast: Intent/Failed gained a mandatory `symbol` after these rows were
    # written. Inject the historically-correct value for legacy payloads; new
    # rows already carry symbol so this is a no-op for them.
    if event_type in ("RESERVATION_INTENT", "RESERVATION_FAILED") and payload.get("symbol") is None:
        payload = {**payload, "symbol": DEFAULT_RECONCILE_SYMBOL}
    kwargs: dict[str, Any] = {field: _coerce(field, payload.get(field)) for field in _FIELDS[cls]}
    return cls(**kwargs)
```

- [ ] **Step 6: Wire the producers (reservation_emitting.py)**

Add `symbol=decision.symbol` to both constructions (decision is in scope at both sites):

```python
        # ReservationIntent (~76-80):
        await self._persister.persist(ReservationIntent(
            cid=cid, size_usdt=size, signal_correlation_id=scid,
            account_id=ctx.account_id, is_simulated=self._is_simulated,
            occurred_at_ms=intent_ms, symbol=decision.symbol,
        ))
        # ReservationFailed (~112-116):
            await self._persister.persist(ReservationFailed(
                cid=cid, size_usdt=size, signal_correlation_id=scid,
                account_id=ctx.account_id, is_simulated=self._is_simulated,
                reason="submit_failed", occurred_at_ms=outcome_ms,
                symbol=decision.symbol,
            ))
```

- [ ] **Step 7: Wire the boot-recovery `ReservationFailed` (use the existing global param)**

In `boot_recovery.py`, the `compute_recovery_actions` stale-PENDING → FAILED loop constructs `ReservationFailed(...)` without symbol. Add `symbol=symbol` (the function's existing `symbol: str` kwarg — P2 will later replace this with `c.symbol` and remove the param):

```python
            actions.append(ReservationFailed(
                cid=c.cid, size_usdt=c.size_usdt,
                signal_correlation_id=c.signal_correlation_id,
                account_id=account_id, is_simulated=is_simulated,
                reason="unresolved_at_boot", occurred_at_ms=now_ms,
                symbol=symbol,
            ))
```

- [ ] **Step 8: Remove the store.py live fallback + the moved constant**

In `store.py`:
- Delete the `DEFAULT_RECONCILE_SYMBOL = "fUST"` definition and its comment block (~41-50). It now lives in events.py and is only used by serialization.
- At the append projection (~104-108) drop the fallback:

```python
        await self._project_position_state(
            session, etype, account_id, getattr(_ev, "amount", None),
            row.event_seq, occurred_at_ms,
            symbol=_ev.symbol,
        )
```

- In `_project_offer_claims` (~156-160) drop the `size_usdt` fallback (all claim-bearing events now carry `amount`) and update the comment:

```python
        now_ms: int = _ev.occurred_at_ms or 0
        # All claim-bearing events now carry canonical native `amount` (mirrored
        # from size_usdt by events._resolve_amount); use it directly.
        await self._upsert_claim(
            session,
            cid=cid,
            account_id=account_id,
            state=state,
            venue_offer_id=getattr(_ev, "venue_offer_id", None),
            size_usdt=Decimal(str(_ev.amount)),
            signal_correlation_id=str(_ev.signal_correlation_id),
            occurred_at_ms=now_ms,
            last_updated_ms=now_ms,
        )
```

- [ ] **Step 9: Update any other references to `store.DEFAULT_RECONCILE_SYMBOL`**

Run: `cd backend_py && grep -rn "DEFAULT_RECONCILE_SYMBOL" src tests`
For each import from `...event_store.store`, repoint to `...execution.events`. Update the failing constructors and the serialization round-trip parametrize that build `ReservationIntent`/`ReservationFailed` without `symbol` — add `symbol="fUST"` (e.g. in `test_serialization.py::test_intent_failed_roundtrip` parametrize entries).

- [ ] **Step 10: Run the new tests — verify they pass**

Run: `cd backend_py && uv run pytest tests/modules/execution/test_events.py tests/modules/execution/event_store/test_serialization.py -v`
Expected: PASS (new field + upcast tests green; round-trip tests green with `symbol="fUST"`).

- [ ] **Step 11: Full gate — fix all remaining constructors**

Run: `cd backend_py && uv run pytest -m "not integration"`
Expected: green. If any test fails with a missing-`symbol` TypeError on `ReservationIntent`/`ReservationFailed`, add `symbol="fUST"` (or the test's symbol) to that constructor. Re-run until green. Then:

Run: `uv run mypy src/ && uv run ruff check`
Expected: clean. (`getattr(_ev, "amount", None)` at the projection call stays — `_ev` is `Any`, so `_ev.symbol`/`_ev.amount` type-check.)

- [ ] **Step 12: Commit**

```bash
git add backend_py/src/bfx_funding_bot/modules/execution/events.py \
        backend_py/src/bfx_funding_bot/modules/execution/event_store/serialization.py \
        backend_py/src/bfx_funding_bot/modules/execution/event_store/store.py \
        backend_py/src/bfx_funding_bot/modules/execution/middleware/reservation_emitting.py \
        backend_py/src/bfx_funding_bot/modules/execution/boot_recovery.py \
        backend_py/tests/
git commit -m "✨ Feat: symbol+amount on ReservationIntent/Failed + deserialize upcaster (fUSD P3)"
```

---

### Task 2: Verify fUST byte-identical + projection invariants

**Files:**
- Test: `backend_py/tests/modules/execution/event_store/test_store_append_unit.py`

- [ ] **Step 1: Add a fail-loud regression — Intent/Failed project under their own symbol**

Add a test that appends a `ReservationIntent(symbol="fUST", ...)` and asserts the `offer_claims` PENDING row + `position_state[fUST]` row are created (and `reserved`/`realized` unchanged — Intent has no ledger effect), proving the symbol routes correctly and no `position_state[fUSD]` row appears:

```python
async def test_intent_projects_under_its_own_symbol(sqlite_session: AsyncSession) -> None:
    await _create_all(sqlite_session)
    store = PostgresEventStore(deployment_environment="ci")
    await store.append(sqlite_session, ReservationIntent(
        cid=701, size_usdt=Decimal("100"), symbol="fUST",
        signal_correlation_id=_SCID, account_id="acct", is_simulated=True,
        occurred_at_ms=1000))
    await sqlite_session.flush()
    ps = (await sqlite_session.execute(select(PositionStateRow).where(
        PositionStateRow.account_id == "acct"))).scalars().all()
    assert [p.symbol for p in ps] == ["fUST"]      # no fUSD row created
    assert ps[0].reserved == Decimal("0")          # intent has no ledger effect
```

(Import `ReservationIntent`, `PositionStateRow`, `_SCID`, `_create_all`, `sqlite_session` per the file's existing imports/fixtures.)

- [ ] **Step 2: Run + gate + commit**

Run: `cd backend_py && uv run pytest tests/modules/execution/event_store/test_store_append_unit.py -v && uv run pytest -m "not integration" && uv run mypy src/ && uv run ruff check`
Expected: green.

```bash
git add backend_py/tests/modules/execution/event_store/test_store_append_unit.py
git commit -m "✅ Test: Intent/Failed project under own symbol; fUST byte-identical (fUSD P3)"
```

---

## Self-Review

- **Spec coverage (§6.2):** dataclass reshape (Task 1 Step 3), constant re-home (Step 4), upcaster (Step 5), producers (Step 6), boot-recovery FAILED via global param (Step 7, handed to P2), store fallback removal + amount simplification (Step 8), reference sweep (Step 9), fUST byte-identical + projection (Task 2). ✓
- **No DB migration:** confirmed — events are JSON in `event_log.payload`.
- **No placeholders:** all steps carry real code/commands.
- **Type consistency:** `symbol: str` mandatory-first on both events ↔ `_ev.symbol` (store.py:107) ↔ `payload["symbol"]` upcast key ↔ `decision.symbol` (producers). `DEFAULT_RECONCILE_SYMBOL` defined once (events.py), imported by serialization only.
- **Ordering hand-off:** Step 7 deliberately uses the global `symbol` param so this plan is green standalone; P2 removes the param and switches to `c.symbol`.
