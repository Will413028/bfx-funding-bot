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
    feed: MarketFeed, clock_ms: Callable[[], int],
    faults: FaultPlan | None = None,
) -> SimulatedVenue:
    """Open the venue over its store, refusing any database that is not on `ledger`.

    `account` already refuses realm `prod`. The authority epoch is reported by the store,
    which reads it on its own connection to the database it writes to; the caller has no
    way to pass one in. Faults are off unless a plan is given.
    """
    authority_epoch = await store.authority_epoch()
    if authority_epoch != REQUIRED_AUTHORITY_EPOCH:
        raise RealmRefusedError(
            f"simulated venue needs authority epoch {REQUIRED_AUTHORITY_EPOCH!r}, "
            f"got {authority_epoch!r}"
        )
    return await SimulatedVenue.open(
        account=account, config=config, store=store, feed=feed, clock_ms=clock_ms,
        faults=faults,
    )
