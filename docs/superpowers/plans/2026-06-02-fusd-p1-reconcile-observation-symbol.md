# fUSD-live P1 — reconcile_observation.symbol Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the reconcile checkpoint base per-symbol so `rebuild_snapshot_from_log` seeds from the correct currency's prior state instead of the latest-of-any-symbol.

**Architecture:** Add a `symbol` column to `reconcile_observation` (NOT NULL, `server_default 'fUST'`), write it in `set_position_snapshot`, and filter the checkpoint select by symbol in `rebuild_snapshot_from_log`. Additive DB migration, forward-compatible with the live canary (old code keeps writing; column auto-defaults). This seam is latent today (`rebuild_snapshot_from_log` has **no production caller** — test/admin-rebuild only) but must be correct before a 2nd currency relies on rebuild.

**Tech Stack:** Python 3.13, SQLAlchemy 2.0 async, Alembic, pytest (asyncio), sqlite (unit) + testcontainers Postgres (integration).

**Spec:** `docs/superpowers/specs/2026-06-02-fusd-live-enablement-design.md` §6.1.

**Independence / ordering:** Independent of P3/P2 (separate table, separate code path). Run in parallel with P3. **P2's migration chains off this one** — do P2 last so its `down_revision` points at this migration (linear chain, no multi-head).

**Gate (run before every commit):** `cd backend_py && uv run pytest -m "not integration" && uv run mypy src/ && uv run ruff check`

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

### Task 2: Alembic migration — add `reconcile_observation.symbol`

**Files:**
- Create: `backend_py/alembic/versions/<auto>_add_symbol_to_reconcile_observation.py`

- [ ] **Step 1: Autogenerate the migration**

Run (must be in `backend_py/` so it uses the uv-managed Python 3.13; needs `.env` symlink → `ln -sf ../.env backend_py/.env` if missing):

```bash
cd backend_py && uv run alembic revision --autogenerate -m "add symbol to reconcile_observation"
```

Expected: a new file under `alembic/versions/` whose `down_revision = 'b7c1d2e3f4a5'` (current head). It will contain an `op.add_column('reconcile_observation', sa.Column('symbol', sa.Text(), nullable=False))` and a `create_index`.

- [ ] **Step 2: Hand-adjust the generated file**

Autogenerate does NOT infer the backfill `server_default`. Edit `upgrade()`/`downgrade()` to read exactly (template: `93c9ff214a61_add_last_reconciled_at_and_n_credits_to_.py`):

```python
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

Confirm `revision`/`down_revision` were assigned by alembic; `down_revision` MUST be `'b7c1d2e3f4a5'`.

- [ ] **Step 2b: Decide on the server_default lifetime**

Keep `server_default=sa.text("'fUST'")` permanently — it backfills the single live canary row and is harmless (the ORM also declares it). Do NOT add a follow-up to drop it (YAGNI; matches the spec D4 ruling).

- [ ] **Step 3: Apply locally + check for drift**

Run:
```bash
cd backend_py && uv run alembic upgrade head && uv run alembic check
```
Expected: upgrade applies cleanly; `alembic check` reports no drift (ORM metadata matches the migrated schema). If `alembic check` flags the `server_default`, ensure the ORM column also declares `server_default=text("'fUST'")` (Task 1 Step 3) so they match.

- [ ] **Step 4: Verify downgrade/upgrade idempotency on a scratch DB**

Run:
```bash
cd backend_py && uv run alembic downgrade -1 && uv run alembic upgrade head && uv run alembic check
```
Expected: clean both ways.

- [ ] **Step 5: Commit**

```bash
git add backend_py/alembic/versions/
git commit -m "✨ Feat: migration — reconcile_observation.symbol (fUSD P1)"
```

> **Live-DB note:** do NOT apply this to the shared Neon canary DB now. It is author-now / apply-at-cutover (with the eventual fUSD deploy), exactly like Phase-1's `b7c1d2e3f4a5`. Local sqlite/Postgres + CI only this session.

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
