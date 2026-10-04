"""The venue axis: which counterparty a process trades against.

Derived from the phase and never set on its own: ``live`` trades Bitfinex, ``shadow`` the
simulated venue (ADR 2026-10-03 D1). Everything that differs between the two is chosen by
this value in one place (``apps``); no module reads the phase to find out.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Final, Literal

from bfx_funding_bot.core.telemetry import Phase

Venue = Literal["bitfinex", "simulated"]

_VENUE_OF_PHASE: Final[dict[Phase, Venue]] = {
    Phase.LIVE: "bitfinex",
    Phase.SHADOW: "simulated",
}


def venue_for_phase(phase: Phase) -> Venue:
    return _VENUE_OF_PHASE[phase]


@dataclass(frozen=True, slots=True)
class VenueCapabilities:
    """What a venue has, stated by its wiring so no consumer compares venue names.

    ``auth_ws``: Bitfinex fills arrive on the authenticated WebSocket and a process without
    it holds stale exposure (``required``); the simulated venue has none (``forbidden``).
    ``rest_fill_tracker``: whether the REST fill tracker may be enabled.
    """

    auth_ws: Literal["required", "forbidden"]
    rest_fill_tracker: bool


__all__ = ["Venue", "VenueCapabilities", "venue_for_phase"]
