# Per-Currency Native Allocation — Phase 1 (per-symbol ledger plumbing) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the execution ledger and its persisted state per-symbol (native units) — events carry `symbol`, the ledger is keyed by symbol, `position_state` is per-symbol, reconcile/boot fire per-symbol, and guards/NAV read per-symbol — while keeping the system green at every commit and behaviour identical for the single active currency (fUST).

**Architecture:** An expand/contract refactor. Phase 1 turns three scalar ledger counters (`_reserved`/`_realized`/`_available`) into per-symbol dicts and threads `symbol` end-to-end (events → ledger → `position_state` → reconcile → guards → NAV). Cap/buffer/threshold VALUES stay the existing global env values; the native per-currency config maps are Phase 2. Field/getter renames land additively (transitional aliases) so every commit stays green; a documented cleanup removes the aliases later.

**Tech Stack:** Python 3.13 (run via `uv` from `backend_py/`), SQLAlchemy 2.0 async, Alembic, pytest. Spec: `docs/superpowers/specs/2026-05-31-per-currency-native-allocation-design.md`.

---

## Context for the implementer (read first)

- **Working dir:** every `pytest`/`uv`/`ruff`/`mypy`/`alembic` command runs from `backend_py/`. From repo root you hit pyenv 3.12 and break on sqlalchemy.
- **Gate per task commit:** `cd backend_py && uv run pytest -m "not integration" -q` green; `uv run mypy src/` clean; `uv run ruff check` clean. (`scripts/` is not in the mypy gate.) The migration (`alembic upgrade head` / `alembic check`) needs a fresh Neon credential — unit tests use sqlite `create_all` and do not.
- **This plan was authored by 5 parallel cluster agents that each read the real code**, then stitched in dependency order: **A (events + DecisionPayload) → C (position_state table + migration) → B (ledger) → D (reconcile + boot) → E (guards + NAV)**. Tasks are globally renumbered; each cluster's own intro prose is retained above its tasks.

## Stitch notes & overrides (apply these — they resolve cross-cluster seams)

These OVERRIDE anything in the per-cluster prose below where they conflict:

- **O1 — additive field rename (keep green):** Cluster A keeps `size_usdt` (on the three execution events) and `reserved_usdt`/`realized_usdt`/`available_usdt` (on `PositionReconciled`) as **transitional constructor kwargs + read-alias properties**, with `amount` / `reserved` / `realized` / `available` as the canonical fields and `symbol: str = "fUSD"` (the legacy single-currency fallback) as the default. Do **not** assert the old names are gone in Phase 1 — their removal is a deferred follow-up (see end). This is what lets producers/consumers migrate without ever going red.
- **O2 — optional `symbol` on ledger getters (keep green):** Where Cluster B gives the ledger getters (`current_exposure`, `reserved_exposure`, `realized_exposure`, `available_balance`) a `symbol` parameter, declare it as **`symbol: str | None = None`**, and when `symbol is None` return the **cross-symbol SUM** of that counter (back-compat for any caller not yet migrated). Every cluster's own tests pass a concrete symbol, so they are unaffected; un-migrated callers calling `current_exposure()` keep their exact current behaviour. The deferred cleanup makes `symbol` required and drops the `None` branch. This keeps the FULL suite green at every commit (no "green-in-isolation only" windows).
- **O3 — source of "configured symbols":** the per-symbol reconcile loop (Cluster D Task in daemon wiring) iterates the **distinct `CellConfig.symbol` values across `config.cells`** (order-preserving). Phase 2 may switch this to the `caps`/`buffers` map keys; the `BootRecovery(symbols=...)` interface is unchanged either way.
- **O4 — `ReconcileResult` stays aggregate:** `ReconcileResult` / `PeriodicReconcile` keep their `*_usdt` field names holding the Σ-across-symbols totals in Phase 1 (not renamed). Only `position_state` columns and the event fields rename.
- **O5 — `store.py` reads:** the two `size_usdt` reads in `store.py` keep reading `.size_usdt` (resolves via the O1 alias) and stay green; renaming them to `.amount` is part of the deferred cleanup.
- **O6 — single migration:** only Cluster C adds an Alembic migration (`b7c1d2e3f4a5`, `down_revision = a3ae9a60862a`). No other task adds a migration; if you regenerate, do not let autogenerate create a second head.
- **O7 — execution order = file order.** Do not reorder. Each task commit must keep `uv run pytest -m "not integration"` green (O1+O2 guarantee this is achievable).
- **O8 — `set_position_snapshot` keyword params keep their `*_usdt` names.** Cluster C keeps the writer's keyword parameters `reserved_usdt=` / `realized_usdt=` (plus the new `symbol=`); only the persisted COLUMNS rename to `.reserved` / `.realized`. Therefore every caller and stub in Cluster D (`BootRecovery.run`, `PeriodicReconcile`, the `_StubStore`) calls `set_position_snapshot(..., symbol=<sym>, reserved_usdt=<x>, realized_usdt=<y>, ...)`. Where Cluster D's draft passes `reserved=`/`realized=` (it left a NOTE about exactly this), use `reserved_usdt=`/`realized_usdt=`. (Renaming the writer params to native is a deferred-cleanup item.)
- **O9 — C's column-rename task also fixes the `ledger.from_snapshot` read in the same commit.** When Cluster C renames `reserved_usdt`→`reserved` / `realized_usdt`→`realized` on `PositionStateRow`, it MUST also update the existing reads in `ledger.from_snapshot` (`row.reserved_usdt`→`row.reserved`, `row.realized_usdt`→`row.realized`, currently `ledger.py:70-71`) in that same commit, so no commit references a renamed-away attribute. Cluster B's `from_snapshot` task then restructures that method to load all per-symbol rows.
- **O10 — under O2, drop Cluster B's caller-bridge.** Because the getters are optional-symbol (O2: no-arg = cross-symbol sum), the in-repo callers in `hard_guards.py` / `reconciler.py` keep working unchanged, so Cluster B's "Restore green" task SKIPS its hard_guards/reconciler bridge edits and keeps ONLY its legacy-ledger-test migrations (tests asserting the old scalar `_reserved`/`_realized` move to dict access or the getters). The real per-symbol guard/reconciler wiring is Tasks 15 / 17 / 18.

## Real-money guardrail (Phase-1 branch)

Do **not** deploy the canary from this branch until Phase 1 is COMPLETE (all producers pass `symbol=`, i.e. through Task 16). Mid-branch, events emitted without `symbol=` fall back to the `"fUSD"` default and bucket under fUSD; the O2 no-arg sum keeps TOTALS (and therefore tests + the existing global guards) correct, but per-symbol buckets are provisional until producers migrate. Koyeb auto-deploy is off and deploys are manual, so this is a guardrail, not a live risk — just never run the deploy script from a mid-Phase-1 commit.

---

# Cluster A — Events + DecisionPayload

## Cluster A — Events + DecisionPayload (shared contract types)

Authored against the ACTUAL code read at:
- `src/bfx_funding_bot/modules/execution/events.py` (the 4 event classes are `ReservationClaimed` L65-80, `OrderFilled` L83-102, `ReservationReleased` L105-121, `PositionReconciled` L140-160)
- `src/bfx_funding_bot/modules/marketfeed/schemas.py` (`DecisionPayload` L101-128, `Envelope.cell` L245)

Baseline verified GREEN before starting: `uv run pytest tests/modules/execution/test_events.py tests/modules/marketfeed/test_schemas.py -q` → `24 passed`.

### Green-at-every-commit design note (READ FIRST)

The contract renames `size_usdt -> amount` on `ReservationClaimed/OrderFilled/ReservationReleased`, but Cluster A is only permitted to edit `events.py` + `schemas.py`. Every existing **producer** still constructs these events with `size_usdt=...` (see callsites in `external/bitfinex/ws_dispatcher.py`, `external/bitfinex/fill_tracker.py`, `modules/execution/middleware/reservation_emitting.py`, `modules/execution/boot_recovery.py`) and every **consumer** reads `.size_usdt` (in `modules/execution/ledger.py`, `modules/admin/smoke_runner.py`). To keep the system GREEN while those callsites are migrated by later clusters, Cluster A:

- makes `amount: Decimal` the **canonical stored field**,
- keeps `size_usdt` accepted as a **transitional constructor keyword** (mapped to `amount` in `__post_init__`) AND readable as a **`@property`** (verified to work with `frozen=True, slots=True`),
- adds `symbol: str = "fUSD"` with the existing single-currency fallback default (`"fUSD"` is the established fallback in `boot_recovery.py` signatures) so producers that don't yet pass a symbol stay valid.

This means at the Cluster A commit the rename is **additive + back-compat**; later clusters flip producers to `amount=`/`symbol=` and the final cluster removes the transitional `size_usdt` keyword. The unit tests below assert the NEW canonical names exist and behave correctly; they do NOT assert the transitional keyword is gone (that removal is a later cluster's commit).

For `PositionReconciled` the rename is `reserved_usdt/realized_usdt/available_usdt -> reserved/realized/available`. Its only producer is `boot_recovery.py:277` and its only consumers are `ledger.py:127-129` and `nav_pnl_source.py:56` — all owned by LATER clusters. To keep green here, `PositionReconciled` also keeps the three old names as transitional keyword aliases + read properties.

---

### Task 1: Add `symbol` + canonical `amount` (back-compat `size_usdt`) to the three execution events

**Files:**
- `src/bfx_funding_bot/modules/execution/events.py` (modify `ReservationClaimed` L65-80, `OrderFilled` L83-102, `ReservationReleased` L105-121)
- `tests/modules/execution/test_events.py` (add tests)

- [ ] **Step 1: Write failing tests.** Append to `tests/modules/execution/test_events.py`:

```python
def test_reservation_claimed_has_symbol_and_amount() -> None:
    e = ReservationClaimed(
        cid=1,
        venue_offer_id="v1",
        amount=Decimal("100"),
        symbol="fUST",
        signal_correlation_id=uuid4(),
        account_id="default",
        is_simulated=False,
    )
    assert e.amount == Decimal("100")
    assert e.symbol == "fUST"
    # transitional read alias still resolves to amount
    assert e.size_usdt == Decimal("100")


def test_reservation_claimed_symbol_defaults_to_fusd() -> None:
    e = ReservationClaimed(
        cid=1,
        venue_offer_id="v1",
        amount=Decimal("100"),
        signal_correlation_id=uuid4(),
        account_id="default",
        is_simulated=False,
    )
    assert e.symbol == "fUSD"


def test_reservation_claimed_back_compat_size_usdt_kwarg() -> None:
    # legacy producers still pass size_usdt= until they migrate; it maps to amount
    e = ReservationClaimed(
        cid=1,
        venue_offer_id="v1",
        size_usdt=Decimal("250"),
        signal_correlation_id=uuid4(),
        account_id="default",
        is_simulated=False,
    )
    assert e.amount == Decimal("250")
    assert e.size_usdt == Decimal("250")
    assert e.symbol == "fUSD"


def test_order_filled_has_symbol_and_amount() -> None:
    e = OrderFilled(
        cid=1,
        venue_offer_id="v1",
        credit_id="C-1",
        amount=Decimal("100"),
        symbol="fUST",
        fill_rate=0.0005,
        signal_correlation_id=uuid4(),
        account_id="default",
        is_simulated=False,
    )
    assert e.amount == Decimal("100")
    assert e.symbol == "fUST"
    assert e.size_usdt == Decimal("100")


def test_order_filled_back_compat_size_usdt_kwarg() -> None:
    e = OrderFilled(
        cid=1,
        venue_offer_id="v1",
        credit_id=None,
        size_usdt=Decimal("70"),
        fill_rate=0.0005,
        signal_correlation_id=uuid4(),
        account_id="default",
        is_simulated=False,
    )
    assert e.amount == Decimal("70")
    assert e.symbol == "fUSD"


def test_reservation_released_has_symbol_and_amount() -> None:
    e = ReservationReleased(
        cid=1,
        venue_offer_id="v1",
        amount=Decimal("100"),
        symbol="fUST",
        reason="user_cancel",
        signal_correlation_id=uuid4(),
        account_id="default",
        is_simulated=False,
    )
    assert e.amount == Decimal("100")
    assert e.symbol == "fUST"
    assert e.size_usdt == Decimal("100")


def test_reservation_released_back_compat_size_usdt_kwarg() -> None:
    e = ReservationReleased(
        cid=1,
        venue_offer_id="v1",
        size_usdt=Decimal("30"),
        reason="expired",
        signal_correlation_id=uuid4(),
        account_id="default",
        is_simulated=False,
    )
    assert e.amount == Decimal("30")
    assert e.symbol == "fUSD"
```

- [ ] **Step 2: Run it — expect FAIL.**
  Command: `uv run pytest tests/modules/execution/test_events.py -q`
  Expected: the new tests error with `TypeError: __init__() got an unexpected keyword argument 'amount'` (and `'symbol'`); the original 24 tests still pass.

- [ ] **Step 3: Implement.** In `src/bfx_funding_bot/modules/execution/events.py`, replace the `ReservationClaimed` class (current L65-80):

  OLD:
```python
@dataclass(frozen=True, slots=True)
class ReservationClaimed:
    """Submit returned status ∈ {submitted, filled} — capital reserved at venue.

    Ledger effect: _reserved += size_usdt.
    """
    cid: int
    venue_offer_id: str
    size_usdt: Decimal
    signal_correlation_id: UUID
    account_id: str
    is_simulated: bool
    venue_seq: int | None = None
    event_seq: int | None = None
    occurred_at_ms: int | None = None
    recorded_at_ms: int | None = None
```

  NEW:
```python
@dataclass(frozen=True, slots=True)
class ReservationClaimed:
    """Submit returned status ∈ {submitted, filled} — capital reserved at venue.

    Ledger effect: reserved[symbol] += amount (native units).

    `symbol` is the offer currency (e.g. "fUST"); defaults to the legacy
    single-currency fallback "fUSD". `amount` is the native reserve size;
    `size_usdt` is a transitional read alias + back-compat constructor kwarg
    kept until producers migrate (Phase 1 per-symbol ledger work).
    """
    cid: int
    venue_offer_id: str
    signal_correlation_id: UUID
    account_id: str
    is_simulated: bool
    amount: Decimal | None = None
    symbol: str = "fUSD"
    size_usdt: Decimal | None = None  # transitional: legacy producers; mapped to amount
    venue_seq: int | None = None
    event_seq: int | None = None
    occurred_at_ms: int | None = None
    recorded_at_ms: int | None = None

    def __post_init__(self) -> None:
        _resolve_amount(self)

    @property
    def amount_native(self) -> Decimal:
        assert self.amount is not None
        return self.amount
```

  Replace the `OrderFilled` class (current L83-102):

  OLD:
```python
@dataclass(frozen=True, slots=True)
class OrderFilled:
    """Offer → credit transition (paper synchronous OR live WS `foc` EXECUTED).

    Ledger effect: _reserved -= size_usdt; _realized += size_usdt.
    `credit_id` is None for paper (no real credit) and for live (the `foc`
    EXECUTED frame carries no credit id; the fill is keyed by venue_offer_id).
    """
    cid: int
    venue_offer_id: str
    credit_id: str | None
    size_usdt: Decimal
    fill_rate: float
    signal_correlation_id: UUID
    account_id: str
    is_simulated: bool
    venue_seq: int | None = None
    event_seq: int | None = None
    occurred_at_ms: int | None = None
    recorded_at_ms: int | None = None
```

  NEW:
```python
@dataclass(frozen=True, slots=True)
class OrderFilled:
    """Offer → credit transition (paper synchronous OR live WS `foc` EXECUTED).

    Ledger effect: reserved[symbol] -= amount; realized[symbol] += amount.
    `credit_id` is None for paper (no real credit) and for live (the `foc`
    EXECUTED frame carries no credit id; the fill is keyed by venue_offer_id).

    `symbol`/`amount`/`size_usdt`: see ReservationClaimed.
    """
    cid: int
    venue_offer_id: str
    credit_id: str | None
    fill_rate: float
    signal_correlation_id: UUID
    account_id: str
    is_simulated: bool
    amount: Decimal | None = None
    symbol: str = "fUSD"
    size_usdt: Decimal | None = None  # transitional: legacy producers; mapped to amount
    venue_seq: int | None = None
    event_seq: int | None = None
    occurred_at_ms: int | None = None
    recorded_at_ms: int | None = None

    def __post_init__(self) -> None:
        _resolve_amount(self)
```

  Replace the `ReservationReleased` class (current L105-121):

  OLD:
```python
@dataclass(frozen=True, slots=True)
class ReservationReleased:
    """Offer cancelled / expired without fill.

    Ledger effect: _reserved -= size_usdt (floor at 0; emits warning + counts).
    """
    cid: int
    venue_offer_id: str
    size_usdt: Decimal
    reason: str  # "venue_cancel" / "user_cancel" / "expired" / "missing_from_venue"
    signal_correlation_id: UUID
    account_id: str
    is_simulated: bool
    venue_seq: int | None = None
    event_seq: int | None = None
    occurred_at_ms: int | None = None
    recorded_at_ms: int | None = None
```

  NEW:
```python
@dataclass(frozen=True, slots=True)
class ReservationReleased:
    """Offer cancelled / expired without fill.

    Ledger effect: reserved[symbol] -= amount (floor at 0; emits warning + counts).
    `symbol`/`amount`/`size_usdt`: see ReservationClaimed.
    """
    cid: int
    venue_offer_id: str
    reason: str  # "venue_cancel" / "user_cancel" / "expired" / "missing_from_venue"
    signal_correlation_id: UUID
    account_id: str
    is_simulated: bool
    amount: Decimal | None = None
    symbol: str = "fUSD"
    size_usdt: Decimal | None = None  # transitional: legacy producers; mapped to amount
    venue_seq: int | None = None
    event_seq: int | None = None
    occurred_at_ms: int | None = None
    recorded_at_ms: int | None = None

    def __post_init__(self) -> None:
        _resolve_amount(self)
```

  Add the shared `_resolve_amount` helper right after the imports block (after `__SCHEMA_VERSION__ = 2`, current L23). Because these dataclasses are `frozen=True`, mutation in `__post_init__` must go through `object.__setattr__`:

```python
def _resolve_amount(ev: object) -> None:
    """Reconcile transitional `size_usdt` with canonical `amount` on frozen events.

    Exactly one of the two must be provided by the caller. We mirror the value
    into BOTH attributes so `.amount` (canonical) and `.size_usdt` (legacy read
    path in ledger/smoke_runner) agree until all callsites migrate to `amount`.
    """
    amount = getattr(ev, "amount", None)
    size_usdt = getattr(ev, "size_usdt", None)
    if amount is None and size_usdt is None:
        raise TypeError(
            f"{type(ev).__name__} requires `amount` (or transitional `size_usdt`)"
        )
    if amount is None:
        object.__setattr__(ev, "amount", size_usdt)
    if size_usdt is None:
        object.__setattr__(ev, "size_usdt", amount)
```

  Note: `size_usdt` stays a real slot (not a `@property`) so the legacy read path `event.size_usdt` in `ledger.py`/`smoke_runner.py` keeps working with zero churn, and legacy `size_usdt=` constructor kwargs keep working. The test `test_reservation_claimed_has_symbol_and_amount` asserts `e.size_usdt == Decimal("100")` which is satisfied by the mirror.

- [ ] **Step 4: Run it — expect PASS.**
  Command: `uv run pytest tests/modules/execution/test_events.py -q`
  Expected: all tests pass (original 24 + new). Then run types + lint:
  `uv run mypy src/bfx_funding_bot/modules/execution/events.py && uv run ruff check src/bfx_funding_bot/modules/execution/events.py`
  Expected: `Success: no issues found` from mypy, `All checks passed!` from ruff.

- [ ] **Step 5: Run the full unit gate (regression guard for producers/consumers).**
  Command: `uv run pytest -m "not integration" -q`
  Expected: all pass. (Existing producers still pass `size_usdt=`; existing consumers still read `.size_usdt`. The mirror keeps both green.)

- [ ] **Step 6: Commit.**
  `git add src/bfx_funding_bot/modules/execution/events.py tests/modules/execution/test_events.py`
  `git commit -m "✨ Feat: add symbol + canonical amount (back-compat size_usdt) to execution events"`

---

### Task 2: Add `symbol` + canonical `reserved/realized/available` (back-compat `*_usdt`) to `PositionReconciled`

**Files:**
- `src/bfx_funding_bot/modules/execution/events.py` (modify `PositionReconciled` L140-160)
- `tests/modules/execution/test_events.py` (add tests)

- [ ] **Step 1: Write failing tests.** Append to `tests/modules/execution/test_events.py`:

```python
def test_position_reconciled_has_symbol_and_native_fields() -> None:
    from bfx_funding_bot.modules.execution.events import PositionReconciled

    e = PositionReconciled(
        account_id="default",
        symbol="fUST",
        reserved=Decimal("300"),
        realized=Decimal("450"),
        available=Decimal("19.10"),
        n_offers=2,
        n_credits=3,
        occurred_at_ms=1000,
    )
    assert e.symbol == "fUST"
    assert e.reserved == Decimal("300")
    assert e.realized == Decimal("450")
    assert e.available == Decimal("19.10")
    # transitional read aliases still resolve
    assert e.reserved_usdt == Decimal("300")
    assert e.realized_usdt == Decimal("450")
    assert e.available_usdt == Decimal("19.10")


def test_position_reconciled_symbol_defaults_to_fusd() -> None:
    from bfx_funding_bot.modules.execution.events import PositionReconciled

    e = PositionReconciled(
        account_id="default",
        reserved=Decimal("300"),
        realized=Decimal("450"),
        available=Decimal("19.10"),
        n_offers=2,
        n_credits=3,
        occurred_at_ms=1000,
    )
    assert e.symbol == "fUSD"


def test_position_reconciled_back_compat_usdt_kwargs() -> None:
    from bfx_funding_bot.modules.execution.events import PositionReconciled

    # legacy producer (boot_recovery) still passes *_usdt= until it migrates
    e = PositionReconciled(
        account_id="default",
        reserved_usdt=Decimal("300"),
        realized_usdt=Decimal("450"),
        available_usdt=Decimal("19.10"),
        n_offers=2,
        n_credits=3,
        occurred_at_ms=1000,
    )
    assert e.reserved == Decimal("300")
    assert e.realized == Decimal("450")
    assert e.available == Decimal("19.10")
    assert e.symbol == "fUSD"
```

- [ ] **Step 2: Run it — expect FAIL.**
  Command: `uv run pytest tests/modules/execution/test_events.py -k position_reconciled -q`
  Expected: errors with `TypeError: __init__() got an unexpected keyword argument 'reserved'` / `'symbol'`.

- [ ] **Step 3: Implement.** In `src/bfx_funding_bot/modules/execution/events.py`, replace the `PositionReconciled` class (current L140-160):

  OLD:
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

  NEW:
```python
@dataclass(frozen=True, slots=True)
class PositionReconciled:
    """Periodic venue snapshot result — in-process pub/sub signal ONLY.

    NOT persisted to event_log. Emitted by BootRecovery / PeriodicReconcile
    after fetching /funding/offers, /funding/credits and /wallets. Drives the
    absolute set in PaperPositionLedger.on_position_reconciled(); the
    store.set_position_snapshot() direct write persists reserved/realized to
    position_state (available is in-memory only — not persisted).

    One event is fired PER SYMBOL (native units). `symbol` is the offer
    currency (defaults to legacy "fUSD"). reserved/realized/available are the
    canonical native fields; `*_usdt` are transitional read aliases +
    back-compat constructor kwargs kept until producers/consumers migrate.

    reserved  = Σ(active offers in `symbol`)  — venue snapshot, not accumulation.
    realized  = Σ(active credits in `symbol`) — venue snapshot.
    available = funding-wallet available balance for `symbol`'s currency.
    """
    account_id: str
    n_offers: int
    n_credits: int
    occurred_at_ms: int
    symbol: str = "fUSD"
    reserved: Decimal | None = None
    realized: Decimal | None = None
    available: Decimal | None = None
    reserved_usdt: Decimal | None = None  # transitional alias of reserved
    realized_usdt: Decimal | None = None  # transitional alias of realized
    available_usdt: Decimal | None = None  # transitional alias of available

    def __post_init__(self) -> None:
        _resolve_position_fields(self)
```

  Add the helper `_resolve_position_fields` directly below `_resolve_amount` (from Task 1):

```python
def _resolve_position_fields(ev: object) -> None:
    """Reconcile transitional `*_usdt` with canonical reserved/realized/available.

    For each of the three buckets, exactly one of (canonical, `_usdt` alias)
    must be supplied; we mirror into both so old (`.reserved_usdt`) and new
    (`.reserved`) read paths agree until callsites migrate.
    """
    for canonical, legacy in (
        ("reserved", "reserved_usdt"),
        ("realized", "realized_usdt"),
        ("available", "available_usdt"),
    ):
        c_val = getattr(ev, canonical, None)
        l_val = getattr(ev, legacy, None)
        if c_val is None and l_val is None:
            raise TypeError(
                f"{type(ev).__name__} requires `{canonical}` "
                f"(or transitional `{legacy}`)"
            )
        if c_val is None:
            object.__setattr__(ev, canonical, l_val)
        if l_val is None:
            object.__setattr__(ev, legacy, c_val)
```

- [ ] **Step 4: Run it — expect PASS.**
  Command: `uv run pytest tests/modules/execution/test_events.py -q`
  Expected: all pass.
  Then: `uv run mypy src/bfx_funding_bot/modules/execution/events.py && uv run ruff check src/bfx_funding_bot/modules/execution/events.py`
  Expected: mypy `Success: no issues found`, ruff `All checks passed!`.

- [ ] **Step 5: Run full unit gate.**
  Command: `uv run pytest -m "not integration" -q`
  Expected: all pass (boot_recovery producer still passes `reserved_usdt=` etc.; ledger/nav consumers still read `.reserved_usdt` etc.).

- [ ] **Step 6: Commit.**
  `git add src/bfx_funding_bot/modules/execution/events.py tests/modules/execution/test_events.py`
  `git commit -m "✨ Feat: add symbol + native reserved/realized/available to PositionReconciled"`

---

### Task 3: Add `symbol` to `DecisionPayload`

**Files:**
- `src/bfx_funding_bot/modules/marketfeed/schemas.py` (modify `DecisionPayload` L101-128)
- `tests/modules/marketfeed/test_schemas.py` (add tests)

- [ ] **Step 1: Write failing tests.** Append to `tests/modules/marketfeed/test_schemas.py`:

```python
def test_decision_payload_has_symbol_default_fusd() -> None:
    """symbol defaults to the legacy single-currency fallback so existing
    producers that do not yet set it keep validating."""
    p = DecisionPayload(
        decision_outcome="post",
        signal_correlation_id=str(uuid4()),
        offer_rate=0.0001,
        offer_amount_usdt=150.0,
        offer_duration_days=2,
    )
    assert p.symbol == "fUSD"


def test_decision_payload_accepts_explicit_symbol() -> None:
    p = DecisionPayload(
        decision_outcome="post",
        signal_correlation_id=str(uuid4()),
        offer_rate=0.0001,
        offer_amount_usdt=150.0,
        offer_duration_days=2,
        symbol="fUST",
    )
    assert p.symbol == "fUST"


def test_decision_payload_skip_carries_symbol() -> None:
    p = DecisionPayload(
        decision_outcome="skip",
        signal_correlation_id=str(uuid4()),
        skip_reason="below_threshold",
        symbol="fUST",
    )
    assert p.symbol == "fUST"
    assert p.decision_outcome.value == "skip"
```

- [ ] **Step 2: Run it — expect FAIL.**
  Command: `uv run pytest tests/modules/marketfeed/test_schemas.py -k symbol -q`
  Expected: `test_decision_payload_has_symbol_default_fusd` fails with `AttributeError: 'DecisionPayload' object has no attribute 'symbol'`; the explicit-symbol tests fail with pydantic `ValidationError` (`extra="forbid"` rejects unknown `symbol`).

- [ ] **Step 3: Implement.** In `src/bfx_funding_bot/modules/marketfeed/schemas.py`, replace the `DecisionPayload` field block (current L101-116, from the class header through `budget_seconds`):

  OLD:
```python
class DecisionPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    decision_outcome: DecisionOutcome
    signal_correlation_id: UUID
    offer_rate: float | None = None
    offer_amount_usdt: float | None = None
    offer_duration_days: int | None = None
    skip_reason: SkipReason | None = None
    skip_reason_detail: str | None = None
    # ── NEW (Phase 4.3 LOCF): staleness dimension on the DURABLE decision record ──
    # SIGNAL carries this too but lands on the ephemeral stdout sink; persisting it
    # here (PG diagnostics) lets canary outcomes be sliced stale-vs-fresh via SQL.
    is_stale: bool = False
    stale_seconds: int = 0
    budget_seconds: int | None = None
```

  NEW:
```python
class DecisionPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    decision_outcome: DecisionOutcome
    signal_correlation_id: UUID
    offer_rate: float | None = None
    offer_amount_usdt: float | None = None
    offer_duration_days: int | None = None
    skip_reason: SkipReason | None = None
    skip_reason_detail: str | None = None
    # ── NEW (Phase 1 per-symbol): the offer currency this decision targets. ──
    # Guards read decision.symbol to pick the per-symbol ledger bucket. Defaults
    # to the legacy single-currency fallback "fUSD" so producers migrate
    # incrementally without breaking validation.
    symbol: str = "fUSD"
    # ── NEW (Phase 4.3 LOCF): staleness dimension on the DURABLE decision record ──
    # SIGNAL carries this too but lands on the ephemeral stdout sink; persisting it
    # here (PG diagnostics) lets canary outcomes be sliced stale-vs-fresh via SQL.
    is_stale: bool = False
    stale_seconds: int = 0
    budget_seconds: int | None = None
```

  (The `@model_validator _check_outcome_fields` at L117-128 is unchanged.)

- [ ] **Step 4: Run it — expect PASS.**
  Command: `uv run pytest tests/modules/marketfeed/test_schemas.py -q`
  Expected: all pass.
  Then: `uv run mypy src/bfx_funding_bot/modules/marketfeed/schemas.py && uv run ruff check src/bfx_funding_bot/modules/marketfeed/schemas.py`
  Expected: mypy `Success: no issues found`, ruff `All checks passed!`.

- [ ] **Step 5: Run full unit gate.**
  Command: `uv run pytest -m "not integration" -q`
  Expected: all pass. (The `Envelope._validate_payload` DECISION branch at L265-266 calls `DecisionPayload.model_validate(self.payload)`; payloads without `symbol` now get the `"fUSD"` default — no existing test breaks.)

- [ ] **Step 6: Commit.**
  `git add src/bfx_funding_bot/modules/marketfeed/schemas.py tests/modules/marketfeed/test_schemas.py`
  `git commit -m "✨ Feat: add symbol (offer currency) to DecisionPayload, default fUSD"`


---

# Cluster C — position_state persistence + migration

### Task 4: Rename PositionStateRow columns + add `symbol`, change PK, thread symbol through the writer

Renames `reserved_usdt`->`reserved` / `realized_usdt`->`realized`, adds the `symbol` column, and changes the PK to `(account_id, deployment_environment, symbol)`. Updates the writer (`store.py`) and the ledger snapshot reader's column names in the SAME commit so the unit suite stays green. The writer keys every position_state row by a default symbol constant (`fUST`); other clusters later pass a real symbol in.

**Files:**
- `src/bfx_funding_bot/modules/execution/event_store/tables.py` (lines 87-106 — `PositionStateRow`)
- `src/bfx_funding_bot/modules/execution/event_store/store.py` (lines 1-9 imports; 92-96, 194-235, 237-292, 310-393)
- `src/bfx_funding_bot/modules/execution/ledger.py` (lines 69-72)
- `tests/modules/execution/event_store/test_store_append_unit.py` (line 201)
- `tests/modules/execution/event_store/test_rebuild_from_checkpoint.py` (lines 68-69)
- `tests/external/bitfinex/test_source_persistence.py` (lines 93-94, 156, 229)
- `tests/modules/execution/event_store/test_position_state_symbol.py` (NEW)

- [ ] **Step 1: Write a failing test for the new schema (symbol column + renamed columns + composite PK).**
  Create `tests/modules/execution/event_store/test_position_state_symbol.py`:
  ```python
  from decimal import Decimal

  import pytest
  from sqlalchemy import select
  from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

  from bfx_funding_bot.core.db import Base
  from bfx_funding_bot.modules.execution.event_store.tables import PositionStateRow


  async def _engine_sm():
      engine = create_async_engine("sqlite+aiosqlite:///:memory:")
      async with engine.begin() as conn:
          await conn.run_sync(Base.metadata.create_all)
      return engine, async_sessionmaker(engine, expire_on_commit=False)


  @pytest.mark.asyncio
  async def test_position_state_has_symbol_and_renamed_columns():
      engine, sm = await _engine_sm()
      async with sm() as session:
          session.add(PositionStateRow(
              account_id="a", deployment_environment="ci", symbol="fUST",
              reserved=Decimal("10"), realized=Decimal("450"),
              last_updated_ms=1, last_event_seq=5))
          await session.commit()
          row = (await session.execute(select(PositionStateRow).where(
              PositionStateRow.account_id == "a",
              PositionStateRow.symbol == "fUST"))).scalar_one()
      assert row.symbol == "fUST"
      assert row.reserved == Decimal("10")
      assert row.realized == Decimal("450")
      await engine.dispose()


  @pytest.mark.asyncio
  async def test_two_symbols_coexist_for_same_account_env():
      """Composite PK (account, env, symbol) lets two currency rows live side by side."""
      engine, sm = await _engine_sm()
      async with sm() as session:
          session.add(PositionStateRow(
              account_id="a", deployment_environment="ci", symbol="fUST",
              reserved=Decimal("10"), realized=Decimal("100"),
              last_updated_ms=1, last_event_seq=1))
          session.add(PositionStateRow(
              account_id="a", deployment_environment="ci", symbol="fUSD",
              reserved=Decimal("20"), realized=Decimal("200"),
              last_updated_ms=1, last_event_seq=1))
          await session.commit()
          rows = (await session.execute(select(PositionStateRow).where(
              PositionStateRow.account_id == "a",
              PositionStateRow.deployment_environment == "ci"))).scalars().all()
      by_symbol = {r.symbol: r for r in rows}
      assert set(by_symbol) == {"fUST", "fUSD"}
      assert by_symbol["fUST"].realized == Decimal("100")
      assert by_symbol["fUSD"].realized == Decimal("200")
      await engine.dispose()
  ```

- [ ] **Step 2: Run the test — expect failure (column/PK do not exist yet).**
  Command: `uv run pytest -m "not integration" tests/modules/execution/event_store/test_position_state_symbol.py`
  Expected: errors/failures — `TypeError: 'symbol' is an invalid keyword argument for PositionStateRow` (and `reserved`/`realized` invalid) on `test_position_state_has_symbol_and_renamed_columns`; the coexistence test fails for the same reason.

- [ ] **Step 3: Update `PositionStateRow` (tables.py).**
  Replace lines 87-106:
  ```python
  class PositionStateRow(Base):
      """Snapshot: ledger projection. One row per (account, env)."""

      __tablename__ = "position_state"

      account_id: Mapped[str] = mapped_column(Text, nullable=False)
      deployment_environment: Mapped[str] = mapped_column(Text, nullable=False)
      reserved_usdt: Mapped[Decimal] = mapped_column(Numeric, nullable=False, server_default=text("0"))
      realized_usdt: Mapped[Decimal] = mapped_column(Numeric, nullable=False, server_default=text("0"))
      # Event/domain time of the latest projected event (epoch ms, from the event's
      # occurred_at_ms) — mirrors offer_claims.last_updated_ms. NOT wall-clock: a
      # projection is a deterministic function of the event stream, so rebuild
      # reproduces it exactly. Projector freshness/lag is monitored via
      # last_event_seq vs the event_log head, not a wall-clock timestamp.
      last_updated_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
      last_event_seq: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("0"))
      last_reconciled_at: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
      n_credits: Mapped[int | None] = mapped_column(Integer, nullable=True)

      __table_args__ = (PrimaryKeyConstraint("account_id", "deployment_environment"),)
  ```
  with:
  ```python
  class PositionStateRow(Base):
      """Snapshot: ledger projection. One row per (account, env, symbol).

      Native units per symbol: `reserved`/`realized` are in the symbol's own
      currency (fUST -> USDT), never summed across symbols. The symbol column +
      composite PK let multiple funding currencies coexist for one tenant.
      """

      __tablename__ = "position_state"

      account_id: Mapped[str] = mapped_column(Text, nullable=False)
      deployment_environment: Mapped[str] = mapped_column(Text, nullable=False)
      symbol: Mapped[str] = mapped_column(Text, nullable=False)
      reserved: Mapped[Decimal] = mapped_column(Numeric, nullable=False, server_default=text("0"))
      realized: Mapped[Decimal] = mapped_column(Numeric, nullable=False, server_default=text("0"))
      # Event/domain time of the latest projected event (epoch ms, from the event's
      # occurred_at_ms) — mirrors offer_claims.last_updated_ms. NOT wall-clock: a
      # projection is a deterministic function of the event stream, so rebuild
      # reproduces it exactly. Projector freshness/lag is monitored via
      # last_event_seq vs the event_log head, not a wall-clock timestamp.
      last_updated_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
      last_event_seq: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("0"))
      last_reconciled_at: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
      n_credits: Mapped[int | None] = mapped_column(Integer, nullable=True)

      __table_args__ = (
          PrimaryKeyConstraint("account_id", "deployment_environment", "symbol"),
      )
  ```

- [ ] **Step 4: Add the default-symbol constant + thread `symbol` through the writer (store.py).**
  4a. After the `_DEDUP_TYPES` block (after line 39, before `_CLAIM_STATE_BY_TYPE`), add the module-level constant:
  ```python
  # Phase 1: a single funding currency is live (fUST). All position_state rows
  # are keyed by symbol; until callers (boot/reconcile) pass an explicit symbol,
  # they default to this so single-currency behavior is unchanged. Phase 2 makes
  # the configured symbol set first-class.
  DEFAULT_RECONCILE_SYMBOL = "fUST"
  ```
  4b. In `append()`, replace the `_project_position_state` call (lines 93-96):
  ```python
          await self._project_position_state(
              session, etype, account_id, getattr(_ev, "size_usdt", None),
              row.event_seq, occurred_at_ms,
          )
  ```
  with:
  ```python
          await self._project_position_state(
              session, etype, account_id, getattr(_ev, "size_usdt", None),
              row.event_seq, occurred_at_ms,
              symbol=getattr(_ev, "symbol", None) or DEFAULT_RECONCILE_SYMBOL,
          )
  ```
  4c. Replace the whole `_project_position_state` method body (lines 194-235):
  ```python
      async def _project_position_state(
          self,
          session: AsyncSession,
          etype: str,
          account_id: str,
          size_usdt: Any,
          event_seq: int,
          occurred_at_ms: int,
      ) -> None:
          size = Decimal(str(size_usdt)) if size_usdt is not None else Decimal("0")
          ps = (
              await session.execute(
                  select(PositionStateRow).where(
                      PositionStateRow.account_id == account_id,
                      PositionStateRow.deployment_environment == self._env,
                  )
              )
          ).scalar_one_or_none()
          if ps is None:
              ps = PositionStateRow(
                  account_id=account_id,
                  deployment_environment=self._env,
                  reserved_usdt=Decimal("0"),
                  realized_usdt=Decimal("0"),
                  last_updated_ms=0,
                  last_event_seq=0,
              )
              session.add(ps)
          reserved = Decimal(str(ps.reserved_usdt))
          realized = Decimal(str(ps.realized_usdt))
          if etype == "RESERVATION_CLAIMED":
              reserved += size
          elif etype == "ORDER_FILL":
              delta = min(reserved, size)
              reserved -= delta
              realized += size
          elif etype == "RESERVATION_RELEASED":
              reserved -= min(reserved, size)
          ps.reserved_usdt = reserved
          ps.realized_usdt = realized
          ps.last_updated_ms = occurred_at_ms
          ps.last_event_seq = event_seq
  ```
  with:
  ```python
      async def _project_position_state(
          self,
          session: AsyncSession,
          etype: str,
          account_id: str,
          size_usdt: Any,
          event_seq: int,
          occurred_at_ms: int,
          *,
          symbol: str = DEFAULT_RECONCILE_SYMBOL,
      ) -> None:
          size = Decimal(str(size_usdt)) if size_usdt is not None else Decimal("0")
          ps = (
              await session.execute(
                  select(PositionStateRow).where(
                      PositionStateRow.account_id == account_id,
                      PositionStateRow.deployment_environment == self._env,
                      PositionStateRow.symbol == symbol,
                  )
              )
          ).scalar_one_or_none()
          if ps is None:
              ps = PositionStateRow(
                  account_id=account_id,
                  deployment_environment=self._env,
                  symbol=symbol,
                  reserved=Decimal("0"),
                  realized=Decimal("0"),
                  last_updated_ms=0,
                  last_event_seq=0,
              )
              session.add(ps)
          reserved = Decimal(str(ps.reserved))
          realized = Decimal(str(ps.realized))
          if etype == "RESERVATION_CLAIMED":
              reserved += size
          elif etype == "ORDER_FILL":
              delta = min(reserved, size)
              reserved -= delta
              realized += size
          elif etype == "RESERVATION_RELEASED":
              reserved -= min(reserved, size)
          ps.reserved = reserved
          ps.realized = realized
          ps.last_updated_ms = occurred_at_ms
          ps.last_event_seq = event_seq
  ```
  4d. Replace the `set_position_snapshot` signature + body (lines 237-292). The method KEYWORD PARAMS `reserved_usdt`/`realized_usdt` are KEPT (callers pass them by name); only the persisted columns become `.reserved`/`.realized`, and the row is keyed by `symbol`. Replace:
  ```python
      async def set_position_snapshot(
          self,
          session: AsyncSession,
          *,
          account_id: str,
          reserved_usdt: Decimal,
          realized_usdt: Decimal,
          n_offers: int,
          n_credits: int,
          occurred_at_ms: int,
      ) -> SnapshotDrift:
          """Absolute venue snapshot. Overwrites the live position_state view,
          appends an immutable reconcile_observation checkpoint (with the event_log
          fence), and returns drift vs the prior materialized belief.

          NOT a delta. NOT through the accumulator. Single-writer for exposure at
          reconcile time.

          n_offers is persisted in the checkpoint row only (audit); position_state
          carries n_credits but has no n_offers column.
          """
          fence: int = (
              await session.execute(
                  select(func.coalesce(func.max(EventLogRow.event_seq), 0)).where(
                      EventLogRow.account_id == account_id,
                      EventLogRow.deployment_environment == self._env,
                  )
              )
          ).scalar_one()

          ps = (
              await session.execute(
                  select(PositionStateRow).where(
                      PositionStateRow.account_id == account_id,
                      PositionStateRow.deployment_environment == self._env,
                  )
              )
          ).scalar_one_or_none()
          prior_reserved = Decimal(str(ps.reserved_usdt)) if ps is not None else Decimal("0")
          prior_realized = Decimal(str(ps.realized_usdt)) if ps is not None else Decimal("0")
          if ps is None:
              ps = PositionStateRow(
                  account_id=account_id,
                  deployment_environment=self._env,
                  reserved_usdt=Decimal("0"),
                  realized_usdt=Decimal("0"),
                  last_updated_ms=0,
                  last_event_seq=0,
              )
              session.add(ps)
          ps.reserved_usdt = reserved_usdt
          ps.realized_usdt = realized_usdt
          ps.last_updated_ms = occurred_at_ms
          ps.last_event_seq = fence
          ps.last_reconciled_at = occurred_at_ms
          ps.n_credits = n_credits
  ```
  with:
  ```python
      async def set_position_snapshot(
          self,
          session: AsyncSession,
          *,
          account_id: str,
          reserved_usdt: Decimal,
          realized_usdt: Decimal,
          n_offers: int,
          n_credits: int,
          occurred_at_ms: int,
          symbol: str = DEFAULT_RECONCILE_SYMBOL,
      ) -> SnapshotDrift:
          """Absolute venue snapshot for one symbol. Overwrites that symbol's live
          position_state view, appends an immutable reconcile_observation checkpoint
          (with the event_log fence), and returns drift vs the prior materialized
          belief.

          NOT a delta. NOT through the accumulator. Single-writer for exposure at
          reconcile time.

          n_offers is persisted in the checkpoint row only (audit); position_state
          carries n_credits but has no n_offers column. The reserved_usdt/realized_usdt
          PARAMS are native units of `symbol` (the name is legacy; never cross-symbol).
          """
          fence: int = (
              await session.execute(
                  select(func.coalesce(func.max(EventLogRow.event_seq), 0)).where(
                      EventLogRow.account_id == account_id,
                      EventLogRow.deployment_environment == self._env,
                  )
              )
          ).scalar_one()

          ps = (
              await session.execute(
                  select(PositionStateRow).where(
                      PositionStateRow.account_id == account_id,
                      PositionStateRow.deployment_environment == self._env,
                      PositionStateRow.symbol == symbol,
                  )
              )
          ).scalar_one_or_none()
          prior_reserved = Decimal(str(ps.reserved)) if ps is not None else Decimal("0")
          prior_realized = Decimal(str(ps.realized)) if ps is not None else Decimal("0")
          if ps is None:
              ps = PositionStateRow(
                  account_id=account_id,
                  deployment_environment=self._env,
                  symbol=symbol,
                  reserved=Decimal("0"),
                  realized=Decimal("0"),
                  last_updated_ms=0,
                  last_event_seq=0,
              )
              session.add(ps)
          ps.reserved = reserved_usdt
          ps.realized = realized_usdt
          ps.last_updated_ms = occurred_at_ms
          ps.last_event_seq = fence
          ps.last_reconciled_at = occurred_at_ms
          ps.n_credits = n_credits
  ```
  (The `ReconcileObservationRow(...)` append immediately below at lines 294-303 is UNCHANGED — that table keeps `reserved_usdt`/`realized_usdt`.)
  4e. Update `rebuild_snapshot_from_log` signature + the two PositionStateRow scopes + the row construction + the tail-fold writes. Replace the signature (lines 310-312):
  ```python
      async def rebuild_snapshot_from_log(
          self, session: AsyncSession, *, account_id: str, deployment_environment: str
      ) -> None:
  ```
  with:
  ```python
      async def rebuild_snapshot_from_log(
          self, session: AsyncSession, *, account_id: str, deployment_environment: str,
          symbol: str = DEFAULT_RECONCILE_SYMBOL,
      ) -> None:
  ```
  Replace the position_state delete (lines 322-324):
  ```python
          await session.execute(delete(PositionStateRow).where(
              PositionStateRow.account_id == account_id,
              PositionStateRow.deployment_environment == deployment_environment))
  ```
  with:
  ```python
          await session.execute(delete(PositionStateRow).where(
              PositionStateRow.account_id == account_id,
              PositionStateRow.deployment_environment == deployment_environment,
              PositionStateRow.symbol == symbol))
  ```
  Replace the `ps = PositionStateRow(...)` construction (lines 360-369):
  ```python
          ps = PositionStateRow(
              account_id=account_id,
              deployment_environment=deployment_environment,
              reserved_usdt=base_reserved,
              realized_usdt=base_realized,
              last_updated_ms=base_ms,
              last_event_seq=base_seq,
              last_reconciled_at=(checkpoint.observed_at_ms if checkpoint else None),
              n_credits=(checkpoint.n_credits if checkpoint else None),
          )
  ```
  with:
  ```python
          ps = PositionStateRow(
              account_id=account_id,
              deployment_environment=deployment_environment,
              symbol=symbol,
              reserved=base_reserved,
              realized=base_realized,
              last_updated_ms=base_ms,
              last_event_seq=base_seq,
              last_reconciled_at=(checkpoint.observed_at_ms if checkpoint else None),
              n_credits=(checkpoint.n_credits if checkpoint else None),
          )
  ```
  Replace the two tail-fold writes (lines 390-391):
  ```python
              ps.reserved_usdt = reserved
              ps.realized_usdt = realized
  ```
  with:
  ```python
              ps.reserved = reserved
              ps.realized = realized
  ```
  (Note: `checkpoint.reserved_usdt` / `checkpoint.realized_usdt` reads at lines 355-356 are `ReconcileObservationRow` columns and stay UNCHANGED.)

- [ ] **Step 5: Update the ledger snapshot reader column names (ledger.py).**
  Replace lines 69-72:
  ```python
          if row is not None:
              ledger._reserved = Decimal(str(row.reserved_usdt))
              ledger._realized = Decimal(str(row.realized_usdt))
          return ledger
  ```
  with:
  ```python
          if row is not None:
              ledger._reserved = Decimal(str(row.reserved))
              ledger._realized = Decimal(str(row.realized))
          return ledger
  ```

- [ ] **Step 6: Update existing tests that read the renamed PositionStateRow columns.**
  6a. `tests/modules/execution/event_store/test_store_append_unit.py` line 201:
  old: `    assert ps.reserved_usdt == Decimal("0")   # FAILED never reserved capital`
  new: `    assert ps.reserved == Decimal("0")   # FAILED never reserved capital`
  6b. `tests/modules/execution/event_store/test_rebuild_from_checkpoint.py` lines 68-69:
  old:
  ```python
      assert ps.realized_usdt == Decimal("500")   # 450 checkpoint + 50 tail
      assert ps.reserved_usdt == Decimal("0")
  ```
  new:
  ```python
      assert ps.realized == Decimal("500")   # 450 checkpoint + 50 tail
      assert ps.reserved == Decimal("0")
  ```
  6c. `tests/external/bitfinex/test_source_persistence.py`:
  line 93-94 old:
  ```python
      assert Decimal(str(ps.reserved_usdt)) == Decimal("0")    # reserved -= 100
      assert Decimal(str(ps.realized_usdt)) == Decimal("100")  # realized += 100
  ```
  new:
  ```python
      assert Decimal(str(ps.reserved)) == Decimal("0")    # reserved -= 100
      assert Decimal(str(ps.realized)) == Decimal("100")  # realized += 100
  ```
  line 156 old: `    assert Decimal(str(ps.reserved_usdt)) == Decimal("0")`
  new: `    assert Decimal(str(ps.reserved)) == Decimal("0")`
  line 229 old: `    assert Decimal(str(ps.reserved_usdt)) == Decimal("0")`
  new: `    assert Decimal(str(ps.reserved)) == Decimal("0")`

- [ ] **Step 7: Run the new test + the full unit gate — expect green.**
  Commands:
  `uv run pytest -m "not integration" tests/modules/execution/event_store/test_position_state_symbol.py`
  then `uv run pytest -m "not integration"`
  then `uv run mypy src/ && uv run ruff check .`
  Expected: the two new tests pass; the whole `not integration` suite passes; mypy + ruff clean.

- [ ] **Step 8: Commit.**
  `git add -A && git commit -m "$(printf '%s' '♻️ Refactor: per-symbol position_state (symbol PK + reserved/realized rename), writer keyed by symbol\n\nCo-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>')"`

---

### Task 5: Writer upserts the correct per-symbol row (no cross-symbol clobber)

Proves the writer mutates only the targeted `(account, env, symbol)` bucket: two symbols claimed in the same env keep independent reserved/realized, and a second event on one symbol upserts in place (one row, accumulated).

**Files:**
- `tests/modules/execution/event_store/test_position_state_symbol.py` (append to NEW file from Task 1)

- [ ] **Step 1: Add failing writer-per-symbol tests.**
  Append to `tests/modules/execution/event_store/test_position_state_symbol.py`:
  ```python
  from sqlalchemy import func

  from bfx_funding_bot.modules.execution.event_store.store import PostgresEventStore


  @pytest.mark.asyncio
  async def test_project_position_state_isolates_symbols():
      """_project_position_state mutates only the event.symbol bucket; a different
      symbol's row is untouched. set_position_snapshot seeds each symbol's row."""
      engine, sm = await _engine_sm()
      store = PostgresEventStore(deployment_environment="ci")
      async with sm() as session:
          # seed two symbols via the snapshot writer
          await store.set_position_snapshot(
              session, account_id="a", reserved_usdt=Decimal("0"),
              realized_usdt=Decimal("100"), n_offers=0, n_credits=1,
              occurred_at_ms=1, symbol="fUST")
          await store.set_position_snapshot(
              session, account_id="a", reserved_usdt=Decimal("0"),
              realized_usdt=Decimal("200"), n_offers=0, n_credits=1,
              occurred_at_ms=1, symbol="fUSD")
          await session.commit()

          # a CLAIMED projection on fUST must not touch fUSD
          await store._project_position_state(
              session, "RESERVATION_CLAIMED", "a", Decimal("30"),
              event_seq=10, occurred_at_ms=2, symbol="fUST")
          await session.commit()

          rows = (await session.execute(select(PositionStateRow).where(
              PositionStateRow.account_id == "a"))).scalars().all()
      by_symbol = {r.symbol: r for r in rows}
      assert by_symbol["fUST"].reserved == Decimal("30")   # +30 claimed
      assert by_symbol["fUST"].realized == Decimal("100")  # unchanged
      assert by_symbol["fUSD"].reserved == Decimal("0")    # isolated
      assert by_symbol["fUSD"].realized == Decimal("200")  # isolated
      await engine.dispose()


  @pytest.mark.asyncio
  async def test_project_position_state_upserts_in_place_per_symbol():
      """Two events on the same symbol accumulate into ONE row, not two."""
      engine, sm = await _engine_sm()
      store = PostgresEventStore(deployment_environment="ci")
      async with sm() as session:
          await store._project_position_state(
              session, "RESERVATION_CLAIMED", "a", Decimal("40"),
              event_seq=1, occurred_at_ms=1, symbol="fUST")
          await store._project_position_state(
              session, "RESERVATION_CLAIMED", "a", Decimal("10"),
              event_seq=2, occurred_at_ms=2, symbol="fUST")
          await session.commit()
          n = (await session.execute(select(func.count()).select_from(
              PositionStateRow).where(PositionStateRow.symbol == "fUST"))).scalar_one()
          row = (await session.execute(select(PositionStateRow).where(
              PositionStateRow.symbol == "fUST"))).scalar_one()
      assert n == 1
      assert row.reserved == Decimal("50")   # 40 + 10 accumulated in place
      await engine.dispose()
  ```

- [ ] **Step 2: Run the tests — expect green (behavior already implemented in Task 1).**
  Command: `uv run pytest -m "not integration" tests/modules/execution/event_store/test_position_state_symbol.py`
  Expected: all four tests pass. (These tests lock in the per-symbol isolation/upsert behavior delivered by Task 1; if any fails, the writer is clobbering across symbols — fix the `symbol` WHERE clause in `_project_position_state`/`set_position_snapshot`.)

- [ ] **Step 3: Run mypy + ruff.**
  Command: `uv run mypy src/ && uv run ruff check .`
  Expected: clean.

- [ ] **Step 4: Commit.**
  `git add -A && git commit -m "$(printf '%s' '✅ Test: per-symbol position_state writer isolation + in-place upsert\n\nCo-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>')"`

---

### Task 6: Alembic migration — drop+recreate position_state with new PK + renamed columns (no backfill)

Hand-written migration (no autogenerate) that drops `position_state` and recreates it with the composite PK `(account_id, deployment_environment, symbol)` and columns `reserved`/`realized`. Pre-launch ⇒ NO data backfill. Provides a symmetric downgrade that restores the old single-tenant PK and `*_usdt` column names.

**Files:**
- `alembic/versions/b7c1d2e3f4a5_position_state_per_symbol.py` (NEW)

- [ ] **Step 1: Write the migration file.**
  Create `alembic/versions/b7c1d2e3f4a5_position_state_per_symbol.py`:
  ```python
  """position_state per-symbol: add symbol to PK, rename reserved_usdt/realized_usdt

  Revision ID: b7c1d2e3f4a5
  Revises: a3ae9a60862a
  Create Date: 2026-05-31 00:00:00.000000

  Pre-launch clean recreate: position_state is a derived snapshot (rebuildable
  from event_log + reconcile_observation), and there is no production data worth
  preserving, so this DROPs and reCREATEs the table with the new composite PK
  (account_id, deployment_environment, symbol) and native-unit column names
  reserved/realized. No backfill. The downgrade restores the legacy single-tenant
  PK and *_usdt names (also a clean recreate — symbol history is not recoverable).
  """
  from collections.abc import Sequence

  import sqlalchemy as sa

  from alembic import op

  # revision identifiers, used by Alembic.
  revision: str = 'b7c1d2e3f4a5'
  down_revision: str | Sequence[str] | None = 'a3ae9a60862a'
  branch_labels: str | Sequence[str] | None = None
  depends_on: str | Sequence[str] | None = None


  def upgrade() -> None:
      """Recreate position_state with the per-symbol PK + renamed columns."""
      op.drop_table('position_state')
      op.create_table(
          'position_state',
          sa.Column('account_id', sa.Text(), nullable=False),
          sa.Column('deployment_environment', sa.Text(), nullable=False),
          sa.Column('symbol', sa.Text(), nullable=False),
          sa.Column('reserved', sa.Numeric(), server_default=sa.text('0'), nullable=False),
          sa.Column('realized', sa.Numeric(), server_default=sa.text('0'), nullable=False),
          sa.Column('last_updated_ms', sa.BigInteger(), nullable=False),
          sa.Column('last_event_seq', sa.BigInteger(), server_default=sa.text('0'), nullable=False),
          sa.Column('last_reconciled_at', sa.BigInteger(), nullable=True),
          sa.Column('n_credits', sa.Integer(), nullable=True),
          sa.PrimaryKeyConstraint('account_id', 'deployment_environment', 'symbol'),
      )


  def downgrade() -> None:
      """Restore the legacy single-tenant position_state (no symbol, *_usdt names)."""
      op.drop_table('position_state')
      op.create_table(
          'position_state',
          sa.Column('account_id', sa.Text(), nullable=False),
          sa.Column('deployment_environment', sa.Text(), nullable=False),
          sa.Column('reserved_usdt', sa.Numeric(), server_default=sa.text('0'), nullable=False),
          sa.Column('realized_usdt', sa.Numeric(), server_default=sa.text('0'), nullable=False),
          sa.Column('last_updated_ms', sa.BigInteger(), nullable=False),
          sa.Column('last_event_seq', sa.BigInteger(), server_default=sa.text('0'), nullable=False),
          sa.Column('last_reconciled_at', sa.BigInteger(), nullable=True),
          sa.Column('n_credits', sa.Integer(), nullable=True),
          sa.PrimaryKeyConstraint('account_id', 'deployment_environment'),
      )
  ```

- [ ] **Step 2: Validate the migration chain is linear (single head) without a DB.**
  Command: `uv run python -c "from alembic.config import Config; from alembic.script import ScriptDirectory; s = ScriptDirectory.from_config(Config('alembic.ini')); heads = s.get_heads(); print('HEADS', heads); assert heads == ('b7c1d2e3f4a5',), heads; print('OK single head, chain linear')"`
  Expected: prints `HEADS ('b7c1d2e3f4a5',)` then `OK single head, chain linear`. (This proves the revision graph parses and `b7c1d2e3f4a5` is the sole head chaining from `a3ae9a60862a`; it does NOT need Neon.)

- [ ] **Step 3: (Manual, needs Neon — run when DB is reachable) apply + verify no drift, then verify downgrade/upgrade round-trips.**
  Commands (do NOT block the unit gate on these; run against the dev DB):
  `uv run alembic upgrade head`
  `uv run alembic check`
  `uv run alembic downgrade -1 && uv run alembic upgrade head && uv run alembic check`
  Expected: `upgrade head` succeeds; `alembic check` reports "No new upgrade operations detected" (model metadata matches DB — confirms tables.py and the migration agree); the downgrade/upgrade round-trip leaves `alembic check` clean.

- [ ] **Step 4: Run the unit gate + lint (migration import is syntactically valid; unit tests unaffected).**
  Command: `uv run pytest -m "not integration" && uv run ruff check .`
  Expected: green; ruff clean.

- [ ] **Step 5: Commit.**
  `git add -A && git commit -m "$(printf '%s' '✨ Feat: Alembic migration recreates position_state per-symbol (PK +symbol, reserved/realized, no backfill)\n\nCo-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>')"`


---

# Cluster B — Ledger per-symbol

> Working dir for ALL commands: `backend_py/`. Unit gate: `uv run pytest -m "not integration"`. Types: `uv run mypy src/`. Lint: `uv run ruff check .`.
>
> **Hard dependency on Cluster A**: this cluster assumes the domain events in `src/bfx_funding_bot/modules/execution/events.py` already carry the renamed/added fields — `ReservationClaimed`/`OrderFilled`/`ReservationReleased` have `symbol: str` and `amount: Decimal` (no more `size_usdt`), and `PositionReconciled` has `symbol: str` plus `reserved/realized/available: Decimal` (no more `*_usdt`). All test fixtures and ledger code below use ONLY those new field names.
>
> **Hard dependency on Cluster C (persistence)**: Task 4 (`from_snapshot`) reads `row.symbol`, `row.reserved`, `row.realized` off `PositionStateRow`. Those columns are introduced by Cluster C. Task 4 must be stitched AFTER Cluster C lands; Tasks 1–3 + 5 do NOT depend on persistence and can land first.
>
> File under change throughout: `src/bfx_funding_bot/modules/execution/ledger.py` (current 1–158).

---

### Task 7: Per-symbol dict counters + per-symbol getters (claim path)

Convert the three scalar counters to `dict[str, Decimal]` and make `current_exposure`/`reserved_exposure`/`realized_exposure`/`available_balance` take a required `symbol` param. Update `on_reservation_claimed` to mutate the `event.symbol` bucket using `event.amount`. Per-symbol isolation: an fUST claim must not touch the fUSD bucket.

**Files:**
- `tests/modules/execution/test_ledger_per_symbol.py` (new)
- `src/bfx_funding_bot/modules/execution/ledger.py` (modify: `__init__` 36–43, `on_reservation_claimed` 76–79, getters 133–156)

- [ ] **Step 1: Write the failing test.** Create `tests/modules/execution/test_ledger_per_symbol.py` with:

```python
"""PaperPositionLedger per-symbol isolation (Phase 1 native-units)."""
from __future__ import annotations

from decimal import Decimal
from uuid import uuid4

from bfx_funding_bot.modules.execution.events import (
    OrderFilled,
    PositionReconciled,
    ReservationClaimed,
    ReservationReleased,
)
from bfx_funding_bot.modules.execution.ledger import PaperPositionLedger


def _claim(symbol: str, amount: str, account_id: str = "default") -> ReservationClaimed:
    return ReservationClaimed(
        cid=1, venue_offer_id="x", symbol=symbol, amount=Decimal(amount),
        signal_correlation_id=uuid4(), account_id=account_id, is_simulated=True,
    )


def _fill(symbol: str, amount: str, venue_offer_id: str = "x",
          venue_seq: int | None = None, account_id: str = "default") -> OrderFilled:
    return OrderFilled(
        cid=1, venue_offer_id=venue_offer_id, credit_id=None, symbol=symbol,
        amount=Decimal(amount), fill_rate=0.0001, signal_correlation_id=uuid4(),
        account_id=account_id, is_simulated=True, venue_seq=venue_seq,
    )


def _release(symbol: str, amount: str, venue_offer_id: str = "x",
             venue_seq: int | None = None, account_id: str = "default") -> ReservationReleased:
    return ReservationReleased(
        cid=1, venue_offer_id=venue_offer_id, symbol=symbol, amount=Decimal(amount),
        reason="venue_cancel", signal_correlation_id=uuid4(),
        account_id=account_id, is_simulated=True, venue_seq=venue_seq,
    )


def _reconciled(symbol: str, reserved: str, realized: str, available: str,
                account_id: str = "default") -> PositionReconciled:
    return PositionReconciled(
        account_id=account_id, symbol=symbol, reserved=Decimal(reserved),
        realized=Decimal(realized), available=Decimal(available),
        n_offers=0, n_credits=0, occurred_at_ms=1_000,
    )


async def test_unknown_symbol_reads_zero() -> None:
    led = PaperPositionLedger(account_id="default")
    assert led.current_exposure("fUST") == Decimal("0")
    assert led.reserved_exposure("fUST") == Decimal("0")
    assert led.realized_exposure("fUST") == Decimal("0")
    assert led.available_balance("fUST") == Decimal("0")


async def test_claim_increments_only_its_symbol_bucket() -> None:
    led = PaperPositionLedger(account_id="default")
    await led.on_reservation_claimed(_claim("fUST", "100"))
    await led.on_reservation_claimed(_claim("fUSD", "30"))
    assert led.reserved_exposure("fUST") == Decimal("100")
    assert led.reserved_exposure("fUSD") == Decimal("30")
    assert led.current_exposure("fUST") == Decimal("100")
    assert led.current_exposure("fUSD") == Decimal("30")
```

- [ ] **Step 2: Run it — expect FAIL.** `uv run pytest tests/modules/execution/test_ledger_per_symbol.py -q` — fails (current `ReservationClaimed` has no `symbol`/`amount`; getters take no positional arg). If Cluster A is not yet stitched the constructor errors first; once A is in, the `TypeError: current_exposure() takes 1 positional argument but 2 were given` is the target failure.

- [ ] **Step 3: Implement — `__init__`.** Replace lines 36–43:

```python
    def __init__(self, account_id: str) -> None:
        self.account_id = account_id
        self._reserved = Decimal("0")
        self._realized = Decimal("0")
        self._available = Decimal("0")
        self.replay_floor_hit_count = 0
        self._processed_fills: set[tuple[str, int | None]] = set()
        self._processed_releases: set[tuple[str, int | None]] = set()
```

with:

```python
    def __init__(self, account_id: str) -> None:
        self.account_id = account_id
        # Per-symbol native-unit counters (Phase 1). Missing key reads as
        # Decimal(0); NEVER sum across symbols inside a guard comparison.
        self._reserved: dict[str, Decimal] = {}
        self._realized: dict[str, Decimal] = {}
        self._available: dict[str, Decimal] = {}
        self.replay_floor_hit_count = 0
        # Dedup stays keyed by (venue_offer_id, venue_seq) — venue-global,
        # not per-symbol (a venue_offer_id is unique across symbols anyway).
        self._processed_fills: set[tuple[str, int | None]] = set()
        self._processed_releases: set[tuple[str, int | None]] = set()
```

- [ ] **Step 4: Implement — `on_reservation_claimed`.** Replace lines 76–79:

```python
    async def on_reservation_claimed(self, event: ReservationClaimed) -> None:
        if event.account_id != self.account_id:
            return
        self._reserved += event.size_usdt
```

with:

```python
    async def on_reservation_claimed(self, event: ReservationClaimed) -> None:
        if event.account_id != self.account_id:
            return
        self._reserved[event.symbol] = (
            self._reserved.get(event.symbol, Decimal("0")) + event.amount
        )
```

- [ ] **Step 5: Implement — getters.** Replace lines 133–156:

```python
    def current_exposure(self) -> Decimal:
        """For AllocationCapGuard: reserved + realized = capital committed at venue."""
        return self._reserved + self._realized

    def reserved_exposure(self) -> Decimal:
        """Pending open-offer capital only (placed but not yet matched).

        Used by CellDeploymentTracker.reconcile_to_total to rescale per-cell
        intent to the reserved total — NOT to current_exposure. Realized credits
        are committed and unattributable to any specific cell; including them in
        the rescale factor would inflate per-cell intent past cap_per_cell.
        """
        return self._reserved

    def realized_exposure(self) -> Decimal:
        """For L2 guards (DrawdownGuard etc., Phase 4.4): matched credits only."""
        return self._realized

    def available_balance(self) -> Decimal:
        """Funding-wallet available balance from the last reconcile (in-memory;
        not persisted). 0 until the first reconcile populates it — fail-closed
        (the reconciler deploys nothing on unknown funds). Read by the
        DeploymentReconciler balance clamp and BuyingPowerGuard."""
        return self._available
```

with:

```python
    def current_exposure(self, symbol: str) -> Decimal:
        """For AllocationCapGuard: reserved + realized for THIS symbol = capital
        committed at venue in that currency. Native units; never cross-symbol."""
        return self._reserved.get(symbol, Decimal("0")) + self._realized.get(
            symbol, Decimal("0")
        )

    def reserved_exposure(self, symbol: str) -> Decimal:
        """Pending open-offer capital only (placed but not yet matched) for this
        symbol.

        Used by CellDeploymentTracker.reconcile_to_total to rescale per-cell
        intent to the reserved total — NOT to current_exposure. Realized credits
        are committed and unattributable to any specific cell; including them in
        the rescale factor would inflate per-cell intent past cap_per_cell.
        """
        return self._reserved.get(symbol, Decimal("0"))

    def realized_exposure(self, symbol: str) -> Decimal:
        """For L2 guards (DrawdownGuard etc., Phase 4.4): matched credits only,
        for this symbol."""
        return self._realized.get(symbol, Decimal("0"))

    def available_balance(self, symbol: str) -> Decimal:
        """Funding-wallet available balance for this symbol from the last
        reconcile (in-memory; not persisted). 0 until the first reconcile
        populates it — fail-closed (the reconciler deploys nothing on unknown
        funds). Read by the DeploymentReconciler balance clamp and
        BuyingPowerGuard."""
        return self._available.get(symbol, Decimal("0"))
```

- [ ] **Step 6: Run it — expect PASS.** `uv run pytest tests/modules/execution/test_ledger_per_symbol.py -q` — the two new tests pass. (The legacy ledger tests + the 3 in-repo getter callers are still red; Tasks 2–5 + the caller-update task fix them. Do NOT run the full gate yet.)

- [ ] **Step 7: Commit.** `git add tests/modules/execution/test_ledger_per_symbol.py src/bfx_funding_bot/modules/execution/ledger.py && git commit -m "♻️ Refactor: ledger per-symbol dict counters + symbol-param claim getters (Phase 1)"`

---

### Task 8: Fill + release handlers use per-symbol bucket; dedup + floor preserved

Update `on_order_filled` and `on_reservation_released` to mutate the `event.symbol` bucket using `event.amount`, with the floor-at-0 and dedup logic now per symbol. Dedup keys stay `(venue_offer_id, venue_seq)` (venue-global). Per-symbol isolation: an fUST fill must not move fUSD.

**Files:**
- `tests/modules/execution/test_ledger_per_symbol.py` (modify: append tests)
- `src/bfx_funding_bot/modules/execution/ledger.py` (modify: `on_order_filled` 81–98, `on_reservation_released` 100–116)

- [ ] **Step 1: Write the failing test.** Append to `tests/modules/execution/test_ledger_per_symbol.py`:

```python
async def test_fill_moves_only_its_symbol_reserved_to_realized() -> None:
    led = PaperPositionLedger(account_id="default")
    await led.on_reservation_claimed(_claim("fUST", "100"))
    await led.on_reservation_claimed(_claim("fUSD", "30"))
    await led.on_order_filled(_fill("fUST", "100", venue_offer_id="o-ust", venue_seq=1))
    assert led.reserved_exposure("fUST") == Decimal("0")
    assert led.realized_exposure("fUST") == Decimal("100")
    # fUSD untouched
    assert led.reserved_exposure("fUSD") == Decimal("30")
    assert led.realized_exposure("fUSD") == Decimal("0")


async def test_fill_dedup_per_venue_offer_seq() -> None:
    led = PaperPositionLedger(account_id="default")
    await led.on_reservation_claimed(_claim("fUST", "100"))
    await led.on_order_filled(_fill("fUST", "100", venue_offer_id="o-ust", venue_seq=7))
    await led.on_order_filled(_fill("fUST", "100", venue_offer_id="o-ust", venue_seq=7))  # dup
    assert led.realized_exposure("fUST") == Decimal("100")


async def test_release_floors_per_symbol_without_claim() -> None:
    led = PaperPositionLedger(account_id="default")
    # fUSD has a live claim; releasing fUST (no claim) floors fUST only.
    await led.on_reservation_claimed(_claim("fUSD", "40"))
    await led.on_reservation_released(_release("fUST", "50", venue_offer_id="o-ust"))
    assert led.reserved_exposure("fUST") == Decimal("0")
    assert led.replay_floor_hit_count == 1
    assert led.reserved_exposure("fUSD") == Decimal("40")  # untouched


async def test_release_dedup_per_venue_offer_seq() -> None:
    led = PaperPositionLedger(account_id="default")
    await led.on_reservation_claimed(_claim("fUST", "100"))
    await led.on_reservation_released(_release("fUST", "100", venue_offer_id="o-ust", venue_seq=9))
    await led.on_reservation_released(_release("fUST", "100", venue_offer_id="o-ust", venue_seq=9))  # dup
    assert led.reserved_exposure("fUST") == Decimal("0")
```

- [ ] **Step 2: Run it — expect FAIL.** `uv run pytest tests/modules/execution/test_ledger_per_symbol.py -k "fill or release" -q` — fails: `on_order_filled` still does `self._reserved -= delta` on a dict (`TypeError: unsupported operand type(s) for -=: 'dict' and 'Decimal'`).

- [ ] **Step 3: Implement — `on_order_filled`.** Replace lines 81–98:

```python
    async def on_order_filled(self, event: OrderFilled) -> None:
        if event.account_id != self.account_id:
            return
        key = (event.venue_offer_id, event.venue_seq)
        if key in self._processed_fills:
            log.debug("ledger_dedup filled %s", key)
            return
        self._processed_fills.add(key)
        delta = min(self._reserved, event.size_usdt)
        self._reserved -= delta
        if delta < event.size_usdt:
            self.replay_floor_hit_count += 1
            log.warning(
                "order_filled_without_claim cid=%d offer=%s expected=%.2f applied=%.2f",
                event.cid, event.venue_offer_id,
                float(event.size_usdt), float(delta),
            )
        self._realized += event.size_usdt
```

with:

```python
    async def on_order_filled(self, event: OrderFilled) -> None:
        if event.account_id != self.account_id:
            return
        key = (event.venue_offer_id, event.venue_seq)
        if key in self._processed_fills:
            log.debug("ledger_dedup filled %s", key)
            return
        self._processed_fills.add(key)
        reserved = self._reserved.get(event.symbol, Decimal("0"))
        delta = min(reserved, event.amount)
        self._reserved[event.symbol] = reserved - delta
        if delta < event.amount:
            self.replay_floor_hit_count += 1
            log.warning(
                "order_filled_without_claim cid=%d offer=%s symbol=%s expected=%.2f applied=%.2f",
                event.cid, event.venue_offer_id, event.symbol,
                float(event.amount), float(delta),
            )
        self._realized[event.symbol] = (
            self._realized.get(event.symbol, Decimal("0")) + event.amount
        )
```

- [ ] **Step 4: Implement — `on_reservation_released`.** Replace lines 100–116:

```python
    async def on_reservation_released(self, event: ReservationReleased) -> None:
        if event.account_id != self.account_id:
            return
        key = (event.venue_offer_id, event.venue_seq)
        if key in self._processed_releases:
            log.debug("ledger_dedup released %s", key)
            return
        self._processed_releases.add(key)
        delta = min(self._reserved, event.size_usdt)
        self._reserved -= delta
        if delta < event.size_usdt:
            self.replay_floor_hit_count += 1
            log.warning(
                "reservation_release_without_claim cid=%d offer=%s expected=%.2f applied=%.2f reason=%s",
                event.cid, event.venue_offer_id,
                float(event.size_usdt), float(delta), event.reason,
            )
```

with:

```python
    async def on_reservation_released(self, event: ReservationReleased) -> None:
        if event.account_id != self.account_id:
            return
        key = (event.venue_offer_id, event.venue_seq)
        if key in self._processed_releases:
            log.debug("ledger_dedup released %s", key)
            return
        self._processed_releases.add(key)
        reserved = self._reserved.get(event.symbol, Decimal("0"))
        delta = min(reserved, event.amount)
        self._reserved[event.symbol] = reserved - delta
        if delta < event.amount:
            self.replay_floor_hit_count += 1
            log.warning(
                "reservation_release_without_claim cid=%d offer=%s symbol=%s "
                "expected=%.2f applied=%.2f reason=%s",
                event.cid, event.venue_offer_id, event.symbol,
                float(event.amount), float(delta), event.reason,
            )
```

- [ ] **Step 5: Run it — expect PASS.** `uv run pytest tests/modules/execution/test_ledger_per_symbol.py -q` — all per-symbol tests pass.

- [ ] **Step 6: Commit.** `git add tests/modules/execution/test_ledger_per_symbol.py src/bfx_funding_bot/modules/execution/ledger.py && git commit -m "♻️ Refactor: ledger fill/release handlers per-symbol bucket + native amount (Phase 1)"`

---

### Task 9: `on_position_reconciled` absolute-sets per-symbol buckets

`on_position_reconciled` must absolute-set the `event.symbol` buckets for reserved/realized/available (not all symbols). A second reconcile for a different symbol must leave the first symbol's buckets intact.

**Files:**
- `tests/modules/execution/test_ledger_per_symbol.py` (modify: append tests)
- `src/bfx_funding_bot/modules/execution/ledger.py` (modify: `on_position_reconciled` 118–129)

- [ ] **Step 1: Write the failing test.** Append to `tests/modules/execution/test_ledger_per_symbol.py`:

```python
async def test_reconciled_absolute_sets_only_its_symbol() -> None:
    led = PaperPositionLedger(account_id="default")
    # seed fUST via a claim then absolute-set fUSD; fUST must remain.
    await led.on_reservation_claimed(_claim("fUST", "100"))
    await led.on_position_reconciled(_reconciled("fUSD", reserved="5", realized="200", available="50"))
    assert led.reserved_exposure("fUSD") == Decimal("5")
    assert led.realized_exposure("fUSD") == Decimal("200")
    assert led.available_balance("fUSD") == Decimal("50")
    # fUST untouched by the fUSD reconcile.
    assert led.reserved_exposure("fUST") == Decimal("100")


async def test_reconciled_overwrites_same_symbol() -> None:
    led = PaperPositionLedger(account_id="default")
    await led.on_position_reconciled(_reconciled("fUST", reserved="0", realized="406.89", available="147.5"))
    await led.on_position_reconciled(_reconciled("fUST", reserved="10", realized="300", available="90"))
    assert led.reserved_exposure("fUST") == Decimal("10")
    assert led.realized_exposure("fUST") == Decimal("300")
    assert led.available_balance("fUST") == Decimal("90")
    assert led.current_exposure("fUST") == Decimal("310")


async def test_reconciled_other_account_ignored() -> None:
    led = PaperPositionLedger(account_id="default")
    await led.on_position_reconciled(_reconciled("fUST", "1", "1", "99", account_id="other"))
    assert led.available_balance("fUST") == Decimal("0")
    assert led.current_exposure("fUST") == Decimal("0")
```

- [ ] **Step 2: Run it — expect FAIL.** `uv run pytest tests/modules/execution/test_ledger_per_symbol.py -k reconciled -q` — fails: `on_position_reconciled` still does `self._reserved = event.reserved_usdt` (attribute gone after Cluster A; and assigns a Decimal to a dict-typed attr).

- [ ] **Step 3: Implement — `on_position_reconciled`.** Replace lines 118–129:

```python
    async def on_position_reconciled(self, event: PositionReconciled) -> None:
        """Absolute set from venue snapshot — NOT a delta.

        Overwrites reserved/realized with the authoritative venue values.
        Called after each reconcile tick (boot + periodic). The next WS delta
        that arrives will temporarily diverge; the next reconcile corrects it.
        """
        if event.account_id != self.account_id:
            return
        self._reserved = event.reserved_usdt
        self._realized = event.realized_usdt
        self._available = event.available_usdt
```

with:

```python
    async def on_position_reconciled(self, event: PositionReconciled) -> None:
        """Absolute set from venue snapshot — NOT a delta.

        Overwrites THIS symbol's reserved/realized/available with the
        authoritative venue values. Called once per configured symbol after
        each reconcile tick (boot + periodic). Other symbols' buckets are left
        intact; each carries its own PositionReconciled. The next WS delta that
        arrives will temporarily diverge; the next reconcile corrects it.
        """
        if event.account_id != self.account_id:
            return
        self._reserved[event.symbol] = event.reserved
        self._realized[event.symbol] = event.realized
        self._available[event.symbol] = event.available
```

- [ ] **Step 4: Run it — expect PASS.** `uv run pytest tests/modules/execution/test_ledger_per_symbol.py -q` — all per-symbol tests pass.

- [ ] **Step 5: Commit.** `git add tests/modules/execution/test_ledger_per_symbol.py src/bfx_funding_bot/modules/execution/ledger.py && git commit -m "♻️ Refactor: ledger on_position_reconciled absolute-sets per-symbol buckets (Phase 1)"`

---

### Task 10: `from_snapshot` loads ALL per-symbol `position_state` rows

**Stitch AFTER Cluster C** (needs `PositionStateRow.symbol`, `.reserved`, `.realized`). `from_snapshot` must load every `(account, env)` row, one per symbol, into the dicts (not just one row).

**Files:**
- `tests/modules/execution/test_ledger_from_snapshot_per_symbol.py` (new)
- `src/bfx_funding_bot/modules/execution/ledger.py` (modify: `from_snapshot` 47–72)

- [ ] **Step 1: Write the failing test.** Create `tests/modules/execution/test_ledger_from_snapshot_per_symbol.py`:

```python
"""from_snapshot rebuilds multiple per-symbol buckets (Phase 1)."""
from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from bfx_funding_bot.core.db import Base
from bfx_funding_bot.modules.execution.event_store.tables import PositionStateRow
from bfx_funding_bot.modules.execution.ledger import PaperPositionLedger


@pytest.fixture
async def session() -> AsyncSession:  # type: ignore[misc]
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as s:
        yield s
    await engine.dispose()


async def test_from_snapshot_loads_multiple_symbol_rows(session: AsyncSession) -> None:
    session.add_all([
        PositionStateRow(
            account_id="default", deployment_environment="prod", symbol="fUST",
            reserved=Decimal("10"), realized=Decimal("440"),
            last_updated_ms=1, last_event_seq=1,
        ),
        PositionStateRow(
            account_id="default", deployment_environment="prod", symbol="fUSD",
            reserved=Decimal("3"), realized=Decimal("90"),
            last_updated_ms=1, last_event_seq=1,
        ),
        # different env — must be ignored
        PositionStateRow(
            account_id="default", deployment_environment="shadow", symbol="fUST",
            reserved=Decimal("999"), realized=Decimal("999"),
            last_updated_ms=1, last_event_seq=1,
        ),
    ])
    await session.commit()

    led = await PaperPositionLedger.from_snapshot(
        session, account_id="default", deployment_environment="prod",
    )
    assert led.reserved_exposure("fUST") == Decimal("10")
    assert led.realized_exposure("fUST") == Decimal("440")
    assert led.reserved_exposure("fUSD") == Decimal("3")
    assert led.realized_exposure("fUSD") == Decimal("90")
    # available is never persisted — 0 until first reconcile
    assert led.available_balance("fUST") == Decimal("0")


async def test_from_snapshot_empty_is_all_zero(session: AsyncSession) -> None:
    led = await PaperPositionLedger.from_snapshot(
        session, account_id="default", deployment_environment="prod",
    )
    assert led.reserved_exposure("fUST") == Decimal("0")
    assert led.realized_exposure("fUST") == Decimal("0")
```

- [ ] **Step 2: Run it — expect FAIL.** `uv run pytest tests/modules/execution/test_ledger_from_snapshot_per_symbol.py -q` — fails: `from_snapshot` uses `.scalar_one_or_none()` (only one row) and reads `row.reserved_usdt`/`row.realized_usdt` (renamed) and writes scalars.

- [ ] **Step 3: Implement — `from_snapshot`.** Replace lines 47–72:

```python
    @classmethod
    async def from_snapshot(
        cls,
        session: AsyncSession,
        *,
        account_id: str,
        deployment_environment: str,
    ) -> PaperPositionLedger:
        """Load ledger counters from the position_state snapshot table (no replay)."""
        from sqlalchemy import select

        from bfx_funding_bot.modules.execution.event_store.tables import PositionStateRow

        ledger = cls(account_id=account_id)
        row = (
            await session.execute(
                select(PositionStateRow).where(
                    PositionStateRow.account_id == account_id,
                    PositionStateRow.deployment_environment == deployment_environment,
                )
            )
        ).scalar_one_or_none()
        if row is not None:
            ledger._reserved = Decimal(str(row.reserved_usdt))
            ledger._realized = Decimal(str(row.realized_usdt))
        return ledger
```

with:

```python
    @classmethod
    async def from_snapshot(
        cls,
        session: AsyncSession,
        *,
        account_id: str,
        deployment_environment: str,
    ) -> PaperPositionLedger:
        """Load per-symbol ledger counters from the position_state snapshot
        table (no replay). Loads ALL rows for (account, env) — one per symbol —
        into the per-symbol dicts. `available` is never persisted (in-memory,
        populated by the first reconcile), so it stays empty here."""
        from sqlalchemy import select

        from bfx_funding_bot.modules.execution.event_store.tables import PositionStateRow

        ledger = cls(account_id=account_id)
        rows = (
            await session.execute(
                select(PositionStateRow).where(
                    PositionStateRow.account_id == account_id,
                    PositionStateRow.deployment_environment == deployment_environment,
                )
            )
        ).scalars().all()
        for row in rows:
            ledger._reserved[row.symbol] = Decimal(str(row.reserved))
            ledger._realized[row.symbol] = Decimal(str(row.realized))
        return ledger
```

- [ ] **Step 4: Run it — expect PASS.** `uv run pytest tests/modules/execution/test_ledger_from_snapshot_per_symbol.py -q` — both tests pass.

- [ ] **Step 5: Commit.** `git add tests/modules/execution/test_ledger_from_snapshot_per_symbol.py src/bfx_funding_bot/modules/execution/ledger.py && git commit -m "♻️ Refactor: ledger from_snapshot loads all per-symbol position_state rows (Phase 1)"`

---

### Task 11: Restore green: migrate legacy ledger tests + bridge in-repo getter callers

The legacy ledger tests (`test_ledger.py`, `test_ledger_dedup.py`, `test_ledger_reserved_realized.py`) and the 3 in-repo getter call sites (hard_guards.py ×2, reconciler.py ×3) still use the old zero-arg getters / `size_usdt` fixtures and are now red. Migrate the legacy tests to the new field names + symbol getters, and pass a single hard-coded `"fUST"` at the call sites as a temporary bridge so the WHOLE suite is green at this commit.

> **Stitching note for controller**: the hard_guards.py / reconciler.py edits below are a TEMPORARY single-symbol bridge to keep this commit green. Cluster E (guards) overwrites those exact lines to read `decision.symbol`; Cluster F (reconciler) overwrites them to use `cell_symbol`. If E and F are stitched in the SAME commit as Cluster B, skip this task's caller edits and let E/F own them. The legacy-test migrations below are always required.

**Files:**
- `tests/modules/execution/test_ledger.py` (modify: 16–90)
- `tests/modules/execution/test_ledger_dedup.py` (modify: 14–74)
- `tests/modules/execution/test_ledger_reserved_realized.py` (modify: 17–101)
- `src/bfx_funding_bot/modules/execution/safety/hard_guards.py` (modify: 107–108, 135, 149–150, 180)
- `src/bfx_funding_bot/modules/execution/deployment/reconciler.py` (modify: 94, 98, 106)

- [ ] **Step 1: Confirm current red.** `uv run pytest -m "not integration" -q 2>&1 | tail -20` — observe failures in the three legacy ledger test files and TypeErrors from hard_guards/reconciler getter calls. This is the baseline this task drives to zero.

- [ ] **Step 2: Migrate `test_ledger_reserved_realized.py`.** Replace the three fixture builders (lines 17–37) so they pass `symbol`/`amount`:

old:
```python
def _claim(size: float, account_id: str = "default") -> ReservationClaimed:
    return ReservationClaimed(
        cid=1, venue_offer_id="x", size_usdt=Decimal(str(size)),
        signal_correlation_id=uuid4(), account_id=account_id, is_simulated=True,
    )


def _fill(size: float, account_id: str = "default") -> OrderFilled:
    return OrderFilled(
        cid=1, venue_offer_id="x", credit_id=None,
        size_usdt=Decimal(str(size)), fill_rate=0.0001,
        signal_correlation_id=uuid4(), account_id=account_id, is_simulated=True,
    )


def _release(size: float, account_id: str = "default") -> ReservationReleased:
    return ReservationReleased(
        cid=1, venue_offer_id="x", size_usdt=Decimal(str(size)),
        reason="venue_cancel", signal_correlation_id=uuid4(),
        account_id=account_id, is_simulated=True,
    )
```

new:
```python
SYM = "fUST"


def _claim(size: float, account_id: str = "default") -> ReservationClaimed:
    return ReservationClaimed(
        cid=1, venue_offer_id="x", symbol=SYM, amount=Decimal(str(size)),
        signal_correlation_id=uuid4(), account_id=account_id, is_simulated=True,
    )


def _fill(size: float, account_id: str = "default") -> OrderFilled:
    return OrderFilled(
        cid=1, venue_offer_id="x", credit_id=None,
        symbol=SYM, amount=Decimal(str(size)), fill_rate=0.0001,
        signal_correlation_id=uuid4(), account_id=account_id, is_simulated=True,
    )


def _release(size: float, account_id: str = "default") -> ReservationReleased:
    return ReservationReleased(
        cid=1, venue_offer_id="x", symbol=SYM, amount=Decimal(str(size)),
        reason="venue_cancel", signal_correlation_id=uuid4(),
        account_id=account_id, is_simulated=True,
    )
```

Then in the SAME file, give every getter call a `SYM` argument: `current_exposure()` → `current_exposure(SYM)`, `realized_exposure()` → `realized_exposure(SYM)`. (Occurrences at lines 43–44, 51–52, 60–61, 68–69, 80, 90, 100–101.) Use find/replace within this file: `current_exposure()` → `current_exposure(SYM)` and `realized_exposure()` → `realized_exposure(SYM)`.

- [ ] **Step 3: Migrate `test_ledger_dedup.py`.** Replace builders (lines 14–35):

old `_filled`/`_released`/`_claimed` use `size_usdt=Decimal("100")` and no `symbol`. Replace with:
```python
SYM = "fUST"


def _filled(venue_seq: int | None, venue_offer_id: str = "v1") -> OrderFilled:
    return OrderFilled(
        cid=42, venue_offer_id=venue_offer_id, credit_id="C-1",
        symbol=SYM, amount=Decimal("100"), fill_rate=0.0005,
        signal_correlation_id=uuid4(), account_id="default", is_simulated=False,
        venue_seq=venue_seq,
    )


def _released(venue_seq: int | None, venue_offer_id: str = "v1") -> ReservationReleased:
    return ReservationReleased(
        cid=42, venue_offer_id=venue_offer_id, symbol=SYM, amount=Decimal("100"),
        reason="venue_cancel", signal_correlation_id=uuid4(),
        account_id="default", is_simulated=False, venue_seq=venue_seq,
    )


def _claimed(venue_offer_id: str = "v1") -> ReservationClaimed:
    return ReservationClaimed(
        cid=42, venue_offer_id=venue_offer_id, symbol=SYM, amount=Decimal("100"),
        signal_correlation_id=uuid4(), account_id="default", is_simulated=False,
    )
```

Then in the same file replace getter calls: `realized_exposure()` → `realized_exposure(SYM)` (lines 44, 64, 74) and `current_exposure()` → `current_exposure(SYM)` (lines 45, 54).

- [ ] **Step 4: Migrate `test_ledger.py`.** This file inlines events. Apply these exact edits:
  - line 19–21 `ReservationClaimed(... size_usdt=Decimal("100") ...)` → add `symbol="fUST",` and rename `size_usdt=` to `amount=`.
  - line 28–32 `OrderFilled(... size_usdt=Decimal("100") ...)` → add `symbol="fUST",` and `size_usdt=`→`amount=`.
  - line 41–45 `ReservationReleased(... size_usdt=Decimal("100") ...)` → add `symbol="fUST",` and `size_usdt=`→`amount=`.
  - line 54–57 `ReservationClaimed(... size_usdt=Decimal("100") ...)` → add `symbol="fUST",` and `size_usdt=`→`amount=`.
  - `PositionReconciled(...)` at lines 69–77 and 84–88: rename `reserved_usdt=`→`reserved=`, `realized_usdt=`→`realized=`, `available_usdt=`→`available=`, and add `symbol="fUST",`.
  - All getter calls: `current_exposure()` → `current_exposure("fUST")`, `realized_exposure()` → `realized_exposure("fUST")`, `available_balance()` → `available_balance("fUST")` (lines 23, 34–36, 47, 59, 64, 78–79, 89). The two `test_on_position_reconciled_*` assertions read `available_balance("fUST")` / `current_exposure("fUST")`.

  Concretely, the two reconcile-arg events become:
  ```python
  PositionReconciled(
      account_id="default", symbol="fUST",
      reserved=Decimal("0"), realized=Decimal("406.89"), available=Decimal("147.5"),
      n_offers=0, n_credits=2, occurred_at_ms=1_000,
  )
  ```
  and
  ```python
  PositionReconciled(
      account_id="other", symbol="fUST",
      reserved=Decimal("1"), realized=Decimal("1"), available=Decimal("99"),
      n_offers=1, n_credits=1, occurred_at_ms=1,
  )
  ```

- [ ] **Step 5: Bridge hard_guards.py callers.** Edit `src/bfx_funding_bot/modules/execution/safety/hard_guards.py`:
  - Protocol at 107–108:
    old `    def current_exposure(self) -> Decimal: ...`
    new `    def current_exposure(self, symbol: str) -> Decimal: ...`
  - line 135:
    old `        exposure = self.ledger.current_exposure()`
    new `        exposure = self.ledger.current_exposure(decision.symbol)`
  - Protocol at 149–150:
    old `    def available_balance(self) -> Decimal: ...`
    new `    def available_balance(self, symbol: str) -> Decimal: ...`
  - line 180:
    old `        available = self.ledger.available_balance()`
    new `        available = self.ledger.available_balance(decision.symbol)`

  > `decision.symbol` is added to `DecisionPayload` by Cluster D. If D is not yet stitched, temporarily use the literal `"fUST"` (e.g. `self.ledger.current_exposure("fUST")`) and flag it; Cluster E replaces with `decision.symbol`.

- [ ] **Step 6: Bridge reconciler.py callers.** Edit `src/bfx_funding_bot/modules/execution/deployment/reconciler.py`:
  - line 94:
    old `        e_total = self._ledger.current_exposure()`
    new `        e_total = self._ledger.current_exposure(self._symbol)`
  - line 98:
    old `        headroom = max(Decimal("0"), self._ledger.available_balance() - self._balance_buffer)`
    new `        headroom = max(Decimal("0"), self._ledger.available_balance(self._symbol) - self._balance_buffer)`
  - line 106:
    old `            self._ledger.reserved_exposure(),`
    new `            self._ledger.reserved_exposure(self._symbol),`

  > `self._symbol` here is the reconciler's single configured symbol — a temporary bridge. If the reconciler has no `self._symbol` attribute, add `self._symbol = "fUST"` in its `__init__` as a stopgap and flag it; Cluster F replaces these with the per-cell `cell_symbol` / `available_balance(cell_symbol)` headroom logic.

- [ ] **Step 7: Run the FULL gate — expect PASS.** `uv run pytest -m "not integration" -q` then `uv run mypy src/` then `uv run ruff check .` — all green.

- [ ] **Step 8: Commit.** `git add tests/modules/execution/test_ledger.py tests/modules/execution/test_ledger_dedup.py tests/modules/execution/test_ledger_reserved_realized.py src/bfx_funding_bot/modules/execution/safety/hard_guards.py src/bfx_funding_bot/modules/execution/deployment/reconciler.py && git commit -m "♻️ Refactor: migrate legacy ledger tests to symbol/amount + single-symbol getter bridge (Phase 1)"`


---

# Cluster D — Reconcile + boot recovery per-symbol

All commands run from `backend_py/`. Unit gate: `uv run pytest -m "not integration"`; types: `uv run mypy src/`; lint: `uv run ruff check .`.

These tasks ASSUME clusters A (events `+symbol`, rename `size_usdt→amount`, `PositionReconciled` fields → `reserved/realized/available` + `symbol`), B (per-symbol ledger getters `current_exposure(symbol)`/`available_balance(symbol)`), C (per-symbol `position_state` + `set_position_snapshot(..., symbol=...)`), and the DecisionPayload `symbol` field are already merged on the branch. Each task below keeps the suite green on top of those.

---

### Task 12: `BootRecovery` accepts `symbols: list[str]` (single-element parity, default keeps `_symbol`)

Refactor the constructor + fetch helpers so a list of configured symbols flows through, but with exactly ONE configured symbol the published event / result / snapshot are byte-identical to today. We do NOT yet emit multiple events (Task 2) — this task only widens the plumbing and proves a 1-element list reproduces current behaviour.

**Files:**
- `tests/modules/execution/test_boot_recovery.py` (add new tests after the existing wallet tests, ~end of file)
- `src/bfx_funding_bot/modules/execution/boot_recovery.py` (constructor `:212-243`, `_fetch_offers` `:316-343`, `_fetch_credits` `:345-372`, `_fetch_available` `:374-404`)

- [ ] **Step 1: Write failing test — list constructor + 1-symbol parity.** Append to `tests/modules/execution/test_boot_recovery.py`:

```python
# ── Per-symbol plumbing (Cluster D Task 1) ───────────────────────────────────


def _boot_recovery_symbols(auth_rest, store, session_factory, bus, *, symbols, **kw):
    """BootRecovery wired with the new `symbols` list arg (run() tests)."""
    return BootRecovery(
        store=store,
        session_factory=session_factory,
        auth_rest=auth_rest,
        account_ctx=AccountContext(
            account_id="default",
            credentials=Credentials(api_key="k", api_secret="s"),
            allocation_cap_usdt=Decimal("1"),
        ),
        deployment_environment="ci",
        bus=bus,
        max_attempts=1,
        backoff_base_s=0,
        clock=lambda: _NOW,
        symbols=symbols,
        **kw,
    )


@pytest.mark.asyncio
async def test_single_symbol_list_reproduces_current_event():
    """One configured symbol → exactly one PositionReconciled, identical natives."""
    offers = [_offer(voi="555", amount="100")]
    credits = [_credit("1", "200")]
    store = _StubStore()
    bus = _StubBus()
    auth = _StubAuthRestFull(offers=offers, credits=credits, available=Decimal("47.5"))
    rec = _boot_recovery_symbols(
        auth, store, _StubSessionFactory(), bus, symbols=["fUST"],
    )

    result = await rec.run()

    pr = [e for e in bus.published if isinstance(e, PositionReconciled)]
    assert len(pr) == 1
    assert pr[0].symbol == "fUST"
    assert pr[0].reserved == Decimal("100")
    assert pr[0].realized == Decimal("200")
    assert pr[0].available == Decimal("47.5")
    assert pr[0].n_offers == 1
    assert pr[0].n_credits == 1
    # result keeps the aggregate dims (single symbol == today)
    assert result.reserved_usdt == Decimal("100")
    assert result.realized_usdt == Decimal("200")
    assert result.available_usdt == Decimal("47.5")
    assert result.n_credits == 1


@pytest.mark.asyncio
async def test_legacy_symbol_kwarg_still_constructs_single_symbol():
    """Back-compat: passing the old `symbol=` kwarg yields a 1-element symbol list."""
    store = _StubStore()
    bus = _StubBus()
    auth = _StubAuthRestFull(offers=[], credits=[_credit("1", "150")])
    rec = _full_boot_recovery(auth, store, _StubSessionFactory(), bus, symbol="fUST")

    await rec.run()

    pr = [e for e in bus.published if isinstance(e, PositionReconciled)]
    assert len(pr) == 1
    assert pr[0].symbol == "fUST"
    assert pr[0].realized == Decimal("150")
```

- [ ] **Step 2: Run it — fails.** Command: `uv run pytest tests/modules/execution/test_boot_recovery.py -m "not integration" -q`. Expected: `test_single_symbol_list_reproduces_current_event` and `test_legacy_symbol_kwarg_still_constructs_single_symbol` fail — `TypeError: __init__() got an unexpected keyword argument 'symbols'` (and, once that is fixed, `AttributeError`/`TypeError` on `pr[0].symbol`).

- [ ] **Step 3: Implement — constructor takes `symbols`, normalises legacy `symbol`.** In `boot_recovery.py`, replace the constructor signature + body. OLD (`:212-243`):

```python
    def __init__(
        self,
        *,
        store: PostgresEventStore,
        session_factory: async_sessionmaker[AsyncSession],
        auth_rest: _AuthRestQuery,
        account_ctx: AccountContext,
        deployment_environment: str,
        bus: _Bus,
        offer_registry: _FsmSink | None = None,
        is_simulated: bool = False,
        symbol: str = "fUSD",
        grace_ms: int = 120_000,
        action_grace_ms: int = 0,
        max_attempts: int = 3,
        backoff_base_s: float = 1.0,
        clock: Callable[[], int] | None = None,
    ) -> None:
        self._store = store
        self._session_factory = session_factory
        self._auth_rest = auth_rest
        self._ctx = account_ctx
        self._env = deployment_environment
        self._bus = bus
        self._offer_registry = offer_registry
        self._is_simulated = is_simulated
        self._symbol = symbol
        self._grace_ms = grace_ms
        self._action_grace_ms = action_grace_ms
        self._max_attempts = max_attempts
        self._backoff_base_s = backoff_base_s
        self._clock = clock or (lambda: int(time.time() * 1000))
```

NEW:

```python
    def __init__(
        self,
        *,
        store: PostgresEventStore,
        session_factory: async_sessionmaker[AsyncSession],
        auth_rest: _AuthRestQuery,
        account_ctx: AccountContext,
        deployment_environment: str,
        bus: _Bus,
        offer_registry: _FsmSink | None = None,
        is_simulated: bool = False,
        symbol: str = "fUSD",
        symbols: list[str] | None = None,
        grace_ms: int = 120_000,
        action_grace_ms: int = 0,
        max_attempts: int = 3,
        backoff_base_s: float = 1.0,
        clock: Callable[[], int] | None = None,
    ) -> None:
        self._store = store
        self._session_factory = session_factory
        self._auth_rest = auth_rest
        self._ctx = account_ctx
        self._env = deployment_environment
        self._bus = bus
        self._offer_registry = offer_registry
        self._is_simulated = is_simulated
        # Configured symbols drive the per-symbol reconcile loop. Back-compat:
        # the legacy single `symbol` kwarg maps to a 1-element list. Dedup while
        # preserving order so a misconfigured duplicate cell can't fire twice.
        raw = symbols if symbols is not None else [symbol]
        seen: set[str] = set()
        self._symbols: list[str] = []
        for s in raw:
            if s not in seen:
                seen.add(s)
                self._symbols.append(s)
        self._grace_ms = grace_ms
        self._action_grace_ms = action_grace_ms
        self._max_attempts = max_attempts
        self._backoff_base_s = backoff_base_s
        self._clock = clock or (lambda: int(time.time() * 1000))
```

- [ ] **Step 4: Implement — fetch helpers take a `symbol` param.** The three fetch helpers currently hard-read `self._symbol`. Make them take an explicit `symbol` so Task 2's loop can fetch per symbol; Task 1's `run()` will pass `self._symbols[0]`. Replace each helper's signature + the line that reads `self._symbol`.

In `_fetch_offers` OLD `:316`/`:323-325`:

```python
    async def _fetch_offers(self) -> list[ActiveFundingOffer]:
```
...
```python
                return await self._auth_rest.get_active_funding_offers(
                    ctx=self._ctx, symbol=self._symbol,
                )
```

NEW:

```python
    async def _fetch_offers(self, symbol: str) -> list[ActiveFundingOffer]:
```
...
```python
                return await self._auth_rest.get_active_funding_offers(
                    ctx=self._ctx, symbol=symbol,
                )
```

In `_fetch_credits` OLD `:345`/`:352-354`:

```python
    async def _fetch_credits(self) -> list[ActiveFundingCredit]:
```
...
```python
                return await self._auth_rest.get_active_funding_credits(
                    ctx=self._ctx, symbol=self._symbol,
                )
```

NEW:

```python
    async def _fetch_credits(self, symbol: str) -> list[ActiveFundingCredit]:
```
...
```python
                return await self._auth_rest.get_active_funding_credits(
                    ctx=self._ctx, symbol=symbol,
                )
```

In `_fetch_available` OLD `:374`/`:380`:

```python
    async def _fetch_available(self) -> Decimal:
```
...
```python
        currency = self._symbol[1:] if self._symbol.startswith("f") else self._symbol
```

NEW:

```python
    async def _fetch_available(self, symbol: str) -> Decimal:
```
...
```python
        currency = symbol[1:] if symbol.startswith("f") else symbol
```

NOTE: the existing tests `test_fetch_offers_does_not_retry_4xx`, `test_fetch_offers_retries_5xx_then_succeeds`, `test_fetch_offers_reraises_after_transient_exhaustion`, `test_fetch_available_does_not_retry_4xx` call these helpers with NO arg. Update those four call sites in the test file to pass a symbol. In `tests/modules/execution/test_boot_recovery.py`:
- `await rec._fetch_offers()` → `await rec._fetch_offers("fUSD")` (3 occurrences, lines ~346, ~354, ~363)
- `await rec._fetch_available()` → `await rec._fetch_available("fUSD")` (line ~594)

- [ ] **Step 5: Implement — `run()` passes the single configured symbol + stamps `symbol` on the event.** Replace the head of `run()` (OLD `:245-285`) so it uses `self._symbols[0]` for the fetches and carries `symbol` into `set_position_snapshot` and `PositionReconciled`. OLD:

```python
    async def run(self) -> ReconcileResult:
        # All fetches may raise → daemon fail-safe (never trade without venue truth).
        venue_offers = await self._fetch_offers()
        venue_credits = await self._fetch_credits()
        available_usdt = await self._fetch_available()

        reserved_usdt = sum((o.amount for o in venue_offers), Decimal("0"))
        realized_usdt = sum((c.amount for c in venue_credits), Decimal("0"))
        now_ms = self._clock()

        async with session_scope(self._session_factory) as session:
            local_claims = await self._load_local_claims(session)
            actions = compute_recovery_actions(
                venue_offers=venue_offers, local_claims=local_claims,
                account_id=self._ctx.account_id, is_simulated=self._is_simulated,
                now_ms=now_ms, grace_ms=self._grace_ms,
                action_grace_ms=self._action_grace_ms,
            )
            for ev in actions:
                await self._store.append(session, ev)
            # Direct-write absolute position snapshot (not through delta accumulator).
            drift = await self._store.set_position_snapshot(
                session,
                account_id=self._ctx.account_id,
                reserved_usdt=reserved_usdt,
                realized_usdt=realized_usdt,
                n_offers=len(venue_offers),
                n_credits=len(venue_credits),
                occurred_at_ms=now_ms,
            )

        # Publish in-memory projection events AFTER durable commit.
        position_reconciled = PositionReconciled(
            account_id=self._ctx.account_id,
            reserved_usdt=reserved_usdt,
            realized_usdt=realized_usdt,
            available_usdt=available_usdt,
            n_offers=len(venue_offers),
            n_credits=len(venue_credits),
            occurred_at_ms=now_ms,
        )
        # Snapshot signal → bus (the ledger's sole exposure authority at reconcile).
        await self._safe_publish(position_reconciled)
```

NEW (single symbol, still ONE event — multi-symbol loop is Task 2):

```python
    async def run(self) -> ReconcileResult:
        # All fetches may raise → daemon fail-safe (never trade without venue truth).
        # Phase 1: the single configured symbol drives the fetch (multi-symbol loop
        # arrives in the per-symbol reconcile task). With one symbol this is identical
        # to the historic global reconcile.
        symbol = self._symbols[0]
        venue_offers = await self._fetch_offers(symbol)
        venue_credits = await self._fetch_credits(symbol)
        available_usdt = await self._fetch_available(symbol)

        reserved_usdt = sum((o.amount for o in venue_offers), Decimal("0"))
        realized_usdt = sum((c.amount for c in venue_credits), Decimal("0"))
        now_ms = self._clock()

        async with session_scope(self._session_factory) as session:
            local_claims = await self._load_local_claims(session)
            actions = compute_recovery_actions(
                venue_offers=venue_offers, local_claims=local_claims,
                account_id=self._ctx.account_id, is_simulated=self._is_simulated,
                now_ms=now_ms, grace_ms=self._grace_ms,
                action_grace_ms=self._action_grace_ms,
            )
            for ev in actions:
                await self._store.append(session, ev)
            # Direct-write absolute position snapshot (not through delta accumulator).
            drift = await self._store.set_position_snapshot(
                session,
                account_id=self._ctx.account_id,
                symbol=symbol,
                reserved=reserved_usdt,
                realized=realized_usdt,
                n_offers=len(venue_offers),
                n_credits=len(venue_credits),
                occurred_at_ms=now_ms,
            )

        # Publish in-memory projection events AFTER durable commit.
        position_reconciled = PositionReconciled(
            account_id=self._ctx.account_id,
            symbol=symbol,
            reserved=reserved_usdt,
            realized=realized_usdt,
            available=available_usdt,
            n_offers=len(venue_offers),
            n_credits=len(venue_credits),
            occurred_at_ms=now_ms,
        )
        # Snapshot signal → bus (the ledger's sole exposure authority at reconcile).
        await self._safe_publish(position_reconciled)
```

NOTE on `set_position_snapshot` kwargs: per the CONTRACT cluster C renames `reserved_usdt/realized_usdt → reserved/realized` on the writer and adds `symbol`. If cluster C kept the old `reserved_usdt=`/`realized_usdt=` kwarg names, change `symbol=symbol, reserved=...` here to match the actual merged signature — see open_questions.

- [ ] **Step 6: Run it — passes.** Command: `uv run pytest tests/modules/execution/test_boot_recovery.py -m "not integration" -q`. Expected: all boot_recovery tests pass (the two new ones + the existing suite, with the four `_fetch_*` call sites updated).

- [ ] **Step 7: Types + lint.** Commands: `uv run mypy src/bfx_funding_bot/modules/execution/boot_recovery.py` and `uv run ruff check src/bfx_funding_bot/modules/execution/boot_recovery.py tests/modules/execution/test_boot_recovery.py`. Expected: clean.

- [ ] **Step 8: Commit.** `git add -A && git commit -m "♻️ Refactor: BootRecovery takes configured symbols list (1-symbol parity)"`

---

### Task 13: `BootRecovery.run()` loops configured symbols, one `PositionReconciled` per symbol

Generalise `run()` from the single-symbol fetch to a per-symbol loop: query venue offers/credits/wallet per symbol, fire ONE `PositionReconciled` per symbol carrying that symbol's native reserved/realized/available, and write per-symbol `position_state`. The FSM recovery diff (claim/release of `offer_claims`) stays GLOBAL because `offer_claims` has no symbol column and `venue_offer_id` is globally unique on Bitfinex — see Step 3's union of venue offers across symbols (prevents a cross-symbol "missing_from_venue" false release). `ReconcileResult` keeps its aggregate dims (Σ across symbols) so `PeriodicReconcile`'s divergence/drift logic is unchanged.

**Files:**
- `tests/modules/execution/test_boot_recovery.py` (append after Task 1's tests)
- `src/bfx_funding_bot/modules/execution/boot_recovery.py` (`run()` `:245-314`)

- [ ] **Step 1: Write failing test — 2-symbol config fires 2 events with per-symbol natives.** First add a per-symbol auth stub + an offer factory that takes a symbol, then the tests. Append to `tests/modules/execution/test_boot_recovery.py`:

```python
# ── Multi-symbol reconcile loop (Cluster D Task 2) ───────────────────────────


def _offer_sym(symbol, voi, amount):
    return ActiveFundingOffer(
        venue_offer_id=voi, symbol=symbol, amount=Decimal(amount),
        rate=0.0003, period_days=2, mts_created=1_000_000, status="ACTIVE",
    )


def _credit_sym(symbol, credit_id, amount):
    return ActiveFundingCredit(
        credit_id=credit_id, symbol=symbol, amount=Decimal(amount),
        rate=0.0003, period_days=2, status="ACTIVE",
    )


class _StubAuthPerSymbol:
    """Per-symbol offers/credits/available keyed by symbol."""
    def __init__(self, offers_by_sym, credits_by_sym, available_by_sym):
        self._offers = offers_by_sym
        self._credits = credits_by_sym
        self._available = available_by_sym
        self.offer_calls: list[str] = []
        self.credit_calls: list[str] = []
        self.wallet_calls: list[str] = []

    async def get_active_funding_offers(self, *, ctx, symbol="fUSD"):
        self.offer_calls.append(symbol)
        return self._offers.get(symbol, [])

    async def get_active_funding_credits(self, *, ctx, symbol="fUSD"):
        self.credit_calls.append(symbol)
        return self._credits.get(symbol, [])

    async def get_funding_available(self, *, ctx, currency):
        self.wallet_calls.append(currency)
        return self._available.get(currency, Decimal("0"))


@pytest.mark.asyncio
async def test_two_symbols_fire_two_position_reconciled_with_per_symbol_natives():
    auth = _StubAuthPerSymbol(
        offers_by_sym={
            "fUST": [_offer_sym("fUST", "1", "100")],
            "fUSD": [_offer_sym("fUSD", "2", "40")],
        },
        credits_by_sym={
            "fUST": [_credit_sym("fUST", "c1", "200")],
            "fUSD": [],
        },
        available_by_sym={"UST": Decimal("17.5"), "USD": Decimal("9")},
    )
    store = _StubStore()
    bus = _StubBus()
    rec = _boot_recovery_symbols(
        auth, store, _StubSessionFactory(), bus, symbols=["fUST", "fUSD"],
    )

    await rec.run()

    pr = [e for e in bus.published if isinstance(e, PositionReconciled)]
    assert len(pr) == 2
    by_sym = {e.symbol: e for e in pr}
    assert by_sym["fUST"].reserved == Decimal("100")
    assert by_sym["fUST"].realized == Decimal("200")
    assert by_sym["fUST"].available == Decimal("17.5")
    assert by_sym["fUST"].n_offers == 1 and by_sym["fUST"].n_credits == 1
    assert by_sym["fUSD"].reserved == Decimal("40")
    assert by_sym["fUSD"].realized == Decimal("0")
    assert by_sym["fUSD"].available == Decimal("9")
    assert by_sym["fUSD"].n_offers == 1 and by_sym["fUSD"].n_credits == 0
    # one wallet read per symbol's currency (no cross-symbol sum)
    assert auth.wallet_calls == ["UST", "USD"]


@pytest.mark.asyncio
async def test_two_symbols_write_per_symbol_snapshot_rows():
    auth = _StubAuthPerSymbol(
        offers_by_sym={"fUST": [_offer_sym("fUST", "1", "100")], "fUSD": []},
        credits_by_sym={"fUST": [], "fUSD": [_credit_sym("fUSD", "c1", "55")]},
        available_by_sym={"UST": Decimal("1"), "USD": Decimal("2")},
    )
    store = _StubStore()
    bus = _StubBus()
    rec = _boot_recovery_symbols(
        auth, store, _StubSessionFactory(), bus, symbols=["fUST", "fUSD"],
    )

    await rec.run()

    assert len(store.snapshot_calls) == 2
    by_sym = {c["symbol"]: c for c in store.snapshot_calls}
    assert by_sym["fUST"]["reserved"] == Decimal("100")
    assert by_sym["fUST"]["realized"] == Decimal("0")
    assert by_sym["fUSD"]["reserved"] == Decimal("0")
    assert by_sym["fUSD"]["realized"] == Decimal("55")


@pytest.mark.asyncio
async def test_multi_symbol_result_aggregates_dims():
    auth = _StubAuthPerSymbol(
        offers_by_sym={"fUST": [_offer_sym("fUST", "1", "100")], "fUSD": []},
        credits_by_sym={
            "fUST": [_credit_sym("fUST", "c1", "200")],
            "fUSD": [_credit_sym("fUSD", "c2", "30")],
        },
        available_by_sym={"UST": Decimal("5"), "USD": Decimal("7")},
    )
    store = _StubStore()
    bus = _StubBus()
    rec = _boot_recovery_symbols(
        auth, store, _StubSessionFactory(), bus, symbols=["fUST", "fUSD"],
    )

    result = await rec.run()

    # aggregate across symbols (PeriodicReconcile divergence/drift unchanged)
    assert result.reserved_usdt == Decimal("100")
    assert result.realized_usdt == Decimal("230")
    assert result.available_usdt == Decimal("12")
    assert result.n_credits == 2
```

Also update `_StubStore.set_position_snapshot` in the SAME file so it records `symbol` and the renamed kwargs (the stub must match cluster C's writer signature). Replace the existing `_StubStore.set_position_snapshot` (`:205-218`) OLD:

```python
    async def set_position_snapshot(
        self, session, *, account_id, reserved_usdt, realized_usdt,
        n_offers, n_credits, occurred_at_ms,
    ):
        from bfx_funding_bot.modules.execution.event_store.store import SnapshotDrift
        self.snapshot_calls.append({
            "account_id": account_id,
            "reserved_usdt": reserved_usdt,
            "realized_usdt": realized_usdt,
            "n_offers": n_offers,
            "n_credits": n_credits,
            "occurred_at_ms": occurred_at_ms,
        })
        return SnapshotDrift(reserved_drift=Decimal("0"), realized_drift=Decimal("0"))
```

NEW:

```python
    async def set_position_snapshot(
        self, session, *, account_id, symbol, reserved, realized,
        n_offers, n_credits, occurred_at_ms,
    ):
        from bfx_funding_bot.modules.execution.event_store.store import SnapshotDrift
        self.snapshot_calls.append({
            "account_id": account_id,
            "symbol": symbol,
            "reserved": reserved,
            "realized": realized,
            "n_offers": n_offers,
            "n_credits": n_credits,
            "occurred_at_ms": occurred_at_ms,
        })
        return SnapshotDrift(reserved_drift=Decimal("0"), realized_drift=Decimal("0"))
```

Existing Task-1-era tests that assert on `call["reserved_usdt"]` / `call["realized_usdt"]` (`test_run_calls_set_position_snapshot_with_credit_sum` `:430-434`) must be updated to the renamed keys. OLD:

```python
    assert call["realized_usdt"] == Decimal("300")
    assert call["reserved_usdt"] == Decimal("0")
    assert call["n_credits"] == 1
```

NEW:

```python
    assert call["realized"] == Decimal("300")
    assert call["reserved"] == Decimal("0")
    assert call["n_credits"] == 1
```

And `test_run_populates_available_from_wallets` `:575` asserts `"available_usdt" not in store.snapshot_calls[-1]` — still true (no `available` key), keep it.

- [ ] **Step 2: Run it — fails.** Command: `uv run pytest tests/modules/execution/test_boot_recovery.py -m "not integration" -q`. Expected: the three new multi-symbol tests fail (`run()` still emits ONE event for `self._symbols[0]`, so `len(pr) == 2` and `auth.wallet_calls == ["UST", "USD"]` fail).

- [ ] **Step 3: Implement — per-symbol loop in `run()`.** Replace the whole `run()` body (OLD `:245-314`, ending at the `return ReconcileResult(...)`):

```python
    async def run(self) -> ReconcileResult:
        # Per-symbol reconcile: each configured currency is an independent wallet
        # (native units), so offers/credits/available are queried per symbol and a
        # PositionReconciled is published per symbol. The FSM recovery diff
        # (orphan-claim / missing-release of offer_claims) stays GLOBAL: offer_claims
        # carries no symbol and venue_offer_id is globally unique on Bitfinex, so we
        # union venue offers across symbols before diffing local claims — otherwise a
        # claim for symbol B would look "missing_from_venue" while reconciling symbol A
        # and be spuriously released. ReconcileResult aggregates across symbols (the
        # PeriodicReconcile divergence/drift logic is per-tick, not per-symbol).
        now_ms = self._clock()
        per_symbol: list[tuple[str, list[ActiveFundingOffer], list[ActiveFundingCredit], Decimal]] = []
        all_offers: list[ActiveFundingOffer] = []
        for symbol in self._symbols:
            offers = await self._fetch_offers(symbol)
            credits = await self._fetch_credits(symbol)
            available = await self._fetch_available(symbol)
            per_symbol.append((symbol, offers, credits, available))
            all_offers.extend(offers)

        agg_reserved = Decimal("0")
        agg_realized = Decimal("0")
        agg_available = Decimal("0")
        agg_n_credits = 0
        agg_reserved_drift = Decimal("0")
        agg_realized_drift = Decimal("0")

        async with session_scope(self._session_factory) as session:
            local_claims = await self._load_local_claims(session)
            # GLOBAL FSM diff against the union of all symbols' venue offers.
            actions = compute_recovery_actions(
                venue_offers=all_offers, local_claims=local_claims,
                account_id=self._ctx.account_id, is_simulated=self._is_simulated,
                now_ms=now_ms, grace_ms=self._grace_ms,
                action_grace_ms=self._action_grace_ms,
            )
            for ev in actions:
                await self._store.append(session, ev)
            # Per-symbol absolute position snapshot (single-writer per symbol).
            for symbol, offers, credits, _avail in per_symbol:
                reserved = sum((o.amount for o in offers), Decimal("0"))
                realized = sum((c.amount for c in credits), Decimal("0"))
                drift = await self._store.set_position_snapshot(
                    session,
                    account_id=self._ctx.account_id,
                    symbol=symbol,
                    reserved=reserved,
                    realized=realized,
                    n_offers=len(offers),
                    n_credits=len(credits),
                    occurred_at_ms=now_ms,
                )
                agg_reserved_drift += drift.reserved_drift
                agg_realized_drift += drift.realized_drift

        # Publish one PositionReconciled per symbol AFTER durable commit.
        for symbol, offers, credits, available in per_symbol:
            reserved = sum((o.amount for o in offers), Decimal("0"))
            realized = sum((c.amount for c in credits), Decimal("0"))
            await self._safe_publish(PositionReconciled(
                account_id=self._ctx.account_id,
                symbol=symbol,
                reserved=reserved,
                realized=realized,
                available=available,
                n_offers=len(offers),
                n_credits=len(credits),
                occurred_at_ms=now_ms,
            ))
            agg_reserved += reserved
            agg_realized += realized
            agg_available += available
            agg_n_credits += len(credits)

        n_claim = n_release = n_fail = 0
        for ev in actions:
            if isinstance(ev, ReservationClaimed):
                n_claim += 1
                await self._route_fsm(ev)
            elif isinstance(ev, ReservationReleased):
                n_release += 1
                await self._route_fsm(ev)
            elif isinstance(ev, ReservationFailed):
                n_fail += 1
        log.info(
            "reconcile_complete symbols=%d venue_offers=%d "
            "reserved=%.2f realized=%.2f available=%.2f "
            "orphans_claimed=%d released=%d pending_failed=%d",
            len(self._symbols), len(all_offers),
            float(agg_reserved), float(agg_realized), float(agg_available),
            n_claim, n_release, n_fail,
        )
        return ReconcileResult(
            n_claimed=n_claim, n_released=n_release, n_failed=n_fail,
            reserved_usdt=agg_reserved, realized_usdt=agg_realized,
            available_usdt=agg_available,
            n_credits=agg_n_credits,
            reserved_drift_usdt=agg_reserved_drift,
            realized_drift_usdt=agg_realized_drift,
        )
```

This replaces BOTH the head (Task 1's single-symbol fetch/snapshot/publish) AND the trailing `n_claim`/log/`return` block (OLD `:289-314`), so apply it as one contiguous `run()` replacement.

- [ ] **Step 4: Run it — passes.** Command: `uv run pytest tests/modules/execution/test_boot_recovery.py -m "not integration" -q`. Expected: all pass, including the single-symbol parity tests from Task 1 (1 symbol → 1 event, aggregate == that symbol) and the new 2-symbol tests.

- [ ] **Step 5: Types + lint.** Commands: `uv run mypy src/bfx_funding_bot/modules/execution/boot_recovery.py` and `uv run ruff check src/bfx_funding_bot/modules/execution/boot_recovery.py tests/modules/execution/test_boot_recovery.py`. Expected: clean.

- [ ] **Step 6: Commit.** `git add -A && git commit -m "✨ Feat: BootRecovery reconciles per configured symbol (one PositionReconciled each)"`

---

### Task 14: `PeriodicReconcile` per-symbol (verify-through + drift parity)

The contract item (2) "periodic_reconcile likewise fires per-symbol" is satisfied STRUCTURALLY by Task 2: `PeriodicReconcile._recovery` IS a `BootRecovery` instance, so `self._recovery.run()` now fires per-symbol inside `run()`. `PeriodicReconcile` itself needs NO behaviour change — it consumes the aggregate `ReconcileResult` for divergence/drift health, which is correct because divergence is a per-tick stream-health signal, not a per-symbol accounting fact. This task ADDS a regression test pinning that contract (so a future refactor can't silently make the loop single-symbol again) and confirms drift aggregation flows through.

**Files:**
- `tests/modules/execution/test_periodic_reconcile.py` (append after the existing tests)

- [ ] **Step 1: Write failing test — periodic loop drives the per-symbol recovery and surfaces aggregate drift.** Append to `tests/modules/execution/test_periodic_reconcile.py`:

```python
# ── Per-symbol recovery is driven by the loop (Cluster D Task 3) ─────────────


class _MultiSymbolRecovery:
    """Fake recovery that records how many times run() was driven and returns an
    aggregate ReconcileResult (as BootRecovery does after the per-symbol loop)."""
    def __init__(self, result):
        self._result = result
        self.runs = 0

    async def run(self) -> ReconcileResult:
        self.runs += 1
        return self._result


@pytest.mark.asyncio
async def test_loop_drives_aggregate_recovery_and_flags_drift():
    probe = _FakeProbe()
    # aggregate result with realized drift above epsilon → divergence flagged
    agg = ReconcileResult(
        n_claimed=0, n_released=0, n_failed=0,
        reserved_usdt=Decimal("100"), realized_usdt=Decimal("230"),
        available_usdt=Decimal("12"), n_credits=2,
        reserved_drift_usdt=Decimal("0"), realized_drift_usdt=Decimal("5"),
    )
    recovery = _MultiSymbolRecovery(agg)
    pr = PeriodicReconcile(
        recovery=recovery, probe=probe, interval_s=0.01, max_consecutive_failures=3,
    )
    stop = asyncio.Event()

    async def _stop_soon():
        await asyncio.sleep(0.02)
        stop.set()

    await asyncio.gather(pr.run_loop(stop), _stop_soon())

    assert recovery.runs >= 1  # the loop owns driving the (now per-symbol) recovery
    assert any(
        t == HealthTarget.RECONCILE and s == HealthStatus.DEGRADED
        for (t, s, _f) in probe.updates
    )
```

- [ ] **Step 2: Run it — passes immediately (parity regression pin).** Command: `uv run pytest tests/modules/execution/test_periodic_reconcile.py -m "not integration" -q`. Expected: PASS. This is a GREEN-confirming regression test (no production change needed in this file); it documents and locks the contract that `PeriodicReconcile` drives the per-symbol recovery via the aggregate result and still flags drift. If it fails, the aggregate-drift threading from Task 2 regressed — fix Task 2, not this file.

- [ ] **Step 3: Full unit gate.** Command: `uv run pytest -m "not integration" -q`. Expected: whole suite green (catches any caller of `PeriodicReconcile` / `ReconcileResult` you missed).

- [ ] **Step 4: Commit.** `git add -A && git commit -m "✅ Test: pin PeriodicReconcile drives per-symbol recovery via aggregate result"`

---

### Task 15: `DeploymentReconciler` sets `decision.symbol` from the cell + per-symbol headroom

Thread the cell's symbol into the per-offer `DecisionPayload` (so cluster E's guards can read it) and compute the balance clamp from `available_balance(cell_symbol)` instead of the global `available_balance()`. The cell→symbol map is built from `cells` in `__init__`. Single-symbol behaviour (today's fUST) is identical because all cells share `fUST`.

**Files:**
- `tests/modules/execution/deployment/test_reconciler.py` (`_FakeLedger` `:40-59`, `_build`/`_build_with_split_ledger`, add new tests at end)
- `src/bfx_funding_bot/modules/execution/deployment/reconciler.py` (`_LedgerProtocol` `:39-42`, `__init__` `:87-90`, `deploy()` headroom `:98` + cell map, decision construction `:169-176`)

- [ ] **Step 1: Write failing test — decision carries the cell symbol + headroom is per-symbol.** First update `_FakeLedger` so its getters take a `symbol` (matching cluster B), then add the two new tests. In `tests/modules/execution/deployment/test_reconciler.py` replace `_FakeLedger` (`:40-59`) OLD:

```python
class _FakeLedger:
    def __init__(
        self, exposure: Decimal, reserved: Decimal | None = None,
        available: Decimal | None = None,
    ) -> None:
        self._e = exposure
        # Default: reserved == exposure (all capital is reserved / open offers).
        # Pass reserved explicitly when simulating realized-only or mixed scenarios.
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

NEW (getters take a symbol; values are scalar because all test cells are fUST — per-symbol isolation is exercised via `available_by_symbol`):

```python
class _FakeLedger:
    def __init__(
        self, exposure: Decimal, reserved: Decimal | None = None,
        available: Decimal | None = None,
        available_by_symbol: dict[str, Decimal] | None = None,
    ) -> None:
        self._e = exposure
        # Default: reserved == exposure (all capital is reserved / open offers).
        # Pass reserved explicitly when simulating realized-only or mixed scenarios.
        self._reserved = reserved if reserved is not None else exposure
        # Default: effectively unbounded so existing cap-driven tests are unaffected.
        self._available = available if available is not None else Decimal("1000000")
        self._available_by_symbol = available_by_symbol or {}

    def current_exposure(self, symbol: str) -> Decimal:
        return self._e

    def reserved_exposure(self, symbol: str) -> Decimal:
        return self._reserved

    def available_balance(self, symbol: str) -> Decimal:
        if symbol in self._available_by_symbol:
            return self._available_by_symbol[symbol]
        return self._available
```

Then append new tests at the end of the file:

```python
# ---------------------------------------------------------------------------
# Cluster D: decision carries the cell symbol; headroom is per-symbol
# ---------------------------------------------------------------------------


async def test_decision_carries_cell_symbol():
    safety = _FakeSafety(allowed=True)
    rec, ex, _, _ = _build(
        exposure=D("370"), quotes=[_post_quote("fUST_a30")], safety=safety,
    )
    await rec.deploy()
    assert len(ex.submitted) == 1
    # both the decision handed to safety AND to the executor carry the symbol
    assert safety.calls[0].symbol == "fUST"
    assert ex.submitted[0].symbol == "fUST"


async def test_headroom_uses_cell_symbol_available():
    # cap gap = 570 - 370 = 200. The cell symbol fUST has available 250 (buffer 3
    # → headroom 247 >= 200), while the global default is starved (0). Reading the
    # per-symbol balance is what lets the deploy proceed.
    cells = [_cell("fUST", "a30"), _cell("fUST", "p2")]
    store = StandingQuoteStore(ttl_ms=3_900_000)
    store.update(_post_quote("fUST_a30"))
    tracker = CellDeploymentTracker()
    ledger = _FakeLedger(
        exposure=D("370"), available=D("0"),
        available_by_symbol={"fUST": D("250")},
    )
    ex = _FakeExecutor()
    rec = DeploymentReconciler(
        store=store, tracker=tracker, ledger=ledger,
        safety_chain=_FakeSafety(allowed=True), executor=ex, account_ctx=_ctx(),
        cells=cells, venue_floor_usd=D("150"), min_offer_buffer_pct=D("0.02"),
        concentration_pct=D("0.70"), balance_buffer_usdt=D("3"),
        clock=lambda: 1_000, event_sink=_CapturingSink(), phase=Phase.CANARY,
    )
    await rec.deploy()
    assert len(ex.submitted) == 1
    assert ex.submitted[0].offer_amount_usdt == 200.0
```

- [ ] **Step 2: Run it — fails.** Command: `uv run pytest tests/modules/execution/deployment/test_reconciler.py -m "not integration" -q`. Expected: the two new tests fail — `AttributeError: 'DecisionPayload' object has no attribute 'symbol'`... actually `symbol` now exists (cluster A) but is unset → `safety.calls[0].symbol` is `None` (not `"fUST"`). `test_headroom_uses_cell_symbol_available` fails because `deploy()` calls the now-`symbol`-less `available_balance()` (TypeError: missing 'symbol') after `_FakeLedger` got the new signature — the existing cap-driven tests also break until production is updated.

- [ ] **Step 3: Implement — per-symbol ledger protocol + cell→symbol map.** In `reconciler.py` update `_LedgerProtocol` (`:39-42`) OLD:

```python
class _LedgerProtocol(Protocol):
    def current_exposure(self) -> Decimal: ...
    def reserved_exposure(self) -> Decimal: ...
    def available_balance(self) -> Decimal: ...
```

NEW:

```python
class _LedgerProtocol(Protocol):
    def current_exposure(self, symbol: str) -> Decimal: ...
    def reserved_exposure(self, symbol: str) -> Decimal: ...
    def available_balance(self, symbol: str) -> Decimal: ...
```

Add a cell→symbol map next to the existing `_cell_strategy` map. OLD (`:87-90`):

```python
        # cell_id → strategy, for the structured ORDER_SUBMIT event envelope.
        self._cell_strategy: dict[str, StrategyName] = {
            c.cell_id: c.strategy for c in cells
        }
```

NEW:

```python
        # cell_id → strategy, for the structured ORDER_SUBMIT event envelope.
        self._cell_strategy: dict[str, StrategyName] = {
            c.cell_id: c.strategy for c in cells
        }
        # cell_id → symbol (offer currency), threaded onto the per-offer decision
        # so the per-symbol guards (allocation cap / buying power) can read it, and
        # used for the per-symbol balance clamp.
        self._cell_symbol: dict[str, str] = {c.cell_id: c.symbol for c in cells}
```

- [ ] **Step 4: Implement — per-symbol exposure/reserved/headroom + decision.symbol.** In `deploy()`, the current global reads `:94-106` and `:98` use a single global exposure/reserved/headroom. For Phase 1 (all cells share `fUST`) the simplest correct shaping keeps the gap/sizing global but derives exposure/reserved/headroom from the SINGLE configured symbol shared by the cells, and stamps each per-cell decision with its own symbol. Replace the head of `deploy()` (OLD `:93-108`):

```python
        now = self._clock()
        e_total = self._ledger.current_exposure()
        # Clamp the deployable gap to funds physically present in the funding
        # wallet (available − buffer) so the reconciler never sizes an offer the
        # venue must reject for insufficient balance (cap>balance loop, 2026-05-29).
        headroom = max(Decimal("0"), self._ledger.available_balance() - self._balance_buffer)
        # Rescale per-cell intent to the *reserved* total (pending open offers),
        # NOT to current_exposure (reserved + realized). Realized credits are
        # committed to the venue and unattributable to any cell — using e_total
        # here would inflate per-cell intent past cap_per_cell (factor > 1) and
        # silently starve cells via negative allocate_gap headroom.
        cap_per_cell = self._concentration_pct * self._ctx.allocation_cap_usdt
        self._tracker.reconcile_to_total(
            self._ledger.reserved_exposure(),
            cap_per_cell=cap_per_cell,
        )
```

NEW:

```python
        now = self._clock()
        # Phase 1: all configured cells share one currency (fUST today). Exposure,
        # reserved, and the wallet balance clamp are read for that symbol; the
        # global gap/sizing math is unchanged (per-symbol gap pools are Phase 2).
        symbol = self._cell_symbol[self._cells[0].cell_id]
        e_total = self._ledger.current_exposure(symbol)
        # Clamp the deployable gap to funds physically present in the funding
        # wallet (available − buffer) so the reconciler never sizes an offer the
        # venue must reject for insufficient balance (cap>balance loop, 2026-05-29).
        headroom = max(Decimal("0"), self._ledger.available_balance(symbol) - self._balance_buffer)
        # Rescale per-cell intent to the *reserved* total (pending open offers),
        # NOT to current_exposure (reserved + realized). Realized credits are
        # committed to the venue and unattributable to any cell — using e_total
        # here would inflate per-cell intent past cap_per_cell (factor > 1) and
        # silently starve cells via negative allocate_gap headroom.
        cap_per_cell = self._concentration_pct * self._ctx.allocation_cap_usdt
        self._tracker.reconcile_to_total(
            self._ledger.reserved_exposure(symbol),
            cap_per_cell=cap_per_cell,
        )
```

Then stamp the per-cell decision with its own cell's symbol. OLD (`:169-175`):

```python
            decision = DecisionPayload(
                decision_outcome=DecisionOutcome.POST,
                signal_correlation_id=quote.signal_correlation_id,
                offer_rate=quote.rate,
                offer_amount_usdt=float(amount),
                offer_duration_days=quote.period_days,
            )
```

NEW:

```python
            decision = DecisionPayload(
                decision_outcome=DecisionOutcome.POST,
                signal_correlation_id=quote.signal_correlation_id,
                offer_rate=quote.rate,
                offer_amount_usdt=float(amount),
                offer_duration_days=quote.period_days,
                symbol=self._cell_symbol[cell_id],
            )
```

- [ ] **Step 5: Run it — passes.** Command: `uv run pytest tests/modules/execution/deployment/test_reconciler.py -m "not integration" -q`. Expected: all reconciler tests pass — the existing cap/headroom tests (now via `_FakeLedger`'s symbol-taking getters returning the scalar default for `fUST`) and the two new symbol/headroom tests.

- [ ] **Step 6: Types + lint.** Commands: `uv run mypy src/bfx_funding_bot/modules/execution/deployment/reconciler.py` and `uv run ruff check src/bfx_funding_bot/modules/execution/deployment/reconciler.py tests/modules/execution/deployment/test_reconciler.py`. Expected: clean.

- [ ] **Step 7: Commit.** `git add -A && git commit -m "✨ Feat: DeploymentReconciler stamps decision.symbol + per-symbol balance headroom"`

---

### Task 16: Daemon wiring: pass configured symbols to BootRecovery (full-suite green)

Update the two `BootRecovery(...)` construction sites in `daemon.py` to pass `symbols=` (distinct cell symbols, order-preserving) instead of the single `symbol=first_cell.symbol`. With today's single-fUST `cells.yaml` this is a 1-element list → identical runtime behaviour. This keeps the whole suite + mypy green end to end.

**Files:**
- `src/bfx_funding_bot/modules/marketfeed/daemon.py` (boot_recovery `:823-833`, runtime_recovery `:844-855`; cells available as `config.cells`)

- [ ] **Step 1: Confirm the failing surface.** Command: `uv run pytest -m "not integration" -q`. Expected at this point: green for the modules touched in Tasks 1–4, but any daemon-construction test that builds `BootRecovery` via `daemon.py` still passes `symbol=` only — that is back-compat-supported (Task 1 normalises `symbol`→1-element list), so the suite is already green. This task is a forward-looking wiring change so the multi-symbol path is reachable from config; the test below pins it.

- [ ] **Step 2: Write failing test — distinct cell symbols thread into BootRecovery.** Add a focused unit test. Create `tests/modules/marketfeed/test_daemon_recovery_symbols.py`:

```python
from decimal import Decimal

from bfx_funding_bot.modules.marketfeed.config import CellConfig
from bfx_funding_bot.modules.marketfeed.schemas import StrategyName


def _configured_symbols(cells: list[CellConfig]) -> list[str]:
    """Helper under test mirrors the daemon's symbol-derivation rule:
    distinct cell symbols, order-preserving."""
    from bfx_funding_bot.modules.marketfeed.daemon import configured_symbols
    return configured_symbols(cells)


def _cell(symbol: str, period_agg: str) -> CellConfig:
    return CellConfig(
        strategy=StrategyName.MEAN_REVERSION, symbol=symbol, period_agg=period_agg,
        timeframe="1h",
        params={"threshold_sigma": 1.0, "ratio_sigma": 1.0, "ema_span": 10},
        reference_amount_usdt=150.0, staleness_budget_hours=48,
    )


def test_configured_symbols_dedups_preserving_order():
    cells = [_cell("fUST", "a30"), _cell("fUST", "p2"), _cell("fUSD", "p2")]
    assert _configured_symbols(cells) == ["fUST", "fUSD"]


def test_configured_symbols_single_currency():
    cells = [_cell("fUST", "a30"), _cell("fUST", "p2")]
    assert _configured_symbols(cells) == ["fUST"]
```

- [ ] **Step 3: Run it — fails.** Command: `uv run pytest tests/modules/marketfeed/test_daemon_recovery_symbols.py -m "not integration" -q`. Expected: `ImportError: cannot import name 'configured_symbols' from ...daemon`.

- [ ] **Step 4: Implement — add `configured_symbols` helper + use it at both wiring sites.** In `daemon.py`, add a module-level helper near the top imports / other helpers (after the imports block, before the daemon build function). Insert:

```python
def configured_symbols(cells: list[CellConfig]) -> list[str]:
    """Distinct cell symbols, order-preserving — the per-currency reconcile loop's
    symbol set. Single-currency cells.yaml → a 1-element list (parity with the
    historic single-symbol BootRecovery)."""
    seen: set[str] = set()
    out: list[str] = []
    for c in cells:
        if c.symbol not in seen:
            seen.add(c.symbol)
            out.append(c.symbol)
    return out
```

Ensure `CellConfig` is imported in `daemon.py` (it already imports cell config / uses `first_cell.symbol`; if `CellConfig` is not yet imported, add `from bfx_funding_bot.modules.marketfeed.config import CellConfig` — verify against the actual import block when implementing). Then at the boot_recovery site, OLD (`:832`):

```python
            is_simulated=spec.is_simulated,
            symbol=first_cell.symbol,
        )
```

NEW:

```python
            is_simulated=spec.is_simulated,
            symbols=configured_symbols(config.cells),
        )
```

And at the runtime_recovery site, OLD (`:853-855`):

```python
            is_simulated=spec.is_simulated,
            symbol=first_cell.symbol,
            action_grace_ms=120_000,
        )
```

NEW:

```python
            is_simulated=spec.is_simulated,
            symbols=configured_symbols(config.cells),
            action_grace_ms=120_000,
        )
```

- [ ] **Step 5: Run it — passes.** Command: `uv run pytest tests/modules/marketfeed/test_daemon_recovery_symbols.py -m "not integration" -q`. Expected: both pass.

- [ ] **Step 6: Full unit gate + types + lint.** Commands: `uv run pytest -m "not integration" -q`, `uv run mypy src/`, `uv run ruff check .`. Expected: all green.

- [ ] **Step 7: Commit.** `git add -A && git commit -m "✨ Feat: daemon wires configured symbols into boot + runtime recovery"`


---

# Cluster E — Guards + NAV + daemon wiring

All commands run from `backend_py/`. Unit gate: `uv run pytest -m "not integration"`; types: `uv run mypy src/`; lint: `uv run ruff check .`. These tasks depend on Cluster A (events + `DecisionPayload` carry `symbol`; `PositionReconciled` fields renamed `reserved/realized/available`) and Cluster B (ledger per-symbol getters `current_exposure(symbol)` / `available_balance(symbol)`). Author this cluster LAST so those symbols exist.

---

### Task 17: `AllocationCapGuard.evaluate` reads `decision.symbol` and calls `ledger.current_exposure(decision.symbol)`

Per-symbol cap enforcement: the fUST bucket exposure must NOT block a fUSD POST and vice-versa. Cap VALUE stays `ctx.allocation_cap_usdt` (Phase 1 global value; per-symbol cap maps are Phase 2).

**Files:**
- `tests/modules/execution/safety/test_hard_guards.py` (modify: `_FakeLedger` at lines 136-141; add new tests after line 186; modify `_post()` at lines 34-39)
- `src/bfx_funding_bot/modules/execution/safety/hard_guards.py` (modify: `_LedgerProtocol` lines 107-108; `AllocationCapGuard.evaluate` lines 125-146)

- [ ] **Step 1: Write the failing tests.** First update the shared `_post()` helper to carry a symbol (Cluster A adds the required `symbol: str` field to `DecisionPayload`), then upgrade `_FakeLedger` to a per-symbol dict and add the discriminating tests.

  Replace `_post()` (lines 34-39) — add `symbol="fUST"`:
  ```python
  def _post(symbol: str = "fUST") -> DecisionPayload:
      return DecisionPayload(
          decision_outcome=DecisionOutcome.POST,
          signal_correlation_id=uuid4(),
          offer_rate=0.0001, offer_amount_usdt=100.0, offer_duration_days=2,
          symbol=symbol,
      )
  ```

  Replace `_FakeLedger` (lines 136-141) with a per-symbol dict fake:
  ```python
  class _FakeLedger:
      def __init__(self, exposures: dict[str, Decimal] | None = None) -> None:
          self._exposures = exposures or {}

      def current_exposure(self, symbol: str) -> Decimal:
          return self._exposures.get(symbol, Decimal("0"))
  ```

  The three existing AllocationCap tests (lines 144-172) construct `_FakeLedger(Decimal("100"))` etc. — update those call sites to the dict form and pass through `_post()`:
  ```python
  @pytest.mark.asyncio
  async def test_allocation_cap_allows_under_cap() -> None:
      ctx = AccountContext("default", Credentials("k", "s"), Decimal("500"))
      ledger = _FakeLedger({"fUST": Decimal("100")})
      g = AllocationCapGuard(ledger=ledger)
      decision = _post()  # offer_amount_usdt=100, symbol=fUST
      r = await g.evaluate(decision, ctx)
      assert r.allowed is True


  @pytest.mark.asyncio
  async def test_allocation_cap_blocks_over_cap() -> None:
      ctx = AccountContext("default", Credentials("k", "s"), Decimal("500"))
      ledger = _FakeLedger({"fUST": Decimal("450")})
      g = AllocationCapGuard(ledger=ledger)
      decision = _post()  # 100 → 450+100=550 > 500
      r = await g.evaluate(decision, ctx)
      assert r.allowed is False
      assert "cap" in (r.reason or "")


  @pytest.mark.asyncio
  async def test_allocation_cap_edge_at_exactly_cap() -> None:
      ctx = AccountContext("default", Credentials("k", "s"), Decimal("500"))
      ledger = _FakeLedger({"fUST": Decimal("400")})  # 400 + 100 = 500 (exactly)
      g = AllocationCapGuard(ledger=ledger)
      r = await g.evaluate(_post(), ctx)
      # Exactly at cap = allowed; strictly over blocks.
      assert r.allowed is True
  ```

  Update `test_allocation_cap_skip_decision_always_allowed` (lines 175-186) ledger construction:
  ```python
  @pytest.mark.asyncio
  async def test_allocation_cap_skip_decision_always_allowed() -> None:
      ctx = AccountContext("default", Credentials("k", "s"), Decimal("100"))
      ledger = _FakeLedger({"fUST": Decimal("99999")})
      g = AllocationCapGuard(ledger=ledger)
      skip = DecisionPayload(
          decision_outcome=DecisionOutcome.SKIP,
          signal_correlation_id=uuid4(),
          skip_reason="below_threshold",
          symbol="fUST",
      )
      r = await g.evaluate(skip, ctx)
      assert r.allowed is True  # SKIP decisions never consume cap
  ```

  Add the new per-symbol isolation test immediately after `test_allocation_cap_skip_decision_always_allowed`:
  ```python
  @pytest.mark.asyncio
  async def test_allocation_cap_isolates_buckets_per_symbol() -> None:
      # fUST bucket is full (over cap) but the empty fUSD bucket must allow a
      # fUSD POST: the guard reads ONLY decision.symbol's exposure, never a sum.
      ctx = AccountContext("default", Credentials("k", "s"), Decimal("500"))
      ledger = _FakeLedger({"fUST": Decimal("500")})  # fUSD absent → reads 0
      g = AllocationCapGuard(ledger=ledger)

      blocked = await g.evaluate(_post(symbol="fUST"), ctx)  # 500+100=600 > 500
      assert blocked.allowed is False

      allowed = await g.evaluate(_post(symbol="fUSD"), ctx)  # 0+100=100 <= 500
      assert allowed.allowed is True
  ```

- [ ] **Step 2: Run the tests — expect failure.**
  ```
  uv run pytest tests/modules/execution/safety/test_hard_guards.py -m "not integration" -q
  ```
  Expected: `TypeError: current_exposure() takes 1 positional argument but 2 were given` (production guard still calls `self.ledger.current_exposure()` with no symbol) on the AllocationCap tests, plus the new `test_allocation_cap_isolates_buckets_per_symbol` failing.

- [ ] **Step 3: Implement.** Update the protocol and the evaluate body to thread `decision.symbol`.

  Replace `_LedgerProtocol` (lines 107-108):
  ```python
  class _LedgerProtocol(Protocol):
      def current_exposure(self, symbol: str) -> Decimal: ...
  ```

  Replace the body of `AllocationCapGuard.evaluate` (lines 135-146 — the lines after the `offer_amount_usdt is None` guard) so exposure is read for the decision's symbol:
  ```python
        exposure = self.ledger.current_exposure(decision.symbol)
        offer = Decimal(str(decision.offer_amount_usdt))
        projected = exposure + offer
        if projected > ctx.allocation_cap_usdt:
            return GuardResult(
                allowed=False, guard_name=self.name,
                reason=(
                    f"symbol={decision.symbol} exposure={exposure}+offer={offer}"
                    f"={projected} > cap={ctx.allocation_cap_usdt}"
                ),
            )
        return GuardResult(allowed=True, guard_name=self.name)
  ```

- [ ] **Step 4: Run tests — expect pass.**
  ```
  uv run pytest tests/modules/execution/safety/test_hard_guards.py -m "not integration" -q
  uv run mypy src/bfx_funding_bot/modules/execution/safety/hard_guards.py
  uv run ruff check src/bfx_funding_bot/modules/execution/safety/hard_guards.py tests/modules/execution/safety/test_hard_guards.py
  ```
  Expected: all green; mypy clean; ruff clean.

- [ ] **Step 5: Commit.**
  ```
  git add src/bfx_funding_bot/modules/execution/safety/hard_guards.py tests/modules/execution/safety/test_hard_guards.py
  git commit -m "✨ Feat: AllocationCapGuard enforces cap per decision.symbol bucket"
  ```

---

### Task 18: `BuyingPowerGuard.evaluate` reads `decision.symbol` and calls `ledger.available_balance(decision.symbol)`

Per-symbol available-balance backstop. Buffer VALUE stays the existing global `BFX_BALANCE_BUFFER_USDT` (injected as `buffer_usdt`; Phase 1 only). Preserve the live-only construction exemption (no guard-internal change — the daemon already gates construction on `not spec.is_simulated`).

**Files:**
- `tests/modules/execution/safety/test_hard_guards.py` (modify: `_FakeBalanceLedger` lines 189-194; `_post_decision` lines 197-204; add new test after line 311)
- `src/bfx_funding_bot/modules/execution/safety/hard_guards.py` (modify: `_BalanceLedgerProtocol` lines 149-150; `BuyingPowerGuard.evaluate` lines 170-191)

- [ ] **Step 1: Write the failing tests.** Make `_FakeBalanceLedger` per-symbol and give `_post_decision` a symbol, then add an isolation test.

  Replace `_FakeBalanceLedger` (lines 189-194):
  ```python
  class _FakeBalanceLedger:
      def __init__(self, balances: dict[str, Decimal] | None = None) -> None:
          self._balances = balances or {}

      def available_balance(self, symbol: str) -> Decimal:
          return self._balances.get(symbol, Decimal("0"))
  ```

  Replace `_post_decision` (lines 197-204) — add `symbol`:
  ```python
  def _post_decision(amount: float | None, symbol: str = "fUST") -> DecisionPayload:
      return DecisionPayload(
          decision_outcome=DecisionOutcome.POST,
          signal_correlation_id=uuid4(),
          offer_rate=0.00012,
          offer_amount_usdt=amount,
          offer_duration_days=2,
          symbol=symbol,
      )
  ```

  Update the existing BuyingPower test call sites that construct `_FakeBalanceLedger(Decimal(...))` to the dict form keyed by `fUST` (lines 217, 226, 233, 246, 302, 309):
  ```python
  @pytest.mark.asyncio
  async def test_buying_power_blocks_over_available() -> None:
      guard = BuyingPowerGuard(ledger=_FakeBalanceLedger({"fUST": Decimal("150")}), buffer_usdt=Decimal("3"))
      # deployable = 150 - 3 = 147; offer 160 > 147 -> block
      res = await guard.evaluate(_post_decision(160.0), _bp_ctx())
      assert res.allowed is False
      assert res.guard_name == "buying_power"


  @pytest.mark.asyncio
  async def test_buying_power_allows_within_available() -> None:
      guard = BuyingPowerGuard(ledger=_FakeBalanceLedger({"fUST": Decimal("250")}), buffer_usdt=Decimal("3"))
      res = await guard.evaluate(_post_decision(200.0), _bp_ctx())
      assert res.allowed is True


  @pytest.mark.asyncio
  async def test_buying_power_skip_bypasses() -> None:
      guard = BuyingPowerGuard(ledger=_FakeBalanceLedger({"fUST": Decimal("0")}), buffer_usdt=Decimal("3"))
      decision = DecisionPayload(
          decision_outcome=DecisionOutcome.SKIP,
          signal_correlation_id=uuid4(),
          skip_reason="below_threshold",
          offer_rate=None, offer_amount_usdt=None, offer_duration_days=None,
          symbol="fUST",
      )
      res = await guard.evaluate(decision, _bp_ctx())
      assert res.allowed is True


  @pytest.mark.asyncio
  async def test_buying_power_missing_amount_blocks() -> None:
      guard = BuyingPowerGuard(ledger=_FakeBalanceLedger({"fUST": Decimal("250")}), buffer_usdt=Decimal("3"))
      # A POST DecisionPayload normally can't carry a None amount (model validator
      # rejects it); model_construct bypasses validation to exercise the guard's
      # defensive missing-amount branch directly.
      decision = DecisionPayload.model_construct(
          decision_outcome=DecisionOutcome.POST,
          signal_correlation_id=uuid4(),
          offer_rate=0.00012,
          offer_amount_usdt=None,
          offer_duration_days=2,
          symbol="fUST",
      )
      res = await guard.evaluate(decision, _bp_ctx())
      assert res.allowed is False
  ```

  Update `test_buying_power_exact_at_fractional_boundary_via_float_bridge` ledger construction (line 302):
  ```python
      guard = BuyingPowerGuard(
          ledger=_FakeBalanceLedger({"fUST": Decimal("409.89")}), buffer_usdt=Decimal("3"),
      )
  ```

  Add the new per-symbol isolation test at the end of the file (after line 311):
  ```python
  @pytest.mark.asyncio
  async def test_buying_power_isolates_buckets_per_symbol() -> None:
      # fUST funding wallet is empty but fUSD has funds: a fUST POST must block
      # while a fUSD POST of the same size is allowed. The guard reads ONLY
      # decision.symbol's available balance — never a cross-symbol sum.
      guard = BuyingPowerGuard(
          ledger=_FakeBalanceLedger({"fUSD": Decimal("250")}),  # fUST absent → 0
          buffer_usdt=Decimal("3"),
      )
      blocked = await guard.evaluate(_post_decision(100.0, symbol="fUST"), _bp_ctx())
      assert blocked.allowed is False  # 0 - 3 = -3; 100 > -3 → block

      allowed = await guard.evaluate(_post_decision(100.0, symbol="fUSD"), _bp_ctx())
      assert allowed.allowed is True   # 250 - 3 = 247; 100 <= 247 → allow
  ```

- [ ] **Step 2: Run the tests — expect failure.**
  ```
  uv run pytest tests/modules/execution/safety/test_hard_guards.py -m "not integration" -q
  ```
  Expected: `TypeError: available_balance() takes 1 positional argument but 2 were given` is NOT yet the failure (production still calls no-arg). Instead the new `_FakeBalanceLedger.available_balance(symbol)` signature now requires an arg the production guard does not pass → `TypeError: available_balance() missing 1 required positional argument: 'symbol'` on every BuyingPower test, plus `test_buying_power_isolates_buckets_per_symbol` failing.

- [ ] **Step 3: Implement.** Thread `decision.symbol` into the balance read.

  Replace `_BalanceLedgerProtocol` (lines 149-150):
  ```python
  class _BalanceLedgerProtocol(Protocol):
      def available_balance(self, symbol: str) -> Decimal: ...
  ```

  Replace the body of `BuyingPowerGuard.evaluate` (lines 180-191 — the lines after the `offer_amount_usdt is None` guard) so balance is read for the decision's symbol:
  ```python
        available = self.ledger.available_balance(decision.symbol)
        deployable = available - self.buffer_usdt
        offer = Decimal(str(decision.offer_amount_usdt))
        if offer > deployable:
            return GuardResult(
                allowed=False, guard_name=self.name,
                reason=(
                    f"symbol={decision.symbol} offer={offer} > "
                    f"available={available}−buffer={self.buffer_usdt}={deployable}"
                ),
            )
        return GuardResult(allowed=True, guard_name=self.name)
  ```

- [ ] **Step 4: Run tests — expect pass.**
  ```
  uv run pytest tests/modules/execution/safety/test_hard_guards.py -m "not integration" -q
  uv run mypy src/bfx_funding_bot/modules/execution/safety/hard_guards.py
  uv run ruff check src/bfx_funding_bot/modules/execution/safety/hard_guards.py tests/modules/execution/safety/test_hard_guards.py
  ```
  Expected: all green; mypy clean; ruff clean.

- [ ] **Step 5: Commit.**
  ```
  git add src/bfx_funding_bot/modules/execution/safety/hard_guards.py tests/modules/execution/safety/test_hard_guards.py
  git commit -m "✨ Feat: BuyingPowerGuard reads available balance per decision.symbol"
  ```

---

### Task 19: `ReconcileNavTracker` consumes per-symbol `PositionReconciled`, keeps per-symbol latest, exposes the global API as the SUM across symbols

`PositionReconciled` now carries `symbol` and renamed fields `reserved/realized/available` (Cluster A). NAV keeps the EXISTING global API (`realized_loss_24h()` / `drawdown_pct()`, no args) so `RealizedLossGuard`/`DrawdownGuard` call sites in `calibrated_guards.py` need NO change. The global NAV at each reconcile instant = SUM of the latest per-symbol NAV components across all symbols seen so far; a single active symbol ⇒ identical to today. Per-symbol thresholds are Phase 2.

**Files:**
- `tests/modules/execution/safety/test_nav_pnl_source.py` (modify: `_reconciled` helper lines 37-53; add two tests at end after line 158)
- `src/bfx_funding_bot/modules/execution/safety/nav_pnl_source.py` (modify: module docstring lines 1-19 lightly; `__init__` lines 47-51; `on_position_reconciled` lines 53-62; `realized_loss_24h`/`drawdown_pct` lines 66-77; imports line 38)

- [ ] **Step 1: Write the failing tests.** First update the `_reconciled` helper to use the renamed fields and a `symbol` kwarg (Cluster A rename), then add the two new tests (global API unchanged for one symbol; two-symbol sum).

  Replace `_reconciled` (lines 37-53):
  ```python
  def _reconciled(
      *,
      available: str = "0",
      reserved: str = "0",
      realized: str = "0",
      ts: int = _T0,
      account_id: str = _ACC,
      symbol: str = "fUST",
  ) -> PositionReconciled:
      return PositionReconciled(
          account_id=account_id,
          symbol=symbol,
          reserved=Decimal(reserved),
          realized=Decimal(realized),
          available=Decimal(available),
          n_offers=0,
          n_credits=0,
          occurred_at_ms=ts,
      )
  ```

  Every existing test calls `_reconciled(available="...", ts=...)` etc. — all default to `symbol="fUST"`, so they keep exercising the single-symbol path unchanged. Add two new tests at the end of the file (after line 158):
  ```python
  @pytest.mark.asyncio
  async def test_single_symbol_global_api_identical_to_pre_per_symbol() -> None:
      """One active symbol ⇒ global realized_loss_24h()/drawdown_pct() behave
      exactly as the pre-per-symbol tracker (the SUM is over the one bucket)."""
      t = ReconcileNavTracker(account_id=_ACC)
      await t.on_position_reconciled(
          _reconciled(available="50", reserved="30", realized="20", ts=_T0)
      )  # NAV(fUST) = 100
      await t.on_position_reconciled(
          _reconciled(
              available="40", reserved="30", realized="20", ts=_T0 + _HOUR_MS,
          )
      )  # NAV(fUST) = 90
      assert t.realized_loss_24h() == Decimal("10")
      assert t.drawdown_pct() == pytest.approx(10.0)


  @pytest.mark.asyncio
  async def test_two_symbols_global_nav_is_sum_of_latest_per_symbol() -> None:
      """Global NAV = Σ latest-per-symbol NAV. The peak forms at the moment both
      symbols are at their joint high; a later drop in one symbol's bucket shows
      as a global loss/drawdown — never via cross-symbol cancellation."""
      t = ReconcileNavTracker(account_id=_ACC)
      # t0: fUST NAV = 100  → global = 100
      await t.on_position_reconciled(_reconciled(available="100", ts=_T0, symbol="fUST"))
      # t1: fUSD NAV = 100  → global = 100 (fUST) + 100 (fUSD) = 200  (joint peak)
      await t.on_position_reconciled(
          _reconciled(available="100", ts=_T0 + _HOUR_MS, symbol="fUSD")
      )
      # t2: fUST drops to 70 → global = 70 (fUST) + 100 (fUSD) = 170
      await t.on_position_reconciled(
          _reconciled(available="70", ts=_T0 + 2 * _HOUR_MS, symbol="fUST")
      )
      # window high over the three global samples (100, 200, 170) = 200; latest 170
      assert t.realized_loss_24h() == Decimal("30")
      # all-time peak global NAV = 200 → drawdown = (200 − 170)/200 × 100 = 15%
      assert t.drawdown_pct() == pytest.approx(15.0)
  ```

- [ ] **Step 2: Run the tests — expect failure.**
  ```
  uv run pytest tests/modules/execution/safety/test_nav_pnl_source.py -m "not integration" -q
  ```
  Expected: existing tests fail with `TypeError: __init__() got an unexpected keyword argument 'symbol'` / `unexpected keyword argument 'reserved'` until production reads the renamed fields, and the production tracker still reads `event.available_usdt` etc. → `AttributeError: 'PositionReconciled' object has no attribute 'available_usdt'`. The new two-symbol test fails on the global-sum assertion.

- [ ] **Step 3: Implement.** Rewrite the tracker to maintain per-symbol latest NAV components and derive a global NAV sample (Σ over latest per-symbol NAV) at each reconcile, feeding the same 24h-window/peak machinery.

  Replace the import block (line 38) — drop `deque` if unused after rewrite; keep it (still used for the global window):
  ```python
  from collections import deque
  from decimal import Decimal
  ```
  (No change needed to line 38-39 if `deque` import already present; keep both lines.)

  Replace `__init__` (lines 47-51):
  ```python
      def __init__(self, account_id: str) -> None:
          self.account_id = account_id
          self._peak: Decimal | None = None
          # (occurred_at_ms, global_nav), oldest first, trimmed to the 24h window.
          # global_nav = Σ over the latest-known NAV of every symbol seen so far.
          self._samples: deque[tuple[int, Decimal]] = deque()
          # Latest NAV component per symbol; summed to form each global sample.
          # Single active symbol today (fUST) ⇒ one bucket ⇒ global == that bucket.
          self._nav_by_symbol: dict[str, Decimal] = {}
  ```

  Replace `on_position_reconciled` (lines 53-62):
  ```python
      async def on_position_reconciled(self, event: PositionReconciled) -> None:
          if event.account_id != self.account_id:
              return
          # Native units, per symbol — NEVER mix currencies inside a bucket.
          self._nav_by_symbol[event.symbol] = (
              event.available + event.reserved + event.realized
          )
          # Global NAV sample = Σ latest-per-symbol NAV. A single active symbol
          # makes this identical to the pre-per-symbol scalar NAV.
          nav = sum(self._nav_by_symbol.values(), Decimal("0"))
          self._samples.append((event.occurred_at_ms, nav))
          if self._peak is None or nav > self._peak:
              self._peak = nav
          cutoff = event.occurred_at_ms - _WINDOW_MS
          while self._samples and self._samples[0][0] < cutoff:
              self._samples.popleft()
  ```

  `realized_loss_24h` (lines 66-71) and `drawdown_pct` (lines 73-77) operate on `self._samples` only — they are UNCHANGED. Leave them exactly as-is.

  Lightly update the module docstring to note the global-NAV-is-Σ semantics. Replace the docstring line block (lines 7-13 — the "i.e. total funding-wallet capital..." paragraph) so it reads:
  ```python
  i.e. total funding-wallet capital for the account's currency — idle funds + open
  offers + lent principal. With multiple active symbols the global NAV sample is
  the SUM of the latest per-symbol NAV (native units kept separate per bucket; the
  sum is a pure equity total, never used as a per-symbol cap). A single active
  symbol (fUST today) makes the global metrics identical to the scalar tracker. A
  maturing credit returns principal to `available` so NAV is unchanged; interest
  paid raises `available` → NAV up; capital lost for ANY reason (a bug burning
  funds, a platform socialised loss, a withdrawal) → NAV down.
  ```

- [ ] **Step 4: Run tests — expect pass.**
  ```
  uv run pytest tests/modules/execution/safety/test_nav_pnl_source.py -m "not integration" -q
  uv run mypy src/bfx_funding_bot/modules/execution/safety/nav_pnl_source.py
  uv run ruff check src/bfx_funding_bot/modules/execution/safety/nav_pnl_source.py tests/modules/execution/safety/test_nav_pnl_source.py
  ```
  Expected: all green; mypy clean (`sum(..., Decimal("0"))` typed as `Decimal`); ruff clean.

- [ ] **Step 5: Commit.**
  ```
  git add src/bfx_funding_bot/modules/execution/safety/nav_pnl_source.py tests/modules/execution/safety/test_nav_pnl_source.py
  git commit -m "✨ Feat: ReconcileNavTracker sums per-symbol PositionReconciled into global NAV"
  ```

---

### Task 20: Verify daemon guard/NAV wiring still constructs correctly (no-edit gate)

The three constructor signatures Cluster E touches are UNCHANGED: `AllocationCapGuard(ledger=ledger)`, `BuyingPowerGuard(ledger=ledger, buffer_usdt=balance_buffer_usdt)`, `ReconcileNavTracker(account_id=account_id)`. The daemon at `daemon.py:711,768,776` already wires these and subscribes `pnl_source.on_position_reconciled` to `PositionReconciled` (line 888). The buffer stays the global `BFX_BALANCE_BUFFER_USDT` (line 732) and the BuyingPowerGuard live-only exemption (line 775 `if not spec.is_simulated`) is preserved. This task confirms the full suite + types + lint stay green end-to-end with no daemon edit.

**Files:**
- (verify only) `src/bfx_funding_bot/modules/marketfeed/daemon.py` (lines 711, 768-776, 886-888)

- [ ] **Step 1: Confirm no daemon edit is required.** Read `daemon.py` lines 707-789 and 885-889 and verify each construction matches the unchanged signatures above. There is nothing to edit — `AllocationCapGuard`/`BuyingPowerGuard`/`ReconcileNavTracker` constructors did not change; only their `.evaluate`/`.on_position_reconciled` internals did.

- [ ] **Step 2: Run the FULL unit gate + types + lint.**
  ```
  uv run pytest -m "not integration" -q
  uv run mypy src/
  uv run ruff check .
  ```
  Expected: full unit suite green (the per-symbol guard + NAV behaviour now active across the whole tree, including any daemon-level smoke tests); mypy clean over `src/`; ruff clean. If a daemon-construction smoke test or any other test references the old `current_exposure()`/`available_balance()` no-arg getters or the old `PositionReconciled` field names, that is a cross-cluster stitch issue to resolve with the controller, NOT a Cluster E source change.

- [ ] **Step 3: Commit (only if the suite produced any incidental no-op formatting or the prior tasks left an uncommitted state).** If the working tree is clean after Step 2, skip this commit. Otherwise:
  ```
  git add -A
  git commit -m "✅ Test: full unit gate green after per-symbol guards + NAV wiring"
  ```


---

# Phase 1 wrap: integration verification + deferred follow-ups

### Task FINAL: End-to-end per-symbol isolation + full Phase-1 green checkpoint

**Files:**
- Test: `tests/modules/execution/test_per_symbol_isolation.py` (create)

- [ ] **Step 1: Write the end-to-end isolation test.** Create `tests/modules/execution/test_per_symbol_isolation.py`:

```python
from decimal import Decimal
from uuid import uuid4

from bfx_funding_bot.modules.execution.events import OrderFilled, PositionReconciled
from bfx_funding_bot.modules.execution.ledger import PaperPositionLedger


async def test_fill_and_reconcile_are_isolated_per_symbol() -> None:
    led = PaperPositionLedger(account_id="default")
    # fUST realized via reconcile absolute-set; fUSD untouched
    await led.on_position_reconciled(
        PositionReconciled(
            account_id="default", symbol="fUST",
            reserved=Decimal("0"), realized=Decimal("300"), available=Decimal("250"),
            n_offers=0, n_credits=3, occurred_at_ms=1,
        )
    )
    await led.on_order_filled(
        OrderFilled(
            cid=1, venue_offer_id="v1", credit_id="C1",
            amount=Decimal("50"), symbol="fUST", fill_rate=0.0005,
            signal_correlation_id=uuid4(), account_id="default", is_simulated=False,
        )
    )
    assert led.current_exposure("fUST") == Decimal("350")  # 300 realized + 50 filled
    assert led.current_exposure("fUSD") == Decimal("0")    # isolated
    assert led.available_balance("fUST") == Decimal("250")
    assert led.available_balance("fUSD") == Decimal("0")
    # back-compat: no-arg getters sum across symbols (still fUST-only here)
    assert led.current_exposure() == Decimal("350")
```

- [ ] **Step 2: Run it — expect PASS** (all prior tasks already implement this behaviour).
  Run: `cd backend_py && uv run pytest tests/modules/execution/test_per_symbol_isolation.py -q`
  Expected: PASS. If it fails, a prior task regressed per-symbol isolation — fix before proceeding.

- [ ] **Step 3: Full Phase-1 gate.**
  Run: `cd backend_py && uv run pytest -m "not integration" -q && uv run mypy src/ && uv run ruff check`
  Expected: all green, mypy `Success`, ruff `All checks passed!`.

- [ ] **Step 4: Migration check (requires fresh Neon credential).**
  Refresh `.env` Neon password (Neon MCP `get_connection_string`) if stale, then:
  Run: `cd backend_py && uv run alembic upgrade head && uv run alembic check`
  Expected: upgrade applies `b7c1d2e3f4a5`; `alembic check` reports no drift.

- [ ] **Step 5: Commit.**
```bash
git add backend_py/tests/modules/execution/test_per_symbol_isolation.py
git commit -m "✅ Test: end-to-end per-symbol ledger isolation (per-currency Phase 1 checkpoint)"
```

## Deferred follow-ups (NOT in Phase 1)

- **Alias removal cleanup** (tidiness, zero functional change): drop the transitional `size_usdt`/`*_usdt` kwargs+properties (O1), make `amount`/`reserved`/`realized`/`available` required (reorder fields), make the ledger getter `symbol` required and drop the `None`-sum branch (O2), make `DecisionPayload.symbol` required, and switch the two `store.py` reads to `.amount` (O5). Best done once all producers pass `symbol=`/`amount=` — sweep `external/bitfinex/ws_dispatcher.py`, `fill_tracker.py`, `modules/execution/middleware/reservation_emitting.py`, `boot_recovery.py`, `store.py`, `smoke_runner.py` and their tests.
- **Phase 2** (separate spec/plan): native per-currency config maps (`caps`/`buffers`/loss-drawdown thresholds) + `default_cap`/`default_buffer` + back-compat env fallbacks; enable fUSD/fADA at `cap = 0`; per-symbol loss/drawdown thresholds. Source `configured_symbols` from the caps-map keys (O3).
- **Phase 3+ (per spec §7, deferred):** unified USD-equivalent exposure overlay / cross-currency ceiling (needs a price feed); per-symbol FSM if `offer_claims` gains a symbol column.
