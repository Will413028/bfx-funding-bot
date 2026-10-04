"""The live `MarketFeed`: it reads buffers, a stream and a gap backfill fill them, and the
event-time watermark says what is complete.

Mutations (one at a time; revert after each): ``book()`` awaits ``fetch_book`` itself
(``test_a_blocked_fetcher_never_blocks_a_read``); the buffer dedupes by ``mts`` instead of id
(``test_trades_are_admitted_exactly_once_by_id``); no gap backfill after a reconnect
(``test_a_reconnect_gap_is_backfilled_once_and_the_watermark_holds_until_then``); the venue
ignores ``complete_through`` (``test_a_trade_that_reaches_the_feed_late_is_still_filled``).
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
)

SYMBOLS = ("fUST", "fUSD")
LAG = 2_000


def _feed(fetch_book, fetch_trades, *, clock: Clock | None = None, **config) -> LiveMarketFeed:  # type: ignore[no-untyped-def]
    return LiveMarketFeed(
        config=LiveFeedConfig(symbols=SYMBOLS, watermark_lag_ms=LAG, **config),
        fetch_book=fetch_book, fetch_trades=fetch_trades, clock_ms=clock or Clock())


async def _no_book(symbol: str) -> BookSnapshot | None:
    return None


async def _no_trades(symbol: str, since_ms: int) -> list[PublicTrade]:
    return []


def _t(id_: int, mts: int, amount: str = "5") -> PublicTrade:
    return PublicTrade(id_, mts, Decimal(amount), Decimal("0.0002"), 2)


async def test_reads_come_from_the_buffers() -> None:
    books = {"fUST": book("fUST", T0, [("0.0003", 2, "500")])}

    async def fetch_book(symbol: str) -> BookSnapshot | None:
        return books.get(symbol)

    feed = _feed(fetch_book, _no_trades)
    assert await feed.book("fUST", at_ms=T0) is None and not feed.has_books()
    await feed.refresh_books()
    assert (await feed.book("fUST", at_ms=T0)).asks[0][2] == Decimal(500)  # type: ignore[union-attr]
    assert await feed.book("fUST", at_ms=T0 - 1) is None  # at or BEFORE ``at_ms`` only
    assert await feed.book("fUSD", at_ms=T0) is None and not feed.has_books()
    feed.ingest("fUST", [_t(1, T0 + 10), _t(2, T0 + 20)])
    got = await feed.trades("fUST", after_ms=T0 + 10, through_ms=T0 + 20)
    assert [t.id for t in got] == [2]  # (after, through]
    assert not await feed.trades("fUSD", after_ms=0, through_ms=T0 + HOUR)


async def test_trades_are_admitted_exactly_once_by_id() -> None:
    """Equal timestamps across fetches, a replayed page and a late out-of-order trade."""
    feed = _feed(_no_book, _no_trades)
    feed.ingest("fUST", [_t(1, T0 + 10), _t(2, T0 + 10)])  # two executions in one millisecond
    feed.ingest("fUST", [_t(2, T0 + 10), _t(3, T0 + 10)])  # an overlapping page, plus a third
    feed.ingest("fUST", [_t(0, T0 + 5)])  # late and out of order, same fetch window
    feed.ingest("fUST", [_t(0, T0 + 5)])
    got = await feed.trades("fUST", after_ms=0, through_ms=T0 + HOUR)
    assert [(t.id, t.mts) for t in got] == [(0, T0 + 5), (1, T0 + 10), (2, T0 + 10), (3, T0 + 10)]


async def test_trades_older_than_the_retention_are_dropped_with_their_ids() -> None:
    clock = Clock()
    rows = [_t(1, T0 - HOUR + 1), _t(2, T0)]

    async def fetch_trades(symbol: str, since_ms: int) -> list[PublicTrade]:
        return rows if symbol == "fUST" else []

    feed = _feed(_no_book, fetch_trades, clock=clock, backfill_ms=HOUR, retention_ms=HOUR)
    feed.stream_connected()
    await feed.backfill_gaps()
    assert feed._ids["fUST"] == {1, 2}
    feed._prune("fUST", T0 + HOUR // 2)
    assert [t.id for t in await feed.trades("fUST", after_ms=0, through_ms=T0 + HOUR)] == [2]
    assert feed._ids["fUST"] == {2}


async def test_a_blocked_fetcher_never_blocks_a_read() -> None:
    """The run task is parked inside both fetchers; every read still answers."""
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
    feed.stream_connected()
    stop = asyncio.Event()
    task = asyncio.create_task(feed.run(stop))
    try:
        await entered["book"].wait()
        assert await asyncio.wait_for(feed.trades("fUST", after_ms=0, through_ms=T0), 10) == []
        assert await asyncio.wait_for(feed.book("fUST", at_ms=T0), 10) is None
        assert await asyncio.wait_for(feed.complete_through("fUST"), 10) is None
    finally:
        stop.set()
        release.set()
        await task


async def test_a_failing_or_overdue_fetcher_keeps_the_buffer_and_is_counted() -> None:
    calls = {"n": 0}
    reported: list[str] = []

    async def fetch_book(symbol: str) -> BookSnapshot | None:
        calls["n"] += 1
        if calls["n"] > 2:
            raise RuntimeError("source down")
        return book(symbol, T0 + calls["n"])

    async def never(symbol: str, since_ms: int) -> list[PublicTrade]:
        await asyncio.Event().wait()
        return []

    feed = LiveMarketFeed(
        config=LiveFeedConfig(symbols=SYMBOLS, fetch_deadline_s=0.05), fetch_book=fetch_book,
        fetch_trades=never, clock_ms=Clock(), on_failure=reported.append)
    await feed.refresh_books()
    held = await feed.book("fUST", at_ms=T0 + 10)
    await feed.refresh_books()  # both symbols raise now
    assert await feed.book("fUST", at_ms=T0 + 10) == held
    feed.stream_connected()
    await feed.backfill_gaps()  # past its deadline
    assert feed.failures == 4
    assert sorted(reported) == ["book", "book", "trades", "trades"]  # the counter's feed
    assert await feed.complete_through("fUST") is None  # a failed backfill proves nothing


class Stream:
    """A fake stream source driving a feed the way ``apps/venue.py`` wires the WS client."""

    def __init__(self, feed: LiveMarketFeed, clock: Clock) -> None:
        self.feed, self.clock = feed, clock

    def connect(self) -> None:
        self.feed.stream_connected()

    def drop(self) -> None:
        self.feed.stream_disconnected()

    def frame(self, symbol: str, *trades: PublicTrade) -> None:
        self.feed.ingest(symbol, list(trades))
        self.feed.alive(symbol, self.clock.now)


async def test_the_watermark_follows_frames_only_while_connected_and_gap_free() -> None:
    clock = Clock()
    fetched: list[tuple[str, int]] = []

    async def fetch_trades(symbol: str, since_ms: int) -> list[PublicTrade]:
        fetched.append((symbol, since_ms))
        return []

    feed = _feed(_no_book, fetch_trades, clock=clock, backfill_ms=HOUR)
    stream = Stream(feed, clock)
    stream.frame("fUST")  # nothing is connected yet
    assert await feed.complete_through("fUST") is None
    stream.connect()
    stream.frame("fUST")  # connected but the start-up gap is open
    assert await feed.complete_through("fUST") is None
    await feed.backfill_gaps()
    assert sorted(fetched) == [("fUSD", T0 - HOUR), ("fUST", T0 - HOUR)]  # the start-up window
    assert await feed.complete_through("fUST") == T0 - LAG
    clock.advance(15_000)
    stream.frame("fUST")  # a heartbeat of a quiet market moves it
    assert await feed.complete_through("fUST") == T0 + 15_000 - LAG
    assert await feed.complete_through("fUSD") == T0 - LAG  # per symbol


async def test_a_reconnect_gap_is_backfilled_once_and_the_watermark_holds_until_then() -> None:
    clock = Clock()
    history = [_t(1, T0 + 1_000), _t(2, T0 + 5_000), _t(3, T0 + 9_000)]
    fetched: list[tuple[str, int]] = []

    async def fetch_trades(symbol: str, since_ms: int) -> list[PublicTrade]:
        fetched.append((symbol, since_ms))
        return [t for t in history if t.mts >= since_ms] if symbol == "fUST" else []

    feed = _feed(_no_book, fetch_trades, clock=clock, backfill_ms=HOUR)
    stream = Stream(feed, clock)
    stream.connect()
    await feed.backfill_gaps()
    clock.now = T0 + 2_000
    stream.frame("fUST", history[0])
    mark_before = await feed.complete_through("fUST")
    assert mark_before == T0 + 2_000 - LAG

    stream.drop()  # trades 2 and 3 happen while the stream is down
    clock.now = T0 + 20_000
    assert await feed.complete_through("fUST") == mark_before  # holds
    stream.frame("fUST")  # a stray frame while disconnected proves nothing
    assert await feed.complete_through("fUST") == mark_before

    fetched.clear()
    stream.connect()
    clock.now = T0 + 21_000
    stream.frame("fUST", _t(4, T0 + 20_500))  # live frames during the gap do not move it either
    assert await feed.complete_through("fUST") == mark_before
    await feed.backfill_gaps()
    # From where the watermark stopped, not from the live trade 4 received during the gap.
    assert ("fUST", mark_before) in fetched
    assert [t.id for t in await feed.trades("fUST", after_ms=0, through_ms=T0 + HOUR)] == [
        1, 2, 3, 4]  # backfilled once, no duplicate of 1 (overlap) or 4 (live)
    assert await feed.complete_through("fUST") == T0 + 21_000 - LAG
    await feed.backfill_gaps()
    assert len([f for f in fetched if f[0] == "fUST"]) == 1  # the gap is closed: no more REST


async def test_a_failed_gap_backfill_is_retried_later_and_the_watermark_stays_frozen() -> None:
    clock = Clock()
    attempts = {"n": 0}

    async def fetch_trades(symbol: str, since_ms: int) -> list[PublicTrade]:
        attempts["n"] += 1
        if attempts["n"] <= 2:
            raise RuntimeError("429")
        return []

    feed = _feed(_no_book, fetch_trades, clock=clock, backfill_retry_s=3600)
    feed.stream_connected()
    await feed.backfill_gaps()  # both symbols fail
    await feed.backfill_gaps()  # inside the retry window: no new request
    assert attempts["n"] == 2 and await feed.complete_through("fUST") is None
    feed._retry_at.clear()  # the window passes
    await feed.backfill_gaps()
    assert attempts["n"] == 4 and await feed.complete_through("fUST") == T0 - LAG


async def _venue_on(feed: LiveMarketFeed, clock: Clock) -> World:
    from tests.modules.simulated_venue.helpers import WORLDS, InMemoryVenueEventStore
    store = InMemoryVenueEventStore()
    venue: SimulatedVenue = await build_simulated_venue(
        account=ACCOUNT, config=SimulatedVenueConfig(
            api_key=API_KEY, api_secret=API_SECRET, max_book_age_ms=60_000),
        store=store, feed=feed, clock_ms=clock)
    await venue.fund_wallet("UST", Decimal(1000))
    world = World(venue, feed, clock, store)  # type: ignore[arg-type]
    WORLDS.append(world)
    return world


_BODY = {"type": "LIMIT", "symbol": "fUST", "amount": "150", "rate": "0.0002", "period": 2,
         "flags": 0}


async def test_a_stale_book_is_no_market_data_not_a_made_up_price() -> None:
    clock = Clock()

    async def fetch_book(symbol: str) -> BookSnapshot | None:
        return book(symbol, T0)

    feed = _feed(fetch_book, _no_trades, clock=clock)
    await feed.refresh_books()
    world = await _venue_on(feed, clock)
    clock.advance(60_001)  # the source stopped: the buffer only ages
    with pytest.raises(NoMarketDataError):
        await world.post("v2/auth/w/funding/offer/submit", _BODY)
    assert [f.kind for f in world.venue.internal_failures] == ["no_market_data"]
    world.venue.internal_failures.clear()
    assert not world.venue.state.offers

    async def fresh(symbol: str) -> BookSnapshot | None:
        return book(symbol, clock.now)

    feed._fetch_book = fresh  # type: ignore[assignment]
    await feed.refresh_books()
    assert (await world.post("v2/auth/w/funding/offer/submit", _BODY)).status_code == 200


async def test_a_trade_that_reaches_the_feed_late_is_still_filled() -> None:
    """F1: after a sync consumed one trade, a second trade that executed BEFORE that sync's
    ``now`` but reached the feed only afterwards is still filled, because the venue never
    advanced past the feed's watermark (mutation: ``through = now`` loses it)."""
    clock = Clock()

    async def fetch_book(symbol: str) -> BookSnapshot | None:
        return book(symbol, clock.now, [("0.0002", 2, "0")])

    feed = _feed(fetch_book, _no_trades, clock=clock)
    stream = Stream(feed, clock)
    stream.connect()
    await feed.backfill_gaps()
    await feed.refresh_books()
    world = await _venue_on(feed, clock)
    offer_id = int((await world.post("v2/auth/w/funding/offer/submit", _BODY)).json()[4][0])
    placed_at = clock.now
    sixty = Decimal(150) * Decimal("0.4")

    # Trade 1 executes at +3 s and the feed has it when the next request syncs at +10 s: the
    # sync consumes it (a kept trade moves the cursor) up to the watermark, +8 s, not to +10 s.
    clock.advance(10_000)
    stream.frame("fUST", _t(1, placed_at + 3_000, str(sixty)))
    await world.post("v2/auth/r/wallets", {})
    offer = world.venue.state.offers[offer_id]
    assert offer.filled == sixty
    assert world.venue.state.market_through["fUST"] == placed_at + 10_000 - LAG

    # Trade 2 executed at +9 s, after that watermark but before that sync's `now`; the feed
    # learns of it only now. It is still in a range nobody has consumed.
    clock.advance(10_000)
    stream.frame("fUST", _t(2, placed_at + 9_000, str(sixty)))
    clock.advance(1_000)
    stream.frame("fUST")
    await world.post("v2/auth/r/wallets", {})
    assert world.venue.state.offers[offer_id].filled == 2 * sixty


async def test_a_venue_without_a_watermark_consumes_nothing_and_moves_nothing() -> None:
    clock = Clock()

    async def fetch_book(symbol: str) -> BookSnapshot | None:
        return book(symbol, clock.now, [("0.0002", 2, "0")])

    feed = _feed(fetch_book, _no_trades, clock=clock)
    await feed.refresh_books()
    world = await _venue_on(feed, clock)  # no stream, no backfill: the watermark is None
    offer_id = int((await world.post("v2/auth/w/funding/offer/submit", _BODY)).json()[4][0])
    through = dict(world.venue.state.market_through)
    feed.ingest("fUST", [_t(1, clock.now + 1_000, "100000")])
    clock.advance(10_000)
    await world.post("v2/auth/r/wallets", {})
    assert world.venue.state.offers[offer_id].resting
    assert world.venue.state.market_through == through
