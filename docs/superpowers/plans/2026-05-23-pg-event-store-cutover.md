# PG Event Store Cutover (Plan 2) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Cut the daemon's cold-start + write path over from Axiom to the Plan-1 Postgres event store: boot loads projections via `from_snapshot` (no Axiom replay query), and runtime events persist to the PG event store. This removes the blocking "Axiom replay fails → daemon exit 1" coupling that is currently crash-looping the shadow deploy (Axiom APL 400).

**Architecture:** Add a `PostgresEventSink` bus subscriber that persists each domain event to `PostgresEventStore` (event_log + snapshot, one txn per event). In `build_daemon`, construct the store + sink + subscribe them, replace `PaperPositionLedger.replay_from_axiom` / `OfferRegistry.replay_from_axiom` with the Plan-1 `from_snapshot` loaders, and drop the `AxiomReplayQueryAdapter`. **`AxiomEventSink` (emit/diagnostics) is intentionally KEPT** — Plan 3 removes Axiom entirely. Paper/shadow mode places no real venue offers, so venue-reconcile/PENDING adoption is **out of scope** (Plan 3, A2 / REAL mode).

**Tech Stack:** Python 3.13, SQLAlchemy 2.0 async, pytest (`asyncio_mode=auto`), `uv`, `mypy --strict`. Commands from `backend_py/`. Commit gitmoji.

**Scope guardrails:**
- Do NOT remove `AxiomConfig`/`AxiomClient`/`AxiomEventSink`/`EventResource` or the `BFX_DEPLOYMENT_ENV` requirement (Plan 3).
- Do NOT add `ReservationIntent`/PENDING/A2 write-ahead intent or venue-reconcile adoption (Plan 3).
- Do NOT touch the migration or tables (Plan 1, done).
- Keep `replay_from_axiom` methods defined (unused after cutover) — deletion is Plan 3.

---

## Reference (verbatim current state — do not re-explore)

`build_daemon` in `modules/marketfeed/daemon.py`:
- `db_engine = make_async_engine_from_url(config.database_url)` / `session_factory = async_sessionmaker(db_engine, expire_on_commit=False)` — **daemon.py:605-606**
- `axiom_cfg = AxiomConfig.from_env()` (reads BFX_DEPLOYMENT_ENV; `axiom_cfg.deployment_env` is a `DeploymentEnvironment` StrEnum) — **daemon.py:613**
- Replay block to REPLACE — **daemon.py:693-716**:
  ```python
  ledger_window_days = _resolve_event_replay_days()
  axiom_query = AxiomReplayQueryAdapter(api_key=axiom_cfg.api_key, dataset=axiom_cfg.dataset, deployment_environment=axiom_cfg.deployment_env)
  ledger = await PaperPositionLedger.replay_from_axiom(account_id=account_id, since=datetime.now(UTC) - timedelta(days=ledger_window_days), axiom_query=axiom_query)
  offer_registry = OfferRegistry(axiom_query=axiom_query, clock=lambda: int(time.time() * 1000))
  await offer_registry.replay_from_axiom()
  offer_registry.cleanup_terminal(older_than_ms=24 * 3600 * 1000)
  ```
- Bus + subscriptions — **daemon.py:785-832** (`bus = DomainEventBus()`; ledger/axiom_sink/offer_registry/cancel subscriptions).
- Session pattern at runtime: `async with session_factory() as s: ...` (daemon.py:633, 660, 909).

Plan-1 API (already on branch):
- `PostgresEventStore(*, deployment_environment: str)`; `async append(session, event) -> bool` (caller commits).
- `PaperPositionLedger.from_snapshot(session, *, account_id: str, deployment_environment: str) -> PaperPositionLedger`
- `OfferRegistry.from_snapshot(session, *, account_id: str, deployment_environment: str, clock=None) -> OfferRegistry`

Domain events (`modules/execution/events.py`): `ReservationClaimed`, `OrderFilled`, `ReservationReleased` (all `account_id`, `occurred_at_ms`, etc.).

`core/db.py`: `session_scope(factory)` async context manager commits on success / rolls back on error.

---

## File Structure

**Create:**
- `backend_py/src/bfx_funding_bot/modules/execution/event_store/sink.py` — `PostgresEventSink`.
- `backend_py/tests/modules/execution/event_store/test_sink.py` — unit (sqlite).
- `backend_py/tests/integration/test_daemon_pg_cutover.py` — integration (testcontainer PG).

**Modify:**
- `backend_py/src/bfx_funding_bot/modules/marketfeed/daemon.py` — boot cutover.
- `backend_py/tests/modules/marketfeed/test_daemon.py` — adapt boot smoke test (PG tables + drop replay mock).

---

### Task 1: `PostgresEventSink` — persist domain events to the PG store

**Files:**
- Create: `backend_py/src/bfx_funding_bot/modules/execution/event_store/sink.py`
- Test: `backend_py/tests/modules/execution/event_store/test_sink.py`

A bus subscriber mirroring `AxiomEventSink`'s handler shape, but persisting to Postgres. Each handler opens its own session (the bus passes no session) and commits via `session_scope` (one txn per event = event_log row + snapshot update, exactly Plan 1's `append`).

- [ ] **Step 1: Write the failing test**

```python
from decimal import Decimal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import bfx_funding_bot.modules.execution.event_store.tables  # noqa: F401
from bfx_funding_bot.core.db import Base, make_async_engine_from_url
from bfx_funding_bot.modules.execution.event_store.sink import PostgresEventSink
from bfx_funding_bot.modules.execution.event_store.store import PostgresEventStore
from bfx_funding_bot.modules.execution.event_store.tables import EventLogRow, PositionStateRow
from bfx_funding_bot.modules.execution.events import OrderFilled, ReservationClaimed

_SCID = UUID("11111111-1111-1111-1111-111111111111")


async def _factory() -> async_sessionmaker[AsyncSession]:
    engine = make_async_engine_from_url("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return async_sessionmaker(engine, expire_on_commit=False)


async def test_sink_persists_claim_and_fill() -> None:
    factory = await _factory()
    store = PostgresEventStore(deployment_environment="ci")
    sink = PostgresEventSink(store=store, session_factory=factory)

    await sink.on_reservation_claimed(ReservationClaimed(
        cid=1, venue_offer_id="v1", size_usdt=Decimal("10"), signal_correlation_id=_SCID,
        account_id="acct", is_simulated=True, venue_seq=1, occurred_at_ms=1000))
    await sink.on_order_filled(OrderFilled(
        cid=1, venue_offer_id="v1", credit_id="c1", size_usdt=Decimal("4"), fill_rate=0.0,
        signal_correlation_id=_SCID, account_id="acct", is_simulated=True,
        venue_seq=2, occurred_at_ms=2000))

    async with factory() as s:
        rows = (await s.execute(select(EventLogRow))).scalars().all()
        assert len(rows) == 2
        ps = (await s.execute(select(PositionStateRow).where(
            PositionStateRow.account_id == "acct"))).scalar_one()
        assert ps.reserved_usdt == Decimal("6")
        assert ps.realized_usdt == Decimal("4")
```

- [ ] **Step 2: Run, expect FAIL** (`ModuleNotFoundError: ...sink`)

Run: `cd backend_py && uv run pytest tests/modules/execution/event_store/test_sink.py -q`

- [ ] **Step 3: Implement** `backend_py/src/bfx_funding_bot/modules/execution/event_store/sink.py`:

```python
from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bfx_funding_bot.core.db import session_scope
from bfx_funding_bot.modules.execution.event_store.store import PostgresEventStore
from bfx_funding_bot.modules.execution.events import (
    OrderFilled,
    ReservationClaimed,
    ReservationReleased,
)


class PostgresEventSink:
    """Bus subscriber: persists SoT domain events to the Postgres event store.

    One txn per event (event_log row + snapshot update, via PostgresEventStore.append).
    Mirrors AxiomEventSink's handler shape so it slots into the same bus subscriptions.
    """

    def __init__(
        self, *, store: PostgresEventStore, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        self._store = store
        self._session_factory = session_factory

    async def on_reservation_claimed(self, event: ReservationClaimed) -> None:
        await self._persist(event)

    async def on_order_filled(self, event: OrderFilled) -> None:
        await self._persist(event)

    async def on_reservation_released(self, event: ReservationReleased) -> None:
        await self._persist(event)

    async def _persist(self, event: object) -> None:
        async with session_scope(self._session_factory) as session:
            await self._store.append(session, event)
```

- [ ] **Step 4: Run, expect PASS**

Run: `cd backend_py && uv run pytest tests/modules/execution/event_store/test_sink.py -q`
Expected: PASS (1 test).

- [ ] **Step 5: Typecheck + lint + commit**

```bash
cd backend_py && uv run mypy --strict src && uv run ruff check
git add backend_py/src/bfx_funding_bot/modules/execution/event_store/sink.py backend_py/tests/modules/execution/event_store/test_sink.py
git commit -m "✨ Feat: PostgresEventSink — bus subscriber persists SoT events to PG store"
```

---

### Task 2: Cut `build_daemon` boot over to Postgres

**Files:**
- Modify: `backend_py/src/bfx_funding_bot/modules/marketfeed/daemon.py`
- Modify: `backend_py/tests/modules/marketfeed/test_daemon.py`

- [ ] **Step 1: Edit the boot replay block** (daemon.py:693-716). Replace the entire block shown in Reference with:

```python
        env_str = axiom_cfg.deployment_env.value
        event_store = PostgresEventStore(deployment_environment=env_str)
        async with session_factory() as snap_session:
            ledger = await PaperPositionLedger.from_snapshot(
                snap_session, account_id=account_id, deployment_environment=env_str
            )
            offer_registry = await OfferRegistry.from_snapshot(
                snap_session,
                account_id=account_id,
                deployment_environment=env_str,
                clock=lambda: int(time.time() * 1000),
            )
```

Add the imports at the top of daemon.py if missing:
```python
from bfx_funding_bot.modules.execution.event_store.sink import PostgresEventSink
from bfx_funding_bot.modules.execution.event_store.store import PostgresEventStore
```
Remove the now-unused `AxiomReplayQueryAdapter` import and the `_resolve_event_replay_days()` call (leave the function defined if it's referenced elsewhere; if its only use was here, also remove the `datetime`/`timedelta` import only if nothing else uses them — verify with grep first).

- [ ] **Step 2: Add the PG sink + subscribe it** — in the bus subscription block (daemon.py:812-832), after `axiom_sink` is built, add:

```python
        pg_sink = PostgresEventSink(store=event_store, session_factory=session_factory)
        bus.subscribe(ReservationClaimed,  pg_sink.on_reservation_claimed)
        bus.subscribe(OrderFilled,         pg_sink.on_order_filled)
        bus.subscribe(ReservationReleased, pg_sink.on_reservation_released)
```
(Keep all existing `axiom_sink` + `ledger` + `offer_registry` subscriptions — PG sink is added alongside.)

- [ ] **Step 3: Update the daemon boot smoke test** `tests/modules/marketfeed/test_daemon.py`:
  - The daemon now reads `position_state`/`offer_claims` via `from_snapshot` at boot, so those tables must exist in the test DB. `:memory:` sqlite is per-connection, so switch to a **file-based** sqlite shared across connections, and create the event-store tables before `build_daemon`.
  - Replace `monkeypatch.setenv("DATABASE_URL", "sqlite+aiosqlite:///:memory:")` with:
    ```python
    db_path = tmp_path / "daemon.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{db_path}")
    # create event-store tables the daemon's from_snapshot boot reads
    import bfx_funding_bot.modules.execution.event_store.tables  # noqa: F401
    from bfx_funding_bot.core.db import Base, make_async_engine_from_url
    _eng = make_async_engine_from_url(f"sqlite+aiosqlite:///{db_path}")
    async with _eng.begin() as _c:
        await _c.run_sync(Base.metadata.create_all)
    await _eng.dispose()
    ```
  - **Remove** the `_apl?format=tabular` httpx mock (no Axiom replay query at boot anymore). Keep the `/ingest` mock (axiom_sink still emits).
  - The assertion `daemon = await build_daemon(...)` should now succeed reading empty snapshot (ledger exposure 0, empty registry).

- [ ] **Step 4: Run boot test + full unit gate**

Run:
```bash
cd backend_py && uv run pytest tests/modules/marketfeed/test_daemon.py -q
cd backend_py && uv run pytest -m "not integration" -q
```
Expected: boot test PASS; full unit gate green.

- [ ] **Step 5: Typecheck + lint + commit**

```bash
cd backend_py && uv run mypy --strict src && uv run ruff check
git add backend_py/src/bfx_funding_bot/modules/marketfeed/daemon.py backend_py/tests/modules/marketfeed/test_daemon.py
git commit -m "✨ Feat: cut build_daemon boot to PG from_snapshot + PostgresEventSink (drop Axiom replay)"
```

---

### Task 3: Integration test — PG-backed boot round-trip (no Axiom replay)

**Files:**
- Create: `backend_py/tests/integration/test_daemon_pg_cutover.py`

Prove the cutover end-to-end on real PG: events persisted via the sink are reflected by a fresh `from_snapshot` (the cold-start contract the daemon now relies on), with NO Axiom involvement.

- [ ] **Step 1: Write the failing integration test**

```python
from decimal import Decimal
from uuid import UUID

import pytest

from bfx_funding_bot.modules.execution.event_store.sink import PostgresEventSink
from bfx_funding_bot.modules.execution.event_store.store import PostgresEventStore
from bfx_funding_bot.modules.execution.ledger import PaperPositionLedger
from bfx_funding_bot.modules.execution.registry_offers import OfferRegistry, RegistryState
from bfx_funding_bot.modules.execution.events import OrderFilled, ReservationClaimed

pytestmark = pytest.mark.integration
_SCID = UUID("11111111-1111-1111-1111-111111111111")


async def test_sink_then_from_snapshot_roundtrip(pg_session_factory) -> None:
    store = PostgresEventStore(deployment_environment="ci")
    sink = PostgresEventSink(store=store, session_factory=pg_session_factory)

    # runtime: events flow through the sink (as the bus would)
    await sink.on_reservation_claimed(ReservationClaimed(
        cid=7, venue_offer_id="v7", size_usdt=Decimal("12"), signal_correlation_id=_SCID,
        account_id="acctZ", is_simulated=True, venue_seq=1, occurred_at_ms=1000))
    await sink.on_order_filled(OrderFilled(
        cid=7, venue_offer_id="v7", credit_id="c7", size_usdt=Decimal("5"), fill_rate=0.0,
        signal_correlation_id=_SCID, account_id="acctZ", is_simulated=True,
        venue_seq=2, occurred_at_ms=2000))

    # cold-start: a fresh boot reads the snapshot (the daemon's new boot path)
    async with pg_session_factory() as s:
        ledger = await PaperPositionLedger.from_snapshot(
            s, account_id="acctZ", deployment_environment="ci")
        reg = await OfferRegistry.from_snapshot(
            s, account_id="acctZ", deployment_environment="ci")

    assert ledger.current_exposure() == Decimal("12")   # reserved 7 + realized 5
    assert ledger.realized_exposure() == Decimal("5")
    assert reg.snapshot()["v7"].state is RegistryState.CLAIMED
```

- [ ] **Step 2: Run, expect FAIL then PASS**

Run: `cd backend_py && uv run pytest tests/integration/test_daemon_pg_cutover.py -q -m integration` (needs Docker).
The test should pass immediately if Tasks 1-2 are correct (no new production code needed) — it's a regression guard for the cutover contract. If it fails, the sink/from_snapshot wiring has a bug; fix in Task 1/2 code.

- [ ] **Step 3: FINAL gate + commit**

```bash
cd backend_py && uv run pytest -m "not integration" -q
cd backend_py && uv run pytest -m integration -q --maxfail=3
cd backend_py && uv run mypy --strict src && uv run ruff check
git add backend_py/tests/integration/test_daemon_pg_cutover.py
git commit -m "✅ Test: PG cutover boot round-trip (sink → from_snapshot, no Axiom)"
```

---

## Self-Review

**Spec coverage:** boot via `from_snapshot` (T2) replaces Axiom replay → removes the blocking coupling (the deploy 400 fix); runtime persistence via `PostgresEventSink` (T1) keeps the snapshot current; integration round-trip (T3) guards the cold-start contract. Axiom emit (AxiomEventSink) kept per scope. Venue-reconcile/A2 correctly deferred to Plan 3.

**Placeholder scan:** none — all steps have full code.

**Type consistency:** `PostgresEventSink(store=, session_factory=)`, handlers `on_reservation_claimed/on_order_filled/on_reservation_released`, `store.append(session, event)`, `from_snapshot(session, *, account_id, deployment_environment)` — consistent with Plan 1 + the daemon wiring.

**Known risk:** the daemon boot now hard-depends on the event-store tables existing in the DB. In prod they exist (migration applied to Neon via the deploy). In tests, T2 creates them (file sqlite). If a fresh env ever lacks them, boot fails fast (acceptable — same class as the old "SoT unavailable" invariant, but now on our own infra).

---

## After this plan

Redeploy (push triggers Koyeb → `alembic upgrade head` no-op since migration applied → daemon boots via `from_snapshot`, no Axiom replay → no 400 → HEALTHY). Then **Plan 3** removes Axiom entirely (client/sink/env + AxiomReplayQueryAdapter + replay_from_axiom methods) + adds A2 write-ahead intent + PG diagnostics table + rewrites T12 test, and addresses the deferred venue-reconcile for REAL mode.
