# Credit-Aware Reconcile v2 (Best-Practice Refactor) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Refactor the v1 credit-aware reconcile (commit `d9cb091`) into the industry best-practice shape: single-writer exposure, append-only reconcile checkpoints with delta-tail rebuild, and a credit-dimension divergence signal.

**Architecture:** Three corrections to v1.
(1) **Single-writer exposure** — at reconcile time `PositionReconciled` (absolute set) is the SOLE authority for the in-memory ledger's `reserved`/`realized`; the orphan/missing recovery actions are routed directly to the `OfferRegistry` (FSM/audit) and are NO LONGER published to the bus the ledger listens on, eliminating the dual-writer double-count.
(2) **Checkpoint + delta tail** — each reconcile appends an immutable `reconcile_observation` row (absolute venue snapshot + an `event_seq_fence`); `position_state` stays a live materialized projection, and `rebuild_snapshot_from_log` rebuilds exposure as `latest checkpoint ⊕ domain events with event_seq > fence` instead of folding from genesis.
(3) **Credit-dimension drift signal** — `PeriodicReconcile._tick` flags `RECONCILE DEGRADED` on realized/reserved drift between the venue snapshot and the prior materialized belief, so a silently-broken WS credit path surfaces instead of self-healing invisibly.

**Tech Stack:** Python 3.13, SQLAlchemy 2.0 async, Alembic, pytest (`-m "not integration"` for unit), mypy, ruff. All commands run from `backend_py/`.

---

## Background: what v1 already shipped (commit `d9cb091`)

- `events.py`: `PositionReconciled` dataclass (in-process bus signal, not persisted to event_log).
- `ledger.py`: `on_position_reconciled` — absolute SET of `_reserved`/`_realized`.
- `auth_rest.py`: `ActiveFundingCredit`, `parse_active_funding_credits`, `get_active_funding_credits`.
- `boot_recovery.py`: `_fetch_credits`, `_AuthRestQuery` combined Protocol, `ReconcileResult` extended with `reserved_usdt`/`realized_usdt`/`n_credits` (with defaults); `run()` fetches offers+credits, calls `store.set_position_snapshot`, publishes `PositionReconciled` then recovery actions.
- `store.py`: `set_position_snapshot` — in-place overwrite of `position_state`.
- `tables.py` + migration `93c9ff214a61`: `position_state` gained `last_reconciled_at` + `n_credits`.

**v1's latent bug (the reason for this plan):** `PositionReconciled` is not yet subscribed by the ledger. The planned wiring (`bus.subscribe(PositionReconciled, ledger.on_position_reconciled)`) would activate a double-count: for an orphan offer, the bus delivers BOTH `PositionReconciled` (sets `reserved=Σoffers`, includes the orphan) AND `ReservationClaimed` (`reserved += orphan`). Confirmed against `daemon.py:855-861` (ledger + offer_registry both subscribe to `ReservationClaimed`/`OrderFilled`/`ReservationReleased`). `venue_seq` cannot discriminate: WS `foc` events carry it (`ws_dispatcher.py:101,130`) but the submit-path `ReservationClaimed` does not, so it collides with reconcile-sourced events. Hence routing separation (Task 5), not a discriminator.

## File Structure

| File | Responsibility | Tasks |
|---|---|---|
| `src/.../event_store/tables.py` | `ReconcileObservationRow` ORM (new) | 1 |
| `alembic/versions/<new>.py` | create `reconcile_observation` table | 1 |
| `src/.../event_store/store.py` | `set_position_snapshot` v2 (fence + observation + drift), `rebuild_snapshot_from_log` v2 (checkpoint+tail) | 2, 3 |
| `src/.../boot_recovery.py` | `ReconcileResult` drift fields; `run()` routes recovery actions to registry, threads drift | 4, 5 |
| `src/.../marketfeed/daemon.py` | subscribe `PositionReconciled`→ledger; pass `offer_registry` to recoveries | 6 |
| `src/.../periodic_reconcile.py` | drift-based `RECONCILE DEGRADED` | 7 |
| `tests/integration/test_credit_reconcile_pg.py` | end-to-end incident reproduction (real ledger + bus) | 8 |

---

### Task 1: `reconcile_observation` append-only table

**Files:**
- Modify: `src/bfx_funding_bot/modules/execution/event_store/tables.py` (append new class after `PositionStateRow`)
- Create: `alembic/versions/<autogen>.py`
- Test: `tests/modules/execution/event_store/test_reconcile_observation_table.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/modules/execution/event_store/test_reconcile_observation_table.py
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from bfx_funding_bot.core.db import Base
from bfx_funding_bot.modules.execution.event_store.tables import ReconcileObservationRow


@pytest.mark.asyncio
async def test_reconcile_observation_is_append_only_insertable():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sm = async_sessionmaker(engine, expire_on_commit=False)
    async with sm() as session:  # type: AsyncSession
        session.add(ReconcileObservationRow(
            account_id="default", deployment_environment="ci",
            reserved_usdt=Decimal("100"), realized_usdt=Decimal("450"),
            n_offers=1, n_credits=3, observed_at_ms=1_000, event_seq_fence=42,
        ))
        await session.commit()
        rows = (await session.execute(
            select(ReconcileObservationRow).where(
                ReconcileObservationRow.account_id == "default")
        )).scalars().all()
    assert len(rows) == 1
    assert rows[0].realized_usdt == Decimal("450")
    assert rows[0].event_seq_fence == 42
    await engine.dispose()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend_py && uv run pytest tests/modules/execution/event_store/test_reconcile_observation_table.py -v`
Expected: FAIL — `ImportError: cannot import name 'ReconcileObservationRow'`

- [ ] **Step 3: Add the ORM model**

In `tables.py`, after the `PositionStateRow` class, add (note `Integer`, `BigInteger`, `Numeric`, `Text`, `DateTime`, `_BIG_PK`, `_NOW` are already imported):

```python
class ReconcileObservationRow(Base):
    """Append-only checkpoint: one row per reconcile tick. Immutable audit of
    venue truth at observation time + the event_log fence it was taken at.
    position_state = latest ReconcileObservationRow ⊕ domain events with
    event_seq > event_seq_fence (see store.rebuild_snapshot_from_log)."""

    __tablename__ = "reconcile_observation"

    id: Mapped[int] = mapped_column(_BIG_PK, primary_key=True, autoincrement=True)
    account_id: Mapped[str] = mapped_column(Text, nullable=False)
    deployment_environment: Mapped[str] = mapped_column(Text, nullable=False)
    reserved_usdt: Mapped[Decimal] = mapped_column(Numeric, nullable=False)
    realized_usdt: Mapped[Decimal] = mapped_column(Numeric, nullable=False)
    n_offers: Mapped[int] = mapped_column(Integer, nullable=False)
    n_credits: Mapped[int] = mapped_column(Integer, nullable=False)
    observed_at_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    event_seq_fence: Mapped[int] = mapped_column(BigInteger, nullable=False)
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=_NOW
    )

    __table_args__ = (
        Index("idx_reconcile_obs_acct_env_id",
              "account_id", "deployment_environment", "id"),
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend_py && uv run pytest tests/modules/execution/event_store/test_reconcile_observation_table.py -v`
Expected: PASS

- [ ] **Step 5: Generate the migration**

Run: `cd backend_py && uv run alembic revision --autogenerate -m "add reconcile_observation checkpoint table"`
Expected: output `Detected added table 'reconcile_observation'`. Open the generated file; confirm `op.create_table("reconcile_observation", ...)` with all columns and the index, `down_revision = "93c9ff214a61"`, and `downgrade()` drops the table.

- [ ] **Step 6: Verify no schema drift**

Run: `cd backend_py && uv run alembic check`
Expected: `No new upgrade operations detected.`

- [ ] **Step 7: Commit**

```bash
git add backend_py/src/bfx_funding_bot/modules/execution/event_store/tables.py \
        backend_py/tests/modules/execution/event_store/test_reconcile_observation_table.py \
        backend_py/alembic/versions/*reconcile_observation*.py
git commit -m "✨ Feat: reconcile_observation append-only checkpoint table"
```

---

### Task 2: `set_position_snapshot` v2 — record checkpoint, capture fence, return drift

**Files:**
- Modify: `src/bfx_funding_bot/modules/execution/event_store/store.py` (replace `set_position_snapshot`)
- Test: `tests/modules/execution/event_store/test_set_position_snapshot.py` (new)

**Design:** `set_position_snapshot` now (a) computes the fence = current `event_log` head for (account, env), (b) reads the existing `position_state` to compute drift vs the new venue values, (c) overwrites `position_state` (live materialized view) incl. `last_event_seq = fence`, (d) appends an immutable `ReconcileObservationRow`, (e) returns a `SnapshotDrift`.

- [ ] **Step 1: Write the failing test**

```python
# tests/modules/execution/event_store/test_set_position_snapshot.py
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from bfx_funding_bot.core.db import Base
from bfx_funding_bot.modules.execution.event_store.store import PostgresEventStore
from bfx_funding_bot.modules.execution.event_store.tables import (
    EventLogRow,
    PositionStateRow,
    ReconcileObservationRow,
)


async def _session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return engine, async_sessionmaker(engine, expire_on_commit=False)


@pytest.mark.asyncio
async def test_set_position_snapshot_writes_state_observation_and_returns_drift():
    engine, sm = await _session()
    store = PostgresEventStore(deployment_environment="ci")
    async with sm() as session:
        # seed prior belief: realized=300 (the drifted-low ledger)
        session.add(PositionStateRow(
            account_id="a", deployment_environment="ci",
            reserved_usdt=Decimal("0"), realized_usdt=Decimal("300"),
            last_updated_ms=1, last_event_seq=5))
        # an event_log head at seq=5
        session.add(EventLogRow(
            account_id="a", deployment_environment="ci", event_type="ORDER_FILL",
            cid=1, venue_offer_id="v", venue_seq=1, payload={}, occurred_at_ms=1))
        await session.commit()

        drift = await store.set_position_snapshot(
            session, account_id="a",
            reserved_usdt=Decimal("0"), realized_usdt=Decimal("450"),
            n_offers=0, n_credits=3, occurred_at_ms=2_000)
        await session.commit()

        ps = (await session.execute(select(PositionStateRow).where(
            PositionStateRow.account_id == "a"))).scalar_one()
        obs = (await session.execute(select(ReconcileObservationRow).where(
            ReconcileObservationRow.account_id == "a"))).scalars().all()

    assert ps.realized_usdt == Decimal("450")        # live view overwritten
    assert ps.n_credits == 3
    assert ps.last_event_seq == 1                     # fence = current head (the seeded row's seq)
    assert len(obs) == 1                              # append-only checkpoint written
    assert obs[0].realized_usdt == Decimal("450")
    assert obs[0].event_seq_fence == ps.last_event_seq
    assert drift.realized_drift == Decimal("150")     # |450 - 300|
    assert drift.reserved_drift == Decimal("0")
    await engine.dispose()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend_py && uv run pytest tests/modules/execution/event_store/test_set_position_snapshot.py -v`
Expected: FAIL — `set_position_snapshot` returns `None` (no `.realized_drift`) / `ReconcileObservationRow` not written / `last_event_seq` wrong.

- [ ] **Step 3: Replace `set_position_snapshot`**

In `store.py`, add the import and a small result dataclass near the top (after existing imports):

```python
from dataclasses import dataclass

from sqlalchemy import delete, func, select  # add func to the existing import line
```

```python
@dataclass(frozen=True, slots=True)
class SnapshotDrift:
    reserved_drift: Decimal
    realized_drift: Decimal
```

Also import the new table at the top of `store.py` (extend the existing `from ...tables import (...)`):

```python
from bfx_funding_bot.modules.execution.event_store.tables import (
    EventLogRow,
    OfferClaimRow,
    PositionStateRow,
    ReconcileObservationRow,
)
```

Replace the whole `set_position_snapshot` method body with:

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

        session.add(ReconcileObservationRow(
            account_id=account_id,
            deployment_environment=self._env,
            reserved_usdt=reserved_usdt,
            realized_usdt=realized_usdt,
            n_offers=n_offers,
            n_credits=n_credits,
            observed_at_ms=occurred_at_ms,
            event_seq_fence=fence,
        ))

        return SnapshotDrift(
            reserved_drift=abs(reserved_usdt - prior_reserved),
            realized_drift=abs(realized_usdt - prior_realized),
        )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend_py && uv run pytest tests/modules/execution/event_store/test_set_position_snapshot.py -v`
Expected: PASS

- [ ] **Step 5: Run the existing boot_recovery unit tests (caller signature unchanged except return)**

Run: `cd backend_py && uv run pytest tests/modules/execution/test_boot_recovery.py -v`
Expected: PASS — the `_StubStore.set_position_snapshot` in that file ignores the return value; signature still matches (`n_offers` already passed by `run()`).

Note: the v1 `_StubStore.set_position_snapshot` lacks an `n_offers` kwarg. If this step shows a `TypeError: unexpected keyword argument 'n_offers'`, update the stub in `tests/modules/execution/test_boot_recovery.py` to accept `n_offers` and return `None` (the production caller in Task 4 will tolerate `None` only in stubs; real store returns `SnapshotDrift`). Re-run until PASS.

- [ ] **Step 6: Commit**

```bash
git add backend_py/src/bfx_funding_bot/modules/execution/event_store/store.py \
        backend_py/tests/modules/execution/event_store/test_set_position_snapshot.py \
        backend_py/tests/modules/execution/test_boot_recovery.py
git commit -m "✨ Feat: set_position_snapshot writes checkpoint + fence, returns drift"
```

---

### Task 3: `rebuild_snapshot_from_log` v2 — checkpoint + delta tail

**Files:**
- Modify: `src/bfx_funding_bot/modules/execution/event_store/store.py` (`rebuild_snapshot_from_log`)
- Test: `tests/modules/execution/event_store/test_rebuild_from_checkpoint.py` (new)

**Design:** Exposure rebuild starts from the latest `reconcile_observation` (absolute) and folds ONLY domain events with `event_seq > fence`. With no checkpoint, fall back to genesis fold (current behaviour). `offer_claims` FSM continues to rebuild from the full event_log (cheap; the checkpoint covers exposure only).

- [ ] **Step 1: Write the failing test**

```python
# tests/modules/execution/event_store/test_rebuild_from_checkpoint.py
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from bfx_funding_bot.core.db import Base
from bfx_funding_bot.modules.execution.event_store.store import PostgresEventStore
from bfx_funding_bot.modules.execution.event_store.tables import (
    EventLogRow,
    PositionStateRow,
    ReconcileObservationRow,
)


async def _engine():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return engine, async_sessionmaker(engine, expire_on_commit=False)


@pytest.mark.asyncio
async def test_rebuild_uses_checkpoint_then_replays_only_tail():
    """A checkpoint sets realized=450 at fence=3. A later ORDER_FILL at seq=4
    (size=50) is the only tail event → rebuilt realized = 450 + 50 = 500.
    Pre-fence events must NOT be re-folded (would double-count to >500)."""
    engine, sm = await _engine()
    store = PostgresEventStore(deployment_environment="ci")
    async with sm() as session:
        # pre-fence noise that the checkpoint already subsumes
        for seq, etype, size in [
            (1, "RESERVATION_CLAIMED", 150),
            (2, "ORDER_FILL", 150),
            (3, "ORDER_FILL", 150),
        ]:
            session.add(EventLogRow(
                account_id="a", deployment_environment="ci", event_type=etype,
                cid=seq, venue_offer_id=f"v{seq}", venue_seq=seq,
                payload={"size_usdt": str(size), "cid": seq, "venue_offer_id": f"v{seq}",
                         "credit_id": None, "fill_rate": 0.0, "reason": "x",
                         "signal_correlation_id": "00000000-0000-4000-8000-000000000000",
                         "account_id": "a", "is_simulated": False},
                occurred_at_ms=seq))
        # checkpoint at fence=3: absolute venue truth realized=450
        session.add(ReconcileObservationRow(
            account_id="a", deployment_environment="ci",
            reserved_usdt=Decimal("0"), realized_usdt=Decimal("450"),
            n_offers=0, n_credits=3, observed_at_ms=100, event_seq_fence=3))
        # tail: one fill AFTER the fence
        session.add(EventLogRow(
            account_id="a", deployment_environment="ci", event_type="ORDER_FILL",
            cid=4, venue_offer_id="v4", venue_seq=4,
            payload={"size_usdt": "50", "cid": 4, "venue_offer_id": "v4",
                     "credit_id": None, "fill_rate": 0.0,
                     "signal_correlation_id": "00000000-0000-4000-8000-000000000000",
                     "account_id": "a", "is_simulated": False},
            occurred_at_ms=200))
        await session.commit()

        await store.rebuild_snapshot_from_log(
            session, account_id="a", deployment_environment="ci")
        await session.commit()

        ps = (await session.execute(select(PositionStateRow).where(
            PositionStateRow.account_id == "a"))).scalar_one()

    assert ps.realized_usdt == Decimal("500")   # 450 checkpoint + 50 tail
    assert ps.reserved_usdt == Decimal("0")
    await engine.dispose()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend_py && uv run pytest tests/modules/execution/event_store/test_rebuild_from_checkpoint.py -v`
Expected: FAIL — current rebuild folds from genesis: realized = 150+150+50 = 350 (no checkpoint baseline), assertion 500 fails.

- [ ] **Step 3: Rewrite `rebuild_snapshot_from_log`**

Replace the method body in `store.py` with:

```python
    async def rebuild_snapshot_from_log(
        self, session: AsyncSession, *, account_id: str, deployment_environment: str
    ) -> None:
        """Rebuild snapshots for (account, env).

        offer_claims: folded from the full event_log (FSM, cheap).
        position_state: latest reconcile_observation checkpoint ⊕ domain events
        with event_seq > fence. Falls back to genesis fold if no checkpoint.
        """
        await session.execute(delete(OfferClaimRow).where(
            OfferClaimRow.account_id == account_id,
            OfferClaimRow.deployment_environment == deployment_environment))
        await session.execute(delete(PositionStateRow).where(
            PositionStateRow.account_id == account_id,
            PositionStateRow.deployment_environment == deployment_environment))
        await session.flush()

        rows = (await session.execute(
            select(EventLogRow).where(
                EventLogRow.account_id == account_id,
                EventLogRow.deployment_environment == deployment_environment,
            ).order_by(EventLogRow.event_seq.asc())
        )).scalars().all()

        # offer_claims: full fold.
        for r in rows:
            event = deserialize_event(r.event_type, r.payload)
            await self._project_offer_claims(session, event, account_id)

        # position_state: checkpoint + tail.
        checkpoint = (await session.execute(
            select(ReconcileObservationRow).where(
                ReconcileObservationRow.account_id == account_id,
                ReconcileObservationRow.deployment_environment == deployment_environment,
            ).order_by(ReconcileObservationRow.id.desc()).limit(1)
        )).scalar_one_or_none()

        if checkpoint is None:
            fence = 0
            base_reserved = Decimal("0")
            base_realized = Decimal("0")
            base_seq = 0
            base_ms = 0
            base_n_credits = 0
        else:
            fence = checkpoint.event_seq_fence
            base_reserved = Decimal(str(checkpoint.reserved_usdt))
            base_realized = Decimal(str(checkpoint.realized_usdt))
            base_seq = checkpoint.event_seq_fence
            base_ms = checkpoint.observed_at_ms
            base_n_credits = checkpoint.n_credits

        ps = PositionStateRow(
            account_id=account_id,
            deployment_environment=deployment_environment,
            reserved_usdt=base_reserved,
            realized_usdt=base_realized,
            last_updated_ms=base_ms,
            last_event_seq=base_seq,
            last_reconciled_at=(checkpoint.observed_at_ms if checkpoint else None),
            n_credits=(base_n_credits if checkpoint else None),
        )
        session.add(ps)

        reserved = base_reserved
        realized = base_realized
        for r in rows:
            if r.event_seq <= fence:
                continue
            event = deserialize_event(r.event_type, r.payload)
            size = Decimal(str(getattr(event, "size_usdt", 0) or 0))
            if r.event_type == "RESERVATION_CLAIMED":
                reserved += size
            elif r.event_type == "ORDER_FILL":
                delta = min(reserved, size)
                reserved -= delta
                realized += size
            elif r.event_type == "RESERVATION_RELEASED":
                reserved -= min(reserved, size)
            ps.reserved_usdt = reserved
            ps.realized_usdt = realized
            ps.last_updated_ms = r.occurred_at_ms
            ps.last_event_seq = r.event_seq
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend_py && uv run pytest tests/modules/execution/event_store/test_rebuild_from_checkpoint.py -v`
Expected: PASS

- [ ] **Step 5: Run existing store/rebuild tests for the no-checkpoint fallback**

Run: `cd backend_py && uv run pytest tests/ -k "rebuild or position_state or store" -m "not integration" -v`
Expected: PASS — pre-existing rebuild tests (no checkpoint rows) hit the genesis fallback and still produce the same numbers.

- [ ] **Step 6: Commit**

```bash
git add backend_py/src/bfx_funding_bot/modules/execution/event_store/store.py \
        backend_py/tests/modules/execution/event_store/test_rebuild_from_checkpoint.py
git commit -m "✨ Feat: rebuild exposure from checkpoint + delta tail"
```

---

### Task 4: `ReconcileResult` drift fields + `run()` threads drift

**Files:**
- Modify: `src/bfx_funding_bot/modules/execution/boot_recovery.py` (`ReconcileResult`, `run()`)
- Test: `tests/modules/execution/test_boot_recovery.py` (add cases; update `_StubStore`)

- [ ] **Step 1: Write the failing test**

Append to `tests/modules/execution/test_boot_recovery.py`:

```python
@pytest.mark.asyncio
async def test_run_threads_drift_from_snapshot_into_result():
    """ReconcileResult carries realized_drift/reserved_drift from set_position_snapshot."""
    from bfx_funding_bot.modules.execution.event_store.store import SnapshotDrift

    class _DriftStore(_StubStore):
        async def set_position_snapshot(self, session, **kw):
            await super().set_position_snapshot(session, **kw)
            return SnapshotDrift(reserved_drift=Decimal("0"), realized_drift=Decimal("150"))

    store = _DriftStore()
    bus = _StubBus()
    auth = _StubAuthRestFull(offers=[], credits=[_credit("1", "450")])
    rec = _full_boot_recovery(auth, store, _StubSessionFactory(), bus)

    result = await rec.run()

    assert result.realized_drift_usdt == Decimal("150")
    assert result.reserved_drift_usdt == Decimal("0")
```

Also update `_StubStore.set_position_snapshot` in that file to accept `n_offers` and return a zero drift by default:

```python
    async def set_position_snapshot(
        self, session, *, account_id, reserved_usdt, realized_usdt,
        n_offers, n_credits, occurred_at_ms,
    ):
        from bfx_funding_bot.modules.execution.event_store.store import SnapshotDrift
        self.snapshot_calls.append({
            "account_id": account_id, "reserved_usdt": reserved_usdt,
            "realized_usdt": realized_usdt, "n_offers": n_offers,
            "n_credits": n_credits, "occurred_at_ms": occurred_at_ms,
        })
        return SnapshotDrift(reserved_drift=Decimal("0"), realized_drift=Decimal("0"))
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend_py && uv run pytest tests/modules/execution/test_boot_recovery.py::test_run_threads_drift_from_snapshot_into_result -v`
Expected: FAIL — `ReconcileResult` has no `realized_drift_usdt`.

- [ ] **Step 3: Extend `ReconcileResult` and `run()`**

In `boot_recovery.py`, extend the dataclass:

```python
@dataclass(frozen=True, slots=True)
class ReconcileResult:
    n_claimed: int
    n_released: int
    n_failed: int
    reserved_usdt: Decimal = Decimal("0")
    realized_usdt: Decimal = Decimal("0")
    n_credits: int = 0
    reserved_drift_usdt: Decimal = Decimal("0")
    realized_drift_usdt: Decimal = Decimal("0")
```

In `run()`, capture the drift return and pass `n_offers`. Replace the `set_position_snapshot` call and the final `return` with:

```python
            drift = await self._store.set_position_snapshot(
                session,
                account_id=self._ctx.account_id,
                reserved_usdt=reserved_usdt,
                realized_usdt=realized_usdt,
                n_offers=len(venue_offers),
                n_credits=len(venue_credits),
                occurred_at_ms=now_ms,
            )
```

```python
        return ReconcileResult(
            n_claimed=n_claim, n_released=n_release, n_failed=n_fail,
            reserved_usdt=reserved_usdt, realized_usdt=realized_usdt,
            n_credits=len(venue_credits),
            reserved_drift_usdt=drift.reserved_drift,
            realized_drift_usdt=drift.realized_drift,
        )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend_py && uv run pytest tests/modules/execution/test_boot_recovery.py -v`
Expected: PASS (all, including the updated stub).

- [ ] **Step 5: Commit**

```bash
git add backend_py/src/bfx_funding_bot/modules/execution/boot_recovery.py \
        backend_py/tests/modules/execution/test_boot_recovery.py
git commit -m "✨ Feat: thread reconcile drift into ReconcileResult"
```

---

### Task 5: Single-writer — route recovery actions to the registry, not the ledger's bus

**Files:**
- Modify: `src/bfx_funding_bot/modules/execution/boot_recovery.py` (`__init__`, `run()`, add `_FsmSink` Protocol)
- Test: `tests/modules/execution/test_boot_recovery.py` (add cases)

**Design:** `BootRecovery` gains an optional `offer_registry` (an FSM sink with `async def handle(event)`). After durable commit it publishes `PositionReconciled` to the bus (the ledger's only exposure authority at reconcile time) and sends recovery `ReservationClaimed`/`ReservationReleased` to `offer_registry.handle` **directly** (not the bus), so the ledger never sees them. If `offer_registry` is None, recovery actions fall back to the bus (preserves current behaviour for any caller that hasn't wired the registry yet) — but the production daemon always wires it (Task 6).

- [ ] **Step 1: Write the failing test**

Append to `tests/modules/execution/test_boot_recovery.py`:

```python
class _StubRegistry:
    def __init__(self):
        self.handled: list = []
    async def handle(self, event):
        self.handled.append(event)


@pytest.mark.asyncio
async def test_run_routes_recovery_actions_to_registry_not_bus():
    """With an offer_registry wired: PositionReconciled goes to the bus (ledger),
    recovery ReservationClaimed goes to the registry — NOT the bus. This prevents
    the double-count once the ledger subscribes to PositionReconciled."""
    registry = _StubRegistry()
    store = _StubStore()
    bus = _StubBus()
    auth = _StubAuthRestFull(offers=[_offer(voi="999", amount="200")], credits=[])
    rec = _full_boot_recovery(auth, store, _StubSessionFactory(), bus, offer_registry=registry)

    await rec.run()

    # bus carries the snapshot signal only
    assert any(isinstance(e, PositionReconciled) for e in bus.published)
    assert not any(isinstance(e, ReservationClaimed) for e in bus.published)
    # registry receives the orphan claim (FSM)
    assert any(isinstance(e, ReservationClaimed) for e in registry.handled)


@pytest.mark.asyncio
async def test_run_falls_back_to_bus_when_no_registry():
    """No registry → recovery actions still reach the bus (legacy path)."""
    store = _StubStore()
    bus = _StubBus()
    auth = _StubAuthRestFull(offers=[_offer(voi="999", amount="200")], credits=[])
    rec = _full_boot_recovery(auth, store, _StubSessionFactory(), bus)  # no offer_registry

    await rec.run()

    assert any(isinstance(e, ReservationClaimed) for e in bus.published)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend_py && uv run pytest tests/modules/execution/test_boot_recovery.py::test_run_routes_recovery_actions_to_registry_not_bus -v`
Expected: FAIL — `_full_boot_recovery` has no `offer_registry` kwarg / recovery actions still go to bus.

- [ ] **Step 3: Add the sink + routing**

In `boot_recovery.py`, add a Protocol near `_Bus`:

```python
class _FsmSink(Protocol):
    async def handle(self, event: Any) -> None: ...
```

Add the param to `__init__` (after `bus: _Bus,`):

```python
        offer_registry: _FsmSink | None = None,
```

and store it:

```python
        self._offer_registry = offer_registry
```

In `run()`, replace the post-commit publish block (the `await self._safe_publish(position_reconciled)` line and the `for ev in actions:` loop) with:

```python
        # Snapshot signal → bus (the ledger's sole exposure authority at reconcile).
        await self._safe_publish(position_reconciled)

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
```

Add the helper method (next to `_safe_publish`):

```python
    async def _route_fsm(self, event: object) -> None:
        """Recovery FSM events go to the registry directly (NOT the ledger's bus),
        so reconcile-time exposure stays single-writer (PositionReconciled).
        Falls back to the bus when no registry is wired."""
        if self._offer_registry is not None:
            try:
                await self._offer_registry.handle(event)
            except Exception as exc:
                log.critical(
                    "boot_recovery_fsm_route_failed event=%s err=%r — FSM projection lost, SoT persisted",
                    type(event).__name__, exc,
                )
        else:
            await self._safe_publish(event)
```

Update `_full_boot_recovery` helper in the test file to thread the kwarg:

```python
def _full_boot_recovery(auth_rest, store, session_factory, bus, **kw):
    return BootRecovery(
        store=store, session_factory=session_factory, auth_rest=auth_rest,
        account_ctx=AccountContext(
            account_id="default",
            credentials=Credentials(api_key="k", api_secret="s"),
            allocation_cap_usdt=Decimal("1")),
        deployment_environment="ci", bus=bus,
        max_attempts=1, backoff_base_s=0, clock=lambda: _NOW, **kw,
    )
```

(`offer_registry=...` now flows through `**kw`.)

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend_py && uv run pytest tests/modules/execution/test_boot_recovery.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend_py/src/bfx_funding_bot/modules/execution/boot_recovery.py \
        backend_py/tests/modules/execution/test_boot_recovery.py
git commit -m "✨ Feat: route recovery FSM events to registry, keep exposure single-writer"
```

---

### Task 6: Daemon wiring — subscribe `PositionReconciled`, pass `offer_registry` to recoveries

**Files:**
- Modify: `src/bfx_funding_bot/modules/marketfeed/daemon.py` (recovery construction ~803-833; subscriptions ~855)
- Test: `tests/modules/marketfeed/test_daemon.py` (extend the existing build/run test)

**Design:** Both `boot_recovery` and `runtime_recovery` receive `offer_registry`. After the existing ledger subscriptions, add `bus.subscribe(PositionReconciled, ledger.on_position_reconciled)`.

- [ ] **Step 1: Write the failing test**

Add to `tests/modules/marketfeed/test_daemon.py` (a focused assertion on the built wiring; adapt to the file's existing `build_daemon` fixture/helper — locate the existing test that calls `build_daemon` and add an assertion in the same style):

```python
@pytest.mark.asyncio
async def test_ledger_subscribes_to_position_reconciled(monkeypatch):
    """build_daemon wires PositionReconciled → ledger.on_position_reconciled."""
    from bfx_funding_bot.modules.execution.events import PositionReconciled
    # Build the daemon via the same helper the existing daemon test uses, in live mode
    # (is_simulated=False) so boot/periodic recovery + the subscription are created.
    components = await _build_live_daemon_for_test(monkeypatch)  # reuse/extend existing helper
    subscribed = components.bus._subscribers.get(PositionReconciled, [])  # adapt to bus internals
    assert any(
        getattr(cb, "__self__", None) is components.ledger and cb.__name__ == "on_position_reconciled"
        for cb in subscribed
    )
```

NOTE for the implementer: `tests/modules/marketfeed/test_daemon.py` already builds a daemon (`test_daemon_builds_and_runs_briefly`). Reuse its construction path. If the bus does not expose `_subscribers`, instead assert behaviourally: publish a `PositionReconciled(realized=450,…)` on `components.bus` and assert `components.ledger.realized_exposure() == Decimal("450")`. Prefer the behavioural assertion if bus internals are private.

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend_py && uv run pytest tests/modules/marketfeed/test_daemon.py -k position_reconciled -v`
Expected: FAIL — no subscription / ledger unchanged after publish.

- [ ] **Step 3: Wire the daemon**

In `daemon.py`, add `offer_registry=offer_registry,` to BOTH the `boot_recovery = BootRecovery(...)` (after `bus=bus,`) and `runtime_recovery = BootRecovery(...)` constructions.

Add the import if not present (it is — `PositionReconciled` lives in the same `events` module already imported for the other events; extend that import line):

```python
from bfx_funding_bot.modules.execution.events import (
    # ...existing...
    PositionReconciled,
)
```

After the existing ledger subscription block (`bus.subscribe(ReservationReleased, ledger.on_reservation_released)`), add:

```python
    # Credit-aware reconcile: snapshot is the sole exposure authority at reconcile.
    bus.subscribe(PositionReconciled, ledger.on_position_reconciled)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend_py && uv run pytest tests/modules/marketfeed/test_daemon.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend_py/src/bfx_funding_bot/modules/marketfeed/daemon.py \
        backend_py/tests/modules/marketfeed/test_daemon.py
git commit -m "✨ Feat: wire PositionReconciled→ledger + offer_registry into recoveries"
```

---

### Task 7: Credit-dimension divergence signal in `PeriodicReconcile._tick`

**Files:**
- Modify: `src/bfx_funding_bot/modules/execution/periodic_reconcile.py` (`_tick`)
- Test: `tests/modules/execution/test_periodic_reconcile.py` (add cases)

**Design:** Flag `RECONCILE DEGRADED` when `realized_drift_usdt` or `reserved_drift_usdt` exceeds a small epsilon, in addition to the existing offer-action signal. This surfaces a silently-broken WS credit path (the canary bug) which produces no offer action.

- [ ] **Step 1: Write the failing test**

Add to `tests/modules/execution/test_periodic_reconcile.py`:

```python
@pytest.mark.asyncio
async def test_realized_drift_sets_degraded_even_without_offer_actions():
    probe = _FakeProbe()
    # no claims/releases, but realized drifted by $150 (WS credit path missed)
    recovery = _FakeRecovery(results=[
        ReconcileResult(0, 0, 0, realized_drift_usdt=Decimal("150")),
    ])
    pr = PeriodicReconcile(recovery=recovery, probe=probe, interval_s=0.01,
                           max_consecutive_failures=3)
    stop = asyncio.Event()

    async def _stop_soon():
        await asyncio.sleep(0.02); stop.set()

    await asyncio.gather(pr.run_loop(stop), _stop_soon())

    assert any(t == HealthTarget.RECONCILE and s == HealthStatus.DEGRADED
               for (t, s, _f) in probe.updates)
```

Add the import to the test file header if missing: `from decimal import Decimal`.

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend_py && uv run pytest tests/modules/execution/test_periodic_reconcile.py::test_realized_drift_sets_degraded_even_without_offer_actions -v`
Expected: FAIL — `_tick` ignores drift fields; no DEGRADED update recorded.

- [ ] **Step 3: Extend `_tick`**

In `periodic_reconcile.py`, add a module constant near the top:

```python
_DRIFT_EPSILON = Decimal("0.01")
```

and `from decimal import Decimal` to the imports.

Replace the divergence condition. Change:

```python
        if result.n_released > 0 or result.n_claimed > 0:
```

to:

```python
        drifted = (
            result.realized_drift_usdt > _DRIFT_EPSILON
            or result.reserved_drift_usdt > _DRIFT_EPSILON
        )
        if result.n_released > 0 or result.n_claimed > 0 or drifted:
```

and extend the DEGRADED `error_message` to include drift:

```python
            self._probe.update(
                HealthTarget.RECONCILE, HealthStatus.DEGRADED,
                error_message=(
                    f"reconcile drift released={result.n_released} "
                    f"claimed={result.n_claimed} "
                    f"realized_drift={result.realized_drift_usdt} "
                    f"reserved_drift={result.reserved_drift_usdt}"
                ),
            )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend_py && uv run pytest tests/modules/execution/test_periodic_reconcile.py -v`
Expected: PASS (existing tests still green — they construct `ReconcileResult(0,0,0)` so drift defaults to 0).

- [ ] **Step 5: Commit**

```bash
git add backend_py/src/bfx_funding_bot/modules/execution/periodic_reconcile.py \
        backend_py/tests/modules/execution/test_periodic_reconcile.py
git commit -m "✨ Feat: surface credit-dimension drift as RECONCILE DEGRADED"
```

---

### Task 8: Integration — end-to-end incident reproduction (real ledger + real bus)

**Files:**
- Create: `tests/integration/test_credit_reconcile_pg.py`
- Reference pattern: `tests/integration/test_boot_recovery_pg.py` (testcontainer DB + unique account_id per test)

**Design:** This is the test v1 lacked — it wires a REAL `PaperPositionLedger` to a REAL bus, subscribes both the delta handlers AND `on_position_reconciled`, then runs reconcile with an orphan offer present, proving NO double-count and that `realized = Σcredits`.

- [ ] **Step 1: Write the failing test**

```python
# tests/integration/test_credit_reconcile_pg.py
import uuid
from decimal import Decimal

import pytest

from bfx_funding_bot.modules.execution.boot_recovery import BootRecovery
from bfx_funding_bot.modules.execution.event_store.store import PostgresEventStore
from bfx_funding_bot.modules.execution.events import (
    OrderFilled,
    PositionReconciled,
    ReservationClaimed,
    ReservationReleased,
)
from bfx_funding_bot.modules.execution.ledger import PaperPositionLedger

pytestmark = pytest.mark.integration


class _Bus:
    def __init__(self):
        self._subs: dict = {}
    def subscribe(self, etype, cb):
        self._subs.setdefault(etype, []).append(cb)
    async def publish(self, ev):
        for cb in self._subs.get(type(ev), []):
            await cb(ev)


class _Registry:
    def __init__(self):
        self.handled = []
    async def handle(self, ev):
        self.handled.append(ev)


class _AuthRest:
    def __init__(self, offers, credits):
        self._o, self._c = offers, credits
    async def get_active_funding_offers(self, *, ctx, symbol="fUST"):
        return self._o
    async def get_active_funding_credits(self, *, ctx, symbol="fUST"):
        return self._c


@pytest.mark.asyncio
async def test_orphan_offer_plus_credits_no_double_count(session_factory, account_ctx_factory):
    """Reconcile with 1 orphan offer ($100) + 3 credits ($450), ledger drifted.
    Ledger subscribes to BOTH the delta handlers and on_position_reconciled.
    Assert exposure == venue truth ($100 reserved + $450 realized), NOT doubled."""
    from bfx_funding_bot.external.bitfinex.auth_rest import ActiveFundingCredit, ActiveFundingOffer

    acct = f"it-{uuid.uuid4().hex[:8]}"
    ctx = account_ctx_factory(account_id=acct)  # adapt to the integration fixtures
    store = PostgresEventStore(deployment_environment="ci")
    ledger = PaperPositionLedger(account_id=acct)
    bus = _Bus()
    bus.subscribe(ReservationClaimed, ledger.on_reservation_claimed)
    bus.subscribe(OrderFilled, ledger.on_order_filled)
    bus.subscribe(ReservationReleased, ledger.on_reservation_released)
    bus.subscribe(PositionReconciled, ledger.on_position_reconciled)
    registry = _Registry()

    offers = [ActiveFundingOffer(
        venue_offer_id="555", symbol="fUST", amount=Decimal("100"),
        rate=0.0003, period_days=2, mts_created=1, status="ACTIVE")]
    credits = [ActiveFundingCredit(
        credit_id=str(i), symbol="fUST", amount=Decimal("150"),
        rate=0.0003, period_days=2, status="ACTIVE") for i in range(3)]

    rec = BootRecovery(
        store=store, session_factory=session_factory,
        auth_rest=_AuthRest(offers, credits), account_ctx=ctx,
        deployment_environment="ci", bus=bus, offer_registry=registry,
        symbol="fUST", clock=lambda: 2_000_000)

    await rec.run()

    # venue truth: reserved=$100 (1 offer), realized=$450 (3 credits). NOT doubled.
    assert ledger.realized_exposure() == Decimal("450")
    assert ledger.current_exposure() == Decimal("550")
    # orphan claim went to FSM registry, not the ledger's bus delta path
    assert any(isinstance(e, ReservationClaimed) for e in registry.handled)
```

NOTE for the implementer: adapt `session_factory` / `account_ctx_factory` to the actual fixtures in `tests/integration/conftest.py` (mirror `test_boot_recovery_pg.py`). If `account_ctx_factory` does not exist, construct `AccountContext(account_id=acct, credentials=Credentials(api_key="k", api_secret="s"), allocation_cap_usdt=Decimal("1000"))` inline.

- [ ] **Step 2: Run test to verify it fails (or errors on fixtures first)**

Run: `cd backend_py && uv run pytest tests/integration/test_credit_reconcile_pg.py -v -m integration`
Expected: FAIL — without Task 5 the orphan `ReservationClaimed` would hit the ledger via bus and double-count `reserved` to $200 (this test guards the fix). If fixtures need adapting, fix imports/fixtures until the assertion is what fails.

- [ ] **Step 3: Confirm it passes against the implemented code**

Run: `cd backend_py && uv run pytest tests/integration/test_credit_reconcile_pg.py -v -m integration`
Expected: PASS — `reserved=$100`, `realized=$450`, no double-count.

- [ ] **Step 4: Commit**

```bash
git add backend_py/tests/integration/test_credit_reconcile_pg.py
git commit -m "✅ Test: integration — credit reconcile, no double-count (real ledger+bus)"
```

---

## Final verification (after all tasks)

- [ ] **Full unit gate**

Run: `cd backend_py && uv run pytest -m "not integration"`
Expected: all pass (≥ 808 + new).

- [ ] **Types + lint**

Run: `cd backend_py && uv run mypy src/ && uv run ruff check`
Expected: `Success` + `All checks passed!`

- [ ] **Migration drift check**

Run: `cd backend_py && uv run alembic check`
Expected: `No new upgrade operations detected.`

- [ ] **Update the spec's status**

In `docs/superpowers/specs/2026-05-29-credit-aware-reconcile-design.md`, change the architecture-decisions note to reference this v2 plan (single-writer + checkpoint + drift signal) and mark the dual-writer issue resolved.

---

## Rollout (unchanged from spec, with one addition)

1. Land all tasks; unit + mypy + ruff + `alembic check` green.
2. `alembic upgrade head` locally; verify `reconcile_observation` table + `position_state` columns exist.
3. Check live `BFX_ALLOCATION_CAP_USDT` on Koyeb console; set to 450. Update `safety.canary.yaml` comment 150→450.
4. Deploy via `scripts/deploy-koyeb.sh canary` (auto-deploy disabled) — cap + code together.
5. First reconcile: `position_state.realized_usdt=450`, a `reconcile_observation` row appears, `10001` noise stops. Because realized drifts $300→$450 on that first tick, expect ONE `RECONCILE DEGRADED` blip that clears on the next clean tick (the drift signal working as designed).
6. Watch first credit maturity: `realized` decrements, freed capital redeployed next tick.

---

## Self-Review notes

- **Spec coverage:** #1 dual-writer → Tasks 5,6,8; #2 drift signal → Task 7; #3 checkpoint+tail → Tasks 1,2,3; audit trail → Task 1 (`reconcile_observation`). All three approved scope items covered.
- **Type consistency:** `SnapshotDrift{reserved_drift,realized_drift}` (store) → `ReconcileResult.reserved_drift_usdt/realized_drift_usdt` (boot_recovery) → consumed in `PeriodicReconcile._tick`. `set_position_snapshot(..., n_offers, n_credits, occurred_at_ms)` signature consistent across store impl (Task 2), stub (Task 4), and caller (Task 4). `_FsmSink.handle` matches `OfferRegistry.handle` used in `daemon.py:859-861`.
- **Fence correctness:** fence = `max(event_seq)` AFTER recovery actions are appended (Task 2 queries within the same txn post-append), so reconcile-sourced events are ≤ fence and excluded from the rebuild tail — no double-apply.
