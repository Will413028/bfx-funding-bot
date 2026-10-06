"""The command gate drives its port inside one transaction and transports after commit."""

from contextlib import asynccontextmanager
from dataclasses import replace
from decimal import Decimal
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from bfx_funding_bot.modules.execution.audit.tables import ExecutionDecisionRow
from bfx_funding_bot.modules.execution.bus import DomainEventBus
from bfx_funding_bot.modules.execution.command_boundary import CommandBoundary, LedgerCommandEffects
from bfx_funding_bot.modules.execution.command_gate import AccountCommandGate, CommandGateBlocked
from bfx_funding_bot.modules.execution.event_store.tables import EventLogRow
from bfx_funding_bot.modules.execution.uncertainty_tables import SubmissionAttemptRow
from bfx_funding_bot.modules.ledger import (
    Authorized,
    CancelAdmitted,
    CancelProvenance,
    CommandAttempt,
    CommandRefused,
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


class Uncertainty:
    def __init__(self, opened=False):
        self.opened = opened

    async def has_open(self, session, scope, symbol):
        return self.opened

    async def list_open(self, session, scope, symbol=None):
        return ()


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
    bus = DomainEventBus()
    boundary = CommandBoundary(SCOPE, sessions, port, LedgerCommandEffects(bus))
    gate = AccountCommandGate(venue,
        uncertainty_reader=Uncertainty(), safety_evaluator=_FakeSafetyEvaluator([]),
        deployment_environment="ci", boundary=boundary, managed_offers=Offers(),
        clock=lambda: 100)

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
