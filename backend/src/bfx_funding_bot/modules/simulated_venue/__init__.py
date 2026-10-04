"""In-process simulated Bitfinex funding venue (httpx transport, event-sourced).

Only `apps` composes it (`apps/venue.py`). It imports nothing of ledger, execution,
trading, marketfeed or `external`, and only composition roots import `wiring`.

Not simulated (the venue is optimistic and the soak must not read P&L from it
before the fill model is calibrated against live submit-to-fill latency):

- taker crossing of bids, and borrowers who take rates other than at the offer;
- newcomers who undercut after placement (queue position only ever improves);
- early repayment by the borrower, and any non-fixed offer type (FRR offers);
- offers expiring on their own (they rest until cancelled or filled);
- rate limits, other wallet types, margin, fee tiers, notify/hidden/renew flags;
- WebSocket (auth channel), `permissions`, and any endpoint not routed in `wire.py`.

Unconfirmed against live Bitfinex (knobs, defaults are the client's assumptions or
the docs): the field `/hist` filters on, the history lag, the cancel-not-active
answer shape (`cancel_rejection`), and the real business-rejection code numbers.

Snapshot trigger: the event log is replayed in full on open and `catch_up` copies the
state only when something is due. Add snapshots (to the SQL store) when a boot replay takes
longer than 5 seconds or the log exceeds 200_000 events, whichever comes first; the
soak report records the event count (`len(await store.load(account))`).

Simulator-internal failures (a feed that is slow or empty, a lost append race, a
bug) are never venue answers: they are recorded in `SimulatedVenue.internal_failures`
with a critical log, and CI and the soak count them apart from injected faults.
"""
from __future__ import annotations

from bfx_funding_bot.modules.simulated_venue._internal.market_replay import FixtureMarketFeed
from bfx_funding_bot.modules.simulated_venue._internal.store_memory import (
    InMemoryVenueEventStore,
)
from bfx_funding_bot.modules.simulated_venue._internal.store_sql import SqlVenueEventStore
from bfx_funding_bot.modules.simulated_venue._internal.transport import SimulatedVenue
from bfx_funding_bot.modules.simulated_venue.contracts import (
    ALLOWED_REALMS,
    REQUIRED_AUTHORITY_EPOCH,
    BookSnapshot,
    ConcurrentAppendError,
    FaultKind,
    FaultPlan,
    FaultRule,
    FaultTarget,
    FeedFailureError,
    HistoryFilter,
    InternalFailure,
    MarketFeed,
    NoMarketDataError,
    PublicTrade,
    RealmRefusedError,
    SimAccount,
    SimulatedVenueConfig,
    SimulatedVenueInternalError,
    VenueEventStore,
    VenueObserver,
    VenueStoreError,
)
from bfx_funding_bot.modules.simulated_venue.live_feed import (
    BookFetcher,
    LiveFeedConfig,
    LiveMarketFeed,
    TradesFetcher,
)

__all__ = [
    "ALLOWED_REALMS",
    "REQUIRED_AUTHORITY_EPOCH",
    "BookFetcher",
    "BookSnapshot",
    "ConcurrentAppendError",
    "FaultKind",
    "FaultPlan",
    "FaultRule",
    "FaultTarget",
    "FeedFailureError",
    "FixtureMarketFeed",
    "HistoryFilter",
    "InMemoryVenueEventStore",
    "InternalFailure",
    "LiveFeedConfig",
    "LiveMarketFeed",
    "MarketFeed",
    "NoMarketDataError",
    "PublicTrade",
    "RealmRefusedError",
    "SimAccount",
    "SimulatedVenue",
    "SimulatedVenueConfig",
    "SimulatedVenueInternalError",
    "SqlVenueEventStore",
    "TradesFetcher",
    "VenueEventStore",
    "VenueObserver",
    "VenueStoreError",
]
