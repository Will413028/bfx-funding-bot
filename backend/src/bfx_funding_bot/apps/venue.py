"""Venue composition: the one place that decides what differs between the venues.

``build_venue`` answers for both venues with the same shape (``VenueWiring``): where the
credentials come from and the credentials themselves, the HTTP client the authenticated REST
client and the executor use, the one per-process ``AuthRequestGate``, what the venue has
(``VenueCapabilities``: consumers check these, never a venue name), the venue's own tasks and
how to close it.

* ``bitfinex``: credentials from the account's vault, the process's real HTTP client, the
  authenticated WebSocket required, no extra task.
* ``simulated``: an event-sourced venue on this process's own database (its log is
  ``sim_venue_event``), reached through ``httpx.AsyncClient(transport=<venue>)``. No auth
  request can leave the process. Credentials are generated for this boot, the vault is never
  opened and a vault key in the environment refuses the boot. The venue reads public market
  data from its own feed: a second WS-fed book store, funding trades over the public WS with
  REST only to fill gaps, and an event-time watermark (``LiveMarketFeed``). This module is the
  only importer of ``simulated_venue.wiring``.

The simulated venue is composed without faults unless ``BFX_SIM_FAULTS`` asks for a seeded
plan (``apps/sim_faults.py``; refused for the bitfinex venue, injections are recorded in the
venue's own log); tests inject a ``FaultPlan`` and a feed through ``VenueSeam``.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import secrets
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from decimal import Decimal
from uuid import UUID

import httpx
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from bfx_funding_bot.apps.sim_faults import SimFaultSpec
from bfx_funding_bot.core.crypto import VaultNotConfiguredError, load_kek
from bfx_funding_bot.core.errors import ConfigurationError
from bfx_funding_bot.core.venue import Venue, VenueCapabilities
from bfx_funding_bot.external.bitfinex.funding_book_ws import FundingBookWSClient
from bfx_funding_bot.external.bitfinex.funding_trades_ws import FundingTradesWSClient
from bfx_funding_bot.external.bitfinex.nonce import AuthRequestGate
from bfx_funding_bot.external.bitfinex.rest import BitfinexREST, FundingTrade
from bfx_funding_bot.modules.accounts.vault import (
    AccountCredentialNotConfiguredError,
    VaultKeyMismatchError,
    load_account_credentials,
)
from bfx_funding_bot.modules.execution.protocols import Credentials
from bfx_funding_bot.modules.marketfeed.config import MarketfeedConfig
from bfx_funding_bot.modules.marketfeed.funding_book import FundingBookService, FundingBookStore
from bfx_funding_bot.modules.observability.metrics import DaemonMetrics, attach_httpx_metrics
from bfx_funding_bot.modules.simulated_venue import (
    BookFetcher,
    BookSnapshot,
    FaultKind,
    FaultPlan,
    FaultRule,
    FaultTarget,
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

BITFINEX_CAPABILITIES = VenueCapabilities(auth_ws="required", rest_fill_tracker=True)
SIMULATED_CAPABILITIES = VenueCapabilities(auth_ws="forbidden", rest_fill_tracker=False)


# ``BFX_SIM_FAULTS`` names -> (request kind, fault). Submit faults make an UNKNOWN attempt for
# the ledger to resolve; cancels are not faulted (a faulted cancel would surface as a quarantine
# the soak report could not attribute to an injection).
_FAULT_KNOBS: dict[str, tuple[FaultTarget, FaultKind]] = {
    "unknown_5xx": (FaultTarget.SUBMIT, FaultKind.UNKNOWN_5XX_ERROR),
    "unknown_placed_lost": (FaultTarget.SUBMIT, FaultKind.UNKNOWN_PLACED_LOST),
    "unknown_not_placed_lost": (FaultTarget.SUBMIT, FaultKind.UNKNOWN_NOT_PLACED_LOST),
    "history_error": (FaultTarget.HISTORY, FaultKind.HISTORY_ERROR),
}


def fault_plan(spec: SimFaultSpec) -> FaultPlan:
    """The plan for the transport; a rate of 0 adds no rule, no rates is the empty plan."""
    rules = tuple(
        FaultRule(target=_FAULT_KNOBS[name][0], kind=_FAULT_KNOBS[name][1], probability=rate)
        for name, rate in spec.rates.items() if rate > 0.0
    )
    return FaultPlan(rules=rules, seed=spec.seed)


@dataclass(frozen=True, slots=True)
class VenueSeam:
    """Tests only: replace the venue's market feed and/or inject venue faults."""

    feed: MarketFeed | None = None
    faults: FaultPlan | None = None


class VenueFeedTask:
    """The simulated venue's market-data task: its own book service, trades stream and feed."""

    name = "venue_feed"

    def __init__(
        self, *, book_service: FundingBookService, trades_stream: FundingTradesWSClient,
        feed: LiveMarketFeed,
    ) -> None:
        self._book_service = book_service
        self._trades_stream = trades_stream
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
            group.create_task(self._trades_stream.run(stop), name="venue_trades_ws")
            group.create_task(self._feed.run(stop), name="venue_feed_buffer")


@dataclass(slots=True)
class VenueWiring:
    venue: Venue
    # Venue credentials: from the vault (Bitfinex) or generated for this boot (simulated).
    credentials: Credentials
    # What BitfinexAuthREST and the executor (cancel-all included) are built on.
    auth_http: httpx.AsyncClient
    # One nonce gate per process: Bitfinex nonces are per key across REST and WS.
    auth_gate: AuthRequestGate
    capabilities: VenueCapabilities
    aclose: Callable[[], Awaitable[None]]
    tasks: tuple[VenueFeedTask, ...] = ()
    # Simulated only: the venue itself (tests and the soak report read its state).
    simulated: SimulatedVenue | None = None


async def _noop() -> None:
    return None


async def _vault_credentials(
    session: AsyncSession, exchange_account_id: UUID,
) -> Credentials:
    try:
        return await load_account_credentials(
            session, exchange_account_id=exchange_account_id, kek=load_kek())
    except AccountCredentialNotConfiguredError as exc:
        raise ConfigurationError(str(exc)) from exc
    except VaultKeyMismatchError as exc:
        raise ConfigurationError(
            f"exchange account {exchange_account_id} credential vault cannot be opened"
        ) from exc
    except VaultNotConfiguredError as exc:
        raise ConfigurationError("BFX_VAULT_KEK is required for daemon boot") from exc
    except ValueError as exc:
        # Crypto and config errors must not leak an implementation-specific traceback
        # through the boot contract.
        raise ConfigurationError(str(exc)) from exc


def _throwaway_credentials() -> Credentials:
    if "BFX_VAULT_KEK" in os.environ:
        raise ConfigurationError(
            "BFX_VAULT_KEK must not be set for a simulated venue boot: the simulation "
            "never opens the credential vault"
        )
    return Credentials(secrets.token_hex(16), secrets.token_hex(32))


async def build_venue(
    config: MarketfeedConfig,
    *,
    exchange_account_id: UUID,
    session_factory: async_sessionmaker[AsyncSession],
    db_engine: AsyncEngine,
    bitfinex_http: httpx.AsyncClient,
    bitfinex: BitfinexREST,
    clock: Callable[[], int],
    metrics: DaemonMetrics,
    seam: VenueSeam | None = None,
) -> VenueWiring:
    gate = AuthRequestGate()
    if config.venue == "bitfinex":
        if config.simulated_initial_wallets:
            raise ConfigurationError(
                "BFX_SIM_INITIAL_WALLETS is only valid for the simulated venue")
        if config.simulated_faults or config.simulated_fault_seed:
            raise ConfigurationError("BFX_SIM_FAULTS is only valid for the simulated venue")
        async with session_factory() as session:
            credentials = await _vault_credentials(session, exchange_account_id)
        return VenueWiring(
            venue="bitfinex", credentials=credentials, auth_http=bitfinex_http,
            auth_gate=gate, capabilities=BITFINEX_CAPABILITIES, aclose=_noop)
    return await _build_simulated(
        config, exchange_account_id=exchange_account_id, db_engine=db_engine,
        bitfinex=bitfinex, clock=clock, metrics=metrics, gate=gate, seam=seam or VenueSeam())


async def _build_simulated(
    config: MarketfeedConfig, *, exchange_account_id: UUID, db_engine: AsyncEngine,
    bitfinex: BitfinexREST, clock: Callable[[], int], metrics: DaemonMetrics,
    gate: AuthRequestGate, seam: VenueSeam,
) -> VenueWiring:
    credentials = _throwaway_credentials()
    symbols = tuple(sorted(configured_symbols(config.cells)))
    observer = metrics.sim_venue_observer()
    # Refuses realm prod (SimAccount), a database not on the ledger epoch, an unstamped or
    # prod-stamped one, or one without sim_venue_event (the store's own reads).
    account = SimAccount(str(exchange_account_id), config.deployment_environment.value)
    store = await SqlVenueEventStore.open(db_engine)
    tasks: tuple[VenueFeedTask, ...] = ()
    feed: MarketFeed
    live_feed: LiveMarketFeed | None = None
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
        built = LiveMarketFeed(
            config=LiveFeedConfig(symbols=symbols),
            fetch_book=_book_fetcher(book_store, clock),
            fetch_trades=_trades_fetcher(bitfinex),
            clock_ms=clock, on_failure=observer.feed_failure, on_stream=observer.feed_stream,
        )
        trades_stream = FundingTradesWSClient(
            symbols=symbols,
            on_trades=lambda symbol, rows: built.ingest(
                symbol, [_public_trade(row) for row in rows]),
            on_alive=built.alive, on_connected=built.stream_connected,
            on_disconnected=built.stream_disconnected, clock_ms=clock,
        )
        tasks = (VenueFeedTask(
            book_service=book_service, trades_stream=trades_stream, feed=built),)
        live_feed = feed = built
    venue = await build_simulated_venue(
        account=account,
        config=SimulatedVenueConfig(
            api_key=credentials.api_key, api_secret=credentials.api_secret, symbols=symbols),
        store=store, feed=feed, clock_ms=clock,
        faults=seam.faults if seam.faults is not None else fault_plan(SimFaultSpec(
            config.simulated_faults, config.simulated_fault_seed)),
        observer=observer,
    )
    if live_feed is not None:
        # A restart resumes the trades from where the venue's log says they were consumed,
        # however long the process was down (bounded by the feed's retention).
        live_feed.resume_from(venue.trade_resume_points())
        metrics.bind_sim_watermark_lag(live_feed.watermark_lag_seconds)
    funded = await venue.fund_wallets_if_empty(config.simulated_initial_wallets)
    if funded:
        log.info("simulated_venue_funded wallets=%s", sorted(config.simulated_initial_wallets))
    client = venue.client()
    attach_httpx_metrics(client, metrics)
    return VenueWiring(
        venue="simulated", credentials=credentials, auth_http=client, auth_gate=gate,
        capabilities=SIMULATED_CAPABILITIES, aclose=_closer(client), tasks=tasks,
        simulated=venue,
    )


def _closer(client: httpx.AsyncClient) -> Callable[[], Awaitable[None]]:
    async def aclose() -> None:
        with contextlib.suppress(Exception):
            await client.aclose()

    return aclose


def _public_trade(row: FundingTrade) -> PublicTrade:
    """Volume is the magnitude of AMOUNT (ADR D2: hourly sums equal candle volume); the sign
    names the taker's side and the simulator counts every execution against a resting offer
    (optimistic; see the venue docstring)."""
    return PublicTrade(
        row.trade_id, row.mts, Decimal(str(abs(row.amount))), Decimal(str(row.rate)),
        row.period)


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
    """Public funding trades with ``mts >= since_ms`` over REST, paged on their timestamp.

    Only start-up and gap backfill use it (the live feed calls it after a stream
    (re)connection). Overlap with what is already held is the feed's to dedupe, by id.
    """

    async def fetch(symbol: str, since_ms: int) -> Sequence[PublicTrade]:
        by_id: dict[int, PublicTrade] = {}
        start = since_ms
        for _ in range(_TRADES_MAX_PAGES):
            page = await rest.get_funding_trades(symbol=symbol, start=start, limit=_TRADES_PAGE)
            for row in page:
                by_id[row.trade_id] = _public_trade(row)
            if len(page) < _TRADES_PAGE or page[-1].mts <= start:
                break
            start = page[-1].mts
        else:
            log.warning("simulated_venue_trades_backfill_truncated symbol=%s since_ms=%d",
                        symbol, since_ms)
        return list(by_id.values())

    return fetch
