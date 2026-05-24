from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
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
