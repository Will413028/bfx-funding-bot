"""The command gate over a ledger boundary: the journal is the only record of an outcome.

The gate runs the real admission, transport guard and outcome path against the
ledger stack. Nothing legacy may be written or published; the committed outcome
is announced exactly once, and only after the journal returned.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from decimal import Decimal
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from bfx_funding_bot.modules.execution.audit.tables import ExecutionDecisionRow
from bfx_funding_bot.modules.execution.bus import DomainEventBus
from bfx_funding_bot.modules.execution.command_boundary import (
    CommandOutcomeNotice,
    LedgerCommandEffects,
)
from bfx_funding_bot.modules.execution.command_gate import AccountCommandGate, CommandGateBlocked
from bfx_funding_bot.modules.execution.contracts import ExecutionPolicy, GuardResult, ReadyToSubmit
from bfx_funding_bot.modules.execution.event_store.persister import EventStorePersister
from bfx_funding_bot.modules.execution.event_store.store import PostgresEventStore
from bfx_funding_bot.modules.execution.event_store.tables import EventLogRow
from bfx_funding_bot.modules.execution.events import (
    OrderFilled,
    ReservationClaimed,
    ReservationFailed,
    ReservationIntent,
    ReservationUnknown,
)
from bfx_funding_bot.modules.execution.protocols import (
    AccountContext,
    Credentials,
    SubmittedOrder,
)
from bfx_funding_bot.modules.execution.submit_outcomes import (
    SubmitAcknowledged,
    SubmitNotSent,
    SubmitOutcomeUnknown,
    SubmitRejected,
)
from bfx_funding_bot.modules.ledger import CapitalAvailable
from bfx_funding_bot.modules.strategy import DecisionOutcome, DecisionPayload

from ..test_ledger_schema_roles import ledger_db  # noqa: F401 - fixture re-export
from .stacks import ACCOUNT, CELL, ENVIRONMENT, NOW, SCOPE, Stack, build_stack, seed_account

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

AMOUNT = Decimal("200.00000500")
LEGACY_EVENTS = (ReservationIntent, ReservationClaimed, ReservationFailed, ReservationUnknown,
                 OrderFilled)


@pytest_asyncio.fixture
async def ledger_stack(ledger_db) -> Stack:  # noqa: F811
    await seed_account(ledger_db)
    engine = create_async_engine(
        ledger_db.url.render_as_string(hide_password=False).replace("+psycopg", "+asyncpg")
    )
    try:
        yield build_stack("ledger", async_sessionmaker(engine, expire_on_commit=False))
    finally:
        await engine.dispose()


class _Allow:
    async def evaluate(self, decision, context) -> GuardResult:
        return GuardResult(allowed=True, guard_name="test")


class _Venue:
    def __init__(self, order: SubmittedOrder | None = None) -> None:
        self.order = order
        self.calls = 0

    async def submit(self, ready, ctx, *, cid, reservation_ref):
        self.calls += 1
        assert self.order is not None
        return replace(self.order, cid=cid, reservation_ref=reservation_ref)


@dataclass
class _Rig:
    gate: AccountCommandGate
    venue: _Venue
    ready: ReadyToSubmit
    ctx: AccountContext
    seen: list[object]
    stack: Stack

    async def event_log_rows(self) -> int:
        async with self.stack.factory() as session:
            return await session.scalar(select(func.count()).select_from(EventLogRow))


async def _rig(stack: Stack, order: SubmittedOrder | None, *, journal=None, bus=None) -> _Rig:
    from tests.external.bitfinex.test_funding_rules import evidence
    from tests.modules.execution.deployment.test_reconciler import _valid_snapshot

    await stack.policy()
    await stack.snapshot("1000")
    view = await stack.capital.read(stack.capital_scope(), now_ms=NOW)
    assert isinstance(view, CapitalAvailable), view
    decision_id, correlation = str(uuid4()), uuid4()
    async with stack.factory.begin() as session:
        session.add(ExecutionDecisionRow(
            decision_id=decision_id, account_id=str(ACCOUNT), exchange_account_id=ACCOUNT,
            deployment_environment=ENVIRONMENT, reconcile_id="gate", cell_id=CELL,
            symbol="fUST", signal_correlation_id=str(correlation), outcome="ready",
            signal_rate=Decimal("0.0001"), applied_rate=Decimal("0.0001"),
            amount_usdt=AMOUNT, duration_days=2, model_evidence={}, safety_result={},
            execution_policy="gate", service_version="test", config_hash="test",
            occurred_at_ms=NOW, recorded_at_ms=NOW,
        ))
    ready = ReadyToSubmit(
        decision=DecisionPayload(
            decision_outcome=DecisionOutcome.POST, signal_correlation_id=correlation,
            offer_rate=Decimal("0.0001"), offer_amount_usdt=AMOUNT, offer_duration_days=2,
            symbol="fUST"),
        decision_id=decision_id, policy=ExecutionPolicy.BOOK_GUARDED,
        market_snapshot_id="book", model_version=None, evidence={},
        safety=GuardResult(True, "test"), capital_view=view,
        market_snapshot=replace(_valid_snapshot(), snapshot_id="book", max_age_ms=30000),
        funding_amount_evidence=evidence(),
    )
    bus = bus or DomainEventBus()
    seen: list[object] = []

    async def capture(event) -> None:
        seen.append(event)

    for event_type in (*LEGACY_EVENTS, CommandOutcomeNotice):
        bus.subscribe(event_type, capture)
    boundary = stack.boundary(LedgerCommandEffects(bus))
    if journal is not None:
        boundary = replace(boundary, journal=journal(boundary.journal, seen))

    class Reader:
        async def has_open(self, session, scope, symbol) -> bool:
            return False

    venue = _Venue(order)
    gate = AccountCommandGate(
        venue, bus=bus, persister=EventStorePersister(store=PostgresEventStore(deployment_environment=ENVIRONMENT),
                                      session_factory=stack.factory),
        uncertainty_reader=Reader(),
        safety_evaluator=_Allow(), deployment_environment=ENVIRONMENT, is_simulated=False,
        clock=lambda: NOW, boundary=boundary, managed_offers=stack.offers,
    )
    ctx = AccountContext(str(ACCOUNT), Credentials("mock", "mock"), Decimal("0"))
    return _Rig(gate, venue, ready, ctx, seen, stack)


def _order(kind: str) -> SubmittedOrder:
    if kind == "ack":
        return SubmittedOrder(cid=0, venue_offer_id="m-1", outcome=SubmitAcknowledged("m-1"))
    if kind == "filled":
        return SubmittedOrder(cid=0, venue_offer_id="m-1", status="filled")
    if kind == "unknown":
        return SubmittedOrder(cid=0, venue_offer_id=None,
                              outcome=SubmitOutcomeUnknown("timeout", transport_started=True))
    if kind == "rejected":
        return SubmittedOrder(cid=0, venue_offer_id=None, outcome=SubmitRejected("venue_rejected"))
    return SubmittedOrder(cid=0, venue_offer_id=None, outcome=SubmitNotSent("local_guard"))


@pytest.mark.parametrize(("kind", "wire_kind", "offer", "reason"), [
    ("ack", "ack", "m-1", None),
    ("filled", "ack", "m-1", None),
    ("unknown", "unknown", None, "timeout"),
    ("rejected", "rejected", None, "venue_rejected"),
    ("not_sent", "not_sent", None, "local_pre_transport"),
])
async def test_ledger_outcome_is_journaled_and_announced_once_with_no_legacy_trace(
    ledger_stack, kind, wire_kind, offer, reason
) -> None:
    rig = await _rig(ledger_stack, _order(kind))
    before = await rig.event_log_rows()

    await rig.gate.submit(rig.ready, rig.ctx)

    assert rig.venue.calls == 1
    assert await rig.event_log_rows() == before == 0  # no legacy event_log row, ever
    assert [type(event) for event in rig.seen] == [CommandOutcomeNotice]
    notice = rig.seen[0]
    assert (notice.scope, notice.kind, notice.symbol, notice.venue_offer_id, notice.amount,
            notice.completed_at_ms) == (SCOPE, wire_kind, "fUST", offer, AMOUNT, NOW)
    stored = await ledger_stack.journal.read_back_outcome(SCOPE, notice.attempt_id)
    assert stored is not None and (stored.kind, stored.venue_offer_id, stored.reason) == (
        wire_kind, offer, reason)


async def test_notice_is_published_only_after_the_journal_committed(ledger_stack) -> None:
    observed: list[object] = []

    class Spy:
        def __init__(self, inner, seen) -> None:
            self.inner, self.seen = inner, seen

        def __getattr__(self, name):
            return getattr(self.inner, name)

        async def record_outcome(self, scope, attempt_id, outcome) -> None:
            assert self.seen == []  # nothing announced before the record
            await self.inner.record_outcome(scope, attempt_id, outcome)
            observed.append(await self.inner.read_back_outcome(scope, attempt_id))
            assert self.seen == []  # and nothing announced until the gate moves on

    rig = await _rig(ledger_stack, _order("ack"), journal=Spy)
    await rig.gate.submit(rig.ready, rig.ctx)
    assert len(observed) == 1 and observed[0] is not None  # committed when the notice follows
    assert len(rig.seen) == 1


async def test_a_failed_record_announces_nothing(ledger_stack) -> None:
    class Failing:
        def __init__(self, inner, seen) -> None:
            self.inner = inner

        def __getattr__(self, name):
            return getattr(self.inner, name)

        async def record_outcome(self, scope, attempt_id, outcome) -> None:
            raise RuntimeError("journal down")

    from bfx_funding_bot.modules.execution.command_gate import SubmitOutcomeLostError

    rig = await _rig(ledger_stack, _order("ack"), journal=Failing)
    with pytest.raises(SubmitOutcomeLostError):
        await rig.gate.submit(rig.ready, rig.ctx)
    assert rig.seen == []


async def test_a_refused_admission_announces_nothing_and_never_reaches_the_venue(
    ledger_stack
) -> None:
    rig = await _rig(ledger_stack, _order("ack"))
    stale = replace(rig.ready, capital_view=replace(rig.ready.capital_view, basis_token="0"))
    with pytest.raises(CommandGateBlocked, match="capital_snapshot_changed"):
        await rig.gate.submit(stale, rig.ctx)
    assert rig.venue.calls == 0
    assert rig.seen == []
    assert await rig.event_log_rows() == 0
