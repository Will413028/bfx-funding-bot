"""Dormant, in-process simulated Bitfinex venue (httpx transport, event-sourced state).

Nothing composes this yet. It imports nothing of ledger, execution, trading or
marketfeed, and only composition roots import `wiring`.
"""
from __future__ import annotations

from bfx_funding_bot.modules.simulated_venue._internal.market_replay import FixtureMarketFeed
from bfx_funding_bot.modules.simulated_venue._internal.store_memory import (
    InMemoryVenueEventStore,
)
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
    HistoryFilter,
    MarketFeed,
    PublicTrade,
    RealmRefusedError,
    SimAccount,
    SimulatedVenueConfig,
    VenueEventStore,
)

__all__ = [
    "ALLOWED_REALMS", "REQUIRED_AUTHORITY_EPOCH", "BookSnapshot", "ConcurrentAppendError",
    "FaultKind", "FaultPlan", "FaultRule", "FaultTarget", "FixtureMarketFeed",
    "HistoryFilter", "InMemoryVenueEventStore", "MarketFeed", "PublicTrade", "RealmRefusedError",
    "SimAccount", "SimulatedVenue", "SimulatedVenueConfig", "VenueEventStore",
]
