"""Command port ownership, legacy payload parity, and first-write-only outcomes."""

import json
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import date
from decimal import Decimal
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from bfx_funding_bot.modules.execution.audit.tables import ExecutionDecisionRow
from bfx_funding_bot.modules.execution.bus import DomainEventBus
from bfx_funding_bot.modules.execution.capital_repository import CapitalBlockedError
from bfx_funding_bot.modules.execution.command_gate import AccountCommandGate, CommandGateBlocked
from bfx_funding_bot.modules.execution.event_store.persister import NoopEventPersister
from bfx_funding_bot.modules.execution.event_store.serialization import (
    event_type_of,
    serialize_event,
)
from bfx_funding_bot.modules.execution.event_store.tables import EventLogRow
from bfx_funding_bot.modules.execution.events import (
    ReservationClaimed,
    ReservationUnknown,
)
from bfx_funding_bot.modules.execution.legacy_command_journal import LegacyCommandJournal
from bfx_funding_bot.modules.execution.uncertainty_tables import SubmissionAttemptRow
from bfx_funding_bot.modules.ledger import (
    Authorized,
    CancelAdmitted,
    CancelProvenance,
    CommandAttempt,
    CommandOutcome,
    CommandRefused,
    OutcomeAlreadyRecorded,
    Scope,
)

from .test_command_gate import ACCOUNT_ID, _context, _FakeSafetyEvaluator, _FakeVenue, _ready

SCOPE = Scope(ACCOUNT_ID, "ci")
CORRELATION = UUID("00000000-0000-0000-0000-000000000123")


class Sessions:
    def __init__(self, decision=None):
        self.active = False
        self.trace = []
        self.decision = decision
        self.attempt = None
        self.event = None

    @asynccontextmanager
    async def begin(self):
        assert not self.active
        self.active = True
        self.trace.append("begin")
        try:
            yield self
        except BaseException:
            self.trace.append("rollback")
            raise
        else:
            self.trace.append("commit")
        finally:
            self.active = False

    @asynccontextmanager
    async def __call__(self):
        yield self

    async def get(self, table, key, **kwargs):
        if table is ExecutionDecisionRow:
            return self.decision
        if table is SubmissionAttemptRow:
            return self.attempt
        if table is EventLogRow:
            return self.event
        raise AssertionError(table)

    async def scalar(self, query):
        return self.scalars.pop(0) if hasattr(self, "scalars") else self.intent_event


class Writer:
    def __init__(self, sessions):
        self.sessions = sessions
        self.events = []
        self.locked = False

    async def prepare_locked(self, session, **kwargs):
        assert session.active
        self.locked = True

    async def append(self, session, event):
        assert session.active and self.locked
        self.events.append(event)
        if self.sessions.attempt is not None and not event_type_of(event).startswith("CANCEL"):
            row = self.sessions.attempt
            row.outcome_kind = ("acknowledged" if isinstance(event, ReservationClaimed) else
                                "unknown" if isinstance(event, ReservationUnknown) else
                                "not_sent" if event.reason == "local_pre_transport" else "rejected")
            row.outcome_reason = getattr(event, "reason", None)
            row.venue_offer_id = getattr(event, "venue_offer_id", None)
            row.completed_at_ms = event.occurred_at_ms
            row.last_event_seq = 1
            self.sessions.event = EventLogRow(
                event_seq=1, event_type=event_type_of(event), payload=serialize_event(event),
                schema_version=3, exchange_account_id=ACCOUNT_ID, deployment_environment="ci",
            )


class Uncertainty:
    def __init__(self, opened=False):
        self.opened = opened

    async def has_open(self, session, scope, symbol):
        return self.opened

    async def list_open(self, session, scope, symbol=None):
        return ()


def _legacy():
    sessions = Sessions(SimpleNamespace(
        decision_id="decision", signal_correlation_id=str(CORRELATION), cell_id="cell",
        applied_rate=Decimal("0.0001"), duration_days=2,
        exchange_account_id=ACCOUNT_ID, deployment_environment="ci", symbol="fUST",
    ))
    sessions.attempt = SimpleNamespace(
        attempt_id=uuid4(), execution_decision_id="decision", exchange_account_id=ACCOUNT_ID,
        deployment_environment="ci", symbol="fUST", cid=987,
        normalized_payload={"amount": "12.5"}, outcome_kind=None, outcome_reason=None,
        completed_at_ms=None, venue_offer_id=None,
    )
    sessions.intent_event = SimpleNamespace(payload={"amount": "12.5"})
    writer = Writer(sessions)
    runtime = SimpleNamespace(session_factory=sessions, repository=SimpleNamespace(
        account_id=ACCOUNT_ID, environment="ci", writer=writer,
    ))
    return LegacyCommandJournal(runtime, date_provider=lambda: date(2026, 9, 3), clock=lambda: 100,
                                uncertainty_reader=Uncertainty()), sessions, writer


@pytest.mark.asyncio
@pytest.mark.parametrize("kind,reason,event_type", [
    ("ack", None, "RESERVATION_CLAIMED"),
    ("unknown", "timeout", "SUBMIT_OUTCOME_UNKNOWN"),
    ("not_sent", "local_pre_transport", "RESERVATION_FAILED"),
    ("rejected", "venue_rejected", "RESERVATION_FAILED"),
])
async def test_legacy_outcome_golden_payload_readback_and_duplicate(kind, reason, event_type):
    port, sessions, writer = _legacy()
    outcome = CommandOutcome(kind, "venue-1" if kind == "ack" else None, reason, 100, {}, event_id=uuid4())
    attempt_id = sessions.attempt.attempt_id
    assert await port.read_back_outcome(SCOPE, attempt_id) is None
    await port.record_outcome(SCOPE, attempt_id, outcome)
    assert sessions.trace == ["begin", "commit"]
    payload = serialize_event(writer.events[0])
    assert writer.events[0].event_id == outcome.event_id
    # UUID generation is the sole nondeterministic field; pin every other byte.
    payload["event_id"] = "<generated>"
    expected = {
        "__event_type__": event_type, "__schema_version__": 3,
        "symbol": "fUST", "cid": 987, "signal_correlation_id": str(CORRELATION),
        "account_id": str(ACCOUNT_ID), "is_simulated": False,
        "amount": "12.5", "size_usdt": "12.5", "venue_seq": None, "event_seq": None,
        "occurred_at_ms": 100, "recorded_at_ms": None, "event_id": "<generated>",
        "reservation_ref": {"execution_decision_id": "decision", "cid": 987,
                            "signal_correlation_id": str(CORRELATION), "venue_offer_id": outcome.venue_offer_id},
    }
    if kind == "ack":
        expected["venue_offer_id"] = "venue-1"
    else:
        expected["reason"] = reason
    def encode(value):
        return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    assert encode(payload) == encode(expected)
    # A fresh adapter has no identity map and reads the committed rows.
    fresh = LegacyCommandJournal(port._runtime, date_provider=lambda: date(2026, 9, 4), clock=lambda: 101)
    assert await fresh.read_back_outcome(SCOPE, attempt_id) == outcome
    for second in (outcome, replace(outcome, completed_at_ms=101)):
        with pytest.raises(OutcomeAlreadyRecorded) as error:
            await fresh.record_outcome(SCOPE, attempt_id, second)
        assert error.value.stored == outcome
    assert len(writer.events) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["scope", "decision", "cid", "offer", "kind"])
async def test_legacy_readback_refuses_mismatched_outcome(change):
    port, sessions, _ = _legacy()
    attempt_id = sessions.attempt.attempt_id
    await port.record_outcome(SCOPE, attempt_id, CommandOutcome("ack", "venue-1", None, 100, {}))
    scope = SCOPE
    if change == "scope":
        scope = replace(SCOPE, deployment_environment="other")
    elif change == "decision":
        sessions.attempt.execution_decision_id = "another"
    elif change == "cid":
        sessions.attempt.cid += 1
    elif change == "offer":
        sessions.attempt.venue_offer_id = "another"
    else:
        sessions.attempt.outcome_kind = "unknown"
    with pytest.raises(ValueError, match=r"scope mismatch|identity mismatch|capital_scope_conflict"):
        await port.read_back_outcome(scope, attempt_id)


@pytest.mark.asyncio
@pytest.mark.parametrize("refused", [False, True])
async def test_legacy_authorize_preserves_amount_identity_date_and_never_retries(refused):
    from bfx_funding_bot.external.bitfinex.cid import generate_cid

    port, sessions, writer = _legacy()
    calls = []
    guarded = []
    attempt = CommandAttempt(uuid4(), "decision", "fUST", {"amount": "12.50000000"},
                             Decimal("12.5"), 100, 1, "digest", uuid4(), command_date=date(2026, 9, 3),
                             event_id=uuid4())
    port._date_provider = lambda: date(2026, 9, 4)  # midnight after gate identity allocation

    async def authorize(session, **kwargs):
        calls.append(kwargs)
        await writer.prepare_locked(session)
        if refused:
            raise CapitalBlockedError("snapshot_changed")
        await kwargs["locked_guard"](session)
        return SimpleNamespace(event_seq=7)

    port._runtime.repository.authorize_and_append_intent = authorize

    async def guard(session):
        assert session is sessions and session.active and writer.locked
        guarded.append(True)

    async with sessions.begin() as session:
        result = await port.authorize(session, SCOPE, attempt, "10", now_ms=100, locked_guard=guard)
    assert len(calls) == 1
    intent = calls[0]["intent"]
    assert str(intent.amount) == "12.5"
    assert intent.event_id == attempt.event_id
    assert intent.cid == generate_cid(CORRELATION, date(2026, 9, 3))
    assert (calls[0]["expected_revision"], calls[0]["expected_digest"],
            calls[0]["expected_snapshot_seq"]) == (1, "digest", 10)
    if refused:
        assert result == CommandRefused("snapshot_changed") and not guarded
    else:
        assert isinstance(result, Authorized) and guarded == [True]


def _claim(sessions):
    from bfx_funding_bot.modules.execution.contracts import ReservationRef

    event = ReservationClaimed(symbol="fUST", cid=987, venue_offer_id="venue-1",
        signal_correlation_id=CORRELATION, account_id=str(ACCOUNT_ID), is_simulated=False,
        size_usdt=Decimal("12.5"), occurred_at_ms=100,
        reservation_ref=ReservationRef("decision", 987, CORRELATION, "venue-1"))
    return SimpleNamespace(cid=987, execution_decision_id="decision", state="claimed", symbol="fUST",
                           signal_correlation_id=str(CORRELATION), size_usdt=Decimal("12.5")), EventLogRow(
        event_seq=1, event_type="RESERVATION_CLAIMED", payload=serialize_event(event),
        schema_version=3, exchange_account_id=ACCOUNT_ID, deployment_environment="ci",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", [None, "missing", "conflict", "uncertain", "guard"])
async def test_legacy_cancel_guard_runs_under_lock_before_any_append(fault):
    port, sessions, writer = _legacy()
    claim, evidence = _claim(sessions)
    if fault == "conflict":
        claim.cid += 1
    port._uncertainty = Uncertainty(fault == "uncertain")
    sessions.scalars = [None] if fault == "missing" else [claim, evidence, sessions.attempt.attempt_id]
    calls = []

    async def guard(session, admission):
        assert session is sessions and sessions.active and writer.locked
        assert not writer.events
        calls.append(admission)
        if fault == "guard":
            raise RuntimeError("guard refused")

    async with sessions.begin() as session:
        if fault == "guard":
            with pytest.raises(RuntimeError, match="guard refused"):
                await port.admit_cancel(session, SCOPE, "venue-1", now_ms=100, locked_guard=guard)
        else:
            result = await port.admit_cancel(session, SCOPE, "venue-1", now_ms=100, locked_guard=guard)
            if fault:
                assert result == CommandRefused(f"cancel_provenance_{fault}")
            else:
                assert isinstance(result, CancelAdmitted)
    assert len(calls) == (1 if fault in {None, "guard"} else 0)
    assert len(writer.events) == (1 if fault is None else 0)


@pytest.mark.asyncio
@pytest.mark.parametrize("command", ["submit", "cancel", "snapshot_changed"])
async def test_gate_port_guard_transaction_and_transport_after_commit(command):
    ready = _ready()
    ready = replace(ready, decision=ready.decision.model_copy(update={"offer_amount_usdt": Decimal("12.49990001")}))
    sessions = Sessions(SimpleNamespace(outcome="ready", applied_rate=ready.decision.offer_rate,
                                       duration_days=ready.decision.offer_duration_days, cell_id="cell"))
    view = SimpleNamespace(applied=SimpleNamespace(revision=1, digest="digest", revision_id=uuid4()),
                           basis_token="10")
    ready = replace(ready, capital_view=view)
    runtime = SimpleNamespace(session_factory=sessions, repository=SimpleNamespace(
        account_id=ACCOUNT_ID, environment="ci",
    ))

    class Offers:
        async def fingerprints_in_use(self, session, scope, symbol):
            return frozenset()

    class Port:
        calls = 0

        async def authorize(self, session, scope, attempt, basis_token, *, now_ms, locked_guard):
            self.calls += 1
            assert isinstance(attempt, CommandAttempt) and not hasattr(attempt, "cid")
            assert session is sessions and sessions.active
            if command == "snapshot_changed":
                return CommandRefused("snapshot_changed")
            await locked_guard(session)
            sessions.trace.append("write")
            return Authorized(attempt.attempt_id, 1, "digest")

        async def admit_cancel(self, session, scope, venue_offer_id, *, now_ms, locked_guard):
            admission = CancelAdmitted(CancelProvenance(venue_offer_id, "fUST", uuid4(), "decision", "cell",
                                                        str(CORRELATION)), Decimal("12.5"), Decimal("0.0001"), 2)
            await locked_guard(session, admission)
            sessions.trace.append("write")
            return admission

        async def record_outcome(self, scope, attempt_id, outcome):
            assert not sessions.active
            sessions.trace.append("outcome")

    class Venue(_FakeVenue):
        async def submit(self, *args, **kwargs):
            assert not sessions.active and sessions.trace[-1] == "transport_guard"
            sessions.trace.append("transport")
            return await super().submit(*args, **kwargs)

        async def cancel(self, **kwargs):
            assert not sessions.active and sessions.trace[-1] == "transport_guard"
            sessions.trace.append("transport")

    venue, port = Venue(), Port()
    gate = AccountCommandGate(venue, bus=DomainEventBus(), persister=NoopEventPersister(),
        uncertainty_reader=Uncertainty(), safety_evaluator=_FakeSafetyEvaluator([]),
        deployment_environment="ci", capital_runtime=runtime, managed_offers=Offers(),
        is_simulated=False, clock=lambda: 100, command_journal=port)

    async def guard(decision, context, **kwargs):
        if context.command_session is not None:
            assert context.command_session is sessions and sessions.active
            sessions.trace.append("guard")
        else:
            assert not sessions.active and "commit" in sessions.trace
            sessions.trace.append("transport_guard")

    gate._guard = guard
    if command == "cancel":
        await gate.cancel(venue_offer_id="venue-1", signal_correlation_id=uuid4(),
                          account_id=str(ACCOUNT_ID), ctx=_context())
        assert sessions.trace == ["begin", "guard", "write", "commit", "transport_guard", "transport"]
    elif command == "snapshot_changed":
        with pytest.raises(CommandGateBlocked, match="capital_snapshot_changed"):
            await gate.submit(ready, _context())
        assert port.calls == 1 and venue.calls == 0 and sessions.trace == ["begin", "rollback"]
    else:
        # Isolate the transaction boundary from unrelated market evidence checks.
        from unittest.mock import patch

        with patch("bfx_funding_bot.modules.execution.command_gate.validate_amount"), patch.object(
            type(ready), "book_valid_at", return_value=True,
        ):
            await gate.submit(ready, _context())
        assert sessions.trace == ["begin", "guard", "write", "commit", "transport_guard", "transport", "outcome"]
