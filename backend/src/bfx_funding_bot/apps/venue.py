"""Venue composition: what a process trades against, chosen by the venue axis in one place.

``build_venue`` answers for both venues with the same shape (``VenueWiring``): the HTTP
client the authenticated REST client and the executor use, the one per-process
``AuthRequestGate``, and, for the simulated venue only, the venue's own market-data task.

* ``bitfinex``: the process's real HTTP client, no extra task.
* ``simulated``: an event-sourced venue on this process's own database (its log is
  ``sim_venue_event``), reached through ``httpx.AsyncClient(transport=<venue>)``. No auth
  request can leave the process. The venue reads public market data from its own feed
  (book from a second WS-fed ``FundingBookStore``, trades from REST polling), never from
  the bot's book store. This module is the only importer of ``simulated_venue.wiring``.

Production composes the simulated venue without faults; tests inject a ``FaultPlan`` and a
feed through ``VenueSeam``.
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from decimal import Decimal
from uuid import UUID

import httpx
from sqlalchemy.ext.asyncio import AsyncEngine

from bfx_funding_bot.core.venue import Venue
from bfx_funding_bot.external.bitfinex.funding_book_ws import FundingBookWSClient
from bfx_funding_bot.external.bitfinex.nonce import AuthRequestGate
from bfx_funding_bot.external.bitfinex.rest import BitfinexREST
from bfx_funding_bot.modules.execution.protocols import Credentials
from bfx_funding_bot.modules.marketfeed.config import MarketfeedConfig
from bfx_funding_bot.modules.marketfeed.funding_book import FundingBookService, FundingBookStore
from bfx_funding_bot.modules.simulated_venue import (
    BookFetcher,
    BookSnapshot,
    FaultPlan,
    LiveFeedConfig,
    LiveMarketFeed,
    MarketFeed,
    PublicTrade,
    SimAccount,
    SimulatedVenue,
    SimulatedVenueConfig,
    SqlVenueEventStore,
    TradesFetcher,
)
from bfx_funding_bot.modules.simulated_venue.wiring import build_simulated_venue
from bfx_funding_bot.modules.strategy import configured_symbols

log = logging.getLogger(__name__)

_TRADES_PAGE = 1000
_TRADES_MAX_PAGES = 10


@dataclass(frozen=True, slots=True)
class VenueSeam:
    """Tests only: replace the venue's market feed and/or inject venue faults."""

    feed: MarketFeed | None = None
    faults: FaultPlan | None = None


class VenueFeedTask:
    """The simulated venue's market-data task: its own book service and its own buffer."""

    def __init__(self, *, book_service: FundingBookService, feed: LiveMarketFeed) -> None:
        self._book_service = book_service
        self._feed = feed

    async def wait_ready(self, stop: asyncio.Event, *, timeout_s: float) -> bool:
        """True once every symbol has a book; False on timeout or stop."""
        deadline = asyncio.get_running_loop().time() + timeout_s
        while not stop.is_set() and asyncio.get_running_loop().time() < deadline:
            if self._feed.has_books():
                return True
            await asyncio.sleep(0.1)
        return self._feed.has_books()

    async def run(self, stop: asyncio.Event) -> None:
        async with asyncio.TaskGroup() as group:
            group.create_task(self._book_service.run(stop), name="venue_book")
            group.create_task(self._feed.run(stop), name="venue_feed")


@dataclass(slots=True)
class VenueWiring:
    venue: Venue
    # What BitfinexAuthREST and the executor (cancel-all included) are built on.
    auth_http: httpx.AsyncClient
    # One nonce gate per process: Bitfinex nonces are per key across REST and WS.
    auth_gate: AuthRequestGate
    # Simulated only: the client this wiring created (the daemon closes it), the venue's
    # market-data task, and the venue itself (tests and the soak report read its state).
    owned_client: httpx.AsyncClient | None = None
    feed_task: VenueFeedTask | None = None
    simulated: SimulatedVenue | None = None


async def build_venue(
    config: MarketfeedConfig,
    *,
    exchange_account_id: UUID,
    credentials: Credentials,
    db_engine: AsyncEngine,
    bitfinex_http: httpx.AsyncClient,
    bitfinex: BitfinexREST,
    clock: Callable[[], int],
    seam: VenueSeam | None = None,
) -> VenueWiring:
    gate = AuthRequestGate()
    if config.venue == "bitfinex":
        return VenueWiring(venue="bitfinex", auth_http=bitfinex_http, auth_gate=gate)
    if config.venue != "simulated":
        raise ValueError(f"unknown venue {config.venue!r}")
    seam = seam or VenueSeam()
    symbols = tuple(sorted(configured_symbols(config.cells)))
    # Refuses realm prod (SimAccount), a database not on the ledger epoch, an unstamped or
    # prod-stamped one, or one without sim_venue_event (the store's own reads).
    account = SimAccount(str(exchange_account_id), config.deployment_environment.value)
    store = await SqlVenueEventStore.open(db_engine)
    book_service: FundingBookService | None = None
    live_feed: LiveMarketFeed | None = None
    feed: MarketFeed
    if seam.feed is not None:
        feed = seam.feed
    else:
        assert config.book_max_age_seconds is not None
        assert config.book_reconcile_interval_seconds is not None
        book_store = FundingBookStore(max_age_seconds=config.book_max_age_seconds)
        book_service = FundingBookService(
            store=book_store, rest=bitfinex, ws=FundingBookWSClient(symbols=symbols),
            symbols=symbols, reconcile_interval_seconds=config.book_reconcile_interval_seconds,
        )
        live_feed = LiveMarketFeed(
            config=LiveFeedConfig(symbols=symbols),
            fetch_book=_book_fetcher(book_store, clock),
            fetch_trades=_trades_fetcher(bitfinex),
            clock_ms=clock,
        )
        feed = live_feed
    venue = await build_simulated_venue(
        account=account,
        config=SimulatedVenueConfig(
            api_key=credentials.api_key, api_secret=credentials.api_secret, symbols=symbols),
        store=store, feed=feed, clock_ms=clock, faults=seam.faults,
    )
    funded = await venue.fund_wallets_if_empty(config.simulated_initial_wallets)
    if funded:
        log.info("simulated_venue_funded wallets=%s", sorted(config.simulated_initial_wallets))
    client = venue.client()
    return VenueWiring(
        venue="simulated", auth_http=client, auth_gate=gate, owned_client=client,
        feed_task=(
            VenueFeedTask(book_service=book_service, feed=live_feed)
            if book_service is not None and live_feed is not None else None),
        simulated=venue,
    )


def _book_fetcher(store: FundingBookStore, clock: Callable[[], int]) -> BookFetcher:
    """The newest book the venue's own WS-fed store holds: a local read, no I/O."""

    async def fetch(symbol: str) -> BookSnapshot | None:
        snapshot = store.snapshot(symbol, now_ms=clock())
        if snapshot is None:
            return None
        asks = tuple(
            (Decimal(str(level.rate)), level.period, Decimal(str(level.amount)))
            for level in snapshot.asks
        )
        # `received_at_ms` is when the venue last confirmed the book: it ages a quiet book
        # by silence, not by content (see MarketSnapshot.is_fresh).
        return BookSnapshot(symbol, snapshot.received_at_ms, asks)

    return fetch


def _trades_fetcher(rest: BitfinexREST) -> TradesFetcher:
    """Public funding trades newer than ``since_ms`` over REST, paged on their timestamp.

    Volume is the magnitude of AMOUNT: the sign names the taker's side and the simulator
    counts every execution against a resting offer (optimistic; see the venue docstring).
    """

    async def fetch(symbol: str, since_ms: int) -> Sequence[PublicTrade]:
        by_id: dict[int, PublicTrade] = {}
        start = since_ms + 1
        for _ in range(_TRADES_MAX_PAGES):
            page = await rest.get_funding_trades(symbol=symbol, start=start, limit=_TRADES_PAGE)
            for row in page:
                if row.mts > since_ms:
                    by_id[row.trade_id] = PublicTrade(
                        row.mts, Decimal(str(abs(row.amount))), Decimal(str(row.rate)),
                        row.period)
            if len(page) < _TRADES_PAGE or page[-1].mts <= start:
                break
            start = page[-1].mts
        else:
            log.warning("simulated_venue_trades_backfill_truncated symbol=%s since_ms=%d",
                        symbol, since_ms)
        return list(by_id.values())

    return fetch
