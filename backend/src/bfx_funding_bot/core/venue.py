"""The venue axis: which counterparty a process trades against.

Derived from the phase and never set on its own: ``live`` trades Bitfinex, ``shadow`` the
simulated venue (ADR 2026-10-03 D1). Everything that differs between the two is chosen by
this value in one place (``apps``); no module reads the phase to find out.
"""
from __future__ import annotations

from typing import Final, Literal

from bfx_funding_bot.core.telemetry import Phase

Venue = Literal["bitfinex", "simulated"]

_VENUE_OF_PHASE: Final[dict[Phase, Venue]] = {
    Phase.LIVE: "bitfinex",
    Phase.SHADOW: "simulated",
}


def venue_for_phase(phase: Phase) -> Venue:
    return _VENUE_OF_PHASE[phase]


__all__ = ["Venue", "venue_for_phase"]
