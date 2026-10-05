"""Managed-only capital authority (D2) and fingerprint UNKNOWN resolution (D3a).

Real recovery, capital classifier, command gate and migrated PostgreSQL; only
the venue is fake. The foreign offer is what a manual offer placed in the
Bitfinex UI looks like to the bot; the UNKNOWN is a real gate submit whose
response was lost.
"""
from __future__ import annotations

import itertools
from dataclasses import replace
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from bfx_funding_bot.core.venue_time import VENUE_CLOCK_TOLERANCE_MS
from bfx_funding_bot.external.bitfinex.auth_rest import (
    FundingOfferHistory,
    FundingOfferHistoryCoverage,
)
from bfx_funding_bot.modules.execution.amount_fingerprint import fingerprints_in_use
from bfx_funding_bot.modules.execution.audit.tables import ExecutionDecisionRow
from bfx_funding_bot.modules.execution.boot_recovery import (
    SYSTEM_RESOLVER,
    UNKNOWN_SETTLE_MS,
    BootRecovery,
)
from bfx_funding_bot.modules.execution.bus import DomainEventBus
from bfx_funding_bot.modules.execution.capital_policy_read import CapitalBlockedError
from bfx_funding_bot.modules.execution.capital_tables import CapitalSnapshotRow
from bfx_funding_bot.modules.execution.command_gate import CommandGateBlocked
from bfx_funding_bot.modules.execution.event_store.store import PostgresEventStore
from bfx_funding_bot.modules.execution.event_store.tables import (
    EventLogRow,
    OfferClaimRow,
    VenueOfferStateRow,
)
from bfx_funding_bot.modules.execution.protocols import AccountContext, Credentials, SubmittedOrder
from bfx_funding_bot.modules.execution.submit_outcomes import (
    SubmitAcknowledged,
    SubmitOutcomeUnknown,
)
from bfx_funding_bot.modules.execution.uncertainty_tables import (
    ExecutionUncertaintyRow,
    SubmissionAttemptRow,
)
from bfx_funding_bot.modules.observability import alerts
from bfx_funding_bot.modules.trading import CapitalPolicy, fingerprint_of

from .test_automatic_protection import FakeAuth, Recorder, _offer
from .test_capital_command_boundary import AMOUNT, boundary, second_ready
from .test_capital_repository import repository

D = Decimal

pytestmark = pytest.mark.integration

# The gate's clock (boundary) is 1100: the UNKNOWN attempt starts there.
ATTEMPT_START = 1100
SETTLED = ATTEMPT_START + UNKNOWN_SETTLE_MS + 1_000


@pytest.fixture
def capital_db(migrated_db):
    return migrated_db


class History(FakeAuth):
    """FakeAuth whose offer history can be reported incomplete."""

    def __init__(self, *, complete: bool = True, **kwargs) -> None:
        super().__init__(**kwargs)
        self.complete = complete

    async def get_funding_offer_history(self, *, ctx, start_ms, end_ms, symbol=None):
        result = await super().get_funding_offer_history(ctx=ctx, start_ms=start_ms,
                                                         end_ms=end_ms, symbol=symbol)
        return FundingOfferHistory(offers=result.offers, coverage=FundingOfferHistoryCoverage(
            requested_start_ms=start_ms, requested_end_ms=end_ms,
            oldest_mts_created=result.coverage.oldest_mts_created,
            newest_mts_created=result.coverage.newest_mts_created,
            pages=1, complete=self.complete))


def recovery(factory, account, auth, protection, *, start) -> BootRecovery:
    clock = itertools.count(start)
    return BootRecovery(store=PostgresEventStore(deployment_environment="ci"),
        session_factory=factory, auth_rest=auth,
        account_ctx=AccountContext(str(account), Credentials("k", "s"), D("0")),
        deployment_environment="ci", bus=DomainEventBus(), symbols=["fUST", "fUSD"],
        clock=lambda: next(clock), uncertainty_handler=_noop,
        capital_repository=repository(account), protection=protection)


async def _noop(_event) -> None:
    return None


async def read(factory, account, symbol="fUST", *, now):
    async with factory() as session:
        return await repository(account).read_capital(session, symbol=symbol, cell_id="a30",
                                                      now_ms=now)


@pytest.fixture
def emitted(monkeypatch):
    sent: list[tuple[str, dict[str, object]]] = []
    monkeypatch.setattr(alerts, "emit", lambda event, **fields: sent.append((event, fields)))
    return sent


# ------------------------------------------------------------ foreign (D2)


@pytest.mark.asyncio
async def test_foreign_offer_shrinks_the_budget_without_halt_quarantine_or_cancel(
        capital_db, emitted):
    """A manual 300 fUST offer: the wallet shows 700 available. No trip, no
    uncertainty, no quarantine breadcrumb; the budget is the 700 that is left
    (not 1000), and the operator hears about it once, not once per reconcile."""
    factory, account = capital_db
    gate, venue, _, ctx, _, _ = await boundary(factory, account)
    auth = FakeAuth(offers=[_offer("777", "fUST", "300", "300")],
                    wallets={"fUST": D("700"), "fUSD": D("0")})
    recorder = Recorder()
    reconcile = recovery(factory, account, auth, recorder, start=5_000)
    first = await reconcile.run()
    second = await reconcile.run()

    assert recorder.trips == []
    assert first.unmanaged_offer_ids == second.unmanaged_offer_ids == frozenset({"777"})
    assert [(event, fields["venue_offer_id"]) for event, fields in emitted
            if event == alerts.FOREIGN_EXPOSURE] == [(alerts.FOREIGN_EXPOSURE, "777")]
    async with factory() as session:
        assert await session.scalar(select(func.count()).select_from(ExecutionUncertaintyRow)) == 0
        assert await session.scalar(select(func.count()).select_from(EventLogRow).where(
            EventLogRow.event_type == "VENUE_OFFER_QUARANTINED")) == 0
        latest = await session.scalar(select(CapitalSnapshotRow)
                                      .order_by(CapitalSnapshotRow.event_seq.desc()).limit(1))
    assert latest.authorization_blocked_reason is None
    assert latest.classification["foreign"] == {"777": {"symbol": "fUST", "amount": "300"}}
    view = await read(factory, account, now=5_100)
    assert view.snapshot.total_capital == D("700")
    assert view.snapshot.cell_exposure == D("0")
    assert view.budget.max_new_offer == D("490")  # 0.70 x 700; with the offer managed, 0
    # Never the bot's to cancel: the gate has no provenance to admit it against.
    with pytest.raises(CommandGateBlocked, match="cancel_provenance_missing"):
        await gate.cancel(venue_offer_id="777", signal_correlation_id=uuid4(),
                          account_id=str(account), ctx=ctx)
    assert venue.received == []


@pytest.mark.asyncio
async def test_foreign_offer_next_to_a_managed_one_keeps_managed_exposure_exact(
        capital_db, emitted):
    factory, account = capital_db
    gate, _, ready, ctx, _, _ = await boundary(factory, account)
    await gate.submit(ready, ctx)  # managed offer "101"
    auth = FakeAuth(offers=[_offer("101", "fUST", AMOUNT, AMOUNT), _offer("777", "fUST", "40", "40")],
                    wallets={"fUST": D("1000") - D(AMOUNT) - D("40"), "fUSD": D("0")})
    recorder = Recorder()
    result = await recovery(factory, account, auth, recorder, start=5_000).run()
    assert recorder.trips == []
    assert result.unmanaged_offer_ids == frozenset({"777"})
    view = await read(factory, account, now=5_100)
    assert view.snapshot.cell_exposure == D(AMOUNT)
    assert view.snapshot.total_capital == D("960")


# ------------------------------------------------- UNKNOWN resolution (D3a)


async def _unknown_submit(factory, account):
    """A real gate submit whose response was lost: UNKNOWN on fUST."""
    gate, venue, ready, ctx, _, _ = await boundary(factory, account)

    async def lost(ready_, ctx_, *, cid, reservation_ref):
        venue.received.append(ready_)
        return SubmittedOrder(cid=cid, venue_offer_id=None,
            outcome=SubmitOutcomeUnknown(reason="transport_timeout", transport_started=True))

    venue.submit = lost
    result = await gate.submit(ready, ctx)
    assert result.outcome_kind.value == "unknown"
    async with factory.begin() as session:
        # A disabled fUSD policy, so the other symbol's capital can be read.
        await repository(account).apply_policy(session, symbol="fUSD",
            policy=CapitalPolicy(enabled=False), expected_revision=0, source={"test": True})
    return gate, ready, ctx


async def _state(factory):
    async with factory() as session:
        attempt = (await session.scalars(select(SubmissionAttemptRow))).one()
        uncertainty = (await session.scalars(select(ExecutionUncertaintyRow))).one()
        claim = (await session.scalars(select(OfferClaimRow))).one()
    return attempt, uncertainty, claim


def _ours(offer_id="900", *, created=ATTEMPT_START + 5, status="ACTIVE", amount=AMOUNT):
    return _offer(offer_id, "fUST", amount, amount, status=status, created=created)


@pytest.mark.asyncio
async def test_unknown_whose_fingerprint_is_on_the_book_is_claimed(capital_db, emitted):
    factory, account = capital_db
    await _unknown_submit(factory, account)
    wallets = {"fUST": D("1000") - D(AMOUNT), "fUSD": D("0")}
    auth = FakeAuth(offers=[_ours()], wallets=wallets)
    result = await recovery(factory, account, auth, Recorder(), start=SETTLED).run()

    assert (result.n_matched, result.n_not_sent) == (1, 0)
    attempt, uncertainty, claim = await _state(factory)
    assert (attempt.outcome_kind, attempt.venue_offer_id) == ("acknowledged", "900")
    assert (uncertainty.state, uncertainty.resolved_by_operator_id) == ("resolved", SYSTEM_RESOLVER)
    assert (claim.state, claim.venue_offer_id) == ("claimed", "900")
    # Resolution was not foreign exposure, and the next snapshot reflects it.
    assert not [event for event, _ in emitted if event == alerts.FOREIGN_EXPOSURE]
    await recovery(factory, account, auth, Recorder(), start=SETTLED + 10_000).run()
    view = await read(factory, account, now=SETTLED + 10_100)
    assert view.snapshot.cell_exposure == D(AMOUNT)
    assert view.snapshot.unreflected_commitments == D("0")


@pytest.mark.asyncio
async def test_unknown_absent_from_complete_history_is_not_sent_and_frees_its_symbol(capital_db):
    factory, account = capital_db
    await _unknown_submit(factory, account)
    auth = FakeAuth(wallets={"fUST": D("1000"), "fUSD": D("0")})
    result = await recovery(factory, account, auth, Recorder(), start=SETTLED).run()

    assert (result.n_matched, result.n_not_sent) == (0, 1)
    attempt, uncertainty, claim = await _state(factory)
    # The audit keeps what the submit was; the fingerprint is free again.
    assert (attempt.outcome_kind, claim.state) == ("unknown", "unknown")
    assert (uncertainty.state, uncertainty.resolved_by_operator_id) == ("resolved", SYSTEM_RESOLVER)
    assert uncertainty.resolution_evidence["candidate_count"] == 0
    async with factory() as session:
        held = await fingerprints_in_use(session, account_id=account, environment="ci",
                                         symbol="fUST")
    assert fingerprint_of(D(AMOUNT)) not in held
    # Taken while the UNKNOWN was open, this snapshot cannot free fUST ...
    with pytest.raises(CapitalBlockedError, match="execution_unknown"):
        await read(factory, account, now=SETTLED + 100)
    # ... the next one, taken after the resolution, does.
    await recovery(factory, account, auth, Recorder(), start=SETTLED + 10_000).run()
    view = await read(factory, account, now=SETTLED + 10_100)
    assert view.snapshot.total_capital == D("1000")
    assert view.snapshot.unreflected_commitments == D("0")
    async with factory() as session:
        latest = await session.scalar(select(CapitalSnapshotRow)
                                      .order_by(CapitalSnapshotRow.event_seq.desc()).limit(1))
    assert str(attempt.attempt_id) in latest.classification["settled"]
    # Idempotent: another reconcile resolves nothing twice.
    again = await recovery(factory, account, auth, Recorder(), start=SETTLED + 20_000).run()
    assert (again.n_matched, again.n_not_sent) == (0, 0)


@pytest.mark.asyncio
@pytest.mark.parametrize("case", [
    "incomplete_history", "two_candidates", "before_settle", "fingerprint_other_rate",
])
async def test_unknown_without_decisive_evidence_stays_open_and_holds_only_its_symbol(
        capital_db, case):
    factory, account = capital_db
    await _unknown_submit(factory, account)
    wallets = {"fUST": D("1000"), "fUSD": D("0")}
    start = SETTLED
    if case == "incomplete_history":
        auth: FakeAuth = History(complete=False, wallets=wallets)
    elif case == "two_candidates":
        auth = FakeAuth(offers=[_ours("900"), _ours("901", created=ATTEMPT_START + 9)],
                        wallets=wallets)
    elif case == "before_settle":
        auth, start = FakeAuth(offers=[_ours()], wallets=wallets), ATTEMPT_START + 60_000
    else:
        # The fingerprint at another rate is not a match -- but not proof of
        # absence either: wait for an operator rather than call it not sent.
        auth = FakeAuth(offers=[replace(_ours(), rate=0.00011, rate_decimal=D("0.00011"))],
                        wallets=wallets)
    result = await recovery(factory, account, auth, Recorder(), start=start).run()

    attempt, uncertainty, _ = await _state(factory)
    assert (result.n_matched, result.n_not_sent) == (0, 0)
    assert (attempt.outcome_kind, uncertainty.state) == ("unknown", "open")
    async with factory() as session:
        held = await fingerprints_in_use(session, account_id=account, environment="ci",
                                         symbol="fUST")
    assert fingerprint_of(D(AMOUNT)) in held  # nothing new may reuse it meanwhile
    # Level 2: the open UNKNOWN withholds fUST, but the snapshot was accepted
    # and the other symbol's capital is readable.
    async with factory() as session:
        latest = await session.scalar(select(CapitalSnapshotRow)
                                      .order_by(CapitalSnapshotRow.event_seq.desc()).limit(1))
    assert latest.classification["unresolved"] == {str(attempt.attempt_id): "fUST"}
    with pytest.raises(CapitalBlockedError, match="execution_unknown"):
        await read(factory, account, now=start + 100)
    other = await read(factory, account, "fUSD", now=start + 100)
    assert other.budget.reason == "policy_disabled"


@pytest.mark.asyncio
async def test_an_older_offer_with_the_same_amount_is_not_the_unknowns(capital_db):
    """Created before the submit started -- by more than the venue's
    whole-second stamping and clock skew explain -- it cannot be its offer:
    complete history over the attempt's own window has nothing, so it was not
    sent. (One ms earlier is not enough: the venue stamps our own offers up to
    a second before the attempt's local start.)"""
    factory, account = capital_db
    await _unknown_submit(factory, account)
    older = _ours(created=ATTEMPT_START - VENUE_CLOCK_TOLERANCE_MS - 1)
    auth = FakeAuth(offers=[older], wallets={"fUST": D("1000") - D(AMOUNT), "fUSD": D("0")})
    result = await recovery(factory, account, auth, Recorder(), start=SETTLED).run()
    assert (result.n_matched, result.n_not_sent) == (0, 1)
    assert result.unmanaged_offer_ids == frozenset({"900"})


@pytest.mark.asyncio
async def test_a_foreign_offer_at_our_terms_but_another_fingerprint_is_not_the_unknowns(
        capital_db):
    """Same symbol, rate, period, type and flags, created inside the window --
    only the amount's last decimals differ. That is exactly what the fingerprint
    is for: the offer stays foreign and the UNKNOWN resolves as not sent."""
    factory, account = capital_db
    await _unknown_submit(factory, account)
    lookalike = _ours("950", amount="499.99990501")
    auth = FakeAuth(offers=[lookalike],
                    wallets={"fUST": D("1000") - D("499.99990501"), "fUSD": D("0")})
    result = await recovery(factory, account, auth, Recorder(), start=SETTLED).run()
    assert (result.n_matched, result.n_not_sent) == (0, 1)
    assert result.unmanaged_offer_ids == frozenset({"950"})
    attempt, _, claim = await _state(factory)
    assert attempt.venue_offer_id is None and claim.venue_offer_id is None


@pytest.mark.asyncio
async def test_a_sole_candidate_already_attributed_elsewhere_is_not_claimed(capital_db):
    """Defence in depth behind fingerprint uniqueness: if the only candidate is
    already bound to another identity, binding it again would be a second
    story for one offer. The UNKNOWN stays open and the reconcile still lands."""
    factory, account = capital_db
    await _unknown_submit(factory, account)
    auth = FakeAuth(offers=[_ours()], wallets={"fUST": D("1000") - D(AMOUNT), "fUSD": D("0")})
    early = await recovery(factory, account, auth, Recorder(), start=ATTEMPT_START + 60_000).run()
    assert early.n_matched == 0  # before the settle window
    async with factory.begin() as session:
        state = await session.get(VenueOfferStateRow, (account, "ci", "900"))
        state.cid, state.execution_decision_id = 4242, "someone-else"
    result = await recovery(factory, account, auth, Recorder(), start=SETTLED).run()
    assert (result.n_matched, result.n_not_sent) == (0, 0)
    attempt, uncertainty, _ = await _state(factory)
    assert (attempt.outcome_kind, uncertainty.state) == ("unknown", "open")


# ------------------------------------------------------------ fingerprints


@pytest.mark.asyncio
async def test_gate_refuses_an_amount_without_a_fingerprint(capital_db):
    factory, account = capital_db
    gate, venue, ready, ctx, _, _ = await boundary(factory, account)
    async with factory.begin() as session:
        row = await session.get(ExecutionDecisionRow, ready.decision_id)
        row.amount_usdt = D("500")
    plain = replace(ready, decision=ready.decision.model_copy(update={"offer_amount_usdt": D("500")}))
    with pytest.raises(CommandGateBlocked, match="amount_fingerprint_missing"):
        await gate.submit(plain, ctx)
    assert venue.received == []


@pytest.mark.asyncio
async def test_gate_refuses_a_fingerprint_an_open_commitment_holds(capital_db):
    """Two acknowledged-but-unresolved submits sharing a fingerprint would make
    the venue's answer ambiguous; the second never gets an intent."""
    factory, account = capital_db
    gate, venue, ready, ctx, _, _ = await boundary(factory, account)
    await gate.submit(ready, ctx)  # claimed, fingerprint of AMOUNT now held
    clash = await second_ready(factory, account, ready, amount="199.99990500")
    with pytest.raises(CommandGateBlocked, match="amount_fingerprint_collision"):
        await gate.submit(clash, ctx)
    distinct = await second_ready(factory, account, ready, amount="199.99990501")

    async def another_offer(ready_, ctx_, *, cid, reservation_ref):
        venue.received.append(ready_)
        return SubmittedOrder(cid=cid, venue_offer_id="102", outcome=SubmitAcknowledged("102"))

    venue.submit = another_offer
    await gate.submit(distinct, ctx)
    assert len(venue.received) == 2
