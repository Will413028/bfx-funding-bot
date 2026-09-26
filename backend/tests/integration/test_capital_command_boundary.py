"""Real isolated SQL and command boundary; the venue is a recording transport."""
from dataclasses import replace
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import select

from bfx_funding_bot.modules.execution.audit.tables import ExecutionDecisionRow
from bfx_funding_bot.modules.execution.bus import DomainEventBus
from bfx_funding_bot.modules.execution.command_gate import AccountCommandGate, CommandGateBlocked
from bfx_funding_bot.modules.execution.contracts import ExecutionPolicy, GuardResult, ReadyToSubmit
from bfx_funding_bot.modules.execution.event_store.persister import EventStorePersister
from bfx_funding_bot.modules.execution.event_store.store import PostgresEventStore
from bfx_funding_bot.modules.execution.event_store.tables import EventLogRow
from bfx_funding_bot.modules.execution.protocols import AccountContext, Credentials, SubmittedOrder
from bfx_funding_bot.modules.execution.safety.hard_guards import ManualKillGuard
from bfx_funding_bot.modules.execution.safety.trading_state import TradingStateRepository
from bfx_funding_bot.modules.execution.submit_outcomes import SubmitAcknowledged
from bfx_funding_bot.modules.marketfeed.schemas import DecisionOutcome, DecisionPayload

from .test_capital_repository import (
    capital_db as capital_db,
)
from .test_capital_repository import (
    capital_engine as capital_engine,
)
from .test_capital_repository import (
    intent,
    repository,
    setup_policy,
    snapshot,
)

# The planned 500, as the planner submits it: rounded down, carrying an amount
# fingerprint (D3a) the command gate requires.
AMOUNT = "499.99990500"


@pytest.mark.asyncio
async def test_policy_guard_and_planner_use_total_capital_cell_limit(capital_db):
    from bfx_funding_bot.modules.execution.deployment.sizing import allocate_capital
    from bfx_funding_bot.modules.execution.safety.hard_guards import CapitalPolicyGuard
    factory, account = capital_db
    _, _, ready, ctx, runtime, _ = await boundary(factory, account)
    guard = CapitalPolicyGuard(runtime=runtime)
    ctx = replace(ctx, capital_cell_id="a30")
    too_large = ready.decision.model_copy(update={"offer_amount_usdt": 701.0})
    assert not (await guard.evaluate(too_large, ctx)).allowed
    view = await runtime.read(symbol="fUST", cell_id="a30")
    assert allocate_capital(views={"a30": view}, min_fill=Decimal("153")) == {"a30": Decimal("700")}
    # Both normal cells can consume the cash, without relaxing the single-cell limit.
    other = await runtime.read(symbol="fUST", cell_id="p2")
    assert allocate_capital(views={"a30": view, "p2": other}, min_fill=Decimal("153")) == {
        "a30": Decimal("700"), "p2": Decimal("300"),
    }
    async with factory.begin() as session:
        await runtime.repository.begin_snapshot(session, now_ms=1100)
    blocked = await guard.evaluate(ready.decision, ctx)
    assert not blocked.allowed
    assert "snapshot_query_pending" in blocked.reason


@pytest.mark.asyncio
async def test_planner_attaches_the_status_budget_and_revision(capital_db):
    from tests.modules.execution.deployment.test_reconciler import _build, _post_quote
    factory, account = capital_db
    _, _, _, _, runtime, _ = await boundary(factory, account)
    rec, ex, *_ = _build(exposure=Decimal("0"), quotes=[_post_quote("fUST_a30")],
                         capital_runtime=runtime)
    await rec.deploy()
    planned = ex.ready_submissions[0]
    # The 700 budget, fingerprinted (D3a): below it by less than 0.0001.
    from bfx_funding_bot.modules.execution.amount_fingerprint import fingerprint_of
    sent = Decimal(str(planned.decision.offer_amount_usdt))
    assert Decimal("699.9999") < sent < Decimal("700") and fingerprint_of(sent)
    assert planned.capital_view.applied.revision == 1
    assert planned.capital_view.budget.max_new_offer == Decimal("700")


@pytest.mark.asyncio
async def test_status_shares_policy_budget_and_dry_run_blocks_without_writes(capital_db):
    from bfx_funding_bot.modules.admin.trading_status import TradingStatusService
    from bfx_funding_bot.modules.execution.deployment.submit_attempt import SubmitAttemptRecorder
    from bfx_funding_bot.modules.execution.safety.chain import SafetyGuardChain
    from bfx_funding_bot.modules.execution.safety.hard_guards import CapitalPolicyGuard
    from bfx_funding_bot.modules.marketfeed.health_monitor import HealthProbe
    from bfx_funding_bot.modules.marketfeed.schemas import Phase, StrategyName
    from tests.modules.execution.deployment.test_reconciler import _CapturingSink, _cell
    factory, account = capital_db
    gate, _, ready, ctx, runtime, halt = await boundary(factory, account)
    chain = SafetyGuardChain(guards=[ManualKillGuard(trading_state=halt), CapitalPolicyGuard(runtime=runtime)],
        probe=HealthProbe(), diagnostics=_CapturingSink(), phase=Phase.LIVE,
        strategy=StrategyName.MEAN_REVERSION, cell="fUST_a30", account_id=str(account))
    service = TradingStatusService(chain=chain, ledger=None, account_ctx=ctx,
        cells=[_cell("fUST", "a30"), _cell("fUST", "p2")], caps={}, default_cap=Decimal("0"),
        env_fallback_cap=None, buffers={}, default_buffer=Decimal("0"), env_fallback_buffer=None,
        phase=Phase.LIVE, attempts=SubmitAttemptRecorder(), trading_state=halt, capital_runtime=runtime)
    snapshot = await service.snapshot()
    assert snapshot["account_id"] == str(account)
    assert snapshot["deployment_environment"] == "ci"
    status = snapshot["symbols"]["fUST"]
    missing = (await service.snapshot())["symbols"]["fUSD"]
    assert missing == {"capital_available": False, "reason": "policy_unavailable"}
    from bfx_funding_bot.modules.execution.capital_policy import CapitalPolicy
    async with factory.begin() as session:
        await runtime.repository.apply_policy(session, symbol="fUSD", policy=CapitalPolicy(enabled=False),
            expected_revision=0, source={"operator": "test"})
    disabled = (await service.snapshot())["symbols"]["fUSD"]
    assert disabled["reason"] == "policy_disabled"
    assert disabled["policy_revision"] == 1
    assert disabled["policy"]["enabled"] is False
    assert "available_balance" not in disabled
    assert status["policy_revision"] == 1
    assert status["cells"]["fUST_a30"]["max_new_offer"] == "700.00"
    assert status["cells"]["fUST_p2"]["max_new_offer"] == "700.00"
    before = None
    async with factory() as session:
        before = len((await session.scalars(select(EventLogRow))).all())
    result = await service.dry_run(symbol="fUST", amount=701)
    assert result["account_id"] == str(account)
    assert result["deployment_environment"] == "ci"
    assert not result["would_submit_any"]
    async with factory() as session:
        assert len((await session.scalars(select(EventLogRow))).all()) == before
    async with factory.begin() as session:
        row = await session.get(ExecutionDecisionRow, ready.decision_id)
        row.cell_id = "fUST_a30"
    await gate.submit(ready, ctx)
    # First cell has only 200 headroom, but the second can still spend 300.
    result = await service.dry_run(symbol="fUST", amount=300)
    assert result["would_submit_any"]
    assert not result["symbols"]["fUST"]["cells"]["fUST_a30"]["would_submit"]
    assert result["symbols"]["fUST"]["cells"]["fUST_p2"]["would_submit"]
    async with factory.begin() as session:
        await runtime.repository.begin_snapshot(session, now_ms=1100)
    unavailable = (await service.snapshot())["symbols"]["fUST"]
    assert unavailable["capital_available"] is False
    assert "snapshot_query_pending" in unavailable["reason"]


class Venue:
    def __init__(self, factory):
        self.factory = factory
        self.received = []

    async def submit(self, ready, ctx, *, cid, reservation_ref):
        async with self.factory() as session:
            events = (await session.scalars(select(EventLogRow))).all()
            assert any(row.event_type == "RESERVATION_INTENT" for row in events)
        self.received.append(ready)
        return SubmittedOrder(cid=cid, venue_offer_id="101", outcome=SubmitAcknowledged("101"))

    async def cancel(self, **kwargs):
        async with self.factory() as session:
            events = (await session.scalars(select(EventLogRow))).all()
            assert any(row.event_type == "CANCEL_REQUESTED" for row in events)
        self.received.append(kwargs["venue_offer_id"])


def stop_chain(halt, account, *guards):
    """The production chain shape: the trading-state guard, then any others."""
    from bfx_funding_bot.modules.execution.safety.chain import SafetyGuardChain
    from bfx_funding_bot.modules.marketfeed.health_monitor import HealthProbe
    from bfx_funding_bot.modules.marketfeed.schemas import Phase, StrategyName
    from tests.modules.execution.deployment.test_reconciler import _CapturingSink
    return SafetyGuardChain(
        guards=[ManualKillGuard(trading_state=halt), *guards],
        probe=HealthProbe(), diagnostics=_CapturingSink(), phase=Phase.LIVE,
        strategy=StrategyName.MEAN_REVERSION, cell="a30", account_id=str(account))


async def boundary(factory, account):
    from bfx_funding_bot.modules.execution.capital_runtime import CapitalRuntime
    from bfx_funding_bot.modules.execution.command_gate import DatabaseOpenUncertaintyReader
    repo = repository(account)
    await setup_policy(factory, repo, reserve="0", fraction="0.70")
    await snapshot(factory, repo)
    runtime = CapitalRuntime(repository=repo, session_factory=factory, clock=lambda: 1100)
    view = await runtime.read(symbol="fUST", cell_id="a30")
    event, row = intent(account, AMOUNT, 10)
    from tests.external.bitfinex.test_funding_rules import evidence
    from tests.modules.execution.deployment.test_reconciler import _valid_snapshot
    async with factory.begin() as session:
        session.add(row)
    ready = ReadyToSubmit(
        decision=DecisionPayload(decision_outcome=DecisionOutcome.POST,
            signal_correlation_id=event.signal_correlation_id, offer_rate=0.0001,
            offer_amount_usdt=float(AMOUNT), offer_duration_days=2, symbol="fUST"),
        decision_id=row.decision_id, policy=ExecutionPolicy.BOOK_GUARDED,
        market_snapshot_id="book", model_version=None, evidence={},
        safety=GuardResult(True, "test"), capital_view=view,
        market_snapshot=replace(_valid_snapshot(), snapshot_id="book", max_age_ms=30000),
        funding_amount_evidence=evidence(),
    )
    halt = TradingStateRepository(factory, account_id=account, deployment_environment="ci")
    await halt.transition("ACTIVE", cause="operator", reason="isolated test only", actor="test",
                          now_ms=1200)
    venue = Venue(factory)
    gate = AccountCommandGate(venue, bus=DomainEventBus(),
        persister=EventStorePersister(store=PostgresEventStore(deployment_environment="ci"),
                                     session_factory=factory),
        uncertainty_reader=DatabaseOpenUncertaintyReader(factory),
        safety_evaluator=stop_chain(halt, account), deployment_environment="ci",
        capital_runtime=runtime, clock=lambda: 1100, is_simulated=False)
    ctx = AccountContext(str(account), Credentials("mock", "mock"), Decimal("0"))
    return gate, venue, ready, ctx, runtime, halt


@pytest.mark.asyncio
@pytest.mark.parametrize("change,reason", [
    ("revision", "capital_policy_revision_changed"),
    ("snapshot", "capital_snapshot_changed"),
    ("pending", "snapshot_query_pending"),
    ("halt", "trading state HALTED"),
])
async def test_queued_ready_cannot_send_after_authority_changes(capital_db, change, reason):
    factory, account = capital_db
    gate, venue, ready, ctx, runtime, halt = await boundary(factory, account)
    if change == "revision":
        async with factory.begin() as session:
            await runtime.repository.apply_policy(session, symbol="fUST",
                policy=ready.capital_view.applied.policy, expected_revision=1, source={})
    elif change == "snapshot":
        await snapshot(factory, runtime.repository, available="200")
    elif change == "pending":
        async with factory.begin() as session:
            await runtime.repository.begin_snapshot(session, now_ms=1100)
    else:
        await halt.transition("HALTED", cause="operator", reason="stop", actor="test", now_ms=1)
    with pytest.raises(CommandGateBlocked, match=reason):
        await gate.submit(ready, ctx)
    assert venue.received == []
    async with factory() as session:
        assert not (await session.scalars(select(EventLogRow).where(
            EventLogRow.event_type == "RESERVATION_INTENT"))).all()


async def second_ready(factory, account, ready, amount="199.99990200"):
    """Another planned offer, so a stop is tested against a submit that could run."""
    event, row = intent(account, amount, 20)
    async with factory.begin() as session:
        session.add(row)
    return replace(ready, decision_id=row.decision_id, decision=ready.decision.model_copy(
        update={"signal_correlation_id": event.signal_correlation_id,
                "offer_amount_usdt": float(amount)}))


@pytest.mark.asyncio
@pytest.mark.parametrize("stop", ["HALTED"])
async def test_stop_blocks_submit_but_cancel_stays_durable_before_io(capital_db, stop):
    """HALTED refuses every new offer; cancelling a managed offer is what the
    state exists to allow, and it is still made durable first."""
    factory, account = capital_db
    gate, venue, ready, ctx, runtime, halt = await boundary(factory, account)
    await gate.submit(ready, ctx)  # managed offer 101 while ACTIVE
    later = await second_ready(factory, account, ready)
    await halt.transition(stop, cause="operator", reason="stop", actor="test", now_ms=1)
    venue.received.clear()
    with pytest.raises(CommandGateBlocked, match=f"trading state {stop}"):
        await gate.submit(later, ctx)
    assert venue.received == []
    # Venue.cancel asserts CANCEL_REQUESTED is committed before it is reached.
    await gate.cancel(venue_offer_id="101", signal_correlation_id=uuid4(),
                      account_id=ctx.account_id, ctx=ctx)
    assert venue.received == ["101"]
    async with factory() as session:
        intents = (await session.scalars(select(EventLogRow).where(
            EventLogRow.event_type == "RESERVATION_INTENT"))).all()
    assert len(intents) == 1
    # A cancel ACK never releases capital inside this boundary.
    view = await runtime.read(symbol="fUST", cell_id="a30")
    assert view.budget.spendable == Decimal("1000") - Decimal(AMOUNT)


@pytest.mark.asyncio
async def test_cancel_admission_needs_an_evaluator_that_knows_cancel_exemptions(capital_db):
    """A bare guard cannot tell a cancel from a submit; the gate refuses rather
    than guessing which guards to skip."""
    factory, account = capital_db
    gate, venue, ready, ctx, _, halt = await boundary(factory, account)
    await gate.submit(ready, ctx)
    venue.received.clear()
    gate._safety_evaluator = ManualKillGuard(trading_state=halt)
    with pytest.raises(CommandGateBlocked, match="cancel_eligibility_unavailable"):
        await gate.cancel(venue_offer_id="101", signal_correlation_id=uuid4(),
                          account_id=ctx.account_id, ctx=ctx)
    assert venue.received == []


@pytest.mark.integration
@pytest.mark.asyncio
async def test_halt_writer_waits_for_authorization_lock(pg_session_factory):
    import asyncio

    from sqlalchemy import text

    from bfx_funding_bot.core.writer_lock import acquire_transaction_lock
    from bfx_funding_bot.modules.accounts.tables import ExchangeAccount
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
            for _ in range(100):
                async with factory() as observer:
                    waiting = await observer.scalar(text(
                        "SELECT count(*) FROM pg_stat_activity WHERE wait_event_type='Lock' "
                        "AND query LIKE 'SELECT pg_advisory_xact_lock%'"
                    ))
                if waiting:
                    break
                await asyncio.sleep(0.01)
            assert waiting, "halt writer never joined the account authorization lock"
            assert not task.done()
        await task
        assert (await halt.current()).state == "HALTED"
    finally:
        if task is not None:
            await task


@pytest.mark.asyncio
async def test_authorization_failure_after_append_rolls_back_before_transport(capital_db, monkeypatch):
    factory, account = capital_db
    gate, venue, ready, ctx, runtime, _ = await boundary(factory, account)
    original = runtime.repository.authorize_and_append_intent

    async def fail_after_append(*args, **kwargs):
        await original(*args, **kwargs)
        raise RuntimeError("injected transaction failure")

    monkeypatch.setattr(runtime.repository, "authorize_and_append_intent", fail_after_append)
    with pytest.raises(RuntimeError, match="injected transaction failure"):
        await gate.submit(ready, ctx)
    assert venue.received == []
    async with factory() as session:
        assert not (await session.scalars(select(EventLogRow).where(
            EventLogRow.event_type == "RESERVATION_INTENT"))).all()


@pytest.mark.asyncio
async def test_locked_guard_observes_same_transaction_halt(capital_db, monkeypatch):
    factory, account = capital_db
    gate, venue, ready, ctx, runtime, _ = await boundary(factory, account)
    from bfx_funding_bot.modules.execution.safety.tables import TradingStateRow
    original = runtime.repository.authorize_and_append_intent

    async def halt_in_transaction(session, **kwargs):
        async def guarded(locked):
            locked.add(TradingStateRow(exchange_account_id=account, deployment_environment="ci",
                state="HALTED", cause="operator", reason="uncommitted halt",
                actor="test", created_at_ms=0))
            await locked.flush()
            await kwargs["locked_guard"](locked)
        return await original(session, **{**kwargs, "locked_guard": guarded})

    monkeypatch.setattr(runtime.repository, "authorize_and_append_intent", halt_in_transaction)
    with pytest.raises(CommandGateBlocked, match="uncommitted halt"):
        await gate.submit(ready, ctx)
    assert venue.received == []


@pytest.mark.integration
@pytest.mark.asyncio
async def test_independent_command_gates_cannot_spend_same_budget(pg_session_factory):
    import asyncio

    from bfx_funding_bot.modules.accounts.tables import ExchangeAccount
    from bfx_funding_bot.modules.execution.command_gate import DatabaseOpenUncertaintyReader
    factory = pg_session_factory
    account = uuid4()
    async with factory.begin() as session:
        session.add(ExchangeAccount(id=account, venue="bitfinex", label="two-gates"))
    gate, venue, first, ctx, runtime, halt = await boundary(factory, account)
    # A distinct fingerprint: only the shared budget may decide between them.
    event, row = intent(account, "499.99990501", 20)
    async with factory.begin() as session:
        session.add(row)
    second = replace(first, decision_id=row.decision_id, decision=first.decision.model_copy(
        update={"signal_correlation_id": event.signal_correlation_id,
                "offer_amount_usdt": 499.99990501}))
    competitor = AccountCommandGate(venue, bus=DomainEventBus(), persister=gate._persister,
        uncertainty_reader=DatabaseOpenUncertaintyReader(factory),
        safety_evaluator=ManualKillGuard(trading_state=halt), deployment_environment="ci",
        capital_runtime=runtime, clock=lambda: 1100, is_simulated=False)
    results = await asyncio.gather(gate.submit(first, ctx), competitor.submit(second, ctx),
                                   return_exceptions=True)
    assert sum(isinstance(r, SubmittedOrder) for r in results) == 1
    assert sum(isinstance(r, CommandGateBlocked) for r in results) == 1
    assert len(venue.received) == 1


@pytest.mark.asyncio
async def test_final_amount_cannot_round_up_across_capital_guard(capital_db):
    factory, account = capital_db
    gate, venue, ready, ctx, _, _ = await boundary(factory, account)
    altered = ready.decision.model_copy(update={"offer_amount_usdt": 700.0000000000001})
    async with factory.begin() as session:
        row = await session.get(ExecutionDecisionRow, ready.decision_id)
        row.amount_usdt = Decimal("700.0000000000001")
    with pytest.raises(CommandGateBlocked):
        await gate.submit(replace(ready, decision=altered), ctx)
    assert venue.received == []


@pytest.mark.asyncio
@pytest.mark.parametrize("identity", ["unknown", "cross_environment", "cross_account", "forged_projection", "runtime_environment"])
async def test_cancel_requires_scoped_managed_provenance(capital_db, identity):
    from bfx_funding_bot.modules.accounts.tables import ExchangeAccount
    from bfx_funding_bot.modules.execution.event_store.tables import OfferClaimRow
    factory, account = capital_db
    gate, venue, ready, ctx, runtime, _ = await boundary(factory, account)
    await gate.submit(ready, ctx)
    venue.received.clear()
    target = "999" if identity in {"unknown", "forged_projection"} else "101"
    async with factory.begin() as session:
        claim = await session.scalar(select(OfferClaimRow))
        if identity == "cross_environment":
            claim.deployment_environment = "shadow"
        elif identity == "cross_account":
            other = uuid4()
            session.add(ExchangeAccount(id=other, venue="bitfinex", label="other"))
            await session.flush()
            claim.exchange_account_id = other
            claim.account_id = str(other)
        elif identity == "forged_projection":
            claim.venue_offer_id = "999"
    if identity == "runtime_environment":
        runtime.repository = repository(account, environment="shadow")
    with pytest.raises(CommandGateBlocked, match=r"cancel_(provenance|environment)"):
        await gate.cancel(venue_offer_id=target, signal_correlation_id=uuid4(),
                          account_id=ctx.account_id, ctx=ctx)
    assert venue.received == []
    async with factory() as session:
        assert not (await session.scalars(select(EventLogRow).where(
            EventLogRow.event_type == "CANCEL_REQUESTED"))).all()


@pytest.mark.asyncio
async def test_account_retired_after_planning_cannot_send(capital_db):
    from bfx_funding_bot.modules.accounts.tables import ExchangeAccount
    factory, account = capital_db
    gate, venue, ready, ctx, _, _ = await boundary(factory, account)
    async with factory.begin() as session:
        row = await session.get(ExchangeAccount, account)
        row.lifecycle_status = "retired"
    with pytest.raises(CommandGateBlocked, match="account_inactive"):
        await gate.submit(ready, ctx)
    assert venue.received == []


@pytest.mark.asyncio
@pytest.mark.parametrize("stop", ["HALTED"])
async def test_stopped_reconcile_never_reposts(capital_db, stop):
    """While HALTED the planner places nothing: the symbol is skipped before
    sizing or reprice (lending envelope D3/D4). Pulling the managed offers is
    the managed sweep's job (test_pre_trade_limits); none is wired here."""
    from tests.modules.execution.deployment.test_reconciler import (
        _REPRICE,
        _build,
        _post_quote,
        _venue_offer,
    )
    factory, account = capital_db
    gate, venue, ready, ctx, runtime, halt = await boundary(factory, account)
    await gate.submit(ready, ctx)
    venue.received.clear()
    await halt.transition(stop, cause="operator", reason="retained startup halt", actor="test")
    rec, _, _, _ = _build(exposure=Decimal("0"), quotes=[_post_quote("fUST_a30")],
        executor=gate, safety=stop_chain(halt, account), capital_runtime=runtime,
        canceller=gate, reprice=_REPRICE)
    rec._ctx = ctx
    await rec.deploy(venue_offers=(_venue_offer("101", 0.001),))
    assert venue.received == []
    async with factory() as session:
        types = [row.event_type for row in (await session.scalars(select(EventLogRow))).all()]
    assert types.count("CANCEL_REQUESTED") == 0
    assert types.count("RESERVATION_INTENT") == 1  # only the offer placed while ACTIVE


@pytest.mark.asyncio
async def test_real_guard_chain_reuses_authorization_session_without_double_reserving(capital_db):
    import asyncio

    from bfx_funding_bot.modules.execution.safety.chain import SafetyGuardChain
    from bfx_funding_bot.modules.execution.safety.hard_guards import (
        CapitalPolicyGuard,
        DatabaseUncertaintyReader,
        UncertaintyGuard,
    )
    from bfx_funding_bot.modules.marketfeed.health_monitor import HealthProbe
    from bfx_funding_bot.modules.marketfeed.schemas import Phase, StrategyName
    from tests.modules.execution.deployment.test_reconciler import _CapturingSink
    factory, account = capital_db
    gate, venue, ready, ctx, runtime, halt = await boundary(factory, account)
    gate._safety_evaluator = SafetyGuardChain(
        guards=[ManualKillGuard(trading_state=halt), UncertaintyGuard(
            reader=DatabaseUncertaintyReader(factory), deployment_environment="ci"),
            CapitalPolicyGuard(runtime=runtime)],
        probe=HealthProbe(), diagnostics=_CapturingSink(), phase=Phase.LIVE,
        strategy=StrategyName.MEAN_REVERSION, cell="a30", account_id=str(account))
    result = await asyncio.wait_for(gate.submit(ready, ctx), timeout=5)
    assert result.status == "submitted"
    assert len(venue.received) == 1


@pytest.mark.asyncio
async def test_cancel_fault_rolls_back_evidence_and_never_calls_transport(capital_db, monkeypatch):
    from bfx_funding_bot.modules.execution.events import CancelRequested
    factory, account = capital_db
    gate, venue, ready, ctx, runtime, _ = await boundary(factory, account)
    await gate.submit(ready, ctx)
    venue.received.clear()
    original = runtime.repository.writer.append

    async def fail(session, event):
        result = await original(session, event)
        if isinstance(event, CancelRequested):
            raise RuntimeError("injected cancel durability failure")
        return result

    monkeypatch.setattr(runtime.repository.writer, "append", fail)
    with pytest.raises(RuntimeError, match="injected cancel durability failure"):
        await gate.cancel(venue_offer_id="101", signal_correlation_id=uuid4(),
                          account_id=ctx.account_id, ctx=ctx)
    assert venue.received == []
    async with factory() as session:
        assert not (await session.scalars(select(EventLogRow).where(
            EventLogRow.event_type == "CANCEL_REQUESTED"))).all()


@pytest.mark.asyncio
async def test_capital_probe_does_not_commit_projection_replay(capital_db):
    from bfx_funding_bot.modules.execution.event_store.tables import ProjectionHeadRow
    from bfx_funding_bot.modules.execution.safety.hard_guards import CapitalPolicyGuard
    factory, account = capital_db
    _, _, ready, ctx, runtime, _ = await boundary(factory, account)
    async with factory.begin() as session:
        cursor = await session.scalar(select(ProjectionHeadRow))
        cursor.last_event_seq = 0
    result = await CapitalPolicyGuard(runtime=runtime).evaluate(ready.decision, replace(ctx, capital_cell_id="a30"))
    assert result.allowed
    async with factory() as session:
        assert (await session.scalar(select(ProjectionHeadRow))).last_event_seq == 0


async def cancel_http_boundary(factory, account, http, *, state="ACTIVE"):
    """Real gate/guards/adapter; only HTTP transport is supplied by the test.

    ``state`` is the trading state the cancel runs under: the managed offer is
    placed while ACTIVE, then the state changes. Cancelling must behave the
    same in every state -- only uncertainty and provenance decide it.
    """
    from bfx_funding_bot.external.bitfinex.live_executor import BitfinexLiveExecutor
    from bfx_funding_bot.modules.execution.safety.chain import SafetyGuardChain
    from bfx_funding_bot.modules.execution.safety.hard_guards import (
        CapitalPolicyGuard,
        DatabaseUncertaintyReader,
        UncertaintyGuard,
    )
    from bfx_funding_bot.modules.marketfeed.health_monitor import HealthProbe
    from bfx_funding_bot.modules.marketfeed.schemas import Phase, StrategyName
    from tests.modules.execution.deployment.test_reconciler import _CapturingSink

    gate, _, ready, ctx, runtime, halt = await boundary(factory, account)
    await gate.submit(ready, ctx)  # Establish a known managed offer, no HTTP.
    if state != "ACTIVE":
        await halt.transition(state, cause="operator", reason="stopped during cancel", actor="test")
    gate._safety_evaluator = SafetyGuardChain(
        guards=[ManualKillGuard(trading_state=halt), UncertaintyGuard(
            reader=DatabaseUncertaintyReader(factory), deployment_environment="ci"),
            CapitalPolicyGuard(runtime=runtime)],
        probe=HealthProbe(), diagnostics=_CapturingSink(), phase=Phase.LIVE,
        strategy=StrategyName.MEAN_REVERSION, cell="a30", account_id=str(account))
    gate._inner = BitfinexLiveExecutor(
        http=http, event_sink=_CapturingSink(), bus=DomainEventBus(), phase=Phase.LIVE,
        strategy=StrategyName.MEAN_REVERSION, configured_symbols=frozenset({"fUST"}),
        cell="a30", nonce_provider=lambda: 123456789,
    )
    return gate, ctx, runtime


async def append_cancel_race_unknown(factory, account, *, symbol="fUST", environment="ci"):
    """Recovery-like immutable UNKNOWN, projected through the actual writer."""
    from bfx_funding_bot.modules.accounts.tables import ExchangeAccount
    from bfx_funding_bot.modules.execution.events import ReservationUnknown

    event, decision = intent(account, amount="10", cid=77, symbol=symbol)
    event = replace(event, submission_attempt=replace(event.submission_attempt, environment=environment))
    decision.deployment_environment = environment
    repo = repository(account, environment=environment)
    async with factory.begin() as session:
        if await session.get(ExchangeAccount, account) is None:
            session.add(ExchangeAccount(id=account, venue="bitfinex", label="adjacent-test"))
            await session.flush()
        session.add(decision)
        await session.flush()
        await repo.writer.append(session, event)
        await repo.writer.append(session, ReservationUnknown(
            symbol=symbol, cid=event.cid, amount=Decimal("10"),
            signal_correlation_id=event.signal_correlation_id, account_id=str(account),
            is_simulated=True, reservation_ref=event.reservation_ref,
            reason="synthetic recovery during cancel", occurred_at_ms=1200,
        ))


@pytest.mark.parametrize("state", ["ACTIVE", "HALTED"])
@pytest.mark.parametrize("fault", ["unknown", "unreadable"])
async def test_cancel_after_admission_rechecks_uncertainty_before_first_http(capital_db, monkeypatch, fault,
                                                                             state):
    import asyncio

    import httpx

    from bfx_funding_bot.modules.execution.safety.hard_guards import DatabaseUncertaintyReader

    factory, account = capital_db
    requests = []

    async def transport(request):
        requests.append(request)
        return httpx.Response(200, json=[0, "foc-req", None, None, None, 0, "SUCCESS", None, "ok"])

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as http:
        gate, ctx, _ = await cancel_http_boundary(factory, account, http, state=state)
        original_guard = gate._guard
        injected = False

        async def after_admission(decision, context, **kwargs):
            nonlocal injected
            if context.command_session is None and not injected:
                injected = True
                async with factory() as observer:
                    assert await observer.scalar(select(EventLogRow.event_seq).where(
                        EventLogRow.event_type == "CANCEL_REQUESTED")) is not None
                if fault == "unknown":
                    await append_cancel_race_unknown(factory, account)
                else:
                    async def unavailable(*args, **kwargs):
                        raise RuntimeError("synthetic uncertainty read failure")
                    monkeypatch.setattr(DatabaseUncertaintyReader, "list_open", unavailable)
            # Scheduling hook only: never fake a guard verdict.
            return await original_guard(decision, context, **kwargs)

        monkeypatch.setattr(gate, "_guard", after_admission)
        with pytest.raises(CommandGateBlocked, match="uncertainty"):
            await asyncio.wait_for(gate.cancel(venue_offer_id="101", signal_correlation_id=uuid4(),
                account_id=str(account), ctx=ctx), timeout=10)
    assert injected
    assert requests == []


@pytest.mark.parametrize("state", ["HALTED"])
async def test_stop_refuses_submit_before_the_http_adapter(capital_db, state):
    """No new offer reaches the real adapter under a stop, even one already planned."""
    import httpx

    from bfx_funding_bot.external.bitfinex.live_executor import BitfinexLiveExecutor
    from bfx_funding_bot.modules.marketfeed.schemas import Phase, StrategyName
    from tests.modules.execution.deployment.test_reconciler import _CapturingSink

    factory, account = capital_db
    requests = []

    async def transport(request):
        requests.append(request)
        return httpx.Response(500)

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as http:
        gate, _, ready, ctx, _, halt = await boundary(factory, account)
        gate._inner = BitfinexLiveExecutor(
            http=http, event_sink=_CapturingSink(), bus=DomainEventBus(), phase=Phase.LIVE,
            strategy=StrategyName.MEAN_REVERSION, configured_symbols=frozenset({"fUST"}),
            cell="a30", nonce_provider=lambda: 123456789,
        )
        await halt.transition(state, cause="operator", reason="stop", actor="test")
        with pytest.raises(CommandGateBlocked, match=f"trading state {state}"):
            await gate.submit(ready, ctx)
    assert requests == []


@pytest.mark.parametrize("state", ["HALTED"])
async def test_stop_after_intent_commit_is_not_sent(capital_db, state):
    """A stop landing between the durable intent and transport ends the
    command as NOT_SENT: the transport recheck keeps the trading-state gate."""
    factory, account = capital_db
    gate, venue, ready, ctx, _, halt = await boundary(factory, account)
    original_guard = gate._guard

    async def stop_before_transport(decision, context, **kwargs):
        if kwargs.get("transport"):
            await halt.transition(state, cause="operator", reason="stop mid-command", actor="test")
        return await original_guard(decision, context, **kwargs)

    gate._guard = stop_before_transport
    result = await gate.submit(ready, ctx)
    assert result.outcome_kind.value == "not_sent"
    assert f"trading state {state}" in result.outcome.reason
    assert venue.received == []
    async with factory() as session:
        types = [row.event_type for row in (await session.scalars(select(EventLogRow))).all()]
    assert types.count("RESERVATION_INTENT") == 1
    assert types.count("RESERVATION_FAILED") == 1
