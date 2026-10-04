"""The live `MarketFeed`: it reads a buffer, a task fills it, a fetcher never blocks a read.

Mutations (one at a time; revert after each): ``book()`` awaits ``fetch_book`` itself
(``test_a_blocked_fetcher_never_blocks_a_read``); ``trades()`` awaits ``fetch_trades``
(same test); the buffer admits a trade it already holds (``test_an_overlapping_fetch_is_idempotent``).
"""
from __future__ import annotations

import asyncio
from decimal import Decimal

import pytest

from bfx_funding_bot.modules.simulated_venue import (
    BookSnapshot,
    LiveFeedConfig,
    LiveMarketFeed,
    NoMarketDataError,
    PublicTrade,
    SimulatedVenue,
    SimulatedVenueConfig,
)
from bfx_funding_bot.modules.simulated_venue.wiring import build_simulated_venue
from tests.modules.simulated_venue.helpers import (
    ACCOUNT,
    API_KEY,
    API_SECRET,
    HOUR,
    T0,
    Clock,
    World,
    book,
    trade,
)

SYMBOLS = ("fUST", "fUSD")


def _feed(fetch_book, fetch_trades, *, clock: Clock | None = None, **config) -> LiveMarketFeed:  # type: ignore[no-untyped-def]
    return LiveMarketFeed(
        config=LiveFeedConfig(symbols=SYMBOLS, **config), fetch_book=fetch_book,
        fetch_trades=fetch_trades, clock_ms=clock or Clock())


async def _no_book(symbol: str) -> BookSnapshot | None:
    return None


async def _no_trades(symbol: str, since_ms: int) -> list[PublicTrade]:
    return []


async def test_reads_come_from_the_buffer_the_refresh_fills() -> None:
    books = {"fUST": book("fUST", T0, [("0.0003", 2, "500")])}
    seen_since: list[tuple[str, int]] = []

    async def fetch_book(symbol: str) -> BookSnapshot | None:
        return books.get(symbol)

    async def fetch_trades(symbol: str, since_ms: int) -> list[PublicTrade]:
        seen_since.append((symbol, since_ms))
        return [trade(T0 + 10, "5"), trade(T0 + 20, "7")] if symbol == "fUST" else []

    feed = _feed(fetch_book, fetch_trades)
    assert await feed.book("fUST", at_ms=T0) is None and not feed.has_books()
    await feed.refresh_books()
    await feed.refresh_trades(backfill=True)
    assert (await feed.book("fUST", at_ms=T0)).asks[0][2] == Decimal(500)  # type: ignore[union-attr]
    assert await feed.book("fUST", at_ms=T0 - 1) is None  # at or BEFORE ``at_ms`` only
    assert await feed.book("fUSD", at_ms=T0) is None and not feed.has_books()
    got = await feed.trades("fUST", after_ms=T0 + 10, through_ms=T0 + 20)
    assert [t.mts for t in got] == [T0 + 20]  # (after, through]
    assert seen_since[0][1] == T0 - LiveFeedConfig(symbols=SYMBOLS).backfill_ms
    assert not await feed.trades("fUSD", after_ms=0, through_ms=T0 + HOUR)


async def test_an_overlapping_fetch_is_idempotent_and_identical_trades_in_one_fetch_all_count() -> None:
    page = [trade(T0 + 10, "5"), trade(T0 + 10, "5"), trade(T0 + 20, "7")]

    async def fetch_trades(symbol: str, since_ms: int) -> list[PublicTrade]:
        return list(page) if symbol == "fUST" else []

    feed = _feed(_no_book, fetch_trades)
    await feed.refresh_trades(backfill=True)
    await feed.refresh_trades()  # the venue returned the same rows again
    got = await feed.trades("fUST", after_ms=0, through_ms=T0 + HOUR)
    assert [(t.mts, t.amount) for t in got] == [
        (T0 + 10, Decimal(5)), (T0 + 10, Decimal(5)), (T0 + 20, Decimal(7))]


async def test_trades_older_than_the_retention_are_dropped() -> None:
    clock = Clock()

    async def fetch_trades(symbol: str, since_ms: int) -> list[PublicTrade]:
        return [trade(T0 - 5 * HOUR, "5"), trade(T0, "7")] if symbol == "fUST" else []

    feed = _feed(_no_book, fetch_trades, clock=clock, backfill_ms=2 * HOUR, retention_ms=2 * HOUR)
    await feed.refresh_trades(backfill=True)
    assert [t.mts for t in await feed.trades("fUST", after_ms=0, through_ms=T0 + 1)] == [T0]


async def test_a_blocked_fetcher_never_blocks_a_read() -> None:
    """The run task is parked inside both fetchers; ``book()`` and ``trades()`` still answer."""
    entered = {"book": asyncio.Event(), "trades": asyncio.Event()}
    release = asyncio.Event()

    async def fetch_book(symbol: str) -> BookSnapshot | None:
        entered["book"].set()
        await release.wait()
        return None

    async def fetch_trades(symbol: str, since_ms: int) -> list[PublicTrade]:
        entered["trades"].set()
        await release.wait()
        return []

    feed = _feed(fetch_book, fetch_trades, fetch_deadline_s=3600)
    stop = asyncio.Event()
    task = asyncio.create_task(feed.run(stop))
    try:
        await entered["trades"].wait()  # the backfill fetch is in flight and will not return
        assert await asyncio.wait_for(feed.trades("fUST", after_ms=0, through_ms=T0), 10) == []
        assert await asyncio.wait_for(feed.book("fUST", at_ms=T0), 10) is None
    finally:
        stop.set()
        release.set()
        await task


async def test_a_failing_or_overdue_fetcher_keeps_the_buffer_and_is_counted() -> None:
    calls = {"n": 0}

    async def fetch_book(symbol: str) -> BookSnapshot | None:
        calls["n"] += 1
        if calls["n"] > 2:
            raise RuntimeError("source down")
        return book(symbol, T0 + calls["n"])

    async def never(symbol: str, since_ms: int) -> list[PublicTrade]:
        await asyncio.Event().wait()
        return []

    feed = _feed(fetch_book, never, fetch_deadline_s=0.05)
    await feed.refresh_books()
    held = await feed.book("fUST", at_ms=T0 + 10)
    await feed.refresh_books()  # both symbols raise now
    assert await feed.book("fUST", at_ms=T0 + 10) == held
    await feed.refresh_trades(backfill=True)  # past its deadline
    assert feed.failures == 4  # two raising book fetches, two overdue trade fetches


async def _venue_on(feed: LiveMarketFeed, clock: Clock) -> World:
    from tests.modules.simulated_venue.helpers import WORLDS, InMemoryVenueEventStore, World
    store = InMemoryVenueEventStore()
    venue: SimulatedVenue = await build_simulated_venue(
        account=ACCOUNT, config=SimulatedVenueConfig(
            api_key=API_KEY, api_secret=API_SECRET, max_book_age_ms=60_000),
        store=store, feed=feed, clock_ms=clock)
    await venue.fund_wallet("UST", Decimal(1000))
    world = World(venue, feed, clock, store)  # type: ignore[arg-type]
    WORLDS.append(world)
    return world


async def test_a_stale_book_is_no_market_data_not_a_made_up_price() -> None:
    clock = Clock()

    async def fetch_book(symbol: str) -> BookSnapshot | None:
        return book(symbol, T0)

    feed = _feed(fetch_book, _no_trades, clock=clock)
    await feed.refresh_books()
    world = await _venue_on(feed, clock)
    body = {"type": "LIMIT", "symbol": "fUST", "amount": "150", "rate": "0.0002",
            "period": 2, "flags": 0}
    clock.advance(60_001)  # the source stopped: the buffer only ages
    with pytest.raises(NoMarketDataError):
        await world.post("v2/auth/w/funding/offer/submit", body)
    assert [f.kind for f in world.venue.internal_failures] == ["no_market_data"]
    world.venue.internal_failures.clear()
    assert not world.venue.state.offers
    # A fresh book from the source: the same request is accepted.
    async def fresh(symbol: str) -> BookSnapshot | None:
        return book(symbol, clock.now)

    feed._fetch_book = fresh  # type: ignore[assignment]
    await feed.refresh_books()
    assert (await world.post("v2/auth/w/funding/offer/submit", body)).status_code == 200


async def test_the_venue_sees_pushed_volume_only_after_the_task_buffered_it() -> None:
    clock = Clock()
    rows: list[PublicTrade] = []

    async def fetch_book(symbol: str) -> BookSnapshot | None:
        return book(symbol, clock.now, [("0.0002", 2, "0")])

    async def fetch_trades(symbol: str, since_ms: int) -> list[PublicTrade]:
        return [t for t in rows if t.mts > since_ms] if symbol == "fUST" else []

    feed = _feed(fetch_book, fetch_trades, clock=clock)
    await feed.refresh_books()
    world = await _venue_on(feed, clock)
    body = {"type": "LIMIT", "symbol": "fUST", "amount": "150", "rate": "0.0002",
            "period": 2, "flags": 0}
    response = await world.post("v2/auth/w/funding/offer/submit", body)
    offer_id = int(response.json()[4][0])
    rows.append(trade(clock.now + 3_000, "150", 2, "0.0002"))
    clock.advance(6_000)
    await world.post("v2/auth/r/wallets", {})
    assert world.venue.state.offers[offer_id].resting  # not buffered yet: not seen
    await feed.refresh_trades()
    await world.post("v2/auth/r/wallets", {})
    assert world.venue.state.offers[offer_id].status == "EXECUTED"
