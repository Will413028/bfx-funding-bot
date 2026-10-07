"""The ledger is the oracle of the simulated venue (ADR 2026-10-03 D2).

The REAL ledger observation cycle (``BitfinexVenueObservation`` over ``BitfinexAuthREST`` into
``build_observation_sink``) and the REAL command gate over the REAL ``BitfinexLiveExecutor`` talk
to the simulated venue through ``httpx.AsyncClient(transport=<venue>)``; the venue's event log is
the SQL store in the same migrated database, written as ``bfx_bot``. A seeded random sequence of
submits, cancels, fills, credit expiries and interest payouts runs; after every cycle:

* the cycle is accepted and its basis is conservation ``baseline`` (the first) or ``conserved``,
  never ``unexplained_lending`` or ``foreign_lending``, with no failed fill reconciliation;
* nothing is quarantined, and no outcome is UNKNOWN except the three injected ones, each of
  which the next cycles resolve by themselves;
* the simulator reports no ``internal_failures`` and no ``unexpected`` request.

Mutations (one at a time, revert after each, run this file):

* the simulator drops the history row of a cancelled offer (``decide`` cancel): the property
  test fails (the ledger sees an offer vanish without a terminal row).
* a fill returns no principal on close / pays gross interest: wallet conservation fails.
* the TICK_AFTER hook is not run in the transport: ``test_a_fill_between_the_reads...`` fails.
"""
from __future__ import annotations

import random
from dataclasses import dataclass, field, replace
from decimal import Decimal
from typing import Any
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from bfx_funding_bot.apps.config import CAPITAL_MAX_SNAPSHOT_AGE_MS
from bfx_funding_bot.core.telemetry import Phase
from bfx_funding_bot.external.bitfinex.auth_rest import BitfinexAuthREST
from bfx_funding_bot.external.bitfinex.live_executor import BitfinexLiveExecutor
from bfx_funding_bot.external.bitfinex.nonce import AuthRequestGate
from bfx_funding_bot.modules.execution.bus import DomainEventBus
from bfx_funding_bot.modules.execution.command_boundary import CommandBoundary, LedgerCommandEffects
from bfx_funding_bot.modules.execution.command_gate import AccountCommandGate
from bfx_funding_bot.modules.execution.contracts import ExecutionPolicy, GuardResult, ReadyToSubmit
from bfx_funding_bot.modules.execution.protocols import AccountContext, Credentials
from bfx_funding_bot.modules.execution.venue_observation import BitfinexVenueObservation
from bfx_funding_bot.modules.ledger import RUNTIME_GRACE_MS, CapitalAvailable, CycleResult
from bfx_funding_bot.modules.ledger.matching import UNKNOWN_SETTLE_MS
from bfx_funding_bot.modules.ledger.tables import (
    ExecutionResolutionJournalRow,
    LedgerObservationRow,
    TransportOutcomeJournalRow,
)
from bfx_funding_bot.modules.ledger.wiring import (
    build_capital_authority,
    build_command_journal,
    build_managed_offer_reader,
    build_observation_sink,
    build_policy_store,
    build_uncertainty_reader,
)
from bfx_funding_bot.modules.simulated_venue import (
    FaultKind,
    FaultPlan,
    FaultRule,
    FaultTarget,
    FixtureMarketFeed,
    HistoryFilter,
    SimAccount,
    SimulatedVenue,
    SimulatedVenueConfig,
    SqlVenueEventStore,
)
from bfx_funding_bot.modules.simulated_venue.wiring import build_simulated_venue
from bfx_funding_bot.modules.strategy import DecisionOutcome, DecisionPayload, StrategyName
from bfx_funding_bot.modules.trading import CapitalPolicy, CapitalScope
from tests.modules.marketfeed.account_test_helpers import (
    TEST_EXCHANGE_ACCOUNT_ID,
    seed_exchange_account,
)
from tests.modules.simulated_venue.helpers import (
    API_KEY,
    API_SECRET,
    DAY,
    HOUR,
    Clock,
    book,
    trade,
)

from .bot_e2e import CELL, POLICY, SCOPE, AllowGuard, ledger_db  # noqa: F401 - fixture dependency

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

ACCOUNT_ID = str(TEST_EXCHANGE_ACCOUNT_ID)
SIM = SimAccount(ACCOUNT_ID, "ci")
CAPITAL_SCOPE = CapitalScope(TEST_EXCHANGE_ACCOUNT_ID, "ci", "fUST", CELL)
CONSERVED = {"baseline", "conserved"}
INJECTED = (FaultKind.UNKNOWN_PLACED_LOST, FaultKind.UNKNOWN_NOT_PLACED_LOST,
            FaultKind.UNKNOWN_5XX_ERROR)
SEEDS = (11, 23, 37, 41, 59)


class _Allow(AllowGuard):
    """The bot's safety chain is not under test: allow submits and cancels."""

    async def evaluate_cancel(self, decision: Any, context: Any) -> GuardResult:
        return GuardResult(allowed=True, guard_name="test")


class _NullSink:
    async def emit(self, event: dict[str, Any]) -> None:
        return None


@dataclass
class Rig:
    factory: async_sessionmaker[Any]
    clock: Clock
    feed: FixtureMarketFeed
    venue: SimulatedVenue
    gate: AccountCommandGate
    sink: Any
    capital: Any
    ctx: AccountContext
    rng: random.Random
    next_fingerprint: int = 1
    periods_used: int = 0
    injected: int = 0
    old_ended: int = 0  # offers that ended minutes after the observation that saw them rest
    cycles: list[CycleResult] = field(default_factory=list)

    # -- the real observation cycle -------------------------------------------------
    async def observe(self) -> CycleResult:
        result = await self.sink.run(SCOPE)
        self.cycles.append(result)
        return result

    async def observe_accepted(self) -> CycleResult:
        result = await self.observe()
        assert result.decision == "accepted", result
        await self.assert_sound()
        return result

    async def assert_sound(self) -> None:
        async with self.factory() as session:
            rows = (await session.execute(text(
                "SELECT symbol, conservation, lent_unexplained, foreign_executed, fill_conflicts "
                "FROM accepted_capital_basis_symbol"))).all()
            quarantines = await session.scalar(text("SELECT count(*) FROM quarantine_opening"))
        assert rows, "no accepted basis yet"
        for symbol, verdict, unexplained, foreign, conflicts in rows:
            assert verdict in CONSERVED, (symbol, verdict, unexplained, foreign, conflicts)
            assert (unexplained, foreign, conflicts) == (0, 0, 0), (symbol, verdict)
        assert quarantines == 0
        assert self.venue.internal_failures == [] and self.venue.unexpected == []

    async def unknown_outcomes(self) -> int:
        async with self.factory() as session:
            return int(await session.scalar(select(func.count()).select_from(
                TransportOutcomeJournalRow).where(TransportOutcomeJournalRow.kind == "unknown")) or 0)

    async def resolutions(self) -> int:
        async with self.factory() as session:
            return int(await session.scalar(
                select(func.count()).select_from(ExecutionResolutionJournalRow)) or 0)

    async def has_open(self) -> bool:
        return await self.gate._uncertainty_reader.has_open(None, SCOPE, "fUST")

    # -- real submit and cancel through the gate and the live executor ---------------
    async def ready(self, amount: Decimal, rate: Decimal, days: int) -> ReadyToSubmit:
        from tests.external.bitfinex.test_funding_rules import evidence
        from tests.modules.execution.deployment.test_reconciler import _valid_snapshot

        now = self.clock()
        view = await self.capital.read(CAPITAL_SCOPE, now_ms=now)
        assert isinstance(view, CapitalAvailable), view
        decision_id, correlation = str(uuid4()), uuid4()
        from bfx_funding_bot.modules.execution.audit.tables import ExecutionDecisionRow
        async with self.factory.begin() as session:
            session.add(ExecutionDecisionRow(
                decision_id=decision_id, account_id=ACCOUNT_ID,
                exchange_account_id=TEST_EXCHANGE_ACCOUNT_ID, deployment_environment="ci",
                reconcile_id="oracle", cell_id=CELL, symbol="fUST",
                signal_correlation_id=str(correlation), outcome="ready",
                signal_rate=rate, applied_rate=rate, amount_usdt=amount, duration_days=days,
                model_evidence={}, safety_result={}, execution_policy="oracle",
                service_version="test", config_hash="test", occurred_at_ms=now,
                recorded_at_ms=now,
            ))
        return ReadyToSubmit(
            decision=DecisionPayload(
                decision_outcome=DecisionOutcome.POST, signal_correlation_id=correlation,
                offer_rate=rate, offer_amount_usdt=amount, offer_duration_days=days,
                symbol="fUST"),
            decision_id=decision_id, policy=ExecutionPolicy.BOOK_GUARDED,
            market_snapshot_id="book", model_version=None, evidence={},
            safety=GuardResult(True, "test"), capital_view=view,
            market_snapshot=replace(_valid_snapshot(), snapshot_id="book", captured_at_ms=now,
                                    received_at_ms=now, max_age_ms=30_000),
            funding_amount_evidence=evidence(now=now),
        )

    def amount(self) -> Decimal:
        # The gate demands a unique non-zero 4-digit fingerprint (``fingerprint_of``).
        fingerprint, self.next_fingerprint = self.next_fingerprint, self.next_fingerprint + 7
        return Decimal(150 + self.rng.randrange(0, 200)) + Decimal(fingerprint) / Decimal(10**8)

    def refresh_books(self) -> None:
        for symbol in ("fUST", "fUSD"):
            self.feed.add_book(book(symbol, self.clock.now, [
                (f"0.000{self.rng.randrange(1, 4)}", 2, str(self.rng.randrange(50, 800))),
                ("0.0003", 30, "400"),
            ]))

    async def submit(self, *, days: int | None = None) -> Any:
        self.refresh_books()
        rate = Decimal(self.rng.randrange(8, 30)) / Decimal(100_000)
        ready = await self.ready(self.amount(), rate, days or self.rng.choice((2, 2, 3)))
        return await self.gate.submit(ready, self.ctx)

    def fresh_period(self) -> int:
        """A period no other scenario uses: its virtual queue holds this offer alone."""
        self.periods_used += 1
        return 3 + self.periods_used

    async def cancel(self, offer_id: int) -> None:
        await self.gate.cancel(
            venue_offer_id=str(offer_id), signal_correlation_id=uuid4(),
            account_id=ACCOUNT_ID, ctx=self.ctx)

    def fill(self, offer_id: int, fraction: Decimal) -> None:
        """Public volume that fills ``fraction`` of the offer a few seconds from now."""
        offer = self.venue.state.offers[offer_id]
        assert not [i for i in offer.ahead_ids if self.venue.state.offers[i].resting]
        volume = offer.queue_ahead + (offer.amount_original * fraction).quantize(Decimal("0.00000001"))
        self.feed.add_trades("fUST", [
            trade(self.clock.now + 3_000, str(volume), offer.period, str(offer.rate))])
        self.clock.advance(6_000)

    def expire(self) -> None:
        self.clock.advance(self.rng.randrange(1, 4) * DAY + 5 * 60_000)

    def interest(self) -> None:
        now = self.clock.now
        payout = (now // DAY) * DAY + self.venue._config.payout_offset_ms
        self.clock.now = (payout if payout > now else payout + DAY) + 10 * 60_000

    async def resolve_injected_unknown(self) -> None:
        assert await self.has_open()
        before = await self.resolutions()
        for _ in range(3):
            self.clock.advance(UNKNOWN_SETTLE_MS + 10_000)
            await self.observe_accepted()
            if not await self.has_open():
                break
        assert not await self.has_open(), "the injected UNKNOWN did not resolve by itself"
        assert await self.resolutions() == before + 1
        # The basis that listed the attempt as unresolved keeps capital blocked until the
        # next accepted basis classifies it settled.
        await self.observe_accepted()


def _plan(extra: tuple[FaultRule, ...] = ()) -> FaultPlan:
    return FaultPlan(rules=(
        FaultRule(FaultTarget.SUBMIT, INJECTED[0], ordinals=frozenset({3})),
        FaultRule(FaultTarget.SUBMIT, INJECTED[1], ordinals=frozenset({5})),
        FaultRule(FaultTarget.SUBMIT, INJECTED[2], ordinals=frozenset({7})),
        *extra,
    ))


@pytest_asyncio.fixture
async def rig_factory(ledger_db):  # noqa: F811
    opened: list[tuple[AsyncEngine, AsyncEngine, Any]] = []

    async def make(seed: int, faults: FaultPlan | None, *,
                   history: HistoryFilter | None = None) -> Rig:
        url = ledger_db.url.set(drivername="postgresql+asyncpg").render_as_string(hide_password=False)
        engine = create_async_engine(url)
        # The venue writes its log as the simulation bot would: bfx_bot.
        sim_engine = create_async_engine(url, connect_args={"server_settings": {"role": "bfx_bot"}})
        factory = async_sessionmaker(engine, expire_on_commit=False)
        await seed_exchange_account(engine, capital_policies=False)
        async with engine.begin() as conn:
            # A head database starts on the ledger (genesis); the switch row is appended anyway.
            await conn.execute(text(
                "INSERT INTO capital_authority_epoch (epoch_seq, authority, set_at_ms, actor, reason) "
                "SELECT max(epoch_seq) + 1, 'ledger', 2, 'test', 'oracle' "
                "FROM capital_authority_epoch"))
        policies = build_policy_store(SCOPE)
        async with factory.begin() as session:
            await policies.apply_policy(session, symbol="fUST", policy=POLICY,
                                        expected_revision=0, source={"fixture": True})
            await policies.apply_policy(session, symbol="fUSD", policy=CapitalPolicy(enabled=False),
                                        expected_revision=0, source={"fixture": True})
        clock, feed = Clock(), FixtureMarketFeed()
        feed.add_book(book("fUST", clock.now))
        store = await SqlVenueEventStore.open(sim_engine)
        venue = await build_simulated_venue(
            account=SIM, config=SimulatedVenueConfig(
                api_key=API_KEY, api_secret=API_SECRET,
                **({} if history is None else {"history_filter": history})),
            store=store, feed=feed, clock_ms=clock, faults=faults)
        await venue.fund_wallet("UST", Decimal("20000"))
        http = venue.client()
        auth_gate = AuthRequestGate()
        ctx = AccountContext(ACCOUNT_ID, Credentials(API_KEY, API_SECRET), Decimal("0"))
        rest = BitfinexAuthREST(http=http, auth_gate=auth_gate)
        observation = BitfinexVenueObservation(rest=rest, ctx=ctx, scope=SCOPE, clock_ms=clock)
        sink = build_observation_sink(factory, observation, now_ms=clock, grace_ms=RUNTIME_GRACE_MS)

        bus = DomainEventBus()
        executor = BitfinexLiveExecutor(
            http=http, event_sink=_NullSink(), bus=bus, phase=Phase.LIVE,
            strategy=StrategyName.RATE_PERCENTILE, configured_symbols=frozenset({"fUST"}),
            cell=CELL, auth_gate=auth_gate, clock=clock)
        gate = AccountCommandGate(
            executor, uncertainty_reader=build_uncertainty_reader(factory),
            safety_evaluator=_Allow(), deployment_environment="ci",
            clock=clock,
            boundary=CommandBoundary(
                SCOPE, factory,
                build_command_journal(factory, max_snapshot_age_ms=CAPITAL_MAX_SNAPSHOT_AGE_MS),
                LedgerCommandEffects(bus)),
            managed_offers=build_managed_offer_reader())
        capital = build_capital_authority(factory, max_snapshot_age_ms=CAPITAL_MAX_SNAPSHOT_AGE_MS)
        opened.append((engine, sim_engine, http))
        return Rig(factory, clock, feed, venue, gate, sink, capital, ctx, random.Random(seed))

    yield make
    for engine, sim_engine, http in opened:
        await http.aclose()
        await sim_engine.dispose()
        await engine.dispose()


async def _drive(rig: Rig, *, steps: int) -> None:
    """A seeded random walk; each step is followed by one real observation cycle.

    Offers rest, and any of them may end later (cancel, fill, partial fill), minutes after the
    observation that saw it rest: far beyond the history window's margin, so the ledger must
    find those terminal rows by id (F2). Credits, expiries and interest run over days.
    """
    await rig.observe_accepted()  # the baseline
    submits = 0
    parked: list[int] = []  # resting offers a later step may end; each owns its period
    for _ in range(steps):
        op = rig.rng.choices(
            ["rest", "fill", "partial", "cancel", "expire", "interest", "end_old"],
            weights=[4, 3, 2, 3, 1, 1, 4])[0]
        rig.clock.advance(rig.rng.randrange(1, 9) * 1_000)
        if op in ("rest", "fill", "partial", "cancel"):
            before = await rig.unknown_outcomes()
            submitted = await rig.submit(
                days=rig.fresh_period() if op in ("rest", "fill", "partial") else None)
            submits += 1
            if submitted.outcome_kind.value == "unknown":
                assert await rig.unknown_outcomes() == before + 1
                rig.injected += 1
                await rig.resolve_injected_unknown()
                continue
            assert submitted.outcome_kind.value == "acknowledged", submitted.outcome_kind
            offer_id = int(submitted.venue_offer_id)
            if op == "rest":
                parked.append(offer_id)
            elif op == "cancel":
                await rig.observe_accepted()  # provenance for the offer cancelled below
                rig.clock.advance(rig.rng.randrange(1, 9) * 1_000)
                await rig.cancel(offer_id)
            else:
                rig.fill(offer_id, Decimal(1) if op == "fill" else Decimal("0.4"))
        elif op == "end_old":
            parked = [i for i in parked if rig.venue.state.offers[i].resting]
            if parked:
                offer_id = parked.pop(rig.rng.randrange(len(parked)))
                rig.clock.advance(rig.rng.randrange(2, 10) * 60_000)  # well beyond the 60 s margin
                how = rig.rng.choice(("cancel", "fill", "partial"))
                rig.old_ended += 1
                if how == "cancel":
                    await rig.cancel(offer_id)
                else:
                    rig.fill(offer_id, Decimal(1) if how == "fill" else Decimal("0.4"))
        elif op == "expire":
            rig.expire()
        else:
            rig.interest()
        await rig.observe_accepted()
    # Every credit runs out: the ledger must see each lending end by its own expiry.
    rig.clock.advance(125 * DAY)
    await rig.observe_accepted()
    assert submits >= 7


# Both history filters the venue may use (undocumented; the knob's default is the client's
# assumption): the ledger must stay sound under either when offers end inside the window.
WALKS = ((11, "create"), (23, "update"), (37, "create"), (41, "update"), (59, "create"))


@pytest.mark.parametrize(("seed", "offers_filter"), WALKS)
async def test_random_sequences_keep_every_accepted_basis_conserved(
        rig_factory, seed: int, offers_filter: str) -> None:
    rig = await rig_factory(seed, _plan(), history=HistoryFilter(offers=offers_filter))  # type: ignore[arg-type]
    await _drive(rig, steps=45)
    assert rig.old_ended >= 2, "the walk never ended an old offer"
    assert rig.injected == 3 and await rig.unknown_outcomes() == 3  # exactly the injected ones
    assert await rig.resolutions() == 3  # and each resolved by the cycle, not an operator
    async with rig.factory() as session:
        actors = (await session.execute(select(
            ExecutionResolutionJournalRow.actor_kind, ExecutionResolutionJournalRow.actor_id,
            ExecutionResolutionJournalRow.action))).all()
    assert {(kind, actor) for kind, actor, _ in actors} == {("system", "system:reconcile")}
    assert sorted(action for *_, action in actors) == ["bound_to_venue", "not_accepted", "not_accepted"]
    state = rig.venue.state
    assert {"CANCELED", "EXECUTED", "PARTIAL"} <= {o.status for o in state.offers.values()}
    assert any(lending.status == "CLOSED (expired)" for lending in state.lendings.values())
    assert state.trades and state.ledger, "the walk never filled or paid interest"
    assert [c for c in rig.cycles if c.decision != "accepted"] == []
    async with rig.factory() as session:
        accepted = await session.scalar(select(func.count()).select_from(LedgerObservationRow)
                                        .where(LedgerObservationRow.accepted.is_(True)))
    assert accepted == len(rig.cycles)
    assert rig.venue.internal_failures == [] and rig.venue.unexpected == []


async def test_a_fill_between_the_reads_makes_that_observation_unacceptable_and_the_next_one_is(
        rig_factory) -> None:
    """TICK_AFTER: the world moves between the first and the confirmation read (C6, mutation 20)."""
    box: dict[str, Any] = {"armed_at": None, "ran": 0}

    def hook() -> None:
        rig: Rig = box["rig"]
        if box["armed_at"] == rig.venue.requests:
            box["ran"] += 1
            rig.clock.advance(HOUR)

    plan = FaultPlan(rules=(FaultRule(
        FaultTarget.ANY_REQUEST, FaultKind.TICK_AFTER, ordinals=frozenset(range(1, 500)),
        hook=hook),))
    rig = await rig_factory(5, plan)
    box["rig"] = rig
    await rig.observe_accepted()  # the baseline
    submitted = await rig.submit(days=rig.fresh_period())
    assert submitted.outcome_kind.value == "acknowledged"
    offer_id = int(submitted.venue_offer_id)
    await rig.observe_accepted()  # the offer rests, observed
    offer = rig.venue.state.offers[offer_id]
    rig.feed.add_trades("fUST", [trade(rig.clock.now + HOUR // 2, str(offer.queue_ahead + offer.amount_original),
                                       offer.period, str(offer.rate))])
    accepted_before = len(rig.cycles)

    # wallets, then the offers read: the fill lands right after the second request.
    box["armed_at"] = rig.venue.requests + 2
    result = await rig.observe()
    assert box["ran"] == 1
    assert result.decision == "incomplete_or_unequal"
    assert rig.venue.state.offers[offer_id].status == "EXECUTED"  # the world did move
    async with rig.factory() as session:
        accepted = await session.scalar(select(func.count()).select_from(LedgerObservationRow)
                                        .where(LedgerObservationRow.accepted.is_(True)))
    assert accepted == accepted_before  # no basis from the moving observation

    await rig.observe_accepted()  # the next one converges and conserves the fill
    assert box["ran"] == 1
    async with rig.factory() as session:
        verdicts = (await session.execute(text(
            "SELECT conservation FROM accepted_capital_basis_symbol s JOIN accepted_capital_basis b "
            "ON b.id = s.basis_id ORDER BY b.accepted_at_ms DESC, b.accept_revision DESC LIMIT 1"))).scalars().all()
    assert verdicts == ["conserved"]


@pytest.mark.parametrize("offers_filter", ["create", "update"])
async def test_the_ledger_sees_the_end_of_an_old_offer(rig_factory, offers_filter: str) -> None:
    """F2: an offer far older than the history window ends; the ledger finds its terminal row by id.

    Both filter fields stay: ``update`` is what the live venue does (probe, 2026-10-04);
    ``create`` proves the ledger does not depend on the undocumented field.
    """
    rig = await rig_factory(7, None, history=HistoryFilter(offers=offers_filter))  # type: ignore[arg-type]
    await rig.observe_accepted()
    submitted = await rig.submit()
    await rig.observe_accepted()
    rig.clock.advance(10 * 60_000)  # far beyond the window margin (60 s)
    await rig.observe_accepted()
    await rig.cancel(int(submitted.venue_offer_id))
    await rig.observe_accepted()
    async with rig.factory() as session:
        terminal = (await session.execute(text(
            "SELECT venue_offer_id, terminal_kind FROM venue_offer_mirror"))).all()
    assert terminal == [(submitted.venue_offer_id, "canceled")]


async def test_the_ledger_persists_a_venue_row_with_fractional_numbers(rig_factory) -> None:
    """F1: the venue's fractional JSON numbers reach the JSON column as exact strings."""
    rig = await rig_factory(1, None)
    await rig.observe_accepted()
    await rig.submit()
    await rig.observe_accepted()
    async with rig.factory() as session:
        row = (await session.execute(text("SELECT raw FROM ledger_observation_offer"))).scalars().all()
    assert row and all(isinstance(value, (str, int, type(None))) for r in row for value in r["row"])
    assert any(isinstance(value, str) and "." in value for r in row for value in r["row"])
