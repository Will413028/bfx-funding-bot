"""The simulated-venue bot process, end to end on PostgreSQL (ADR 2026-10-03 D1/D2).

``build_daemon`` composes phase ``shadow`` as production does; see ``sim_daemon`` for what is
controlled. After every cycle each accepted basis is conserved, nothing is unexplained or
quarantined, the simulator reports no internal failure and no unrouted request, and no
authenticated request left the process.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest
import pytest_asyncio

from bfx_funding_bot.modules.simulated_venue import (
    FaultKind,
    FaultPlan,
    FaultRule,
    FaultTarget,
)

from .sim_daemon import DAY, HOUR, SCOPE, T0, SimEnv, close_sim_env, make_sim_env
from .test_ledger_schema_roles import ledger_db  # noqa: F401 - fixture dependency

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


@pytest_asyncio.fixture
async def sim(ledger_db, monkeypatch, httpx_mock, tmp_path):  # noqa: F811
    env, engine = await make_sim_env(ledger_db, monkeypatch, httpx_mock, tmp_path)
    try:
        yield env
    finally:
        await close_sim_env(env, engine)


def _wallet(venue: Any, currency: str = "UST") -> Any:
    return venue.state.wallets[currency]


def _push_trades_after_the_next_request(sim: SimEnv, box: dict[str, Any]) -> FaultPlan:
    """TICK_AFTER: the world moves between two requests of one caller (public volume arrives)."""
    def hook() -> None:
        if box.get("armed"):
            box["armed"] = False
            box["ran"] = box.get("ran", 0) + 1
            offer = box["offer"]
            sim.trades((sim.clock.now + 3_000,
                        str(offer.queue_ahead + offer.amount_original * Decimal("0.4")),
                        offer.period, str(offer.rate)))
            sim.clock.advance(6_000)

    return FaultPlan(rules=(FaultRule(
        FaultTarget.ANY_REQUEST, FaultKind.TICK_AFTER, ordinals=frozenset(range(1, 5000)),
        hook=hook),))


async def test_a_full_simulated_run_conserves_every_accepted_basis(sim: SimEnv) -> None:
    box: dict[str, Any] = {}
    daemon = await sim.build(faults=_push_trades_after_the_next_request(sim, box))
    venue = sim.venue(daemon)
    assert _wallet(venue).balance == Decimal(1000)  # funded once, from config
    # Boot: the observation sink accepts the first basis.
    await sim.boot(daemon, T0)
    await sim.assert_sound(daemon)

    # Tick: the real reconciler deploys toward the standing quote through the real chain.
    await sim.activate()
    sim.clock.advance(HOUR)
    sim.quote(daemon)
    await sim.tick(daemon)
    await sim.assert_sound(daemon)
    offers = list(venue.state.offers.values())
    assert len(offers) == 1 and offers[0].resting, offers
    offer = box["offer"] = offers[0]
    assert offer.symbol == "fUST" and offer.period == 2

    # Public trades arrive between two requests of the next observation: the fill is a
    # partial one, and the observation that straddled it is not accepted, the next is.
    box["armed"] = True
    sim.clock.advance(60_000)
    await sim.tick(daemon)
    assert box["ran"] == 1
    await sim.tick(daemon)
    await sim.assert_sound(daemon)
    assert venue.state.offers[offer.offer_id].status == "PARTIAL"
    assert venue.state.lendings, "a partial fill opens a loan"
    lent = sum((lend.amount for lend in venue.state.lendings.values()), Decimal(0))
    assert lent == offer.amount_original * Decimal("0.4")

    # Time passes beyond the period: the loan expires, principal and interest are in the wallet.
    sim.clock.advance(3 * DAY)
    await sim.tick(daemon)
    await sim.tick(daemon)
    await sim.assert_sound(daemon)
    assert all(lend.status.startswith("CLOSED") for lend in venue.state.lendings.values())
    assert venue.state.ledger, "interest was paid"
    assert _wallet(venue).balance > Decimal(1000)

    # The managed sweep (what a reprice or an automatic stop requests) cancels the resting rest.
    sweep = daemon.periodic_reconcile._deployment._managed_sweep
    open_ids = {o.venue_offer_id for o in await sweep.managed_open()}
    assert str(offer.offer_id) in open_ids, "the partially filled offer is still managed and open"
    assert open_ids == {str(o.offer_id) for o in venue.state.offers.values() if o.resting}
    result = await sweep.cancel(reason="test reprice")
    assert result.failed == () and set(result.requested) == open_ids
    await sim.tick(daemon)
    await sim.assert_sound(daemon)
    assert venue.state.offers[offer.offer_id].status == "CANCELED"
    assert not [o for o in venue.state.offers.values() if o.resting]

    # A fresh offer rests, then the kill switch cancels everything at the venue.
    sim.quote(daemon)
    await sim.tick(daemon)
    assert [o for o in venue.state.offers.values() if o.resting]
    killed = await daemon.trading_control.kill_switch.engage(
        cause="operator", actor="test", reason="end of the run")
    assert killed.complete, killed
    assert not [o for o in venue.state.offers.values() if o.resting]
    await sim.tick(daemon)
    await sim.assert_sound(daemon)
    sim.assert_venue_clean(daemon)


async def test_a_simulated_daemon_composes_no_legacy_state(sim: SimEnv) -> None:
    """Mutation: compose a ``Legacy*`` object (or the projection, registry, persister) here."""
    from tests.apps.walk import legacy_state

    daemon = await sim.build()
    assert legacy_state(daemon, "daemon") == []
    assert daemon.auth_ws is None and daemon.ws_dispatcher is None
    assert daemon.venue_tasks == ()  # the seam replaced the live feed
    handlers = {
        type(getattr(handler, "__self__", None)).__name__
        for handlers in daemon.bus._handlers.values() for handler in handlers
    }
    assert not handlers & {"PaperPositionLedger", "OfferRegistry"}


async def test_a_restart_mid_run_neither_quarantines_nor_refunds(sim: SimEnv) -> None:
    daemon = await sim.build()
    await sim.boot(daemon, T0)
    await sim.activate()
    sim.clock.advance(HOUR)
    sim.quote(daemon)
    await sim.tick(daemon)
    first = sim.venue(daemon)
    offers_before = {i: o.status for i, o in first.state.offers.items()}
    assert offers_before and _wallet(first).balance == Decimal(1000)

    second = await sim.restart(daemon)  # a second ``build_daemon`` over the same database
    venue = sim.venue(second)
    assert venue is not first
    assert _wallet(venue).balance == Decimal(1000), "the wallet was funded again"
    assert {i: o.status for i, o in venue.state.offers.items()} == offers_before
    sim.clock.advance(30_000)
    await sim.boot(second)
    await sim.assert_sound(second)  # no quarantine
    assert not await second.command_gate._uncertainty_reader.has_open(None, SCOPE, "fUST")
    from sqlalchemy import func, select

    from bfx_funding_bot.modules.ledger.tables import TransportOutcomeJournalRow
    async with sim.factory() as session:
        unknown = await session.scalar(select(func.count()).select_from(
            TransportOutcomeJournalRow).where(TransportOutcomeJournalRow.kind == "unknown"))
    assert unknown == 0


@pytest.mark.parametrize(("kind", "action"), [
    (FaultKind.UNKNOWN_PLACED_LOST, "bound_to_venue"),
    (FaultKind.UNKNOWN_NOT_PLACED_LOST, "not_accepted"),
])
async def test_an_injected_unknown_resolves_by_itself(
        ledger_db, monkeypatch, httpx_mock, tmp_path, kind: FaultKind, action: str) -> None:  # noqa: F811
    from sqlalchemy import select

    from bfx_funding_bot.modules.ledger.matching import UNKNOWN_SETTLE_MS
    from bfx_funding_bot.modules.ledger.tables import ExecutionResolutionJournalRow

    sim, engine = await make_sim_env(ledger_db, monkeypatch, httpx_mock, tmp_path)
    try:
        plan = FaultPlan(rules=(FaultRule(FaultTarget.SUBMIT, kind, ordinals=frozenset({1})),))
        daemon = await sim.build(faults=plan)
        await sim.boot(daemon, T0)
        await sim.activate()
        sim.clock.advance(HOUR)
        sim.quote(daemon)
        await sim.tick(daemon)
        reader = daemon.command_gate._uncertainty_reader
        assert await reader.has_open(None, SCOPE, "fUST"), "the lost submit left an UNKNOWN"
        for _ in range(4):
            sim.clock.advance(UNKNOWN_SETTLE_MS + 10_000)
            sim.quote(daemon)
            await sim.tick(daemon)
            if not await reader.has_open(None, SCOPE, "fUST"):
                break
        assert not await reader.has_open(None, SCOPE, "fUST"), "the UNKNOWN did not resolve"
        async with sim.factory() as session:
            rows = (await session.execute(select(
                ExecutionResolutionJournalRow.actor_kind, ExecutionResolutionJournalRow.actor_id,
                ExecutionResolutionJournalRow.action))).all()
        assert [(k, a, act) for k, a, act in rows] == [("system", "system:reconcile", action)]
        sim.quote(daemon)
        await sim.tick(daemon)
        await sim.tick(daemon)
        await sim.assert_sound(daemon)
    finally:
        await close_sim_env(sim, engine)


async def test_every_time_source_follows_the_composition_clock(sim: SimEnv) -> None:
    """Mutations: the signal engine, the gate's date, or the executor's clock/date back on the
    wall (the composition clock sits in 2024; the wall is not)."""
    from datetime import date

    daemon = await sim.build()
    assert daemon.signal_engine._clock() == T0
    gate = daemon.command_gate
    assert gate._clock() == T0 and gate._date_provider() == date(2024, 1, 1)
    executor = daemon.executor
    assert executor._clock() == T0 and executor._date_provider() == date(2024, 1, 1)  # type: ignore[attr-defined]
    sim.clock.advance(DAY)
    assert gate._date_provider() == date(2024, 1, 2)
    assert executor._date_provider() == date(2024, 1, 2)  # type: ignore[attr-defined]


async def test_an_internal_failure_of_the_simulator_fails_the_harness_check(sim: SimEnv) -> None:
    """Mutation: the e2e invariant ignores ``internal_failures`` (this is what would show it)."""
    from bfx_funding_bot.modules.simulated_venue import FixtureMarketFeed

    daemon = await sim.build(feed=FixtureMarketFeed())  # a feed that never has a book
    await sim.boot(daemon, T0)
    await sim.activate()
    sim.clock.advance(HOUR)
    sim.quote(daemon)
    await sim.tick(daemon)  # the submit reaches the venue, which has no book to freeze
    venue = sim.venue(daemon)
    assert [f.kind for f in venue.internal_failures] == ["no_market_data"]
    with pytest.raises(AssertionError):
        sim.assert_venue_clean(daemon)
    assert not venue.state.offers  # and it was not a venue answer: nothing was placed
    venue.internal_failures.clear()
    sim.assert_venue_clean(daemon)


async def test_the_daemon_fills_a_trade_the_live_feed_learned_of_late(sim: SimEnv) -> None:
    """F1 end to end: the real daemon on a ``LiveMarketFeed``; a trade executed between two
    observations that the feed only receives afterwards is still filled, and the ledger
    conserves it."""
    from bfx_funding_bot.modules.simulated_venue import (
        BookSnapshot,
        LiveFeedConfig,
        LiveMarketFeed,
        PublicTrade,
    )

    async def fetch_book(symbol: str) -> BookSnapshot | None:
        return BookSnapshot(symbol, sim.clock.now, ((Decimal("0.0003"), 2, Decimal("800")),))

    async def no_trades(symbol: str, since_ms: int) -> list[PublicTrade]:
        return []

    feed = LiveMarketFeed(config=LiveFeedConfig(symbols=("fUST", "fUSD")), fetch_book=fetch_book,
                          fetch_trades=no_trades, clock_ms=sim.clock)

    def beat() -> None:
        for symbol in ("fUST", "fUSD"):
            feed.alive(symbol, sim.clock.now)

    feed.stream_connected()
    await feed.backfill_gaps()
    await feed.refresh_books()
    daemon = await sim.build(feed=feed)
    venue = sim.venue(daemon)
    await sim.boot(daemon, T0)
    await sim.activate()
    sim.clock.advance(HOUR)
    beat()
    await feed.refresh_books()
    sim.quote(daemon)
    await sim.tick(daemon)
    (offer,) = venue.state.offers.values()
    placed_at = sim.clock.now

    fifth = offer.amount_original * Decimal("0.2")
    sim.clock.advance(60_000)
    feed.ingest("fUST", [PublicTrade(
        900, placed_at + 5_000, offer.queue_ahead + fifth, offer.rate, offer.period)])
    beat()
    await feed.refresh_books()
    await sim.tick(daemon)  # consumes trade 900 up to the watermark, not up to `now`
    assert venue.state.offers[offer.offer_id].filled == fifth
    cursor = venue.state.market_through["fUST"]
    assert cursor <= sim.clock.now - 2_000

    # Trade 901 executed after that watermark but before that tick's `now`; the feed learns
    # of it only afterwards. It is still in a range nobody consumed, so it is filled.
    sim.clock.advance(60_000)
    feed.ingest("fUST", [PublicTrade(
        901, cursor + 1_000, fifth, offer.rate, offer.period)])
    beat()
    await feed.refresh_books()
    await sim.tick(daemon)
    await sim.tick(daemon)
    await sim.assert_sound(daemon)
    assert venue.state.offers[offer.offer_id].filled == 2 * fifth
    assert venue.state.offers[offer.offer_id].status == "PARTIAL"
