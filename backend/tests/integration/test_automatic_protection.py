"""Automatic protections through the real recovery, capital classifier and gate.

SQLite and PostgreSQL (``capital_db``); only the venue is fake. Each trigger is
raised by the component that meets the condition in production, and the ones
that must never halt (a loan ending, a fill caught by reconcile, a first
observation) are run through the same path.
"""
from __future__ import annotations

import asyncio
import itertools
from decimal import Decimal

import pytest
from sqlalchemy import select

from bfx_funding_bot.core.health import HealthProbe
from bfx_funding_bot.core.telemetry import Phase
from bfx_funding_bot.external.bitfinex.auth_rest import (
    ActiveFundingCredit,
    ActiveFundingOffer,
    FundingOfferHistory,
    FundingOfferHistoryCoverage,
)
from bfx_funding_bot.modules.execution.boot_recovery import BootRecovery
from bfx_funding_bot.modules.execution.bus import DomainEventBus
from bfx_funding_bot.modules.execution.capital_policy_read import CapitalBlockedError
from bfx_funding_bot.modules.execution.command_gate import CommandGateBlocked
from bfx_funding_bot.modules.execution.event_store.entities import VenueCreditObservation
from bfx_funding_bot.modules.execution.event_store.store import PostgresEventStore
from bfx_funding_bot.modules.execution.protocols import AccountContext, Credentials, SubmittedOrder
from bfx_funding_bot.modules.execution.safety.chain import SafetyGuardChain
from bfx_funding_bot.modules.execution.safety.hard_guards import ManualKillGuard
from bfx_funding_bot.modules.execution.safety.protection import AutomaticProtection
from bfx_funding_bot.modules.execution.safety.tables import FundingCancelAllAuditRow
from bfx_funding_bot.modules.execution.submit_outcomes import SubmitOutcomeUnknown
from bfx_funding_bot.modules.strategy import StrategyName

from .test_capital_command_boundary import AMOUNT, boundary, second_ready
from .test_capital_repository import repository, setup_policy, snapshot
from .test_kill_switch import FakeVenue

D = Decimal


pytestmark = pytest.mark.integration


@pytest.fixture
def capital_db(migrated_db):
    """The migrated PostgreSQL schema: its triggers are the rules' authority."""
    return migrated_db


class Recorder:
    def __init__(self) -> None:
        self.trips: list[tuple[str, str]] = []
        self.clean: list[int | None] = []

    def trip(self, trigger: str, detail: str) -> None:
        self.trips.append((trigger, detail))

    def observe_clean(self, evidence: str | None) -> None:
        self.clean.append(evidence)

    @property
    def triggers(self) -> set[str]:
        return {trigger for trigger, _ in self.trips}


class FakeAuth:
    """Venue REST as recovery reads it; identical answers for the confirmation."""

    def __init__(self, *, offers=(), credits=(), wallets=None, history=()) -> None:
        self.offers, self.credits, self.history = list(offers), list(credits), tuple(history)
        self.wallets = wallets or {"fUST": D("1000"), "fUSD": D("0")}

    async def get_active_funding_offers(self, *, ctx, symbol=None):
        return list(self.offers)

    async def get_active_funding_credits(self, *, ctx, symbol=None):
        return list(self.credits)

    async def get_active_funding_loans(self, **kwargs):
        return []

    async def get_funding_available_all(self, *, ctx):
        return dict(self.wallets)

    async def get_funding_offer_history(self, *, ctx, start_ms, end_ms, symbol=None):
        created = [offer.mts_created for offer in self.history]
        return FundingOfferHistory(offers=self.history, coverage=FundingOfferHistoryCoverage(
            requested_start_ms=start_ms, requested_end_ms=end_ms,
            oldest_mts_created=min(created) if created else None,
            newest_mts_created=max(created) if created else None, pages=1, complete=True))


def _offer(offer_id, symbol, remaining, original, status="ACTIVE", created=1500):
    return ActiveFundingOffer(venue_offer_id=offer_id, symbol=symbol, amount=D(remaining),
        rate=0.0001, period_days=2, mts_created=created, mts_updated=created, status=status,
        amount_original=D(original), offer_type="LIMIT", flags=0, rate_decimal=D("0.0001"))


def _credit(credit_id, symbol, amount):
    return ActiveFundingCredit(credit_id=credit_id, symbol=symbol, amount=D(amount), rate=0.0003,
                               period_days=2, status="ACTIVE", mts_created=1500, mts_updated=1500)


async def _noop(_event) -> None:
    return None


def recovery(factory, account, auth, protection, *, start=5000) -> BootRecovery:
    clock = itertools.count(start)
    return BootRecovery(store=PostgresEventStore(deployment_environment="ci"),
        session_factory=factory, auth_rest=auth,
        account_ctx=AccountContext(str(account), Credentials("k", "s"), D("0")),
        deployment_environment="ci", bus=DomainEventBus(), symbols=["fUST", "fUSD"],
        clock=lambda: next(clock), uncertainty_handler=_noop,
        capital_repository=repository(account), protection=protection)


# ------------------------------------------------------------- triggers


# A foreign offer (no claim, no attempt) trips nothing since lending envelope
# D2; test_managed_offers runs it through this same recovery and gate.


@pytest.mark.asyncio
async def test_offer_amount_mismatch_trips(capital_db):
    factory, account = capital_db
    gate, _, ready, ctx, _, _ = await boundary(factory, account)
    await gate.submit(ready, ctx)  # managed offer 101, 500 claimed
    auth = FakeAuth(offers=[_offer("101", "fUST", "500", "5000")],
                    wallets={"fUST": D("500"), "fUSD": D("0")})
    recorder = Recorder()
    with pytest.raises(CapitalBlockedError, match="offer_amount_conflict"):
        await recovery(factory, account, auth, recorder).run()
    assert recorder.triggers == {"offer_amount_mismatch"}


@pytest.mark.asyncio
async def test_unclassifiable_commitment_trips(capital_db):
    factory, account = capital_db
    gate, _, ready, ctx, _, _ = await boundary(factory, account)
    await gate.submit(ready, ctx)
    # The acknowledged offer is neither on the book nor in complete history.
    auth = FakeAuth(wallets={"fUST": D("500"), "fUSD": D("0")})
    recorder = Recorder()
    with pytest.raises(CapitalBlockedError, match="unclassifiable_commitment"):
        await recovery(factory, account, auth, recorder).run()
    assert recorder.triggers == {"unclassifiable_commitment"}


@pytest.mark.asyncio
async def test_lent_our_offers_cannot_explain_trips(capital_db):
    """Positive control: a baselined ledger with nothing lent and no offer
    that could have filled, then a 392.4 loan appears at the venue."""
    factory, account = capital_db
    await boundary(factory, account)
    auth = FakeAuth(credits=[_credit("c-1", "fUST", "392.4")],
                    wallets={"fUST": D("607.6"), "fUSD": D("0")})
    recorder = Recorder()
    await recovery(factory, account, auth, recorder).run()
    assert recorder.triggers == {"venue_lent_above_ledger"}
    assert "unexplained=392.4" in recorder.trips[0][1]
    # An accepted snapshot that tripped is not evidence a halt's condition cleared.
    assert recorder.clean == []


@pytest.mark.asyncio
async def test_interrupted_submit_recovered_as_unknown_quarantines_without_tripping(capital_db):
    """Lending envelope D3 level 2: the open uncertainty holds its symbol; nothing halts."""
    factory, account = capital_db
    _, _, _, _, runtime, _ = await boundary(factory, account)
    from .test_capital_repository import intent
    event, decision = intent(account, "150", 31)
    async with factory.begin() as session:
        session.add(decision)
        await session.flush()
        await runtime.repository.writer.append(session, event)  # PENDING, never finished
    recorder = Recorder()
    result = await recovery(factory, account, FakeAuth(), recorder, start=500_000).run()
    assert result.n_unknown == 1
    assert recorder.trips == []


# ---------------------------------------------------------- never trips


@pytest.mark.asyncio
async def test_replay_2026_09_24_loan_end_does_not_trip(capital_db):
    """realized_drift=150.77638588 reserved_drift=0: the canary loan ended."""
    factory, account = capital_db
    repo = repository(account)
    await setup_policy(factory, repo, reserve="0", fraction="0.70")
    await snapshot(factory, repo, available="849.22361412", credits=(VenueCreditObservation(
        credit_id="canary-loan", symbol="fUST", amount=D("150.77638588"), rate=D("0.0003"),
        period_days=2, status="ACTIVE", mts_created=900, mts_updated=900),))
    auth = FakeAuth(wallets={"fUST": D("1000.01"), "fUSD": D("0")})
    recorder = Recorder()
    result = await recovery(factory, account, auth, recorder).run()
    assert result.realized_drift_usdt == D("150.77638588")
    assert result.reserved_drift_usdt == 0
    assert recorder.trips == []
    assert len(recorder.clean) == 1  # clean evidence for an automatic resume


@pytest.mark.asyncio
async def test_replay_migration_catch_up_as_a_first_observation_does_not_trip(capital_db):
    """realized_drift=392.x reserved_drift=0 (2026-08-27..30) read as the first
    observation of a ledger with no venue baseline."""
    factory, account = capital_db
    await setup_policy(factory, repository(account), reserve="0", fraction="0.70")
    auth = FakeAuth(credits=[_credit("c-migrated", "fUST", "392.4")],
                    wallets={"fUST": D("607.6"), "fUSD": D("0")})
    recorder = Recorder()
    result = await recovery(factory, account, auth, recorder).run()
    assert result.realized_drift_usdt == D("392.4")
    assert recorder.trips == []
    assert len(recorder.clean) == 1  # clean evidence for an automatic resume


@pytest.mark.asyncio
async def test_fill_caught_by_reconcile_instead_of_ws_does_not_trip(capital_db):
    factory, account = capital_db
    gate, _, ready, ctx, _, _ = await boundary(factory, account)
    await gate.submit(ready, ctx)  # AMOUNT (the fingerprinted 500) offered in the ledger
    filled = _offer("101", "fUST", "0", AMOUNT, status="EXECUTED at 0.01% (500.0)", created=1100)
    auth = FakeAuth(credits=[_credit("from-101", "fUST", AMOUNT)], history=[filled],
                    wallets={"fUST": D("1000") - D(AMOUNT), "fUSD": D("0")})
    recorder = Recorder()
    result = await recovery(factory, account, auth, recorder).run()
    assert result.reserved_drift_usdt == D(AMOUNT) and result.realized_drift_usdt == D(AMOUNT)
    assert recorder.trips == []
    assert len(recorder.clean) == 1  # clean evidence for an automatic resume


# ------------------------------------------------------- the gate lock


def _guarded_chain(trading, protection, account):
    from tests.modules.execution.deployment.test_reconciler import _CapturingSink
    return SafetyGuardChain(
        guards=[ManualKillGuard(trading_state=trading, pending_stop=protection.pending_reason)],
        probe=HealthProbe(), diagnostics=_CapturingSink(), phase=Phase.LIVE,
        strategy=StrategyName.MEAN_REVERSION, cell="a30", account_id=str(account))


@pytest.mark.asyncio
async def test_an_unknown_submit_blocks_its_symbol_but_never_halts(capital_db):
    """D3 level 2: an ambiguous submit leaves trading ACTIVE and cancels nothing;
    the next submit on that symbol is refused until the UNKNOWN is resolved."""
    factory, account = capital_db
    gate, venue, ready, ctx, _, trading = await boundary(factory, account)
    protection = AutomaticProtection()
    gate._safety_evaluator = _guarded_chain(trading, protection, account)
    gate.protection = protection
    cancel_venue = FakeVenue(factory, account, {"UST": set()})
    protection.bind(trading)

    async def unknown(ready_, ctx_, *, cid, reservation_ref):
        venue.received.append(ready_)
        return SubmittedOrder(cid=cid, venue_offer_id=None,
            outcome=SubmitOutcomeUnknown(reason="transport_timeout", transport_started=True))

    venue.submit = unknown
    result = await asyncio.wait_for(gate.submit(ready, ctx), timeout=10)
    assert result.outcome_kind.value == "unknown"
    await protection.run_pending()
    assert protection.pending_reason() is None
    assert (await trading.current()).state == "ACTIVE"
    assert cancel_venue.calls == []
    later = await second_ready(factory, account, ready)
    with pytest.raises(CommandGateBlocked):
        await gate.submit(later, ctx)
    assert len(venue.received) == 1


@pytest.mark.asyncio
async def test_a_trip_blocks_the_next_submit_before_halted_is_written(capital_db):
    factory, account = capital_db
    gate, venue, ready, ctx, _, trading = await boundary(factory, account)
    protection = AutomaticProtection()
    gate._safety_evaluator = _guarded_chain(trading, protection, account)
    protection.trip("loss_limiter", "realized_loss_pct_24h[fUST]=6 > 5")
    assert (await trading.current()).state == "ACTIVE"  # nothing durable yet
    with pytest.raises(CommandGateBlocked, match="automatic protection tripped"):
        await gate.submit(await second_ready(factory, account, ready), ctx)
    assert venue.received == []


@pytest.mark.asyncio
async def test_identity_conflict_trips(capital_db):
    """The managed commitment's decision says another currency: two durable
    records disagree about what the commitment is. The attempt inventory at the
    capital fence finds it before any venue call."""
    from bfx_funding_bot.modules.execution.audit.tables import ExecutionDecisionRow
    factory, account = capital_db
    gate, _, ready, ctx, _, _ = await boundary(factory, account)
    await gate.submit(ready, ctx)
    async with factory.begin() as session:
        (await session.get(ExecutionDecisionRow, ready.decision_id)).symbol = "fUSD"
    auth = FakeAuth(offers=[_offer("101", "fUST", "500", "500")],
                    wallets={"fUST": D("500"), "fUSD": D("0")})
    recorder = Recorder()
    await recovery(factory, account, auth, recorder).run()
    assert recorder.triggers == {"identity_conflict"}
    assert "refused the fence: attempt_decision_conflict" in recorder.trips[0][1]


@pytest.mark.asyncio
async def test_a_persisting_condition_writes_one_halt_and_never_the_cancel_all(capital_db):
    """An identity conflict re-trips every reconcile tick; only the first tick
    writes HALTED. No protection calls the venue: the planner pulls managed
    offers, and the venue cancel-all is the operator's kill alone (D3)."""
    from bfx_funding_bot.modules.execution.audit.tables import ExecutionDecisionRow
    factory, account = capital_db
    gate, _, ready, ctx, _, trading = await boundary(factory, account)
    await gate.submit(ready, ctx)
    async with factory.begin() as session:
        (await session.get(ExecutionDecisionRow, ready.decision_id)).symbol = "fUSD"
    venue = FakeVenue(factory, account, {"UST": {"101"}})
    protection = AutomaticProtection()
    protection.bind(trading)
    auth = FakeAuth(offers=[_offer("101", "fUST", "500", "500")],
                    wallets={"fUST": D("500"), "fUSD": D("0")})
    tick = recovery(factory, account, auth, protection)
    for _ in range(3):
        await tick.run()
        await protection.run_pending()
    async with factory() as session:
        rows = (await session.scalars(select(FundingCancelAllAuditRow))).all()
    assert venue.calls == [] and rows == []  # never the venue cancel-all
    halts = [h for h in await trading.history(limit=10) if h.state == "HALTED"]
    assert len(halts) == 1  # the first tick only
    assert protection.persisting == 2
    assert protection.pending_reason() is None
    state = await trading.current()
    assert (state.state, state.cause, state.actor) == ("HALTED", "auto", "auto:identity_conflict")
