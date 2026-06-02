# fUSD-live P2 — offer_claims.symbol + per-claim recovery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Persist the per-offer `symbol` on `offer_claims`, thread it through the projection and the in-memory registry, and make boot recovery's missing-claim release use the claim's **own** symbol — deleting the `symbols[0]` hardcode that mis-attributes released capital to the wrong currency.

**Architecture:** Additive `offer_claims.symbol` column (NOT NULL, `server_default 'fUST'`); the cid-keyed upsert writes + reasserts it; `LocalClaim` and `ClaimRecord` carry it (loaded from the column); `compute_recovery_actions` becomes fully per-claim and fail-loud (raises if a claim's symbol ∉ configured set). This is the **riskiest seam** in the fUSD batch — it runs on the live boot/reconcile path every restart and mutates `reserved[symbol]` directly.

**Tech Stack:** Python 3.13, SQLAlchemy 2.0 async (pg/sqlite upsert), Alembic, pytest (asyncio), testcontainers Postgres.

**Spec:** `docs/superpowers/specs/2026-06-02-fusd-live-enablement-design.md` §6.3 + §10 (riskiest concern).

**Ordering — run LAST (after P1 and P3):**
- Depends on **P3** — `_project_offer_claims` reads `_ev.symbol` (present on all claim-bearing events only after P3); the boot-recovery `ReservationFailed` switches from P3's global `symbol` param to per-claim `c.symbol`.
- Its migration `down_revision` must point at **P1's** migration (the head once P1 ∥ P3 are merged) → linear chain, no multi-head. Generate P2's migration only after P1's exists.
- Note: the two already-correct release paths — `fill_tracker.py:207-218` (`symbol=claim.symbol`) and `ws_dispatcher.py:123-134` (`symbol=foc.symbol`) — need **no change**; the tests just confirm them.

**Gate (run before every commit):** `cd backend_py && uv run pytest -m "not integration" && uv run mypy src/ && uv run ruff check`

> ⚠️ **LIVE-DB SAFETY (read first).** `backend_py/.env` → `../.env` `DATABASE_URL` is the **shared live Neon DB the VM canary runs on**. NEVER run alembic `upgrade`/`downgrade`/`check`/`current`/`revision --autogenerate` (they connect to / reflect live). The migration is **hand-authored** (zero DB connection); correctness is proven by the testcontainers integration test (Task 5, CI/Docker). Offline-safe: `alembic heads`/`history`/`show`. Unit tests use sqlite.

---

### Task 1: `offer_claims.symbol` column + projection threading

**Files:**
- Modify: `backend_py/src/bfx_funding_bot/modules/execution/event_store/tables.py:65-84`
- Modify: `backend_py/src/bfx_funding_bot/modules/execution/event_store/store.py:128-217` (`_project_offer_claims`, `_upsert_claim`)
- Test: `backend_py/tests/modules/execution/event_store/test_store_append_unit.py`

- [ ] **Step 1: Write the failing test — claim persists its symbol**

Add to `test_store_append_unit.py` (mirror `test_claim_then_release_updates_offer_claims`):

```python
async def test_offer_claims_persists_symbol(sqlite_session: AsyncSession) -> None:
    await _create_all(sqlite_session)
    store = PostgresEventStore(deployment_environment="ci")
    await store.append(sqlite_session, ReservationClaimed(
        cid=820, venue_offer_id="v820", amount=Decimal("100"), symbol="fUSD",
        signal_correlation_id=_SCID, account_id="acct", is_simulated=True,
        occurred_at_ms=1000))
    await sqlite_session.flush()
    row = (await sqlite_session.execute(
        select(OfferClaimRow).where(OfferClaimRow.cid == 820))).scalar_one()
    assert row.symbol == "fUSD"      # the offer's real currency, not a default
```

- [ ] **Step 2: Run it — verify it fails**

Run: `cd backend_py && uv run pytest tests/modules/execution/event_store/test_store_append_unit.py::test_offer_claims_persists_symbol -v`
Expected: FAIL — `TypeError: 'symbol' is an invalid keyword argument for OfferClaimRow` (column missing).

- [ ] **Step 3: Add the `symbol` column to OfferClaimRow**

In `tables.py`, add to `OfferClaimRow` after `venue_offer_id` (before `size_usdt`):

```python
    venue_offer_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    symbol: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'fUST'"))
    size_usdt: Mapped[Decimal] = mapped_column(Numeric, nullable=False)
```

Confirm `text` is imported in `tables.py` (it is). 

- [ ] **Step 4: Thread symbol through `_project_offer_claims` (fail-loud) and `_upsert_claim`**

In `store.py` `_project_offer_claims` (post-P3 body), extract symbol and pass it (raise if absent — all claim-bearing events carry it after P3):

```python
        now_ms: int = _ev.occurred_at_ms or 0
        symbol = getattr(_ev, "symbol", None)
        if symbol is None:
            raise ValueError(f"{etype} reached offer_claims projection without symbol")
        await self._upsert_claim(
            session,
            cid=cid,
            account_id=account_id,
            state=state,
            venue_offer_id=getattr(_ev, "venue_offer_id", None),
            symbol=symbol,
            size_usdt=Decimal(str(_ev.amount)),
            signal_correlation_id=str(_ev.signal_correlation_id),
            occurred_at_ms=now_ms,
            last_updated_ms=now_ms,
        )
```

In `_upsert_claim`, add the `symbol: str` param, write it, and **include it in the conflict update set** (a cid's currency is invariant, but the CLAIMED event reasserts it defensively):

```python
    async def _upsert_claim(
        self,
        session: AsyncSession,
        *,
        cid: int,
        account_id: str,
        state: RegistryState,
        venue_offer_id: str | None,
        symbol: str,
        size_usdt: Decimal,
        signal_correlation_id: str,
        occurred_at_ms: int,
        last_updated_ms: int,
    ) -> None:
        dialect = session.bind.dialect.name if session.bind else "postgresql"
        ins = pg_insert if dialect == "postgresql" else sqlite_insert
        values: dict[str, Any] = {
            "cid": cid,
            "account_id": account_id,
            "deployment_environment": self._env,
            "symbol": symbol,
            "state": state.value,
            "venue_offer_id": venue_offer_id,
            "size_usdt": size_usdt,
            "signal_correlation_id": signal_correlation_id,
            "occurred_at_ms": occurred_at_ms,
            "last_updated_ms": last_updated_ms,
            "last_event_seq": 0,
        }
        stmt = ins(OfferClaimRow).values(values).on_conflict_do_update(
            index_elements=["account_id", "deployment_environment", "cid"],
            set_={k: values[k] for k in ("state", "venue_offer_id", "last_updated_ms", "symbol")},
        )
        await session.execute(stmt)
        cached = session.identity_map.get(
            (OfferClaimRow, (account_id, self._env, cid), None)
        )
        if cached is not None:
            session.expire(cached)
```

- [ ] **Step 5: Run + gate + commit**

Run: `cd backend_py && uv run pytest tests/modules/execution/event_store/test_store_append_unit.py -v && uv run pytest -m "not integration" && uv run mypy src/ && uv run ruff check`
Expected: green.

```bash
git add backend_py/src/bfx_funding_bot/modules/execution/event_store/tables.py \
        backend_py/src/bfx_funding_bot/modules/execution/event_store/store.py \
        backend_py/tests/modules/execution/event_store/test_store_append_unit.py
git commit -m "✨ Feat: offer_claims.symbol column + projection threading (fUSD P2)"
```

---

### Task 2: Alembic migration — add `offer_claims.symbol` (hand-authored)

**Files:**
- Create: `backend_py/alembic/versions/dac1e2f3a4b5_add_symbol_to_offer_claims.py`

> Hand-write the file (no `--autogenerate`). Revision id `dac1e2f3a4b5`; `down_revision` is **P1's** migration `c9d0e1f2a3b4` (linear chain — P1 must be on this branch first). If P1's revision id differs in practice, set `down_revision` to the actual P1 revision so `alembic heads` shows a single head.

- [ ] **Step 1: Write the migration file verbatim**

Create `backend_py/alembic/versions/dac1e2f3a4b5_add_symbol_to_offer_claims.py`:

```python
"""add symbol to offer_claims

Revision ID: dac1e2f3a4b5
Revises: c9d0e1f2a3b4
Create Date: 2026-06-02

"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'dac1e2f3a4b5'
down_revision: str | Sequence[str] | None = 'c9d0e1f2a3b4'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        'offer_claims',
        sa.Column('symbol', sa.Text(), nullable=False, server_default=sa.text("'fUST'")),
    )


def downgrade() -> None:
    op.drop_column('offer_claims', 'symbol')
```

(Keep `server_default='fUST'` permanently — D4; backfills active CLAIMED rows, correct on the fUST-only canary, self-heals on next reconcile.)

- [ ] **Step 2: Verify linear single head (OFFLINE)**

Run: `cd backend_py && uv run alembic heads && uv run alembic history -r c9d0e1f2a3b4:head`
Expected: single head `dac1e2f3a4b5`; history `c9d0e1f2a3b4 -> dac1e2f3a4b5`. (Reads `versions/`; no DB connection. Do NOT run `upgrade`/`check`/`current`.)

- [ ] **Step 3: Verify import + ruff-clean**

Run: `cd backend_py && uv run python -c "import alembic.versions.dac1e2f3a4b5_add_symbol_to_offer_claims as m; print(m.revision, m.down_revision)" && uv run ruff check alembic/versions/dac1e2f3a4b5_add_symbol_to_offer_claims.py`
Expected: prints `dac1e2f3a4b5 c9d0e1f2a3b4`; ruff clean.

- [ ] **Step 4: Commit**

```bash
git add backend_py/alembic/versions/dac1e2f3a4b5_add_symbol_to_offer_claims.py
git commit -m "✨ Feat: migration — offer_claims.symbol (fUSD P2)"
```

> Applied to the live DB only at the coordinated cutover by the operator. Correctness is proven by the Task 5 testcontainers integration test, never by upgrading live.

---

### Task 3: `LocalClaim.symbol` + `ClaimRecord` symbol load

**Files:**
- Modify: `backend_py/src/bfx_funding_bot/modules/execution/boot_recovery.py:78-86` (`LocalClaim`), `:469-484` (`_load_local_claims`)
- Modify: `backend_py/src/bfx_funding_bot/modules/execution/registry_offers.py:222-257` (`from_snapshot`)
- Test: `backend_py/tests/modules/execution/test_boot_recovery.py`, `tests/modules/execution/test_registry_offers*.py`

- [ ] **Step 1: Write the failing test — from_snapshot loads symbol**

Add to the registry test module that covers `from_snapshot` (e.g. `tests/modules/execution/test_registry_offers_snapshot.py`; if none, add to `test_boot_recovery.py` near `_load_local_claims` tests). Mirror the snapshot pattern:

```python
async def test_from_snapshot_loads_symbol(sqlite_session: AsyncSession) -> None:
    await _create_all(sqlite_session)
    sqlite_session.add(OfferClaimRow(
        cid=42, account_id="acct", deployment_environment="ci",
        state="claimed", venue_offer_id="v42", symbol="fUST",
        size_usdt=Decimal("100"),
        signal_correlation_id="11111111-1111-1111-1111-111111111111",
        occurred_at_ms=1000, last_updated_ms=2000, last_event_seq=0))
    await sqlite_session.flush()
    reg = await OfferRegistry.from_snapshot(
        sqlite_session, account_id="acct", deployment_environment="ci")
    assert reg._snapshot["v42"].symbol == "fUST"
```

- [ ] **Step 2: Run it — verify it fails**

Run: `cd backend_py && uv run pytest -k from_snapshot_loads_symbol -v`
Expected: FAIL — `ClaimRecord.symbol` is the dataclass default `"fUSD"`, not the loaded `"fUST"`.

- [ ] **Step 3: Add `symbol` to `LocalClaim` and load it everywhere**

`boot_recovery.py` `LocalClaim` — add the field (kwargs everywhere, so order is safe; append at end):

```python
@dataclass(frozen=True, slots=True)
class LocalClaim:
    cid: int
    venue_offer_id: str | None
    state: RegistryState
    size_usdt: Decimal
    signal_correlation_id: UUID
    occurred_at_ms: int
    symbol: str
```

`boot_recovery.py` `_load_local_claims` — pass `symbol=r.symbol`:

```python
        return [
            LocalClaim(
                cid=r.cid, venue_offer_id=r.venue_offer_id,
                state=RegistryState(r.state), size_usdt=Decimal(str(r.size_usdt)),
                signal_correlation_id=UUID(r.signal_correlation_id),
                occurred_at_ms=r.occurred_at_ms, symbol=r.symbol,
            )
            for r in rows
        ]
```

`registry_offers.py` `from_snapshot` — pass `symbol=r.symbol` to `ClaimRecord`:

```python
            reg._snapshot[r.venue_offer_id] = ClaimRecord(
                venue_offer_id=r.venue_offer_id,
                cid=r.cid,
                signal_correlation_id=UUID(r.signal_correlation_id),
                size_usdt=Decimal(str(r.size_usdt)),
                account_id=r.account_id,
                state=RegistryState(r.state),
                occurred_at_ms=r.occurred_at_ms,
                last_updated_ms=r.last_updated_ms,
                symbol=r.symbol,
            )
```

(The `ClaimRecord.symbol = "fUSD"` dataclass default stays — out of scope; it is now always overridden.)

- [ ] **Step 4: Update any `LocalClaim(...)` test fixtures**

Run: `cd backend_py && grep -rn "LocalClaim(" tests src`
For each positional/kwarg construction that omits `symbol`, add `symbol="fUST"` (the `_claim()` helper in `test_boot_recovery.py` should gain a `symbol: str = "fUST"` kwarg and pass it through).

- [ ] **Step 5: Run + gate + commit**

Run: `cd backend_py && uv run pytest -m "not integration" && uv run mypy src/ && uv run ruff check`
Expected: green.

```bash
git add backend_py/src/bfx_funding_bot/modules/execution/boot_recovery.py \
        backend_py/src/bfx_funding_bot/modules/execution/registry_offers.py \
        backend_py/tests/
git commit -m "✨ Feat: LocalClaim.symbol + ClaimRecord symbol load from offer_claims (fUSD P2)"
```

---

### Task 4: Per-claim recovery symbol + fail-loud (kill the symbols[0] hardcode)

**Files:**
- Modify: `backend_py/src/bfx_funding_bot/modules/execution/boot_recovery.py:111-171` (`compute_recovery_actions`), `:306-317` (the `run()` caller)
- Test: `backend_py/tests/modules/execution/test_boot_recovery.py`

- [ ] **Step 1: Write the failing test — multi-symbol release isolation (the L2 regression)**

Add to `test_boot_recovery.py` (mirror `test_local_claimed_missing_from_venue_is_released`; `_claim()` now takes `symbol`):

```python
def test_missing_release_uses_per_claim_symbol_not_primary():
    """fUSD is configured first (symbols[0]); a CLAIMED fUST offer missing from
    venue must release as fUST, not the primary fUSD."""
    scid = uuid4()
    claim = _claim(cid=42, voi="999", state=RegistryState.CLAIMED, size="80",
                   scid=scid, symbol="fUST")
    acts = compute_recovery_actions(
        venue_offers=[], local_claims=[claim],
        account_id="acct", is_simulated=False, now_ms=1_000,
        grace_ms=0, action_grace_ms=0,
        configured_symbols=frozenset({"fUSD", "fUST"}))
    assert len(acts) == 1
    ev = acts[0]
    assert isinstance(ev, ReservationReleased)
    assert ev.symbol == "fUST"          # per-claim, NOT symbols[0]=="fUSD"


def test_recovery_fails_loud_on_unconfigured_symbol():
    claim = _claim(cid=7, voi="7", state=RegistryState.CLAIMED, size="10",
                   scid=uuid4(), symbol="fXXX")
    with pytest.raises(ValueError):
        compute_recovery_actions(
            venue_offers=[], local_claims=[claim],
            account_id="acct", is_simulated=False, now_ms=1_000,
            grace_ms=0, action_grace_ms=0,
            configured_symbols=frozenset({"fUSD", "fUST"}))
```

- [ ] **Step 2: Run them — verify they fail**

Run: `cd backend_py && uv run pytest tests/modules/execution/test_boot_recovery.py -k "per_claim_symbol or fails_loud" -v`
Expected: FAIL — `compute_recovery_actions` still takes `symbol=` (not `configured_symbols=`) and stamps the global symbol.

- [ ] **Step 3: Make `compute_recovery_actions` per-claim + fail-loud**

In `boot_recovery.py`, change the signature: drop `symbol: str`, add `configured_symbols: frozenset[str]`. Replace the missing-release and stale-FAILED stamps with the claim's own symbol, guarding each with the fail-loud check:

```python
def compute_recovery_actions(
    *,
    venue_offers: list[ActiveFundingOffer],
    local_claims: list[LocalClaim],
    account_id: str,
    is_simulated: bool,
    now_ms: int,
    grace_ms: int,
    action_grace_ms: int = 0,
    configured_symbols: frozenset[str],
) -> list[RecoveryAction]:
    ...
    # missing: local CLAIMED, venue gone -> release (reserved -= size)
    for voi, claim in claimed_by_voi.items():
        if voi in venue_by_voi:
            continue
        if (now_ms - claim.occurred_at_ms) < action_grace_ms:
            continue
        if claim.symbol not in configured_symbols:
            raise ValueError(
                f"recovery release for cid={claim.cid} has symbol={claim.symbol!r} "
                f"not in configured {sorted(configured_symbols)}")
        actions.append(ReservationReleased(
            cid=claim.cid, venue_offer_id=voi, size_usdt=claim.size_usdt,
            reason="missing_from_venue", signal_correlation_id=claim.signal_correlation_id,
            account_id=account_id, is_simulated=is_simulated, occurred_at_ms=now_ms,
            symbol=claim.symbol,
        ))

    # stale PENDING (crash-mid-flight) -> FAILED (capital-neutral)
    for c in local_claims:
        if c.state == RegistryState.PENDING and (now_ms - c.occurred_at_ms) >= grace_ms:
            if c.symbol not in configured_symbols:
                raise ValueError(
                    f"recovery fail for cid={c.cid} has symbol={c.symbol!r} "
                    f"not in configured {sorted(configured_symbols)}")
            actions.append(ReservationFailed(
                cid=c.cid, size_usdt=c.size_usdt,
                signal_correlation_id=c.signal_correlation_id,
                account_id=account_id, is_simulated=is_simulated,
                reason="unresolved_at_boot", occurred_at_ms=now_ms,
                symbol=c.symbol,
            ))

    return actions
```

(The orphan `ReservationClaimed` already uses `symbol=offer.symbol` from the venue — unchanged; the venue is only queried per configured symbol in `run()`, so it is configured by construction.)

- [ ] **Step 4: Update the `run()` caller**

In `boot_recovery.py` (~306-317), drop `symbol=self._symbols[0]`, pass the configured set, and refresh the comment:

```python
            local_claims = await self._load_local_claims(session)
            # GLOBAL FSM diff against the union of all symbols' venue offers
            # (venue_offer_id is globally unique). Releases/fails now use each
            # claim's own symbol (offer_claims.symbol); fail-loud if a claim's
            # symbol is not configured.
            actions = compute_recovery_actions(
                venue_offers=all_offers, local_claims=local_claims,
                account_id=self._ctx.account_id, is_simulated=self._is_simulated,
                now_ms=now_ms, grace_ms=self._grace_ms,
                action_grace_ms=self._action_grace_ms,
                configured_symbols=frozenset(self._symbols),
            )
```

- [ ] **Step 5: Update remaining `compute_recovery_actions` call sites/helpers**

Run: `cd backend_py && grep -rn "compute_recovery_actions" tests src`
Update every call (notably the `_actions()` helper in `test_boot_recovery.py`) to pass `configured_symbols=frozenset({...})` instead of `symbol=`. Ensure helper-built claims carry a configured symbol.

- [ ] **Step 6: Run + gate + commit**

Run: `cd backend_py && uv run pytest tests/modules/execution/test_boot_recovery.py -v && uv run pytest -m "not integration" && uv run mypy src/ && uv run ruff check`
Expected: green; the L2 regression + fail-loud tests pass.

```bash
git add backend_py/src/bfx_funding_bot/modules/execution/boot_recovery.py \
        backend_py/tests/modules/execution/test_boot_recovery.py
git commit -m "✨ Feat: per-claim recovery symbol + fail-loud; kill symbols[0] hardcode (fUSD P2)"
```

---

### Task 5: Confirm all three release paths + migration integration test

**Files:**
- Test: `backend_py/tests/modules/execution/` (release-path coverage), `backend_py/tests/integration/test_migration_offer_claims_symbol.py`

- [ ] **Step 1: Assert the two already-correct release paths stay symbol-correct**

Add focused tests (or assertions to existing tests) that:
- `fill_tracker` emits `ReservationReleased` with `symbol=claim.symbol` (registry claim's symbol) for an offer that disappears.
- `ws_dispatcher` emits `ReservationReleased` with `symbol=foc.symbol` for a foc CANCELED/EXPIRED frame.

Mirror the existing fill_tracker / ws_dispatcher test fixtures; assert `released.symbol == "<the claim/foc symbol>"`. (These paths already pass — the tests lock the behaviour so a future refactor can't regress them.)

- [ ] **Step 2: Run them**

Run: `cd backend_py && uv run pytest tests/ -k "fill_tracker or ws_dispatcher" -m "not integration" -v`
Expected: PASS.

- [ ] **Step 3: Migration integration test (mirror P1 Task 3 / test_migration_per_symbol_pk.py)**

Create `tests/integration/test_migration_offer_claims_symbol.py`:

```python
import pytest

from tests.integration.test_migration_per_symbol_pk import _ALEMBIC_INI

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_offer_claims_has_symbol_after_upgrade(pg_engine, monkeypatch) -> None:
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
            cols = {c["name"]: c for c in inspect(verify_conn).get_columns("offer_claims")}
    finally:
        verify_eng.dispose()

    assert "symbol" in cols
    assert cols["symbol"]["nullable"] is False
```

- [ ] **Step 4: Run integration (Docker — CI/cutover) + final gate + commit**

Run: `cd backend_py && uv run pytest tests/integration/test_migration_offer_claims_symbol.py -v -m integration` (Docker required; skipped on this machine).
Run: `cd backend_py && uv run pytest -m "not integration" && uv run mypy src/ && uv run ruff check`
Expected: unit gate green.

```bash
git add backend_py/tests/
git commit -m "✅ Test: release-path symbol coverage + offer_claims.symbol migration integration (fUSD P2)"
```

---

## Self-Review

- **Spec coverage (§6.3):** column (Task 1 Step 3) + migration (Task 2); `_project_offer_claims`/`_upsert_claim` threading incl. conflict-set reassert (Task 1 Step 4); `LocalClaim.symbol` + `_load_local_claims` + `from_snapshot` (Task 3); `compute_recovery_actions` per-claim + fail-loud + caller (Task 4, kills `symbols[0]` — §10 riskiest); all-three-release-path coverage + integration test (Task 5). ✓
- **Acceptance:** L2 multi-symbol release isolation regression (Task 4 Step 1); fail-loud on ∉-configured (D7); fUST byte-identical — single-symbol release still stamps fUST (Task 4 test with one symbol path unchanged).
- **No placeholders:** all steps carry real code/commands. Step 1 of Task 5 is described against existing fixtures (no invented signatures) since the paths already pass; lock them with asserts.
- **Type consistency:** `symbol: str` on `OfferClaimRow`/`LocalClaim`/`ClaimRecord`; `_upsert_claim(symbol: str)`; `compute_recovery_actions(configured_symbols: frozenset[str])` ↔ caller `frozenset(self._symbols)`. `_ev.amount` read matches the P3 canonical field.
- **Ordering:** migration `down_revision` = P1's revision (Task 2 Step 1); P3 must be merged first (Task 1 reads `_ev.symbol`/`_ev.amount`; Task 4 replaces P3's global-param FAILED stamp).
