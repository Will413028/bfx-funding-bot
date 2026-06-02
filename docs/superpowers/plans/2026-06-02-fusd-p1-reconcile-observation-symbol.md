# fUSD-live P1 — reconcile_observation.symbol Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the reconcile checkpoint base per-symbol so `rebuild_snapshot_from_log` seeds from the correct currency's prior state instead of the latest-of-any-symbol.

**Architecture:** Add a `symbol` column to `reconcile_observation` (NOT NULL, `server_default 'fUST'`), write it in `set_position_snapshot`, and filter the checkpoint select by symbol in `rebuild_snapshot_from_log`. Additive DB migration, forward-compatible with the live canary (old code keeps writing; column auto-defaults). This seam is latent today (`rebuild_snapshot_from_log` has **no production caller** — test/admin-rebuild only) but must be correct before a 2nd currency relies on rebuild.

**Tech Stack:** Python 3.13, SQLAlchemy 2.0 async, Alembic, pytest (asyncio), sqlite (unit) + testcontainers Postgres (integration).

**Spec:** `docs/superpowers/specs/2026-06-02-fusd-live-enablement-design.md` §6.1.

**Independence / ordering:** Independent of P3/P2 (separate table, separate code path). Run in parallel with P3. **P2's migration chains off this one** — do P2 last so its `down_revision` points at this migration (linear chain, no multi-head).

**Gate (run before every commit):** `cd backend_py && uv run pytest -m "not integration" && uv run mypy src/ && uv run ruff check`

> ⚠️ **LIVE-DB SAFETY (read first).** `backend_py/.env` → `../.env` has `DATABASE_URL` pointing at the **shared live Neon DB the VM canary runs on**. NEVER run any alembic command that connects to it: `upgrade`, `downgrade`, `check`, `current`, or `revision --autogenerate` (autogenerate reflects the live schema). The migration is **hand-authored** (zero DB connection) and verified **only** via the testcontainers integration test (ephemeral PG, CI/Docker). Offline-safe alembic commands (read the `versions/` dir, no DB): `heads`, `history`, `show`. Unit tests (`-m "not integration"`) use sqlite and never touch Neon.

---

### Task 1: Per-symbol checkpoint base (ORM + behaviour)

**Files:**
- Modify: `backend_py/src/bfx_funding_bot/modules/execution/event_store/tables.py:117-142`
- Modify: `backend_py/src/bfx_funding_bot/modules/execution/event_store/store.py:327-336` (write) and `:382-387` (read)
- Test: `backend_py/tests/modules/execution/event_store/test_rebuild_from_checkpoint.py`, `test_set_position_snapshot.py`

- [ ] **Step 1: Write the failing test — two-symbol checkpoint base isolation**

Add to `tests/modules/execution/event_store/test_rebuild_from_checkpoint.py` (mirror the existing `_engine()` / `PostgresEventStore` style):

```python
@pytest.mark.asyncio
async def test_rebuild_seeds_from_same_symbol_checkpoint():
    """Two checkpoints exist: fUST (fence=3, realized=450) then a LATER fUSD
    (fence=4, realized=200). Rebuilding fUST must seed from the fUST checkpoint
    (450), not the newer fUSD one (200). Proves the checkpoint base is per-symbol."""
    engine, sm = await _engine()
    store = PostgresEventStore(deployment_environment="ci")
    async with sm() as session:
        session.add(ReconcileObservationRow(
            account_id="a", deployment_environment="ci", symbol="fUST",
            reserved_usdt=Decimal("0"), realized_usdt=Decimal("450"),
            n_offers=0, n_credits=3, observed_at_ms=100, event_seq_fence=3))
        session.add(ReconcileObservationRow(
            account_id="a", deployment_environment="ci", symbol="fUSD",
            reserved_usdt=Decimal("0"), realized_usdt=Decimal("200"),
            n_offers=0, n_credits=1, observed_at_ms=200, event_seq_fence=4))
        await session.commit()
        await store.rebuild_snapshot_from_log(
            session, account_id="a", deployment_environment="ci", symbol="fUST")
        await session.commit()
        ps = (await session.execute(select(PositionStateRow).where(
            PositionStateRow.account_id == "a",
            PositionStateRow.symbol == "fUST"))).scalar_one()
    assert ps.realized == Decimal("450")   # seeded from fUST checkpoint, not fUSD's 200
```

- [ ] **Step 2: Run it — verify it fails**

Run: `cd backend_py && uv run pytest tests/modules/execution/event_store/test_rebuild_from_checkpoint.py::test_rebuild_seeds_from_same_symbol_checkpoint -v`
Expected: FAIL — `TypeError: 'symbol' is an invalid keyword argument for ReconcileObservationRow` (column not yet on the ORM).

- [ ] **Step 3: Add the `symbol` column + per-symbol index to the ORM**

In `tables.py`, edit `ReconcileObservationRow` — add `symbol` after `deployment_environment`, and widen the index:

```python
    account_id: Mapped[str] = mapped_column(Text, nullable=False)
    deployment_environment: Mapped[str] = mapped_column(Text, nullable=False)
    symbol: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'fUST'"))
    reserved_usdt: Mapped[Decimal] = mapped_column(Numeric, nullable=False)
    ...
    __table_args__ = (
        Index("idx_reconcile_obs_acct_env_symbol_id",
              "account_id", "deployment_environment", "symbol", "id"),
    )
```

Confirm `text` is already imported in `tables.py` (it is — used by `PositionStateRow.reserved` `server_default=text("0")`). If not, add `from sqlalchemy import text`.

- [ ] **Step 4: Write the symbol in `set_position_snapshot`**

In `store.py` (~327-336), add `symbol=symbol,` to the `ReconcileObservationRow(...)` construction (the method already receives `symbol: str`, store.py:275):

```python
        session.add(ReconcileObservationRow(
            account_id=account_id,
            deployment_environment=self._env,
            symbol=symbol,
            reserved_usdt=reserved_usdt,
            realized_usdt=realized_usdt,
            n_offers=n_offers,
            n_credits=n_credits,
            observed_at_ms=occurred_at_ms,
            event_seq_fence=fence,
        ))
```

- [ ] **Step 5: Filter the checkpoint select by symbol in `rebuild_snapshot_from_log`**

In `store.py` (~382-387), add the symbol predicate and update the stale NOTE comment above it (store.py:375-381) to record the fix:

```python
        # position_state: checkpoint + tail. The checkpoint base is now per-symbol
        # (reconcile_observation.symbol) so fUST/fUSD rebuild from their own base.
        checkpoint = (await session.execute(
            select(ReconcileObservationRow).where(
                ReconcileObservationRow.account_id == account_id,
                ReconcileObservationRow.deployment_environment == deployment_environment,
                ReconcileObservationRow.symbol == symbol,
            ).order_by(ReconcileObservationRow.id.desc()).limit(1)
        )).scalar_one_or_none()
```

- [ ] **Step 6: Run the new test — verify it passes**

Run: `cd backend_py && uv run pytest tests/modules/execution/event_store/test_rebuild_from_checkpoint.py -v`
Expected: PASS (new test + all existing rebuild tests still green — existing tests already pass `symbol="fUST"` in seeded payloads).

- [ ] **Step 7: Add a `set_position_snapshot` assertion that the checkpoint carries symbol**

Append to `test_set_position_snapshot.py` (mirror its `_session()` style):

```python
@pytest.mark.asyncio
async def test_set_position_snapshot_stamps_symbol_on_checkpoint():
    engine, sm = await _session()
    store = PostgresEventStore(deployment_environment="ci")
    async with sm() as session:
        await store.set_position_snapshot(
            session, account_id="a",
            reserved_usdt=Decimal("0"), realized_usdt=Decimal("10"),
            n_offers=0, n_credits=1, occurred_at_ms=1, symbol="fUST")
        await session.commit()
        obs = (await session.execute(select(ReconcileObservationRow).where(
            ReconcileObservationRow.account_id == "a"))).scalar_one()
    assert obs.symbol == "fUST"
```

- [ ] **Step 8: Run the full gate + commit**

Run: `cd backend_py && uv run pytest -m "not integration" && uv run mypy src/ && uv run ruff check`
Expected: all green.

```bash
git add backend_py/src/bfx_funding_bot/modules/execution/event_store/tables.py \
        backend_py/src/bfx_funding_bot/modules/execution/event_store/store.py \
        backend_py/tests/modules/execution/event_store/test_rebuild_from_checkpoint.py \
        backend_py/tests/modules/execution/event_store/test_set_position_snapshot.py
git commit -m "✨ Feat: per-symbol reconcile_observation checkpoint base (fUSD P1)"
```

---

### Task 2: Alembic migration — add `reconcile_observation.symbol` (hand-authored)

**Files:**
- Create: `backend_py/alembic/versions/c9d0e1f2a3b4_add_symbol_to_reconcile_observation.py`

> Do NOT use `alembic revision --autogenerate` — it reflects the live Neon schema. Hand-write the file with the Write tool (template: `93c9ff214a61_add_last_reconciled_at_and_n_credits_to_.py`). Revision id is pre-assigned below (`c9d0e1f2a3b4`); `down_revision` is the current head `b7c1d2e3f4a5`.

- [ ] **Step 1: Write the migration file verbatim**

Create `backend_py/alembic/versions/c9d0e1f2a3b4_add_symbol_to_reconcile_observation.py`:

```python
"""add symbol to reconcile_observation

Revision ID: c9d0e1f2a3b4
Revises: b7c1d2e3f4a5
Create Date: 2026-06-02

"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'c9d0e1f2a3b4'
down_revision: str | Sequence[str] | None = 'b7c1d2e3f4a5'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        'reconcile_observation',
        sa.Column('symbol', sa.Text(), nullable=False, server_default=sa.text("'fUST'")),
    )
    op.create_index(
        'idx_reconcile_obs_acct_env_symbol_id',
        'reconcile_observation',
        ['account_id', 'deployment_environment', 'symbol', 'id'],
    )
    op.drop_index('idx_reconcile_obs_acct_env_id', table_name='reconcile_observation')


def downgrade() -> None:
    op.create_index(
        'idx_reconcile_obs_acct_env_id',
        'reconcile_observation',
        ['account_id', 'deployment_environment', 'id'],
    )
    op.drop_index('idx_reconcile_obs_acct_env_symbol_id', table_name='reconcile_observation')
    op.drop_column('reconcile_observation', 'symbol')
```

(Keep `server_default='fUST'` permanently — D4 backfill; the ORM column declares the same, so they match.)

- [ ] **Step 2: Verify the chain is linear + single head (OFFLINE — reads versions/ only)**

Run: `cd backend_py && uv run alembic heads && uv run alembic history -r b7c1d2e3f4a5:head`
Expected: a single head `c9d0e1f2a3b4`; history shows `b7c1d2e3f4a5 -> c9d0e1f2a3b4`. These commands read the `versions/` directory and do NOT connect to the DB. Do NOT run `alembic upgrade`/`check`/`current` (they connect to live Neon).

- [ ] **Step 3: Verify the file imports + ruff-clean**

Run: `cd backend_py && uv run python -c "import alembic.versions.c9d0e1f2a3b4_add_symbol_to_reconcile_observation as m; print(m.revision, m.down_revision)" && uv run ruff check alembic/versions/c9d0e1f2a3b4_add_symbol_to_reconcile_observation.py`
Expected: prints `c9d0e1f2a3b4 b7c1d2e3f4a5`; ruff clean. (Module import does not connect to the DB.)

- [ ] **Step 4: Commit**

```bash
git add backend_py/alembic/versions/c9d0e1f2a3b4_add_symbol_to_reconcile_observation.py
git commit -m "✨ Feat: migration — reconcile_observation.symbol (fUSD P1)"
```

> The actual `alembic upgrade head` happens only at the coordinated cutover deploy (with the eventual fUSD go-live), against the live DB by the operator — exactly like Phase-1's `b7c1d2e3f4a5`. Correctness is proven locally by the Task 3 testcontainers integration test, never by upgrading the live DB.

---

### Task 3: Migration integration test (testcontainers)

**Files:**
- Create: `backend_py/tests/integration/test_migration_reconcile_observation_symbol.py`

- [ ] **Step 1: Write the integration test (mirror `test_migration_per_symbol_pk.py`)**

```python
import pytest

from tests.integration.test_migration_per_symbol_pk import _ALEMBIC_INI

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_reconcile_observation_has_symbol_after_upgrade(pg_engine, monkeypatch) -> None:
    """Drive a real alembic upgrade against testcontainer Postgres; assert the
    reconcile_observation.symbol column exists and is NOT NULL after head."""
    sync_url = pg_engine.url.render_as_string(hide_password=False).replace(
        "+asyncpg", "+psycopg")
    monkeypatch.setenv("DATABASE_URL", sync_url)

    from sqlalchemy import create_engine, inspect
    eng = create_engine(sync_url)
    try:
        with eng.begin() as setup_conn:
            setup_conn.exec_driver_sql("DROP SCHEMA public CASCADE")
            setup_conn.exec_driver_sql("CREATE SCHEMA public")
    finally:
        eng.dispose()

    from alembic.config import Config
    from alembic import command
    command.upgrade(Config(str(_ALEMBIC_INI)), "head")

    verify_eng = create_engine(sync_url)
    try:
        with verify_eng.connect() as verify_conn:
            cols = {c["name"]: c for c in inspect(verify_conn).get_columns("reconcile_observation")}
    finally:
        verify_eng.dispose()

    assert "symbol" in cols
    assert cols["symbol"]["nullable"] is False
```

If `_ALEMBIC_INI` is not importable from that module, copy its definition (the path to `backend_py/alembic.ini`) from the top of `test_migration_per_symbol_pk.py`.

- [ ] **Step 2: Run it (Docker required — CI/cutover only)**

Run: `cd backend_py && uv run pytest tests/integration/test_migration_reconcile_observation_symbol.py -v -m integration`
Expected: PASS where Docker is available. On this machine (no Docker) it is collected but skipped/errors at container start — that is expected; it runs in CI.

- [ ] **Step 3: Confirm the unit gate is unaffected + commit**

Run: `cd backend_py && uv run pytest -m "not integration" && uv run mypy src/ && uv run ruff check`
Expected: green (the integration test is excluded by the marker).

```bash
git add backend_py/tests/integration/test_migration_reconcile_observation_symbol.py
git commit -m "✅ Test: integration — reconcile_observation.symbol migration (fUSD P1)"
```

---

## Self-Review

- **Spec coverage (§6.1):** column + index (Task 1 Step 3, Task 2), `set_position_snapshot` write (Task 1 Step 4), `rebuild` filter (Task 1 Step 5), two-symbol regression (Task 1 Step 1), migration + integration test (Tasks 2-3), server_default='fUST' (D4, Task 2 Step 2), index (D6). ✓
- **Acceptance:** fUST byte-identical — existing rebuild/set_position_snapshot tests untouched and still green (Task 1 Step 6). `alembic check` clean (Task 2 Step 3).
- **No placeholders:** all steps carry real code/commands.
- **Type consistency:** `symbol: Mapped[str]` (ORM) ↔ `symbol: str` param (`set_position_snapshot`, already present) ↔ `ReconcileObservationRow.symbol` (rebuild filter). Index name `idx_reconcile_obs_acct_env_symbol_id` used consistently in ORM + migration.
