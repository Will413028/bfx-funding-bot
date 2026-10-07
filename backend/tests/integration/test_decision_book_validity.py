"""Original pricing evidence must survive every await until transport (the ledger gate)."""
import asyncio
from dataclasses import replace
from decimal import Decimal
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from bfx_funding_bot.modules.execution.audit import AuditContext, ExecutionDecisionRecorder
from bfx_funding_bot.modules.execution.audit.tables import ExecutionDecisionRow
from bfx_funding_bot.modules.execution.bus import DomainEventBus
from bfx_funding_bot.modules.execution.command_boundary import LedgerCommandEffects
from bfx_funding_bot.modules.execution.command_gate import AccountCommandGate
from bfx_funding_bot.modules.execution.contracts import ExecutionPolicy, GuardResult, ReadyToSubmit
from bfx_funding_bot.modules.execution.deployment.eligibility import ExecutionGate
from bfx_funding_bot.modules.execution.deployment.period_pricing import PeriodPricer
from bfx_funding_bot.modules.execution.protocols import (
    AccountContext,
    Credentials,
    SubmittedOrder,
)
from bfx_funding_bot.modules.execution.submit_outcomes import (
    SubmitAcknowledged,
    SubmitOutcomeKind,
)
from bfx_funding_bot.modules.ledger import CapitalAvailable
from bfx_funding_bot.modules.ledger.tables import (
    SubmissionAttemptJournalRow,
    TransportOutcomeJournalRow,
)
from bfx_funding_bot.modules.marketfeed.funding_book import FundingBookStore
from bfx_funding_bot.modules.strategy import DecisionOutcome, DecisionPayload
from tests.modules.execution.deployment.test_reconciler import _Readiness, _valid_snapshot

from .contracts.stacks import ACCOUNT, CELL, ENVIRONMENT, NOW, Stack, build_stack, seed_account
from .test_ledger_schema_roles import ledger_db  # noqa: F401 - fixture dependency

pytestmark = pytest.mark.integration

AMOUNT = "499.99990500"


def book_store(captured=-27900):
    store = FundingBookStore(max_age_seconds=30, clock=lambda: captured)
    store.apply_snapshot("fUST", _valid_snapshot().bids, sequence=10)
    store.apply_sequence("fUST", 11)
    store.apply_checksum("fUST", checksum=123, expected=123, sequence=12)
    return store


@pytest_asyncio.fixture
async def stack(ledger_db) -> Stack:  # noqa: F811
    await seed_account(ledger_db)
    engine = create_async_engine(
        ledger_db.url.render_as_string(hide_password=False).replace("+psycopg", "+asyncpg"))
    try:
        built = build_stack(async_sessionmaker(engine, expire_on_commit=False))
        await built.policy()
        await built.snapshot("1000")
        yield built
    finally:
        await engine.dispose()


class _Allow:
    async def evaluate(self, decision, context) -> GuardResult:
        return GuardResult(allowed=True, guard_name="test")


class _NoneOpen:
    async def has_open(self, session, scope, symbol) -> bool:
        return False


class Venue:
    def __init__(self):
        self.received = []

    async def submit(self, ready, ctx, *, reservation_ref):
        self.received.append(ready)
        return SubmittedOrder(venue_offer_id="101", outcome=SubmitAcknowledged("101"),
                              reservation_ref=reservation_ref)


async def boundary(stack: Stack):
    """The ledger command gate, one admissible submit of ``AMOUNT`` and its audit row."""
    from tests.external.bitfinex.test_funding_rules import evidence

    view = await stack.capital.read(stack.capital_scope(), now_ms=NOW)
    assert isinstance(view, CapitalAvailable), view
    decision_id, correlation = str(uuid4()), uuid4()
    async with stack.factory.begin() as session:
        session.add(ExecutionDecisionRow(
            decision_id=decision_id, account_id=str(ACCOUNT), exchange_account_id=ACCOUNT,
            deployment_environment=ENVIRONMENT, reconcile_id="gate", cell_id=CELL,
            symbol="fUST", signal_correlation_id=str(correlation), outcome="ready",
            signal_rate=Decimal("0.0001"), applied_rate=Decimal("0.0001"),
            amount_usdt=Decimal(AMOUNT), duration_days=2, model_evidence={}, safety_result={},
            execution_policy="gate", service_version="test", config_hash="test",
            occurred_at_ms=NOW, recorded_at_ms=NOW,
        ))
    ready = ReadyToSubmit(
        decision=DecisionPayload(decision_outcome=DecisionOutcome.POST,
            signal_correlation_id=correlation, offer_rate=Decimal("0.0001"),
            offer_amount_usdt=Decimal(AMOUNT), offer_duration_days=2, symbol="fUST"),
        decision_id=decision_id, policy=ExecutionPolicy.BOOK_GUARDED,
        market_snapshot_id="book", model_version=None, evidence={},
        safety=GuardResult(True, "test"), capital_view=view,
        market_snapshot=replace(_valid_snapshot(), snapshot_id="book", max_age_ms=30000),
        funding_amount_evidence=evidence(),
    )
    venue = Venue()
    gate = AccountCommandGate(
        venue, uncertainty_reader=_NoneOpen(), safety_evaluator=_Allow(),
        deployment_environment=ENVIRONMENT, clock=lambda: NOW,
        boundary=stack.boundary(LedgerCommandEffects(DomainEventBus())),
        managed_offers=stack.offers,
    )
    ctx = AccountContext(str(ACCOUNT), Credentials("mock", "mock"), Decimal("0"))
    return gate, venue, ready, ctx


def planner_ports(stack: Stack):
    return {"capital": stack.capital, "offers": stack.offers, "uncertainty": stack.uncertainties,
            "scope": stack.scope, "session_factory": stack.factory}


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["missing", "bound", "identity", "symbol", "sequence", "checksum", "future"])
async def test_invalid_original_book_cannot_send(stack, fault):
    gate, venue, ready, ctx = await boundary(stack)
    snap = ready.market_snapshot
    if fault == "missing":
        snap = None
    else:
        changes = {
            "bound": {"max_age_ms": None}, "identity": {"snapshot_id": "other"},
            "symbol": {"symbol": "fUSD"}, "sequence": {"sequence_valid": False},
            "checksum": {"checksum_valid": False}, "future": {"captured_at_ms": 1200},
        }
        snap = replace(snap, **changes[fault])
    result = await gate.submit(replace(ready, market_snapshot=snap), ctx)
    assert result.outcome_kind is SubmitOutcomeKind.NOT_SENT
    assert venue.received == []


@pytest.mark.asyncio
@pytest.mark.parametrize("delay", ["none", "lock", "transport_guard"])
@pytest.mark.parametrize("expiring", ["book", "fx"])
async def test_expired_decision_is_durable_not_sent(stack, delay, expiring):
    factory = stack.factory
    gate, venue, base, ctx = await boundary(stack)
    now = NOW
    gate._clock = lambda: now
    candidate = base.decision.model_copy(update={"offer_amount_usdt": Decimal(AMOUNT)})
    snap = book_store(-27900 if expiring == "book" else 1000).snapshot("fUST", now_ms=now)
    price = PeriodPricer(max_down_pct=Decimal("0.15"), tick=Decimal("0.00000001")).price(
        candidate=candidate, snapshot=snap)
    ready = await ExecutionGate(policy=ExecutionPolicy.BOOK_GUARDED,
        audit=ExecutionDecisionRecorder(factory), readiness=_Readiness()).prepare(
            candidate, decision_id=str(uuid4()), reconcile_id="test", snapshot=snap,
            price=price, fill_evidence=None, safety=GuardResult(True, "test"),
            audit_context=AuditContext(account_id=str(ACCOUNT), deployment_environment=ENVIRONMENT,
                reconcile_id="test", cell_id=CELL, symbol="fUST",
                signal_correlation_id=str(candidate.signal_correlation_id), service_version="test",
                config_hash="test", strategy="mean_reversion"))
    assert isinstance(ready, ReadyToSubmit)
    ready = replace(ready, capital_view=base.capital_view,
                    funding_amount_evidence=base.funding_amount_evidence)
    if expiring == "fx":
        ready = replace(ready, funding_amount_evidence=replace(ready.funding_amount_evidence,
            requested_at_ms=-27900, received_at_ms=-27900))
    if delay == "transport_guard":
        guard = gate._guard
        async def delayed_guard(*args, **kwargs):
            nonlocal now
            await guard(*args, **kwargs)
            if kwargs.get("transport"):
                now = 3100
        gate._guard = delayed_guard
    if delay == "lock":
        lock = gate._account_locks.setdefault((str(ACCOUNT), ENVIRONMENT), asyncio.Lock())
        await lock.acquire()
        task = asyncio.create_task(gate.submit(ready, ctx))
        await asyncio.sleep(0)  # let submit wait on the actual account lock
        assert not task.done()
        now = 3100
        lock.release()
        result = await task
    else:
        result = await gate.submit(ready, ctx)
    expected = SubmitOutcomeKind.ACKNOWLEDGED if delay == "none" else SubmitOutcomeKind.NOT_SENT
    assert result.outcome_kind is expected
    assert len(venue.received) == (1 if delay == "none" else 0)
    async with factory() as session:
        audited = await session.get(ExecutionDecisionRow, ready.decision_id)
        assert audited.snapshot_id == ready.market_snapshot_id
        assert audited.safety_result["book_validity"]["max_age_ms"] == 30000
        assert audited.safety_result["book_validity"]["captured_at_ms"] == snap.captured_at_ms
        attempt = await session.scalar(select(SubmissionAttemptJournalRow).where(
            SubmissionAttemptJournalRow.execution_decision_id == ready.decision_id))
        assert attempt is not None
        outcome = await session.scalar(select(TransportOutcomeJournalRow).where(
            TransportOutcomeJournalRow.attempt_id == attempt.attempt_id))
        assert outcome is not None
        assert outcome.kind == ("ack" if delay == "none" else "not_sent")


@pytest.mark.asyncio
async def test_second_cell_uses_current_clock_for_its_book(stack):
    from tests.modules.execution.deployment.test_reconciler import _build, _post_quote
    now = NOW
    rec, venue, *_ = _build(exposure=Decimal("0"),
        quotes=[_post_quote("fUST_a30"), _post_quote("fUST_p2")],
        capital_ports=planner_ports(stack), book_provider=book_store())
    rec._clock = lambda: now
    submit = venue.submit
    async def delayed_submit(*args, **kwargs):
        nonlocal now
        result = await submit(*args, **kwargs)
        now = 3100
        return result
    venue.submit = delayed_submit
    await rec.deploy()
    assert len(venue.ready_submissions) == 1
