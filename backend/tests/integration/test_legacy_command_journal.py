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

    gate._boundary.effects._bus.subscribe(ReservationClaimed, capture)
    gate._boundary.effects._uncertainty_handler = capture

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
    stored = await gate._boundary.journal.read_back_outcome(Scope(account, "ci"), attempt.attempt_id)
    assert stored.kind == ("ack" if reason is None else outcome.kind.value)
    assert stored.reason == reason
    if event_type in {"RESERVATION_CLAIMED", "SUBMIT_OUTCOME_UNKNOWN"}:
        assert len(notifications) == 1
        assert notifications[0].event_id == row.event_id == stored.event_id


@pytest.mark.asyncio
async def test_event_log_golden_for_filled_acknowledgement(capital_db):  # noqa: F811
    """A filled ACK: claimed is the journal outcome, the fill is persisted after it,
    and the bus sees claimed before filled."""
    from bfx_funding_bot.modules.execution.events import OrderFilled

    factory, account = capital_db
    gate, venue, ready, ctx, _runtime, _ = await boundary(factory, account)
    published: list[object] = []

    async def capture(event):
        published.append(event)

    gate._boundary.effects._bus.subscribe(ReservationClaimed, capture)
    gate._boundary.effects._bus.subscribe(OrderFilled, capture)

    async def submit(ready, context, *, cid, reservation_ref):
        return SubmittedOrder(cid=cid, venue_offer_id="101", status="filled",
                              reservation_ref=reservation_ref)

    venue.submit = submit
    result = await gate.submit(ready, ctx)
    async with factory() as session:
        rows = (await session.scalars(select(EventLogRow).where(
            EventLogRow.event_type.in_(("RESERVATION_CLAIMED", "ORDER_FILL")),
        ).order_by(EventLogRow.event_seq))).all()
    assert [row.event_type for row in rows] == ["RESERVATION_CLAIMED", "ORDER_FILL"]
    fill_payload = dict(rows[1].payload)
    assert fill_payload["venue_offer_id"] == "101"
    assert fill_payload["cid"] == result.cid
    assert fill_payload["occurred_at_ms"] == 1100
    assert fill_payload["size_usdt"] == AMOUNT
    assert fill_payload["is_simulated"] is False
    assert fill_payload["credit_id"] is None
    assert fill_payload["fill_rate"] == 0.0001
    assert [type(event) for event in published] == [ReservationClaimed, OrderFilled]
    assert published[0].event_id == rows[0].event_id
    assert published[1].event_id == rows[1].event_id
