"""In-memory `VenueEventStore` for CI and unit tests (same shape as the SQL store)."""
from __future__ import annotations

from collections.abc import Sequence

from bfx_funding_bot.modules.simulated_venue.contracts import ConcurrentAppendError, SimAccount
from bfx_funding_bot.modules.simulated_venue.events import VenueEvent


class InMemoryVenueEventStore:
    def __init__(self) -> None:
        self._events: dict[SimAccount, list[VenueEvent]] = {}

    async def load(self, account: SimAccount) -> Sequence[VenueEvent]:
        return tuple(self._events.get(account, ()))

    async def append(
        self, account: SimAccount, expected_seq: int, events: Sequence[VenueEvent],
    ) -> None:
        log = self._events.setdefault(account, [])
        if len(log) != expected_seq:
            raise ConcurrentAppendError(
                f"{account}: expected seq {expected_seq}, log has {len(log)} events"
            )
        log.extend(events)
