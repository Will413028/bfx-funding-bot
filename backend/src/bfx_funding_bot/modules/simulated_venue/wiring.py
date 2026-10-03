"""Composition root entry for the simulated venue. Only `apps` (and tests) import this."""
from __future__ import annotations

from collections.abc import Callable

from bfx_funding_bot.modules.simulated_venue._internal.transport import SimulatedVenue
from bfx_funding_bot.modules.simulated_venue.contracts import (
    REQUIRED_AUTHORITY_EPOCH,
    FaultPlan,
    MarketFeed,
    RealmRefusedError,
    SimAccount,
    SimulatedVenueConfig,
    VenueEventStore,
)


async def build_simulated_venue(
    *, account: SimAccount, config: SimulatedVenueConfig, store: VenueEventStore,
    feed: MarketFeed, clock_ms: Callable[[], int], authority_epoch: str,
    faults: FaultPlan | None = None,
) -> SimulatedVenue:
    """Open the venue over its store, refusing any database that is not on `ledger`.

    `account` already refuses realm `prod`. `authority_epoch` is the raw latest
    authority epoch of the database the process runs against, read by the
    composition root (not through the bot's `read_authority`, which is venue-blind).
    Faults are off unless a plan is given.
    """
    if authority_epoch != REQUIRED_AUTHORITY_EPOCH:
        raise RealmRefusedError(
            f"simulated venue needs authority epoch {REQUIRED_AUTHORITY_EPOCH!r}, "
            f"got {authority_epoch!r}"
        )
    return await SimulatedVenue.open(
        account=account, config=config, store=store, feed=feed, clock_ms=clock_ms,
        faults=faults,
    )
