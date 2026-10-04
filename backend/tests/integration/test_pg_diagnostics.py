"""Integration tests for DiagnosticsSink — real Postgres round-trip.

Requires testcontainers. Run with:
    cd backend && uv run pytest tests/integration/test_pg_diagnostics.py -q -m integration
"""
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import select

from bfx_funding_bot.modules.execution.diagnostics.sink import DiagnosticsSink
from bfx_funding_bot.modules.execution.diagnostics.tables import DiagnosticsRow

pytestmark = pytest.mark.integration


async def test_emit_decision_round_trip(pg_session_factory) -> None:
    acct = str(uuid4())
    sink = DiagnosticsSink(session_factory=pg_session_factory, account_id=acct, deployment_environment="ci")
    scid = str(uuid4())
    await sink.emit({
        "timestamp": datetime.now(UTC).isoformat(),
        "level": "info", "phase": "shadow", "strategy": "rate_percentile",
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
