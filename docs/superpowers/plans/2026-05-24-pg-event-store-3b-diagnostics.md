# 3b — Diagnostics Sink (forensic → PG) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stand up a `diagnostics` table + `DiagnosticsSink` and clean-switch the three forensic event kinds (DECISION / SAFETY_TRIGGER / CANCEL_AUDIT) off Axiom onto Postgres, best-effort, never blocking trading.

**Architecture:** `DiagnosticsSink` is a drop-in for the `AxiomClient` `emit(dict)` port. `emit()` leniently extracts `event_type`/`account_id`/`timestamp`/`payload`, maps `event_type → DiagnosticKind`, drops non-forensic types, and persists one row via a shared best-effort `_insert()` that opens its **own** txn (never the command txn). Cancel domain events arrive via bus subscribers that build a payload dict and call the same `_insert()`. Operational events (HEALTH_CHECK/SIGNAL/ORDER_SUBMIT) stay on Axiom for now — Axiom removal + stdout repoint is 3c.

**Tech Stack:** Python 3.13, SQLAlchemy 2.0 async, Alembic, pytest (+ testcontainers for PG), sqlite for unit tests.

**Design source:** `docs/superpowers/specs/2026-05-23-postgres-event-store-sot-migration-design.md` §4 + §13.1 item 9.

---

## File Structure

**Create:**
- `src/bfx_funding_bot/modules/execution/diagnostics/__init__.py` — empty package marker
- `src/bfx_funding_bot/modules/execution/diagnostics/tables.py` — `DiagnosticsRow` ORM (sqlite-safe JSONB variant)
- `src/bfx_funding_bot/modules/execution/diagnostics/sink.py` — `DiagnosticKind`, `DiagnosticsSink`, `NoopDiagnosticsSink`
- `alembic/versions/d8e3f1a2b4c6_add_diagnostics_table.py` — migration (down_revision `c7d1e2f3a4b5`)
- `tests/modules/execution/diagnostics/__init__.py`
- `tests/modules/execution/diagnostics/test_tables_metadata.py`
- `tests/modules/execution/diagnostics/test_sink_unit.py`
- `tests/integration/test_pg_diagnostics.py`

**Modify:**
- `tests/conftest.py:61-64` — register diagnostics tables in `pg_engine` fixture
- `src/bfx_funding_bot/modules/marketfeed/daemon.py` — construct sink, Daemon dataclass field, cancel subscription swap, SignalEngine + SafetyGuardChain wiring
- `src/bfx_funding_bot/modules/marketfeed/signal_engine.py` — inject `diagnostics`, repoint `_emit_decision_final`
- `src/bfx_funding_bot/modules/execution/emit.py` — `emit_safety_trigger` axiom→diagnostics
- `src/bfx_funding_bot/modules/execution/safety/chain.py` — axiom→diagnostics
- `src/bfx_funding_bot/modules/marketfeed/daemon_smoke_boot.py:48` — axiom→diagnostics
- `tests/modules/marketfeed/conftest.py` — `capture_engine`/`capture_engine_blocked` add diagnostics fake
- `tests/modules/marketfeed/test_signal_engine.py` — DECISION assertions axiom→diagnostics
- `tests/modules/marketfeed/test_signal_engine_phase42.py` — DECISION assertions axiom→diagnostics
- `tests/modules/execution/safety/test_chain.py` — SAFETY_TRIGGER assertion axiom→diagnostics
- `tests/modules/admin/test_boot_smoke_integration.py` — SAFETY_TRIGGER assertion axiom→diagnostics
- `tests/integration/test_daemon_locf_lifecycle.py:143` — SignalEngine ctor add diagnostics

**Out of scope (→ 3c):** delete AxiomClient/Config/adapter; HEALTH_CHECK/boot-lifecycle/SIGNAL → structured stdout; L3 smoke → PG read-your-writes; remove `AxiomEventSink` redundant SoT emits; env/CI cleanup; decouple `deployment_environment` from `AxiomConfig`. `AxiomEventSink` class is left untouched in 3b (only its cancel *subscription* is removed).

---

## Task 1: `diagnostics` table (ORM, sqlite-safe)

**Files:**
- Create: `src/bfx_funding_bot/modules/execution/diagnostics/__init__.py`
- Create: `src/bfx_funding_bot/modules/execution/diagnostics/tables.py`
- Create: `tests/modules/execution/diagnostics/__init__.py`
- Test: `tests/modules/execution/diagnostics/test_tables_metadata.py`

- [ ] **Step 1: Write the failing metadata test**

Create `tests/modules/execution/diagnostics/__init__.py` (empty) and `tests/modules/execution/diagnostics/test_tables_metadata.py`:

```python
import bfx_funding_bot.modules.execution.diagnostics.tables  # noqa: F401  (register)
from bfx_funding_bot.core.db import Base


def test_diagnostics_table_registered() -> None:
    assert "diagnostics" in Base.metadata.tables


def test_diagnostics_columns() -> None:
    cols = Base.metadata.tables["diagnostics"].columns
    for name in ("id", "account_id", "deployment_environment", "kind",
                 "payload", "occurred_at", "recorded_at"):
        assert name in cols, name
    assert cols["id"].primary_key is True


def test_diagnostics_index_present() -> None:
    idx_names = {i.name for i in Base.metadata.tables["diagnostics"].indexes}
    assert "idx_diagnostics_acct_occurred" in idx_names
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend_py && uv run pytest tests/modules/execution/diagnostics/test_tables_metadata.py -q`
Expected: FAIL — `ModuleNotFoundError: ...diagnostics.tables`

- [ ] **Step 3: Create the package + ORM table**

Create `src/bfx_funding_bot/modules/execution/diagnostics/__init__.py` (empty file).

Create `src/bfx_funding_bot/modules/execution/diagnostics/tables.py`:

```python
from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    DateTime,
    Index,
    Integer,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from bfx_funding_bot.core.db import Base

# JSONB on Postgres, generic JSON on sqlite (unit tests). Mirrors
# event_store/tables.py — a bare JSONB column poisons Base.metadata.create_all
# on sqlite (global metadata, surfaces only in test ordering).
_JSON = JSON().with_variant(JSONB, "postgresql")
# now() on Postgres, CURRENT_TIMESTAMP on sqlite. ANSI, works on both.
_NOW = func.current_timestamp()
# SQLite requires INTEGER (not BIGINT) for autoincrement PKs.
_BIG_PK = BigInteger().with_variant(Integer(), "sqlite")


class DiagnosticsRow(Base):
    """Forensic/audit records (DECISION / SAFETY_TRIGGER / CANCEL_AUDIT).

    Non-SoT, prunable (30-90d). Best-effort writes — a failure here NEVER
    blocks trading (spec §240-241). SoT lives in event_log, not here.
    """

    __tablename__ = "diagnostics"

    id: Mapped[int] = mapped_column(_BIG_PK, primary_key=True, autoincrement=True)
    account_id: Mapped[str] = mapped_column(Text, nullable=False)
    deployment_environment: Mapped[str] = mapped_column(Text, nullable=False)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(_JSON, nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=_NOW
    )

    __table_args__ = (
        Index("idx_diagnostics_acct_occurred", "account_id", "occurred_at"),
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend_py && uv run pytest tests/modules/execution/diagnostics/test_tables_metadata.py -q`
Expected: PASS (3 passed)

- [ ] **Step 5: Verify no sqlite metadata-pollution regression**

Run: `cd backend_py && uv run pytest -m "not integration" -q`
Expected: PASS (whole unit suite still green — the JSON variant must not break `Base.metadata.create_all`)

- [ ] **Step 6: Commit**

```bash
cd /Users/will/second-brain/projects/startup/bfx-funding-bot
git add backend_py/src/bfx_funding_bot/modules/execution/diagnostics/ backend_py/tests/modules/execution/diagnostics/
git commit -m "✅ Feat: diagnostics ORM table (sqlite-safe JSONB variant) (3b)"
```

---

## Task 2: Alembic migration for `diagnostics`

**Files:**
- Create: `alembic/versions/d8e3f1a2b4c6_add_diagnostics_table.py`

- [ ] **Step 1: Confirm current migration head**

Run: `cd backend_py && uv run alembic heads`
Expected: shows `c7d1e2f3a4b5 (head)`

- [ ] **Step 2: Hand-write the migration**

Create `backend_py/alembic/versions/d8e3f1a2b4c6_add_diagnostics_table.py` (mirrors the event_store migration — migrations run only on Postgres, so use `postgresql.JSONB` directly):

```python
"""add diagnostics table

Revision ID: d8e3f1a2b4c6
Revises: c7d1e2f3a4b5
Create Date: 2026-05-24 00:00:00.000000

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "d8e3f1a2b4c6"
down_revision: str | Sequence[str] | None = "c7d1e2f3a4b5"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "diagnostics",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("account_id", sa.Text(), nullable=False),
        sa.Column("deployment_environment", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "recorded_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.current_timestamp(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "idx_diagnostics_acct_occurred",
        "diagnostics",
        ["account_id", "occurred_at"],
    )


def downgrade() -> None:
    op.drop_index("idx_diagnostics_acct_occurred", table_name="diagnostics")
    op.drop_table("diagnostics")
```

- [ ] **Step 3: Validate migration on an ephemeral Postgres (no Neon, no MCP)**

```bash
docker run -d --rm --name bfx-diag-pg -e POSTGRES_PASSWORD=pg -p 55432:5432 postgres:16-alpine
sleep 3
cd backend_py && DATABASE_URL='postgresql://postgres:pg@localhost:55432/postgres' uv run alembic upgrade head
```
Expected: ends at `d8e3f1a2b4c6`, no error.

- [ ] **Step 4: Confirm no metadata drift (ORM ↔ migration agree)**

Run: `cd backend_py && DATABASE_URL='postgresql://postgres:pg@localhost:55432/postgres' uv run alembic check`
Expected: `No new upgrade operations detected.`

Then tear down: `docker stop bfx-diag-pg`

> If Docker is unavailable in the execution environment, mark steps 3-4 as a pre-merge gate and note it in the commit body. Do NOT run against Neon.

- [ ] **Step 5: Commit**

```bash
cd /Users/will/second-brain/projects/startup/bfx-funding-bot
git add backend_py/alembic/versions/d8e3f1a2b4c6_add_diagnostics_table.py
git commit -m "✅ Feat: alembic migration for diagnostics table (3b)"
```

---

## Task 3: `DiagnosticsSink` (emit + cancel handlers + best-effort `_insert`)

**Files:**
- Create: `src/bfx_funding_bot/modules/execution/diagnostics/sink.py`
- Test: `tests/modules/execution/diagnostics/test_sink_unit.py`

- [ ] **Step 1: Write the failing unit tests**

Create `tests/modules/execution/diagnostics/test_sink_unit.py`:

```python
from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

import bfx_funding_bot.modules.execution.diagnostics.tables  # noqa: F401
from bfx_funding_bot.core.db import Base
from bfx_funding_bot.modules.execution.diagnostics.sink import (
    DiagnosticsSink,
    NoopDiagnosticsSink,
)
from bfx_funding_bot.modules.execution.diagnostics.tables import DiagnosticsRow
from bfx_funding_bot.modules.execution.events import CancelAcknowledged, CancelRequested

_SCID = uuid4()


@pytest_asyncio.fixture
async def diag_factory():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


def _decision_event() -> dict:
    return {
        "timestamp": datetime.now(UTC).isoformat(),
        "level": "info",
        "phase": "paper",
        "strategy": "rate_percentile",
        "cell": "bfx_USDT",
        "event_type": "decision",
        "correlation_id": str(_SCID),
        "account_id": "acct",
        "payload": {"decision_outcome": "skip", "signal_correlation_id": str(_SCID),
                    "skip_reason": "below_threshold"},
    }


async def _rows(factory) -> list[DiagnosticsRow]:
    async with factory() as s:
        return list((await s.execute(select(DiagnosticsRow))).scalars().all())


async def test_emit_decision_persists_row(diag_factory) -> None:
    sink = DiagnosticsSink(session_factory=diag_factory, deployment_environment="ci")
    await sink.emit(_decision_event())
    rows = await _rows(diag_factory)
    assert len(rows) == 1
    assert rows[0].kind == "decision"
    assert rows[0].account_id == "acct"
    assert rows[0].deployment_environment == "ci"
    assert rows[0].payload["payload"]["skip_reason"] == "below_threshold"


async def test_emit_safety_persists_row(diag_factory) -> None:
    sink = DiagnosticsSink(session_factory=diag_factory, deployment_environment="ci")
    ev = _decision_event()
    ev["event_type"] = "safety_trigger"
    ev["payload"] = {"guard_name": "cap", "reason": "over_cap", "decision_snapshot": {}}
    await sink.emit(ev)
    rows = await _rows(diag_factory)
    assert len(rows) == 1 and rows[0].kind == "safety_trigger"


async def test_emit_safety_with_none_strategy_persists(diag_factory) -> None:
    """Regression: smoke_boot SAFETY_TRIGGER legitimately has strategy=None/cell=None.
    The sink must NOT full-validate Envelope (that rule would drop this)."""
    sink = DiagnosticsSink(session_factory=diag_factory, deployment_environment="ci")
    ev = _decision_event()
    ev["event_type"] = "safety_trigger"
    ev["strategy"] = None
    ev["cell"] = None
    ev["payload"] = {"check_target": "smoke_boot", "status": "degraded", "error_message": "x"}
    await sink.emit(ev)
    rows = await _rows(diag_factory)
    assert len(rows) == 1 and rows[0].kind == "safety_trigger"


@pytest.mark.parametrize("etype", ["signal", "health_check", "order_submit", "order_fill"])
async def test_emit_non_forensic_dropped(diag_factory, etype) -> None:
    sink = DiagnosticsSink(session_factory=diag_factory, deployment_environment="ci")
    ev = _decision_event()
    ev["event_type"] = etype
    await sink.emit(ev)
    assert await _rows(diag_factory) == []


async def test_emit_unknown_event_type_dropped(diag_factory) -> None:
    sink = DiagnosticsSink(session_factory=diag_factory, deployment_environment="ci")
    ev = _decision_event()
    ev["event_type"] = "totally_bogus"
    await sink.emit(ev)  # must not raise
    assert await _rows(diag_factory) == []


async def test_emit_best_effort_swallows_db_error(caplog) -> None:
    """A persistence failure must be swallowed + logged, never raised."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    factory = async_sessionmaker(engine, expire_on_commit=False)  # NO create_all -> no table
    sink = DiagnosticsSink(session_factory=factory, deployment_environment="ci")
    await sink.emit(_decision_event())  # must not raise
    assert any("diagnostics_persist_failed" in r.message for r in caplog.records)
    await engine.dispose()


async def test_handle_cancel_requested_persists(diag_factory) -> None:
    sink = DiagnosticsSink(session_factory=diag_factory, deployment_environment="ci")
    await sink.handle_cancel_requested(CancelRequested(
        venue_offer_id="v1", requested_at_ms=1000, signal_correlation_id=_SCID,
        account_id="acct"))
    rows = await _rows(diag_factory)
    assert len(rows) == 1
    assert rows[0].kind == "cancel_audit"
    assert rows[0].payload["venue_offer_id"] == "v1"


async def test_handle_cancel_acknowledged_persists(diag_factory) -> None:
    sink = DiagnosticsSink(session_factory=diag_factory, deployment_environment="ci")
    await sink.handle_cancel_acknowledged(CancelAcknowledged(
        venue_offer_id="v1", acknowledged_at_ms=2000, signal_correlation_id=_SCID,
        account_id="acct", rest_status="ok", venue_response_text="done"))
    rows = await _rows(diag_factory)
    assert len(rows) == 1 and rows[0].kind == "cancel_audit"


async def test_noop_sink_does_nothing(diag_factory) -> None:
    sink = NoopDiagnosticsSink()
    await sink.emit(_decision_event())
    await sink.handle_cancel_requested(CancelRequested(
        venue_offer_id="v1", requested_at_ms=1, signal_correlation_id=_SCID, account_id="a"))
    assert await _rows(diag_factory) == []
```

> Before writing the sink, confirm `CancelAcknowledged`'s exact field names (`rest_status`, `venue_response_text`) by reading `src/bfx_funding_bot/modules/execution/events.py` around line 141. They mirror the fields `axiom_sink.handle_cancel_acknowledged` reads (`axiom_sink.py:117-121`).

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd backend_py && uv run pytest tests/modules/execution/diagnostics/test_sink_unit.py -q`
Expected: FAIL — `ModuleNotFoundError: ...diagnostics.sink`

- [ ] **Step 3: Implement the sink**

Create `src/bfx_funding_bot/modules/execution/diagnostics/sink.py`:

```python
"""DiagnosticsSink — forensic events (DECISION / SAFETY_TRIGGER / CANCEL_AUDIT)
to the `diagnostics` table.

Drop-in for the AxiomClient `emit(dict)` port. Best-effort: a persistence
failure is swallowed + logged to stdout and NEVER propagates — forensic writes
must not block trading (spec §240-241). Each write opens its OWN txn via
session_scope (never the command txn). Non-forensic / unknown event types are
silently dropped — operational telemetry stays on stdout/Axiom (3c).
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bfx_funding_bot.core.db import session_scope
from bfx_funding_bot.modules.execution.diagnostics.tables import DiagnosticsRow
from bfx_funding_bot.modules.execution.events import (
    CancelAcknowledged,
    CancelRequested,
)
from bfx_funding_bot.modules.marketfeed.schemas import EventType

log = logging.getLogger(__name__)


class DiagnosticKind(StrEnum):
    DECISION = "decision"
    SAFETY_TRIGGER = "safety_trigger"
    CANCEL_AUDIT = "cancel_audit"


# Single source of truth for "what is forensic". Anything not here is dropped.
_KIND_BY_EVENT_TYPE: dict[str, DiagnosticKind] = {
    EventType.DECISION.value: DiagnosticKind.DECISION,
    EventType.SAFETY_TRIGGER.value: DiagnosticKind.SAFETY_TRIGGER,
}


def _occurred_at(event: dict[str, Any]) -> datetime:
    ts = event.get("timestamp")
    if isinstance(ts, str):
        try:
            return datetime.fromisoformat(ts)
        except ValueError:
            pass
    return datetime.now(UTC)


class DiagnosticsSink:
    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        deployment_environment: str,
    ) -> None:
        self._sf = session_factory
        self._env = deployment_environment

    async def emit(self, event: dict[str, Any]) -> None:
        """Drop-in for AxiomClient.emit. Persists only forensic kinds; lenient
        field extraction (NO full Envelope validation — its strategy/cell rule
        over-constrains forensic storage, e.g. smoke_boot SAFETY_TRIGGER)."""
        kind = _KIND_BY_EVENT_TYPE.get(str(event.get("event_type")))
        if kind is None:
            return  # operational / unknown — not forensic, dropped
        await self._insert(
            kind=kind,
            account_id=str(event.get("account_id", "default")),
            payload=event,
            occurred_at=_occurred_at(event),
        )

    async def handle_cancel_requested(self, event: CancelRequested) -> None:
        await self._insert(
            kind=DiagnosticKind.CANCEL_AUDIT,
            account_id=event.account_id,
            payload={
                "event_type": EventType.CANCEL_REQUESTED.value,
                "venue_offer_id": event.venue_offer_id,
                "requested_at_ms": event.requested_at_ms,
                "signal_correlation_id": str(event.signal_correlation_id),
            },
            occurred_at=datetime.fromtimestamp(event.requested_at_ms / 1000, tz=UTC),
        )

    async def handle_cancel_acknowledged(self, event: CancelAcknowledged) -> None:
        await self._insert(
            kind=DiagnosticKind.CANCEL_AUDIT,
            account_id=event.account_id,
            payload={
                "event_type": EventType.CANCEL_ACKNOWLEDGED.value,
                "venue_offer_id": event.venue_offer_id,
                "acknowledged_at_ms": event.acknowledged_at_ms,
                "rest_status": event.rest_status,
                "venue_response_text": event.venue_response_text,
                "signal_correlation_id": str(event.signal_correlation_id),
            },
            occurred_at=datetime.fromtimestamp(event.acknowledged_at_ms / 1000, tz=UTC),
        )

    async def _insert(
        self,
        *,
        kind: DiagnosticKind,
        account_id: str,
        payload: dict[str, Any],
        occurred_at: datetime,
    ) -> None:
        try:
            async with session_scope(self._sf) as session:
                session.add(DiagnosticsRow(
                    account_id=account_id,
                    deployment_environment=self._env,
                    kind=kind.value,
                    payload=payload,
                    occurred_at=occurred_at,
                ))
        except Exception:
            log.warning(
                "diagnostics_persist_failed kind=%s account_id=%s",
                kind.value, account_id, exc_info=True,
            )


class NoopDiagnosticsSink:
    """Null object — chain tests / contexts that don't exercise diagnostics.
    Mirrors NoopEventPersister (event_store/persister.py)."""

    async def emit(self, event: dict[str, Any]) -> None:
        return None

    async def handle_cancel_requested(self, event: CancelRequested) -> None:
        return None

    async def handle_cancel_acknowledged(self, event: CancelAcknowledged) -> None:
        return None
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd backend_py && uv run pytest tests/modules/execution/diagnostics/test_sink_unit.py -q`
Expected: PASS (all)

- [ ] **Step 5: Type + lint**

Run: `cd backend_py && uv run mypy src/bfx_funding_bot/modules/execution/diagnostics/ && uv run ruff check src/bfx_funding_bot/modules/execution/diagnostics/`
Expected: clean

- [ ] **Step 6: Commit**

```bash
cd /Users/will/second-brain/projects/startup/bfx-funding-bot
git add backend_py/src/bfx_funding_bot/modules/execution/diagnostics/sink.py backend_py/tests/modules/execution/diagnostics/test_sink_unit.py
git commit -m "✅ Feat: DiagnosticsSink emit/cancel handlers, best-effort PG insert (3b)"
```

---

## Task 4: Integration test — real Postgres round-trip

**Files:**
- Modify: `tests/conftest.py:61-64` (register diagnostics tables in `pg_engine`)
- Test: `tests/integration/test_pg_diagnostics.py`

- [ ] **Step 1: Register diagnostics tables in the pg_engine fixture**

In `backend_py/tests/conftest.py`, the `pg_engine` fixture imports table modules so `Base.metadata.create_all` sees them. Add the diagnostics import to that block (currently lines 61-64):

```python
    import bfx_funding_bot.modules.accounts.tables
    import bfx_funding_bot.modules.candles.tables
    import bfx_funding_bot.modules.execution.diagnostics.tables  # noqa: F401
    import bfx_funding_bot.modules.execution.event_store.tables
    import bfx_funding_bot.modules.funding_stats.tables  # noqa: F401
```

- [ ] **Step 2: Write the integration test**

Create `tests/integration/test_pg_diagnostics.py`:

```python
"""Integration tests for DiagnosticsSink — real Postgres round-trip.

Requires testcontainers. Run with:
    cd backend_py && uv run pytest tests/integration/test_pg_diagnostics.py -q -m integration
"""
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import select

from bfx_funding_bot.modules.execution.diagnostics.sink import DiagnosticsSink
from bfx_funding_bot.modules.execution.diagnostics.tables import DiagnosticsRow

pytestmark = pytest.mark.integration


async def test_emit_decision_round_trip(pg_session_factory) -> None:
    acct = f"acct-{uuid4().hex[:8]}"  # session-scoped container -> unique per test
    sink = DiagnosticsSink(session_factory=pg_session_factory, deployment_environment="ci")
    scid = str(uuid4())
    await sink.emit({
        "timestamp": datetime.now(UTC).isoformat(),
        "level": "info", "phase": "paper", "strategy": "rate_percentile",
        "cell": "bfx_USDT", "event_type": "decision",
        "correlation_id": scid, "account_id": acct,
        "payload": {"decision_outcome": "post", "signal_correlation_id": scid,
                    "offer_rate": 0.0001, "offer_amount_usdt": 100.0, "offer_duration_days": 2},
    })
    async with pg_session_factory() as s:
        rows = list((await s.execute(
            select(DiagnosticsRow).where(DiagnosticsRow.account_id == acct))).scalars().all())
    assert len(rows) == 1
    assert rows[0].kind == "decision"
    assert rows[0].payload["payload"]["decision_outcome"] == "post"
    assert rows[0].recorded_at is not None
```

- [ ] **Step 3: Run the integration test (needs Docker)**

Run: `cd backend_py && uv run pytest tests/integration/test_pg_diagnostics.py -q -m integration`
Expected: PASS (1 passed). If Docker unavailable, the testcontainers fixture skips — note that in the commit body.

- [ ] **Step 4: Commit**

```bash
cd /Users/will/second-brain/projects/startup/bfx-funding-bot
git add backend_py/tests/conftest.py backend_py/tests/integration/test_pg_diagnostics.py
git commit -m "✅ Test: DiagnosticsSink PG round-trip integration test (3b)"
```

---

## Task 5: Wire `DiagnosticsSink` in `build_daemon` + swap cancel subscription

**Files:**
- Modify: `src/bfx_funding_bot/modules/marketfeed/daemon.py`

This task constructs the sink, adds it to the `Daemon` dataclass, and routes cancel events to it (cancel becomes the first production consumer). DECISION/SAFETY wiring is Tasks 6-8.

- [ ] **Step 1: Import the sink**

In `daemon.py`, near the event_store imports (around line 61), add:

```python
from bfx_funding_bot.modules.execution.diagnostics.sink import DiagnosticsSink
```

- [ ] **Step 2: Add `diagnostics` field to the `Daemon` dataclass**

In the `Daemon` dataclass (line 148), add the field right after `axiom: AxiomClient` (line 152) — it has no default, so it must stay in the non-default block (before `smoke_runner` at line 168):

```python
    axiom: AxiomClient
    diagnostics: DiagnosticsSink
    probe: HealthProbe
```

- [ ] **Step 3: Construct the sink in build_daemon**

In `build_daemon`, right after `persister = EventStorePersister(...)` (line 697), add:

```python
    diagnostics = DiagnosticsSink(
        session_factory=session_factory, deployment_environment=env_str,
    )
```

- [ ] **Step 4: Swap cancel subscriptions axiom_sink → diagnostics**

Replace the cancel-subscription block (lines 836-840):

```python
    # CancelRequested → axiom (audit trail; replay-able cancel decisions).
    bus.subscribe(CancelRequested,     axiom_sink.handle_cancel_requested)
    # Phase 4.4b D1: CancelAcknowledged → axiom (cancel ACK from BFX WS or
    # REST path; pairs with CancelRequested for cancel-lifecycle audit).
    bus.subscribe(CancelAcknowledged,  axiom_sink.handle_cancel_acknowledged)
```

with:

```python
    # 3b: cancel lifecycle → diagnostics (CANCEL_AUDIT). Forensic, best-effort.
    # (axiom_sink keeps its redundant SoT emits until 3c.)
    bus.subscribe(CancelRequested,     diagnostics.handle_cancel_requested)
    bus.subscribe(CancelAcknowledged,  diagnostics.handle_cancel_acknowledged)
```

- [ ] **Step 5: Pass `diagnostics` into the `Daemon(...)` construction**

In the `return Daemon(...)` block (line 1049), add after `axiom=axiom,` (line 1053):

```python
        axiom=axiom,
        diagnostics=diagnostics,
```

- [ ] **Step 6: Verify daemon construction + full unit suite**

Run: `cd backend_py && uv run pytest -m "not integration" -q`
Expected: PASS. (`phase4_4a` cancel tests compose their own bus and are unaffected.)

Run: `cd backend_py && uv run mypy src/bfx_funding_bot/modules/marketfeed/daemon.py`
Expected: clean

- [ ] **Step 7: Commit**

```bash
cd /Users/will/second-brain/projects/startup/bfx-funding-bot
git add backend_py/src/bfx_funding_bot/modules/marketfeed/daemon.py
git commit -m "✅ Feat: wire DiagnosticsSink in build_daemon; cancel → diagnostics (3b)"
```

---

## Task 6: Repoint DECISION (signal_engine) → diagnostics

**Files:**
- Modify: `src/bfx_funding_bot/modules/marketfeed/signal_engine.py`
- Modify: `src/bfx_funding_bot/modules/marketfeed/daemon.py` (SignalEngine construction)
- Modify: `tests/modules/marketfeed/conftest.py`
- Modify: `tests/modules/marketfeed/test_signal_engine.py`
- Modify: `tests/modules/marketfeed/test_signal_engine_phase42.py`
- Modify: `tests/integration/test_daemon_locf_lifecycle.py`

`diagnostics` is a **required** keyword param on `SignalEngine` (explicit DI). Every construction site is updated in this one commit so the suite stays green.

- [ ] **Step 1: Update the failing tests first (TDD)**

In `tests/modules/marketfeed/conftest.py`, update both fixtures to inject a diagnostics fake and return it. `_CaptureAxiom` (line 28) already implements the `emit(dict)` port — reuse it. Change `capture_engine` (lines 114-126):

```python
@pytest.fixture
def capture_engine() -> tuple:
    """Engine wired with _AllowChain — safety eval passes, executor.submit called."""
    axiom = _CaptureAxiom()
    diagnostics = _CaptureAxiom()
    chain = _AllowChain()
    executor = _SpyExecutor()
    cell = _cell()
    engine = SignalEngine(
        phase=Phase.PAPER, axiom=axiom, diagnostics=diagnostics,
        candles_repo=_StubCandlesRepo(),
        safety_chain=chain, executor=executor, account_ctx=_ctx(),
    )
    registry = _make_registry(cell)
    return engine, axiom, diagnostics, executor, chain, cell, _candle(), registry
```

Apply the identical change to `capture_engine_blocked` (lines 129-141): add `diagnostics = _CaptureAxiom()`, pass `diagnostics=diagnostics` to `SignalEngine`, and return `engine, axiom, diagnostics, executor, chain, cell, _candle(), registry`.

In `tests/modules/marketfeed/test_signal_engine_phase42.py`, update the two tests' unpacking + assertions:

```python
# test_decision_emitted_once_when_allowed_then_executor_called (line ~26)
    engine, axiom, diagnostics, executor, _chain, cell, candle, registry = capture_engine
    ...
    decisions = [e for e in diagnostics.events if e["event_type"] == EventType.DECISION.value]
    assert len(decisions) == 1
    assert decisions[0]["payload"]["decision_outcome"] == DecisionOutcome.POST.value
```

```python
# test_decision_emitted_once_when_blocked_executor_not_called (line ~38)
    engine, axiom, diagnostics, executor, _chain, cell, candle, registry = capture_engine_blocked
    ...
    decisions = [e for e in diagnostics.events if e["event_type"] == EventType.DECISION.value]
    assert len(decisions) == 1
    assert decisions[0]["payload"]["decision_outcome"] == DecisionOutcome.SKIP.value
    assert decisions[0]["payload"]["skip_reason"] == SkipReason.SAFETY_BLOCK.value
```

In `tests/modules/marketfeed/test_signal_engine.py`, DECISION now lands on a separate diagnostics fake. Update the three tests:

`test_signal_engine_emits_signal_and_decision` (lines 40-60):
```python
async def test_signal_engine_emits_signal_and_decision():
    captured: list[dict] = []
    decisions: list[dict] = []
    axiom = MagicMock()
    axiom.emit = AsyncMock(side_effect=lambda e: captured.append(e))
    diagnostics = MagicMock()
    diagnostics.emit = AsyncMock(side_effect=lambda e: decisions.append(e))

    candles_repo = MagicMock()
    candles_repo.get_up_to = AsyncMock(return_value=_history(8))

    engine = SignalEngine(phase=Phase.PAPER, axiom=axiom, diagnostics=diagnostics,
                          candles_repo=candles_repo)
    cell = _cell()
    reg = StrategyRegistry()
    strategy = build_strategy(cell)
    for c in _history(7):
        strategy.observe(c)
    reg.put(cell, strategy)

    await engine.process_candle(cell=cell, candle=_history(8)[-1], registry=reg)

    assert "signal" in [e["event_type"] for e in captured]
    assert "decision" in [e["event_type"] for e in decisions]
```

`test_cp3_every_emit_passes_schema_validation` (lines 63-85): add the `diagnostics` fake the same way, pass it to `SignalEngine`, and assert on the split — signal on axiom, decision on diagnostics, both validate:
```python
    # Non-divergent path: 1 signal on axiom + 1 decision on diagnostics
    assert len(captured) == 1
    assert len(decisions) == 1
    for event in captured + decisions:
        Envelope.model_validate(event)  # raises if schema violation
```

`test_cp3_divergence_path_also_passes_schema` (lines 88-145): add the `diagnostics` fake, pass it to `SignalEngine` (keep `reporter=reporter`), and adjust the counts — axiom now gets signal + signal_divergence (2), diagnostics gets decision (1):
```python
    # Divergent path: axiom = signal + signal_divergence (2), diagnostics = decision (1)
    assert len(captured) == 2
    assert len(decisions) == 1
    types_and_levels = [(e["event_type"], e["level"]) for e in captured]
    assert ("signal", "info") in types_and_levels
    assert ("signal_divergence", "warn") in types_and_levels
    assert decisions[0]["event_type"] == "decision"
    assert decisions[0]["level"] == "info"

    signal_events = [e for e in captured if e["event_type"] == "signal"]
    assert len(signal_events) == 1

    for event in captured + decisions:
        Envelope.model_validate(event)

    div_events = [e for e in captured if e["event_type"] == "signal_divergence"]
    assert len(div_events) == 1
    assert div_events[0]["payload"]["divergence_detail"] is not None
```

In `tests/integration/test_daemon_locf_lifecycle.py:143`, the SignalEngine construction needs the new required param. It does not assert DECISION, so inject the Noop:
```python
        from bfx_funding_bot.modules.execution.diagnostics.sink import NoopDiagnosticsSink
        engine = SignalEngine(phase=Phase.PAPER, axiom=axiom, diagnostics=NoopDiagnosticsSink(),
                              candles_repo=_RepoBridge())
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd backend_py && uv run pytest tests/modules/marketfeed/test_signal_engine.py tests/modules/marketfeed/test_signal_engine_phase42.py -q`
Expected: FAIL — `SignalEngine.__init__() got an unexpected keyword argument 'diagnostics'`

- [ ] **Step 3: Add `diagnostics` param to SignalEngine + repoint DECISION**

In `signal_engine.py`, add the param to `__init__` (after `axiom` at line 93) and store it:

```python
    def __init__(
        self,
        *,
        phase: Phase,
        axiom: _AxiomProtocol,
        diagnostics: _AxiomProtocol,
        candles_repo: _CandlesRepoProtocol,
        reporter: DivergenceReporter | None = None,
        safety_chain: _SafetyChainProtocol | None = None,
        executor: ExecutorPort | None = None,
        account_ctx: AccountContext | None = None,
    ) -> None:
        self.phase = phase
        self.axiom = axiom
        self.diagnostics = diagnostics
        self.candles_repo = candles_repo
        ...
```

In `_emit_decision_final` (line 292), change the emit target from axiom to diagnostics:

```python
        await self.diagnostics.emit({
            "timestamp": datetime.now(UTC).isoformat(),
            "level": Level.INFO.value,
            "phase": self.phase.value,
            "strategy": cell.strategy.value,
            "cell": cell.cell_id,
            "event_type": EventType.DECISION.value,
            "correlation_id": str(correlation_id),
            "account_id": getattr(self.account_ctx, "account_id", "default"),
            "payload": decision.model_dump(mode="json"),
        })
```

(Leave `_emit_signal`, `_emit_signal_divergence_warn`, `_emit_health` on `self.axiom` — operational, → 3c.)

- [ ] **Step 4: Wire diagnostics into the daemon's SignalEngine**

In `daemon.py`, the SignalEngine construction (line 890) — add `diagnostics=diagnostics,` after `axiom=axiom,`:

```python
    signal_engine_obj = SignalEngine(
        phase=config.phase,
        axiom=axiom,
        diagnostics=diagnostics,
        candles_repo=_CandlesRepoBridge(),
        safety_chain=safety_chain,
        executor=wrapped_executor,
        account_ctx=account_ctx,
    )
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd backend_py && uv run pytest tests/modules/marketfeed/test_signal_engine.py tests/modules/marketfeed/test_signal_engine_phase42.py -q`
Expected: PASS

- [ ] **Step 6: Full unit suite + types**

Run: `cd backend_py && uv run pytest -m "not integration" -q && uv run mypy src/`
Expected: PASS / clean

- [ ] **Step 7: Commit**

```bash
cd /Users/will/second-brain/projects/startup/bfx-funding-bot
git add backend_py/src/bfx_funding_bot/modules/marketfeed/signal_engine.py backend_py/src/bfx_funding_bot/modules/marketfeed/daemon.py backend_py/tests/modules/marketfeed/conftest.py backend_py/tests/modules/marketfeed/test_signal_engine.py backend_py/tests/modules/marketfeed/test_signal_engine_phase42.py backend_py/tests/integration/test_daemon_locf_lifecycle.py
git commit -m "✅ Feat: repoint DECISION emit signal_engine → diagnostics (3b)"
```

---

## Task 7: Repoint SAFETY_TRIGGER (safety chain) → diagnostics

**Files:**
- Modify: `src/bfx_funding_bot/modules/execution/emit.py`
- Modify: `src/bfx_funding_bot/modules/execution/safety/chain.py`
- Modify: `src/bfx_funding_bot/modules/marketfeed/daemon.py` (SafetyGuardChain construction)
- Modify: `tests/modules/execution/safety/test_chain.py`

- [ ] **Step 1: Update the failing test first**

In `tests/modules/execution/safety/test_chain.py`, the `SafetyGuardChain` construction takes `axiom=...`; rename to `diagnostics=...` at all four sites (lines 92-96, 107-111, 120-124, 132-136). `_CaptureAxiom` (line 42) already implements `emit(dict)` — reuse it as the diagnostics fake. For `test_chain_internal_exception_fail_closed` (lines 129-143), assert on the diagnostics fake:

```python
@pytest.mark.asyncio
async def test_chain_internal_exception_fail_closed() -> None:
    diagnostics = _CaptureAxiom()
    chain = SafetyGuardChain(
        guards=[_CrashGuard()], probe=HealthProbe(), diagnostics=diagnostics,
        phase=Phase.PAPER, strategy=StrategyName.MEAN_REVERSION, cell="fUSD_a30",
        account_id="default",
    )
    r = await chain.evaluate(_post(), _ctx())
    assert r.allowed is False
    triggers = [e for e in diagnostics.events if e["event_type"] == EventType.SAFETY_TRIGGER.value]
    assert len(triggers) == 1
    assert triggers[0]["level"] == "critical"
    assert "guard_internal_error" in triggers[0]["payload"]["reason"]
```

For the other three constructions (lines 92/107/120), just change the keyword `axiom=_CaptureAxiom()` → `diagnostics=_CaptureAxiom()`.

- [ ] **Step 2: Run to verify it fails**

Run: `cd backend_py && uv run pytest tests/modules/execution/safety/test_chain.py -q`
Expected: FAIL — `SafetyGuardChain.__init__() got an unexpected keyword argument 'diagnostics'`

- [ ] **Step 3: Repoint `emit_safety_trigger`**

In `emit.py`, change the `emit_safety_trigger` signature param `axiom: _AxiomProtocol` → `diagnostics: _AxiomProtocol` (line 118), and the final call (line 145) `await axiom.emit(...)` → `await diagnostics.emit(env.model_dump(mode="json"))`. The Envelope construction in between is unchanged.

- [ ] **Step 4: Repoint `SafetyGuardChain`**

In `safety/chain.py`:
- `__init__` (line 44): `axiom: _AxiomProtocol` → `diagnostics: _AxiomProtocol`
- store (line 52): `self.axiom = axiom` → `self.diagnostics = diagnostics`
- `_emit_block` (line 115): `axiom=self.axiom,` → `diagnostics=self.diagnostics,`
- `_emit_internal_error` (line 129): `axiom=self.axiom,` → `diagnostics=self.diagnostics,`

- [ ] **Step 5: Wire diagnostics into the daemon's SafetyGuardChain**

In `daemon.py`, the SafetyGuardChain construction (line 764), change `axiom=axiom,` (line 767) → `diagnostics=diagnostics,`:

```python
    safety_chain = SafetyGuardChain(
        guards=guards,
        probe=probe,
        diagnostics=diagnostics,
        phase=config.phase,
        strategy=first_cell.strategy,
        cell=first_cell.cell_id,
        account_id=account_id,
    )
```

> `diagnostics` is constructed at line ~697 (Task 5), before this line — no ordering issue.

- [ ] **Step 6: Run tests + types**

Run: `cd backend_py && uv run pytest tests/modules/execution/safety/test_chain.py -q && uv run mypy src/`
Expected: PASS / clean

- [ ] **Step 7: Commit**

```bash
cd /Users/will/second-brain/projects/startup/bfx-funding-bot
git add backend_py/src/bfx_funding_bot/modules/execution/emit.py backend_py/src/bfx_funding_bot/modules/execution/safety/chain.py backend_py/src/bfx_funding_bot/modules/marketfeed/daemon.py backend_py/tests/modules/execution/safety/test_chain.py
git commit -m "✅ Feat: repoint SAFETY_TRIGGER safety chain → diagnostics (3b)"
```

---

## Task 8: Repoint smoke-boot SAFETY_TRIGGER + final verification

**Files:**
- Modify: `src/bfx_funding_bot/modules/marketfeed/daemon_smoke_boot.py:48`
- Modify: `tests/modules/admin/test_boot_smoke_integration.py`

- [ ] **Step 1: Update the failing tests first**

In `tests/modules/admin/test_boot_smoke_integration.py`, the stub uses `daemon_stub.axiom.emit`; the smoke-boot path now uses `daemon.diagnostics.emit`. Update all three tests:

`test_boot_smoke_passes_logs_info` (line 21-28): add `daemon_stub.diagnostics = MagicMock(); daemon_stub.diagnostics.emit = AsyncMock()` and change the assertion to `daemon_stub.diagnostics.emit.assert_not_awaited()`.

`test_boot_smoke_fails_emits_safety_trigger` (lines 42-53):
```python
    daemon_stub.diagnostics = MagicMock()
    daemon_stub.diagnostics.emit = AsyncMock()
    daemon_stub.config.phase.value = "paper"

    from bfx_funding_bot.modules.marketfeed.daemon_smoke_boot import run_boot_smoke
    await run_boot_smoke(daemon_stub)

    daemon_stub.diagnostics.emit.assert_awaited_once()
    emit_call = daemon_stub.diagnostics.emit.call_args[0][0]
    assert emit_call["event_type"] == "safety_trigger"
    assert emit_call["level"] == "critical"
    assert "smoke_boot" in str(emit_call["payload"])
```

`test_boot_smoke_none_runner_skips_silently` (lines 56-66): add `daemon_stub.diagnostics = MagicMock(); daemon_stub.diagnostics.emit = AsyncMock()` and change the final assertion to `daemon_stub.diagnostics.emit.assert_not_awaited()`.

- [ ] **Step 2: Run to verify it fails**

Run: `cd backend_py && uv run pytest tests/modules/admin/test_boot_smoke_integration.py -q`
Expected: FAIL (asserts on `diagnostics.emit` which the impl doesn't call yet)

- [ ] **Step 3: Repoint daemon_smoke_boot**

In `daemon_smoke_boot.py`, change line 48 `await daemon.axiom.emit({` → `await daemon.diagnostics.emit({`. The dict (strategy=None/cell=None) is fine — the sink does not full-validate Envelope (Task 3).

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd backend_py && uv run pytest tests/modules/admin/test_boot_smoke_integration.py -q`
Expected: PASS

- [ ] **Step 5: Full verification gate**

Run: `cd backend_py && uv run pytest -m "not integration" -q`
Expected: PASS (entire unit suite)

Run: `cd backend_py && uv run mypy src/ && uv run ruff check`
Expected: clean

Run (if Docker available): `cd backend_py && uv run pytest -q -m integration`
Expected: PASS (or skipped if no Docker)

- [ ] **Step 6: Confirm no forensic event still routes to Axiom**

Run: `cd backend_py && python3 -c "
import pathlib, re
for f in ['modules/marketfeed/signal_engine.py','modules/execution/emit.py','modules/execution/safety/chain.py','modules/marketfeed/daemon_smoke_boot.py']:
    t = pathlib.Path('src/bfx_funding_bot/'+f).read_text()
    for kw in ('DECISION','SAFETY_TRIGGER'):
        for i,l in enumerate(t.splitlines(),1):
            if kw in l: print(f, i, l.strip()[:80])
"`
Expected: every DECISION/SAFETY_TRIGGER line is near a `diagnostics.emit` / `diagnostics.record`, none near `axiom.emit`. (SIGNAL/HEALTH_CHECK staying on axiom is correct.)

- [ ] **Step 7: Commit**

```bash
cd /Users/will/second-brain/projects/startup/bfx-funding-bot
git add backend_py/src/bfx_funding_bot/modules/marketfeed/daemon_smoke_boot.py backend_py/tests/modules/admin/test_boot_smoke_integration.py
git commit -m "✅ Feat: repoint smoke-boot SAFETY_TRIGGER → diagnostics; 3b complete"
```

---

## Done criteria

- `diagnostics` table + migration in place; `alembic check` clean on ephemeral PG.
- `DiagnosticsSink` persists DECISION / SAFETY_TRIGGER / CANCEL_AUDIT best-effort in its own txn; drops non-forensic; swallows DB errors.
- DECISION (signal_engine), SAFETY_TRIGGER (safety chain + smoke-boot), CANCEL (bus) all route to PG, off Axiom.
- Full unit suite + mypy + ruff green; integration green (Docker).
- Axiom still carries operational events (HEALTH_CHECK/SIGNAL) + redundant SoT emits — those are 3c.

## Spec self-review notes (gaps to watch during execution)

- `CancelAcknowledged` field names (`rest_status`, `venue_response_text`) — confirm against `events.py:141` before Task 3 (they mirror `axiom_sink.py:117-121`).
- If any other test constructs `SignalEngine` / `SafetyGuardChain` beyond those enumerated, the required `diagnostics` param will surface it as a TypeError — fix by injecting `NoopDiagnosticsSink()` (no DECISION/SAFETY assertion) or a `_CaptureAxiom()` fake (asserting).
- `phase4_4a` cancel tests are NOT affected (they compose their own bus, not `build_daemon`).
