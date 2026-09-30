"""Actual event_log golden parity for every live transport outcome."""

import json

import pytest
from sqlalchemy import select

from bfx_funding_bot.modules.execution.event_store.tables import EventLogRow
from bfx_funding_bot.modules.execution.events import ReservationClaimed
from bfx_funding_bot.modules.execution.protocols import SubmittedOrder
from bfx_funding_bot.modules.execution.submit_outcomes import (
    SubmitAcknowledged,
    SubmitNotSent,
    SubmitOutcomeUnknown,
    SubmitRejected,
)
from bfx_funding_bot.modules.execution.uncertainty_tables import SubmissionAttemptRow
from bfx_funding_bot.modules.ledger import Scope

from .test_capital_command_boundary import (
    AMOUNT,
    boundary,
    capital_db,  # noqa: F401 - fixture re-export
    capital_engine,  # noqa: F401 - fixture dependency
)

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome,event_type,reason", [
    (SubmitAcknowledged("101"), "RESERVATION_CLAIMED", None),
    (SubmitRejected("venue_rejected"), "RESERVATION_FAILED", "venue_rejected"),
    (SubmitOutcomeUnknown("timeout", transport_started=True), "SUBMIT_OUTCOME_UNKNOWN", "timeout"),
    (SubmitNotSent("local_guard"), "RESERVATION_FAILED", "local_pre_transport"),
])
async def test_event_log_golden_for_all_command_outcomes(capital_db, outcome, event_type, reason):  # noqa: F811
    factory, account = capital_db
    gate, venue, ready, ctx, _runtime, _ = await boundary(factory, account)
    notifications = []

    async def capture(event):
        notifications.append(event)

    gate._bus.subscribe(ReservationClaimed, capture)
    gate._uncertainty_handler = capture

    async def submit(ready, context, *, cid, reservation_ref):
        return SubmittedOrder(cid=cid, venue_offer_id=None, outcome=outcome, reservation_ref=reservation_ref)

    venue.submit = submit
    result = await gate.submit(ready, ctx)
    async with factory() as session:
        row = await session.scalar(select(EventLogRow).where(EventLogRow.event_type == event_type)
                                   .order_by(EventLogRow.event_seq.desc()).limit(1))
        attempt = await session.scalar(select(SubmissionAttemptRow).where(
            SubmissionAttemptRow.execution_decision_id == ready.decision_id,
        ))
    payload = dict(row.payload)
    payload["event_id"] = "<generated>"
    correlation = str(ready.decision.signal_correlation_id)
    expected = {
        "__event_type__": event_type, "__schema_version__": 3,
        "symbol": "fUST", "cid": result.cid, "signal_correlation_id": correlation,
        "account_id": str(account), "is_simulated": False,
        "amount": AMOUNT, "size_usdt": AMOUNT, "venue_seq": None, "event_seq": None,
        "occurred_at_ms": 1100, "recorded_at_ms": None, "event_id": "<generated>",
        "reservation_ref": {"execution_decision_id": ready.decision_id, "cid": result.cid,
                            "signal_correlation_id": correlation,
                            "venue_offer_id": "101" if reason is None else None},
    }
    if reason is None:
        expected["venue_offer_id"] = "101"
    else:
        expected["reason"] = reason
    assert json.dumps(payload, sort_keys=True, separators=(",", ":")) == json.dumps(
        expected, sort_keys=True, separators=(",", ":")), payload
    stored = await gate._journal.read_back_outcome(Scope(account, "ci"), attempt.attempt_id)
    assert stored.kind == ("ack" if reason is None else outcome.kind.value)
    assert stored.reason == reason
    if event_type in {"RESERVATION_CLAIMED", "SUBMIT_OUTCOME_UNKNOWN"}:
        assert len(notifications) == 1
        assert notifications[0].event_id == row.event_id == stored.event_id
