"""The command gate over the real ledger boundary on migrated PostgreSQL; the venue is a
recording transport.

``boundary`` builds the gate production composes (``apps/bot_ports``): the ledger's
command journal and effects, its uncertainty reader and managed offers, and the stop
chain. Other integration suites (kill switch, pre-trade limits) reuse it.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from bfx_funding_bot.external.bitfinex.nonce import AuthRequestGate
from bfx_funding_bot.modules.accounts.tables import ExchangeAccount
from bfx_funding_bot.modules.execution.audit.tables import ExecutionDecisionRow
from bfx_funding_bot.modules.execution.bus import DomainEventBus
from bfx_funding_bot.modules.execution.command_boundary import LedgerCommandEffects
from bfx_funding_bot.modules.execution.command_gate import AccountCommandGate, CommandGateBlocked
from bfx_funding_bot.modules.execution.contracts import ExecutionPolicy, GuardResult, ReadyToSubmit
from bfx_funding_bot.modules.execution.protocols import AccountContext, Credentials, SubmittedOrder
from bfx_funding_bot.modules.execution.safety.hard_guards import CapitalPolicyGuard, ManualKillGuard
from bfx_funding_bot.modules.execution.safety.trading_state import TradingStateRepository
from bfx_funding_bot.modules.execution.submit_outcomes import SubmitAcknowledged
from bfx_funding_bot.modules.ledger import CapitalAvailable, PolicyRefused
from bfx_funding_bot.modules.ledger.tables import (
    CapitalCommandClockRow,
    SubmissionAttemptJournalRow,
    TransportOutcomeJournalRow,
)
from bfx_funding_bot.modules.ledger.wiring import build_policy_store
from bfx_funding_bot.modules.strategy import DecisionOutcome, DecisionPayload
from bfx_funding_bot.modules.trading import CapitalPolicy, CapitalScope
from tests.async_wait import until

from .contracts.stacks import ACCOUNT, ENVIRONMENT, NOW, SCOPE, Stack, build_stack, seed_account
from .test_ledger_schema_roles import ledger_db  # noqa: F401 - fixture re-export

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

# The planned 500, as the planner submits it: rounded down, carrying an amount
# fingerprint (D3a) the command gate requires.
AMOUNT = "499.99990500"


@pytest_asyncio.fixture
async def gate_stack(ledger_db) -> Stack:  # noqa: F811
    """The ledger stack of one scope (``contracts.stacks``) on a migrated clone."""
    await seed_account(ledger_db)
    engine = create_async_engine(
        ledger_db.url.render_as_string(hide_password=False).replace("+psycopg", "+asyncpg")
    )
    try:
        yield build_stack(async_sessionmaker(engine, expire_on_commit=False))
    finally:
        await engine.dispose()


async def apply_policy(stack: Stack, symbol: str, policy: CapitalPolicy) -> None:
    """The next revision of ``symbol``'s policy, under the scope lock (the amend path)."""
    store = build_policy_store(SCOPE)
    async with stack.factory.begin() as session:
        await stack.lock.lock(session, SCOPE)
        try:
            revision = (await store.read_applied(session, symbol=symbol)).revision
        except PolicyRefused:
            revision = 0
        await store.apply_policy(session, symbol=symbol, policy=policy,
                                 expected_revision=revision, source={"operator": "test"})


async def read_policy(stack: Stack, symbol: str = "fUST") -> Any:
    async with stack.factory() as session:
        return await build_policy_store(SCOPE).read_applied(session, symbol=symbol)


def policy_guard(stack: Stack) -> CapitalPolicyGuard:
    """The production capital guard over the ledger (apps wiring)."""
    return CapitalPolicyGuard(authority=stack.capital, scope=SCOPE, clock=lambda: NOW)


async def read_capital(stack: Stack, cell_id: str, symbol: str = "fUST") -> CapitalAvailable:
    """What the planner reads for one cell."""
    view = await stack.capital.read(CapitalScope(ACCOUNT, ENVIRONMENT, symbol, cell_id),
                                    now_ms=NOW)
    assert isinstance(view, CapitalAvailable), view
    return view


def status_reads(stack: Stack) -> Any:
    from bfx_funding_bot.modules.admin.trading_status import CapitalStatusReads
    return CapitalStatusReads(authority=stack.capital, lock=stack.lock, scope=SCOPE,
                              session_factory=stack.factory, clock=lambda: NOW)


def planner_ports(stack: Stack) -> dict[str, Any]:
    return {"capital": stack.capital, "offers": stack.offers, "uncertainty": stack.uncertainties,
            "scope": SCOPE, "session_factory": stack.factory}


async def attempts(stack: Stack) -> int:
    async with stack.factory() as session:
        return int(await session.scalar(
            select(func.count()).select_from(SubmissionAttemptJournalRow)) or 0)


async def outcomes(stack: Stack) -> list[tuple[str, str | None]]:
    async with stack.factory() as session:
        rows = (await session.scalars(select(TransportOutcomeJournalRow))).all()
    return [(row.kind, row.venue_offer_id) for row in rows]


async def clock_revision(stack: Stack) -> int:
    async with stack.factory() as session:
        return int(await session.scalar(select(CapitalCommandClockRow.revision)) or 0)


async def decision(stack: Stack, amount: str = AMOUNT, *, cell: str = "a30",
                   symbol: str = "fUST") -> tuple[str, UUID]:
    """The audit row the planner writes before a submit; returns (decision id, signal)."""
    decision_id, signal = str(uuid4()), uuid4()
    async with stack.factory.begin() as session:
        session.add(ExecutionDecisionRow(
            decision_id=decision_id, account_id=str(ACCOUNT), exchange_account_id=ACCOUNT,
            deployment_environment=ENVIRONMENT, reconcile_id="test", cell_id=cell, symbol=symbol,
            signal_correlation_id=str(signal), outcome="ready", signal_rate=Decimal("0.0001"),
            applied_rate=Decimal("0.0001"), amount_usdt=Decimal(amount), duration_days=2,
            model_evidence={}, safety_result={}, execution_policy="test", service_version="test",
            config_hash="test", occurred_at_ms=NOW, recorded_at_ms=NOW,
        ))
    return decision_id, signal


class Venue:
    """Records what reaches the transport; asserts the admission was durable first."""

    def __init__(self, stack: Stack) -> None:
        self.stack = stack
        self.received: list[Any] = []
        self.cancel_clock: list[int] = []

    async def submit(self, ready, ctx, *, reservation_ref):
        async with self.stack.factory() as session:
            assert await session.scalar(select(SubmissionAttemptJournalRow.attempt_id).where(
                SubmissionAttemptJournalRow.execution_decision_id == ready.decision_id)) is not None
        self.received.append(ready)
        return SubmittedOrder(venue_offer_id="101", outcome=SubmitAcknowledged("101"),
                              reservation_ref=reservation_ref)

    async def cancel(self, **kwargs):
        self.cancel_clock.append(await clock_revision(self.stack))
        self.received.append(kwargs["venue_offer_id"])


def stop_chain(halt, *guards):
    """The production chain shape: the trading-state guard, then any others."""
    from bfx_funding_bot.core.health import HealthProbe
    from bfx_funding_bot.core.telemetry import Phase
    from bfx_funding_bot.modules.execution.safety.chain import SafetyGuardChain
    from bfx_funding_bot.modules.strategy import StrategyName
    from tests.modules.execution.deployment.test_reconciler import _CapturingSink
    return SafetyGuardChain(
        guards=[ManualKillGuard(trading_state=halt), *guards],
        probe=HealthProbe(), diagnostics=_CapturingSink(), phase=Phase.LIVE,
        strategy=StrategyName.MEAN_REVERSION, cell="a30", account_id=str(ACCOUNT))


@dataclass
class Rig:
    gate: AccountCommandGate
    venue: Venue
    ready: ReadyToSubmit
    ctx: AccountContext
    stack: Stack
    halt: TradingStateRepository


async def boundary(stack: Stack, *, available: str = "1000") -> Rig:
    """Policy (no reserve, 70 % per cell), an accepted snapshot, a planned 500, ACTIVE."""
    from tests.external.bitfinex.test_funding_rules import evidence
    from tests.modules.execution.deployment.test_reconciler import _valid_snapshot
    await apply_policy(stack, "fUST", CapitalPolicy(
        enabled=True, reserve_amount=Decimal("0"), max_cell_fraction=Decimal("0.70")))
    await stack.snapshot(available)
    view = await read_capital(stack, "a30")
    decision_id, signal = await decision(stack)
    ready = ReadyToSubmit(
        decision=DecisionPayload(decision_outcome=DecisionOutcome.POST,
            signal_correlation_id=signal, offer_rate=Decimal("0.0001"),
            offer_amount_usdt=Decimal(AMOUNT), offer_duration_days=2, symbol="fUST"),
        decision_id=decision_id, policy=ExecutionPolicy.BOOK_GUARDED,
        market_snapshot_id="book", model_version=None, evidence={},
        safety=GuardResult(True, "test"), capital_view=view,
        market_snapshot=replace(_valid_snapshot(), snapshot_id="book", max_age_ms=30000),
        funding_amount_evidence=evidence(),
    )
    halt = TradingStateRepository(stack.factory, account_id=ACCOUNT, deployment_environment="ci")
    await halt.transition("ACTIVE", cause="operator", reason="isolated test only", actor="test",
                          now_ms=1200)
    venue = Venue(stack)
    gate = AccountCommandGate(venue,
        uncertainty_reader=stack.uncertainties, safety_evaluator=stop_chain(halt),
        deployment_environment="ci", boundary=stack.boundary(LedgerCommandEffects(DomainEventBus())),
        managed_offers=stack.offers, clock=lambda: NOW)
    ctx = AccountContext(str(ACCOUNT), Credentials("mock", "mock"), Decimal("0"))
    return Rig(gate, venue, ready, ctx, stack, halt)


async def second_ready(rig: Rig, amount: str = "199.99990200") -> ReadyToSubmit:
    """Another planned offer, so a stop is tested against a submit that could run."""
    decision_id, signal = await decision(rig.stack, amount)
    return replace(rig.ready, decision_id=decision_id, decision=rig.ready.decision.model_copy(
        update={"signal_correlation_id": signal, "offer_amount_usdt": Decimal(amount)}))


def with_journal(rig: Rig, wrap: Any) -> None:
    """Swap the gate's journal for ``wrap(inner)`` (fault injection at the boundary)."""
    rig.gate._boundary = replace(rig.gate._boundary, journal=wrap(rig.gate._boundary.journal))


class _Delegate:
    def __init__(self, inner: Any) -> None:
        self.inner = inner

    def __getattr__(self, name: str) -> Any:
        return getattr(self.inner, name)


async def place_managed(stack: Stack, venue_offer_id: str, amount: str, *,
                        symbol: str = "fUST") -> None:
    """An acknowledged submit of ``venue_offer_id``, as the gate journals it: the decision
    carries a real signal correlation, which a managed cancel names."""
    from bfx_funding_bot.modules.ledger import Attempt, Outcome
    from bfx_funding_bot.modules.ledger._internal.journal import record_attempt

    from .test_ledger_capital_reader import _ATTEMPT_POLICY, JOURNAL
    book = stack.builders.book
    assert book.basis_id is not None
    decision_id, _signal = await decision(stack, amount, symbol=symbol)
    attempt_id = uuid4()
    async with stack.factory.begin() as session:
        await record_attempt(session, SCOPE, Attempt(
            attempt_id, decision_id, symbol, "a30", {"amount": amount, "symbol": symbol},
            book.basis_id, UUID(_ATTEMPT_POLICY), {}, NOW))
    async with stack.factory.begin() as session:
        await JOURNAL.record_outcome(session, SCOPE, Outcome(
            attempt_id, "ack", venue_offer_id, None, NOW, {}))


async def append_unknown(stack: Stack, *, symbol: str = "fUST") -> None:
    """An UNKNOWN submit of this scope, recorded through the ledger journal."""
    await stack.unknown("10", symbol=symbol)


async def test_policy_guard_and_planner_use_total_capital_cell_limit(gate_stack):
    from bfx_funding_bot.modules.execution.deployment.sizing import allocate_capital
    rig = await boundary(gate_stack)
    guard = policy_guard(gate_stack)
    ctx = replace(rig.ctx, capital_cell_id="a30")
    too_large = rig.ready.decision.model_copy(update={"offer_amount_usdt": Decimal("701")})
    assert not (await guard.evaluate(too_large, ctx)).allowed
    view = await read_capital(gate_stack, "a30")
    assert allocate_capital(views={"a30": view}, min_fill=Decimal("153")) == {"a30": Decimal("700")}
    # Both normal cells can consume the cash, without relaxing the single-cell limit.
    other = await read_capital(gate_stack, "p2")
    assert allocate_capital(views={"a30": view, "p2": other}, min_fill=Decimal("153")) == {
        "a30": Decimal("700"), "p2": Decimal("300"),
    }
    await gate_stack.builders.book.begin(started=NOW)  # a newer snapshot query is pending
    blocked = await guard.evaluate(rig.ready.decision, ctx)
    assert not blocked.allowed
    assert "snapshot_query_pending" in blocked.reason


async def test_planner_attaches_the_status_budget_and_revision(gate_stack):
    from bfx_funding_bot.modules.trading import fingerprint_of
    from tests.modules.execution.deployment.test_reconciler import _build, _post_quote
    await boundary(gate_stack)
    rec, ex, *_ = _build(exposure=Decimal("0"), quotes=[_post_quote("fUST_a30")],
                         capital_ports=planner_ports(gate_stack))
    # The planner reads capital at its own clock (the daemon injects one clock).
    rec._clock = lambda: NOW
    await rec.deploy()
    planned = ex.ready_submissions[0]
    # The 700 budget, fingerprinted (D3a): below it by less than 0.0001.
    sent = planned.decision.offer_amount_usdt
    assert isinstance(sent, Decimal)
    assert Decimal("699.9999") < sent < Decimal("700") and fingerprint_of(sent)
    assert planned.capital_view.applied.revision == 1
    assert planned.capital_view.budget.max_new_offer == Decimal("700")


async def test_status_shares_policy_budget_and_dry_run_blocks_without_writes(gate_stack):
    from bfx_funding_bot.core.health import HealthProbe
    from bfx_funding_bot.core.telemetry import Phase
    from bfx_funding_bot.modules.admin.trading_status import TradingStatusService
    from bfx_funding_bot.modules.execution.deployment.submit_attempt import SubmitAttemptRecorder
    from bfx_funding_bot.modules.execution.safety.chain import SafetyGuardChain
    from bfx_funding_bot.modules.strategy import StrategyName
    from tests.modules.execution.deployment.test_reconciler import _CapturingSink, _cell
    rig = await boundary(gate_stack)
    chain = SafetyGuardChain(guards=[ManualKillGuard(trading_state=rig.halt),
                                     policy_guard(gate_stack)],
        probe=HealthProbe(), diagnostics=_CapturingSink(), phase=Phase.LIVE,
        strategy=StrategyName.MEAN_REVERSION, cell="fUST_a30", account_id=str(ACCOUNT))
    service = TradingStatusService(chain=chain, account_ctx=rig.ctx,
        cells=[_cell("fUST", "a30"), _cell("fUST", "p2")], phase=Phase.LIVE,
        attempts=SubmitAttemptRecorder(), trading_state=rig.halt,
        exposure=status_reads(gate_stack))
    snapshot = await service.snapshot()
    assert snapshot["account_id"] == str(ACCOUNT)
    assert snapshot["deployment_environment"] == "ci"
    status = snapshot["symbols"]["fUST"]
    missing = (await service.snapshot())["symbols"]["fUSD"]
    assert missing == {"capital_available": False, "reason": "policy_unavailable"}
    await apply_policy(gate_stack, "fUSD", CapitalPolicy(enabled=False))
    disabled = (await service.snapshot())["symbols"]["fUSD"]
    assert disabled["reason"] == "policy_disabled"
    assert disabled["policy_revision"] == 1
    assert disabled["policy"]["enabled"] is False
    assert "available_balance" not in disabled
    assert status["policy_revision"] == 1
    assert status["cells"]["fUST_a30"]["max_new_offer"] == "700.00"
    assert status["cells"]["fUST_p2"]["max_new_offer"] == "700.00"
    before = (await attempts(gate_stack), await clock_revision(gate_stack))
    result = await service.dry_run(symbol="fUST", amount=701)
    assert result["account_id"] == str(ACCOUNT)
    assert result["deployment_environment"] == "ci"
    assert not result["would_submit_any"]
    assert (await attempts(gate_stack), await clock_revision(gate_stack)) == before
    async with gate_stack.factory.begin() as session:
        row = await session.get(ExecutionDecisionRow, rig.ready.decision_id)
        row.cell_id = "fUST_a30"
    await rig.gate.submit(rig.ready, rig.ctx)
    # First cell has only 200 headroom, but the second can still spend 300.
    result = await service.dry_run(symbol="fUST", amount=300)
    assert result["would_submit_any"]
    assert not result["symbols"]["fUST"]["cells"]["fUST_a30"]["would_submit"]
    assert result["symbols"]["fUST"]["cells"]["fUST_p2"]["would_submit"]
    await gate_stack.builders.book.begin(started=NOW)
    unavailable = (await service.snapshot())["symbols"]["fUST"]
    assert unavailable["capital_available"] is False
    assert "snapshot_query_pending" in unavailable["reason"]


async def test_status_with_an_envelope_serializes_to_json(gate_stack):
    # The admin router returns the snapshot through JSONResponse: a Decimal nested
    # in the envelope made /admin/trading-status a 500 once a policy carried one.
    import json

    from bfx_funding_bot.core.health import HealthProbe
    from bfx_funding_bot.core.telemetry import Phase
    from bfx_funding_bot.modules.admin.trading_status import TradingStatusService
    from bfx_funding_bot.modules.execution.deployment.submit_attempt import SubmitAttemptRecorder
    from bfx_funding_bot.modules.execution.safety.chain import SafetyGuardChain
    from bfx_funding_bot.modules.strategy import StrategyName
    from bfx_funding_bot.modules.trading import OfferEnvelope
    from tests.modules.execution.deployment.test_reconciler import _CapturingSink, _cell
    rig = await boundary(gate_stack)
    envelope = OfferEnvelope(min_period_days=2, max_period_days=2, max_open_offers=6,
                             rate_floor_ratio=Decimal("0.5"), min_rate_apr=Decimal("0.01"))
    current = await read_policy(gate_stack)
    await apply_policy(gate_stack, "fUST", replace(
        current.policy, max_offer_amount=Decimal("200"), envelope=envelope))
    await apply_policy(gate_stack, "fUSD", CapitalPolicy(
        enabled=False, max_offer_amount=Decimal("200"), envelope=envelope))
    chain = SafetyGuardChain(guards=[ManualKillGuard(trading_state=rig.halt)],
        probe=HealthProbe(), diagnostics=_CapturingSink(), phase=Phase.LIVE,
        strategy=StrategyName.MEAN_REVERSION, cell="fUST_a30", account_id=str(ACCOUNT))
    service = TradingStatusService(chain=chain, account_ctx=rig.ctx,
        cells=[_cell("fUST", "a30")], phase=Phase.LIVE, attempts=SubmitAttemptRecorder(),
        trading_state=rig.halt, exposure=status_reads(gate_stack))
    symbols = json.loads(json.dumps(await service.snapshot()))["symbols"]
    for symbol in ("fUST", "fUSD"):
        assert symbols[symbol]["policy"]["envelope"] == {
            "min_period_days": 2, "max_period_days": 2, "max_open_offers": 6,
            "rate_floor_ratio": "0.5", "min_rate_apr": "0.01"}, symbol
        assert symbols[symbol]["policy"]["max_offer_amount"] == "200"
    assert symbols["fUST"]["capital_available"] is True
    assert symbols["fUSD"]["reason"] == "policy_disabled"


@pytest.mark.parametrize("change,reason", [
    ("revision", "capital_policy_revision_changed"),
    ("snapshot", "insufficient_deployable_funds"),
    ("pending", "query_pending"),  # the journal's code for a pending snapshot query
    ("halt", "trading state HALTED"),
])
async def test_queued_ready_cannot_send_after_authority_changes(gate_stack, change, reason):
    rig = await boundary(gate_stack)
    if change == "revision":
        await apply_policy(gate_stack, "fUST", rig.ready.capital_view.applied.policy)
    elif change == "snapshot":
        # A newer basis: the stale token is re-read once, and 200 cannot hold the 500.
        await gate_stack.snapshot("200")
    elif change == "pending":
        await gate_stack.builders.book.begin(started=NOW)
    else:
        await rig.halt.transition("HALTED", cause="operator", reason="stop", actor="test", now_ms=1)
    with pytest.raises(CommandGateBlocked, match=reason):
        await rig.gate.submit(rig.ready, rig.ctx)
    assert rig.venue.received == []
    assert await attempts(gate_stack) == 0


@pytest.mark.parametrize("stop", ["HALTED"])
async def test_stop_blocks_submit_but_cancel_stays_durable_before_io(gate_stack, stop):
    """HALTED refuses every new offer; cancelling a managed offer is what the
    state exists to allow, and it is still made durable first."""
    rig = await boundary(gate_stack)
    await rig.gate.submit(rig.ready, rig.ctx)  # managed offer 101 while ACTIVE
    later = await second_ready(rig)
    await rig.halt.transition(stop, cause="operator", reason="stop", actor="test", now_ms=1)
    rig.venue.received.clear()
    with pytest.raises(CommandGateBlocked, match=f"trading state {stop}"):
        await rig.gate.submit(later, rig.ctx)
    assert rig.venue.received == []
    before = await clock_revision(gate_stack)
    await rig.gate.cancel(venue_offer_id="101", signal_correlation_id=uuid4(),
                          account_id=rig.ctx.account_id, ctx=rig.ctx)
    assert rig.venue.received == ["101"]
    # The cancel admission committed (its clock bump) before the transport was reached.
    assert rig.venue.cancel_clock == [before + 1]
    assert await attempts(gate_stack) == 1
    # A cancel ACK never releases capital inside this boundary.
    view = await read_capital(gate_stack, "a30")
    assert view.budget.spendable == Decimal("1000") - Decimal(AMOUNT)


async def test_cancel_admission_needs_an_evaluator_that_knows_cancel_exemptions(gate_stack):
    """A bare guard cannot tell a cancel from a submit; the gate refuses rather
    than guessing which guards to skip."""
    rig = await boundary(gate_stack)
    await rig.gate.submit(rig.ready, rig.ctx)
    rig.venue.received.clear()
    rig.gate._safety_evaluator = ManualKillGuard(trading_state=rig.halt)
    with pytest.raises(CommandGateBlocked, match="cancel_eligibility_unavailable"):
        await rig.gate.cancel(venue_offer_id="101", signal_correlation_id=uuid4(),
                              account_id=rig.ctx.account_id, ctx=rig.ctx)
    assert rig.venue.received == []


async def test_halt_writer_waits_for_authorization_lock(pg_session_factory):
    from sqlalchemy import text

    from bfx_funding_bot.core.writer_lock import acquire_transaction_lock
    factory = pg_session_factory
    account = uuid4()
    async with factory.begin() as session:
        session.add(ExchangeAccount(id=account, venue="bitfinex", label="halt-lock"))
    halt = TradingStateRepository(factory, account_id=account, deployment_environment="ci")
    task = None
    try:
        async with factory.begin() as session:
            await acquire_transaction_lock(session, account_id=str(account), deployment_environment="ci")
            task = asyncio.create_task(halt.transition("HALTED", cause="operator", reason="stop",
                                                       actor="test"))

            async def _waiting() -> bool:
                async with factory() as observer:
                    return bool(await observer.scalar(text(
                        "SELECT count(*) FROM pg_stat_activity WHERE wait_event_type='Lock' "
                        "AND query LIKE 'SELECT pg_advisory_xact_lock%'"
                    )))

            await until(_waiting, what="the halt writer to join the account authorization lock")
            assert not task.done()
        await task
        assert (await halt.current()).state == "HALTED"
    finally:
        if task is not None:
            await task


async def test_authorization_failure_after_the_attempt_rolls_back_before_transport(gate_stack):
    rig = await boundary(gate_stack)

    class FailAfterAuthorize(_Delegate):
        async def authorize(self, *args, **kwargs):
            await self.inner.authorize(*args, **kwargs)
            raise RuntimeError("injected transaction failure")

    with_journal(rig, FailAfterAuthorize)
    with pytest.raises(RuntimeError, match="injected transaction failure"):
        await rig.gate.submit(rig.ready, rig.ctx)
    assert rig.venue.received == []
    assert await attempts(gate_stack) == 0


async def test_locked_guard_observes_same_transaction_halt(gate_stack):
    from bfx_funding_bot.modules.execution.safety.tables import TradingStateRow
    rig = await boundary(gate_stack)

    class HaltInTransaction(_Delegate):
        async def authorize(self, session, scope, attempt, token, *, now_ms, locked_guard):
            async def guarded(locked):
                locked.add(TradingStateRow(exchange_account_id=ACCOUNT, deployment_environment="ci",
                    state="HALTED", cause="operator", reason="uncommitted halt",
                    actor="test", created_at_ms=0))
                await locked.flush()
                await locked_guard(locked)
            return await self.inner.authorize(session, scope, attempt, token, now_ms=now_ms,
                                              locked_guard=guarded)

    with_journal(rig, HaltInTransaction)
    with pytest.raises(CommandGateBlocked, match="uncommitted halt"):
        await rig.gate.submit(rig.ready, rig.ctx)
    assert rig.venue.received == []


async def test_independent_command_gates_cannot_spend_same_budget(gate_stack):
    rig = await boundary(gate_stack)
    # A distinct fingerprint: only the shared budget may decide between them.
    second = await second_ready(rig, "499.99990501")
    competitor = AccountCommandGate(rig.venue,
        uncertainty_reader=gate_stack.uncertainties,
        safety_evaluator=ManualKillGuard(trading_state=rig.halt), deployment_environment="ci",
        boundary=rig.gate._boundary, managed_offers=gate_stack.offers, clock=lambda: NOW)
    results = await asyncio.gather(rig.gate.submit(rig.ready, rig.ctx),
                                   competitor.submit(second, rig.ctx), return_exceptions=True)
    assert sum(isinstance(r, SubmittedOrder) for r in results) == 1
    assert sum(isinstance(r, CommandGateBlocked) for r in results) == 1
    assert len(rig.venue.received) == 1


async def test_final_amount_cannot_round_up_across_capital_guard(gate_stack):
    rig = await boundary(gate_stack)
    altered = rig.ready.decision.model_copy(update={"offer_amount_usdt": Decimal("700.0000000000001")})
    async with gate_stack.factory.begin() as session:
        row = await session.get(ExecutionDecisionRow, rig.ready.decision_id)
        row.amount_usdt = Decimal("700.0000000000001")
    with pytest.raises(CommandGateBlocked):
        await rig.gate.submit(replace(rig.ready, decision=altered), rig.ctx)
    assert rig.venue.received == []


@pytest.mark.parametrize("identity", ["unknown", "other_account", "runtime_environment"])
async def test_cancel_requires_scoped_managed_provenance(gate_stack, identity):
    from bfx_funding_bot.modules.ledger import Scope
    rig = await boundary(gate_stack)
    await rig.gate.submit(rig.ready, rig.ctx)
    rig.venue.received.clear()
    target = "999" if identity == "unknown" else "101"
    if identity == "other_account":
        other = uuid4()
        async with gate_stack.factory.begin() as session:
            session.add(ExchangeAccount(id=other, venue="bitfinex", label="other"))
        rig.gate._boundary = replace(rig.gate._boundary, scope=Scope(other, "ci"))
        account_id = str(other)
        ctx = replace(rig.ctx, account_id=account_id)
    else:
        if identity == "runtime_environment":
            rig.gate._boundary = replace(rig.gate._boundary, scope=Scope(ACCOUNT, "shadow"))
        account_id, ctx = rig.ctx.account_id, rig.ctx
    before = await clock_revision(gate_stack)
    with pytest.raises(CommandGateBlocked, match=r"cancel_(provenance|environment)"):
        await rig.gate.cancel(venue_offer_id=target, signal_correlation_id=uuid4(),
                              account_id=account_id, ctx=ctx)
    assert rig.venue.received == []
    assert await clock_revision(gate_stack) == before


async def test_account_retired_after_planning_cannot_send(gate_stack):
    rig = await boundary(gate_stack)
    async with gate_stack.factory.begin() as session:
        row = await session.get(ExchangeAccount, ACCOUNT)
        row.lifecycle_status = "retired"
    with pytest.raises(CommandGateBlocked, match="account_inactive"):
        await rig.gate.submit(rig.ready, rig.ctx)
    assert rig.venue.received == []


@pytest.mark.parametrize("stop", ["HALTED"])
async def test_stopped_reconcile_never_reposts(gate_stack, stop):
    """While HALTED the planner places nothing: the symbol is skipped before
    sizing or reprice (lending envelope D3/D4). Pulling the managed offers is
    the managed sweep's job (test_pre_trade_limits); none is wired here."""
    from tests.modules.execution.deployment.test_reconciler import (
        _REPRICE,
        _build,
        _post_quote,
        _venue_offer,
    )
    rig = await boundary(gate_stack)
    await rig.gate.submit(rig.ready, rig.ctx)
    rig.venue.received.clear()
    await rig.halt.transition(stop, cause="operator", reason="retained startup halt", actor="test")
    before = await clock_revision(gate_stack)
    rec, _, _ = _build(exposure=Decimal("0"), quotes=[_post_quote("fUST_a30")],
        executor=rig.gate, safety=stop_chain(rig.halt), capital_ports=planner_ports(gate_stack),
        canceller=rig.gate, reprice=_REPRICE)
    rec._ctx = rig.ctx
    await rec.deploy(venue_offers=(_venue_offer("101", 0.001),))
    assert rig.venue.received == []
    assert await attempts(gate_stack) == 1  # only the offer placed while ACTIVE
    assert await clock_revision(gate_stack) == before  # no cancel admitted


async def test_real_guard_chain_reuses_authorization_session_without_double_reserving(gate_stack):
    from bfx_funding_bot.core.health import HealthProbe
    from bfx_funding_bot.core.telemetry import Phase
    from bfx_funding_bot.modules.execution.safety.chain import SafetyGuardChain
    from bfx_funding_bot.modules.execution.safety.hard_guards import UncertaintyGuard
    from bfx_funding_bot.modules.strategy import StrategyName
    from tests.modules.execution.deployment.test_reconciler import _CapturingSink
    rig = await boundary(gate_stack)
    rig.gate._safety_evaluator = SafetyGuardChain(
        guards=[ManualKillGuard(trading_state=rig.halt), UncertaintyGuard(
            reader=gate_stack.uncertainties, deployment_environment="ci"),
            policy_guard(gate_stack)],
        probe=HealthProbe(), diagnostics=_CapturingSink(), phase=Phase.LIVE,
        strategy=StrategyName.MEAN_REVERSION, cell="a30", account_id=str(ACCOUNT))
    result = await asyncio.wait_for(rig.gate.submit(rig.ready, rig.ctx), timeout=5)
    assert result.status == "submitted"
    assert len(rig.venue.received) == 1


async def test_cancel_fault_rolls_back_the_admission_and_never_calls_transport(gate_stack):
    rig = await boundary(gate_stack)
    await rig.gate.submit(rig.ready, rig.ctx)
    rig.venue.received.clear()
    before = await clock_revision(gate_stack)

    class FailAfterAdmission(_Delegate):
        async def admit_cancel(self, *args, **kwargs):
            await self.inner.admit_cancel(*args, **kwargs)
            raise RuntimeError("injected cancel durability failure")

    with_journal(rig, FailAfterAdmission)
    with pytest.raises(RuntimeError, match="injected cancel durability failure"):
        await rig.gate.cancel(venue_offer_id="101", signal_correlation_id=uuid4(),
                              account_id=rig.ctx.account_id, ctx=rig.ctx)
    assert rig.venue.received == []
    assert await clock_revision(gate_stack) == before


async def test_the_capital_probe_writes_nothing(gate_stack):
    rig = await boundary(gate_stack)
    before = (await attempts(gate_stack), await clock_revision(gate_stack))
    result = await policy_guard(gate_stack).evaluate(
        rig.ready.decision, replace(rig.ctx, capital_cell_id="a30"))
    assert result.allowed
    assert (await attempts(gate_stack), await clock_revision(gate_stack)) == before


async def cancel_http_boundary(stack: Stack, http, *, state="ACTIVE") -> Rig:
    """Real gate/guards/adapter; only HTTP transport is supplied by the test.

    ``state`` is the trading state the cancel runs under: the managed offer is
    placed while ACTIVE, then the state changes. Cancelling must behave the
    same in every state -- only uncertainty and provenance decide it.
    """
    from bfx_funding_bot.core.health import HealthProbe
    from bfx_funding_bot.core.telemetry import Phase
    from bfx_funding_bot.external.bitfinex.live_executor import BitfinexLiveExecutor
    from bfx_funding_bot.modules.execution.safety.chain import SafetyGuardChain
    from bfx_funding_bot.modules.execution.safety.hard_guards import UncertaintyGuard
    from bfx_funding_bot.modules.strategy import StrategyName
    from tests.modules.execution.deployment.test_reconciler import _CapturingSink

    rig = await boundary(stack)
    await rig.gate.submit(rig.ready, rig.ctx)  # Establish a known managed offer, no HTTP.
    if state != "ACTIVE":
        await rig.halt.transition(state, cause="operator", reason="stopped during cancel",
                                  actor="test")
    rig.gate._safety_evaluator = SafetyGuardChain(
        guards=[ManualKillGuard(trading_state=rig.halt), UncertaintyGuard(
            reader=stack.uncertainties, deployment_environment="ci"), policy_guard(stack)],
        probe=HealthProbe(), diagnostics=_CapturingSink(), phase=Phase.LIVE,
        strategy=StrategyName.MEAN_REVERSION, cell="a30", account_id=str(ACCOUNT))
    rig.gate._inner = BitfinexLiveExecutor(
        http=http, event_sink=_CapturingSink(), bus=DomainEventBus(), phase=Phase.LIVE,
        strategy=StrategyName.MEAN_REVERSION, configured_symbols=frozenset({"fUST"}),
        cell="a30", auth_gate=AuthRequestGate(lambda: 123456789),
    )
    return rig


@pytest.mark.parametrize("state", ["ACTIVE", "HALTED"])
@pytest.mark.parametrize("fault", ["unknown", "unreadable"])
async def test_cancel_after_admission_rechecks_uncertainty_before_first_http(
        gate_stack, monkeypatch, fault, state):
    import httpx

    requests = []

    async def transport(request):
        requests.append(request)
        return httpx.Response(200, json=[0, "foc-req", None, None, None, 0, "SUCCESS", "ok"])

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as http:
        rig = await cancel_http_boundary(gate_stack, http, state=state)
        admitted_at = await clock_revision(gate_stack)
        original_guard = rig.gate._guard
        injected = False

        async def after_admission(decision, context, **kwargs):
            nonlocal injected
            if context.command_session is None and not injected:
                injected = True
                # The cancel admission is committed (its clock bump) before this point.
                assert await clock_revision(gate_stack) == admitted_at + 1
                if fault == "unknown":
                    await append_unknown(gate_stack)
                else:
                    async def unavailable(*args, **kwargs):
                        raise RuntimeError("synthetic uncertainty read failure")
                    monkeypatch.setattr(type(gate_stack.uncertainties), "has_open", unavailable)
                    monkeypatch.setattr(type(gate_stack.uncertainties), "list_open", unavailable)
            # Scheduling hook only: never fake a guard verdict.
            return await original_guard(decision, context, **kwargs)

        monkeypatch.setattr(rig.gate, "_guard", after_admission)
        with pytest.raises(CommandGateBlocked, match="uncertainty"):
            await asyncio.wait_for(rig.gate.cancel(venue_offer_id="101",
                signal_correlation_id=uuid4(), account_id=str(ACCOUNT), ctx=rig.ctx), timeout=10)
    assert injected
    assert requests == []


@pytest.mark.parametrize("state", ["HALTED"])
async def test_stop_refuses_submit_before_the_http_adapter(gate_stack, state):
    """No new offer reaches the real adapter under a stop, even one already planned."""
    import httpx

    from bfx_funding_bot.core.telemetry import Phase
    from bfx_funding_bot.external.bitfinex.live_executor import BitfinexLiveExecutor
    from bfx_funding_bot.modules.strategy import StrategyName
    from tests.modules.execution.deployment.test_reconciler import _CapturingSink

    requests = []

    async def transport(request):
        requests.append(request)
        return httpx.Response(500)

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as http:
        rig = await boundary(gate_stack)
        rig.gate._inner = BitfinexLiveExecutor(
            http=http, event_sink=_CapturingSink(), bus=DomainEventBus(), phase=Phase.LIVE,
            strategy=StrategyName.MEAN_REVERSION, configured_symbols=frozenset({"fUST"}),
            cell="a30", auth_gate=AuthRequestGate(lambda: 123456789),
        )
        await rig.halt.transition(state, cause="operator", reason="stop", actor="test")
        with pytest.raises(CommandGateBlocked, match=f"trading state {state}"):
            await rig.gate.submit(rig.ready, rig.ctx)
    assert requests == []


@pytest.mark.parametrize("state", ["HALTED"])
async def test_stop_after_the_attempt_commit_is_not_sent(gate_stack, state):
    """A stop landing between the durable attempt and transport ends the
    command as NOT_SENT: the transport recheck keeps the trading-state gate."""
    rig = await boundary(gate_stack)
    original_guard = rig.gate._guard

    async def stop_before_transport(decision, context, **kwargs):
        if kwargs.get("transport"):
            await rig.halt.transition(state, cause="operator", reason="stop mid-command",
                                      actor="test")
        return await original_guard(decision, context, **kwargs)

    rig.gate._guard = stop_before_transport
    result = await rig.gate.submit(rig.ready, rig.ctx)
    assert result.outcome_kind.value == "not_sent"
    assert f"trading state {state}" in result.outcome.reason
    assert rig.venue.received == []
    assert await attempts(gate_stack) == 1
    assert await outcomes(gate_stack) == [("not_sent", None)]


# ---- the durable write path through the ledger journal ----


async def test_the_attempt_is_committed_without_an_outcome_when_the_venue_is_called(gate_stack):
    """Read-your-writes: the venue sees the write-ahead attempt durable and still open."""
    rig = await boundary(gate_stack)
    seen = []

    class Asserting(Venue):
        async def submit(self, ready, ctx, *, reservation_ref):
            seen.append((await attempts(gate_stack), await outcomes(gate_stack)))
            return await super().submit(ready, ctx, reservation_ref=reservation_ref)

    rig.gate._inner = Asserting(gate_stack)
    await rig.gate.submit(rig.ready, rig.ctx)
    assert seen == [(1, [])]


async def test_an_acknowledged_submit_records_the_offer_and_charges_the_amount(gate_stack):
    rig = await boundary(gate_stack)
    await rig.gate.submit(rig.ready, rig.ctx)
    assert await outcomes(gate_stack) == [("ack", "101")]
    view = await read_capital(gate_stack, "a30")
    assert view.snapshot.unreflected_commitments == Decimal(AMOUNT)


async def test_a_rejected_submit_records_the_rejection_and_charges_nothing(gate_stack):
    from bfx_funding_bot.modules.execution.submit_outcomes import SubmitRejected
    rig = await boundary(gate_stack)

    class Rejecting(Venue):
        async def submit(self, ready, ctx, *, reservation_ref):
            return SubmittedOrder(outcome=SubmitRejected("no_funds"),
                                  reservation_ref=reservation_ref)

    rig.gate._inner = Rejecting(gate_stack)
    await rig.gate.submit(rig.ready, rig.ctx)
    assert [kind for kind, _ in await outcomes(gate_stack)] == ["rejected"]
    view = await read_capital(gate_stack, "a30")
    assert view.snapshot.unreflected_commitments == Decimal("0")


async def test_a_crash_between_attempt_and_outcome_leaves_the_attempt_open(gate_stack):
    from bfx_funding_bot.modules.execution.command_gate import SubmitOutcomeLostError
    rig = await boundary(gate_stack)

    class Crashing(Venue):
        async def submit(self, ready, ctx, *, reservation_ref):
            raise RuntimeError("crash between the attempt and its outcome")

    rig.gate._inner = Crashing(gate_stack)
    with pytest.raises(SubmitOutcomeLostError) as lost:
        await rig.gate.submit(rig.ready, rig.ctx)
    assert isinstance(lost.value.__cause__, RuntimeError)
    # Open for the observation cycle's dangling-attempt closure.
    assert (await attempts(gate_stack), await outcomes(gate_stack)) == (1, [])


async def test_reprice_cancels_while_market_data_is_stale(gate_stack):
    """A stale ws_data blocks a new offer, never the reprice cancel of a managed
    one: both go through the real command gate and its guard chain."""
    from datetime import UTC, datetime, timedelta

    from bfx_funding_bot.core.health import HealthProbe
    from bfx_funding_bot.modules.execution.safety.hard_guards import HeartbeatGuard
    from tests.integration.contracts.stacks import Venue as Offer
    from tests.modules.execution.deployment.test_reconciler import (
        _REPRICE,
        _build,
        _post_quote,
        _venue_offer,
    )
    rig = await boundary(gate_stack)
    await rig.gate.submit(rig.ready, rig.ctx)  # managed offer 101, market data fresh
    await gate_stack.snapshot("1000", offers=(Offer("101", AMOUNT),))  # the venue shows it
    rig.venue.received.clear()
    market_data = HealthProbe()
    market_data.last_active_ts["ws_data"] = datetime.now(UTC) - timedelta(minutes=10)
    chain = stop_chain(rig.halt, HeartbeatGuard(probe=market_data, watched_sub_tasks=["ws_data"]))
    rig.gate._safety_evaluator = chain
    with pytest.raises(CommandGateBlocked, match="ws_data stale"):
        await rig.gate.submit(await second_ready(rig), rig.ctx)
    assert rig.venue.received == []
    rec, _, _ = _build(exposure=Decimal("0"), quotes=[_post_quote("fUST_a30")],
        executor=rig.gate, safety=chain, capital_ports=planner_ports(gate_stack),
        canceller=rig.gate, reprice=_REPRICE)
    rec._ctx = rig.ctx
    rec._clock = lambda: NOW  # the planner reads capital at its own clock
    await rec.deploy(venue_offers=(_venue_offer("101", 0.001),))
    assert rig.venue.received == ["101"]  # the reprice cancel reached the venue
