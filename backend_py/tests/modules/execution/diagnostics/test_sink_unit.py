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
        # NB: real signal-engine decision events carry NO account_id — the sink is
        # authoritative for it (constructed with account_id=...), mirroring deployment_environment.
        "payload": {"decision_outcome": "skip", "signal_correlation_id": str(_SCID),
                    "skip_reason": "below_threshold"},
    }


async def _rows(factory) -> list[DiagnosticsRow]:
    async with factory() as s:
        return list((await s.execute(select(DiagnosticsRow))).scalars().all())


async def test_emit_decision_persists_row(diag_factory) -> None:
    sink = DiagnosticsSink(session_factory=diag_factory, account_id="acct", deployment_environment="ci")
    await sink.emit(_decision_event())
    rows = await _rows(diag_factory)
    assert len(rows) == 1
    assert rows[0].kind == "decision"
    assert rows[0].account_id == "acct"
    assert rows[0].deployment_environment == "ci"
    assert rows[0].payload["payload"]["skip_reason"] == "below_threshold"


async def test_emit_tags_row_with_sink_account_id_not_event(diag_factory) -> None:
    # Regression (VM 2026-07-07): real decision events carry no account_id →
    # old code tagged rows "default" while event_log fills carried BFX_ACCOUNT_ID,
    # breaking the per-cell attribution join. The sink is authoritative: even a
    # stale account_id in the event must NOT win over the sink's configured value.
    sink = DiagnosticsSink(
        session_factory=diag_factory, account_id="primary", deployment_environment="ci",
    )
    ev = _decision_event()
    ev["account_id"] = "stale-wrong"  # event carries a bogus one
    await sink.emit(ev)
    rows = await _rows(diag_factory)
    assert len(rows) == 1
    assert rows[0].account_id == "primary"  # sink wins, not "stale-wrong" / "default"


async def test_emit_safety_persists_row(diag_factory) -> None:
    sink = DiagnosticsSink(session_factory=diag_factory, account_id="acct", deployment_environment="ci")
    ev = _decision_event()
    ev["event_type"] = "safety_trigger"
    ev["payload"] = {"guard_name": "cap", "reason": "over_cap", "decision_snapshot": {}}
    await sink.emit(ev)
    rows = await _rows(diag_factory)
    assert len(rows) == 1 and rows[0].kind == "safety_trigger"


async def test_emit_safety_with_none_strategy_persists(diag_factory) -> None:
    """Regression: smoke_boot SAFETY_TRIGGER legitimately has strategy=None/cell=None.
    The sink must NOT full-validate Envelope (that rule would drop this)."""
    sink = DiagnosticsSink(session_factory=diag_factory, account_id="acct", deployment_environment="ci")
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
    sink = DiagnosticsSink(session_factory=diag_factory, account_id="acct", deployment_environment="ci")
    ev = _decision_event()
    ev["event_type"] = etype
    await sink.emit(ev)
    assert await _rows(diag_factory) == []


async def test_emit_unknown_event_type_dropped(diag_factory) -> None:
    sink = DiagnosticsSink(session_factory=diag_factory, account_id="acct", deployment_environment="ci")
    ev = _decision_event()
    ev["event_type"] = "totally_bogus"
    await sink.emit(ev)  # must not raise
    assert await _rows(diag_factory) == []


async def test_emit_best_effort_swallows_db_error(caplog) -> None:
    """A persistence failure must be swallowed + logged, never raised."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    factory = async_sessionmaker(engine, expire_on_commit=False)  # NO create_all -> no table
    sink = DiagnosticsSink(session_factory=factory, account_id="acct", deployment_environment="ci")
    await sink.emit(_decision_event())  # must not raise
    assert any("diagnostics_persist_failed" in r.message for r in caplog.records)
    await engine.dispose()


async def test_handle_cancel_requested_persists(diag_factory) -> None:
    sink = DiagnosticsSink(session_factory=diag_factory, account_id="acct", deployment_environment="ci")
    await sink.handle_cancel_requested(CancelRequested(
        venue_offer_id="v1", requested_at_ms=1000, signal_correlation_id=_SCID,
        account_id="acct"))
    rows = await _rows(diag_factory)
    assert len(rows) == 1
    assert rows[0].kind == "cancel_audit"
    assert rows[0].payload["venue_offer_id"] == "v1"


async def test_handle_cancel_acknowledged_persists(diag_factory) -> None:
    sink = DiagnosticsSink(session_factory=diag_factory, account_id="acct", deployment_environment="ci")
    await sink.handle_cancel_acknowledged(CancelAcknowledged(
        venue_offer_id="v1", acknowledged_at_ms=2000, signal_correlation_id=_SCID,
        account_id="acct", rest_status="ok", venue_response_text="done"))
    rows = await _rows(diag_factory)
    assert len(rows) == 1
    assert rows[0].kind == "cancel_audit"
    assert rows[0].payload["rest_status"] == "ok"
    assert rows[0].payload["venue_response_text"] == "done"
    assert rows[0].payload["acknowledged_at_ms"] == 2000


async def test_noop_sink_does_nothing(diag_factory) -> None:
    sink = NoopDiagnosticsSink()
    await sink.emit(_decision_event())
    await sink.handle_cancel_requested(CancelRequested(
        venue_offer_id="v1", requested_at_ms=1, signal_correlation_id=_SCID, account_id="a"))
    assert await _rows(diag_factory) == []


# ── Four Golden Signals metrics hook (observe-only, fail-open) ────────────────


async def test_emit_forensic_counts_diagnostic_metric(diag_factory) -> None:
    from bfx_funding_bot.modules.observability.metrics import DaemonMetrics

    metrics = DaemonMetrics()
    sink = DiagnosticsSink(
        session_factory=diag_factory, account_id="acct",
        deployment_environment="ci", metrics=metrics,
    )
    ev = _decision_event()
    ev["event_type"] = "safety_trigger"
    ev["level"] = "critical"
    await sink.emit(ev)
    assert metrics.registry.get_sample_value(
        "bfx_diagnostic_events_total",
        {"event_type": "safety_trigger", "level": "critical"},
    ) == 1.0
    rows = await _rows(diag_factory)
    assert len(rows) == 1  # persistence unchanged


async def test_emit_non_forensic_not_counted(diag_factory) -> None:
    from bfx_funding_bot.modules.observability.metrics import DaemonMetrics

    metrics = DaemonMetrics()
    sink = DiagnosticsSink(
        session_factory=diag_factory, account_id="acct",
        deployment_environment="ci", metrics=metrics,
    )
    ev = _decision_event()
    ev["event_type"] = "signal"  # operational — dropped by the sink, not counted
    await sink.emit(ev)
    assert metrics.registry.get_sample_value(
        "bfx_diagnostic_events_total", {"event_type": "signal", "level": "info"},
    ) is None


async def test_emit_persists_even_when_metrics_hook_raises(diag_factory) -> None:
    class _BrokenMetrics:
        def observe_diagnostic_event(self, event) -> None:
            raise RuntimeError("metrics down")

    sink = DiagnosticsSink(
        session_factory=diag_factory, account_id="acct",
        deployment_environment="ci", metrics=_BrokenMetrics(),
    )
    ev = _decision_event()
    await sink.emit(ev)  # must NOT raise
    rows = await _rows(diag_factory)
    assert len(rows) == 1
