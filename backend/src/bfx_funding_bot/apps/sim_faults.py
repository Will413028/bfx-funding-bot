"""``BFX_SIM_FAULTS``: seeded fault injection for the simulated venue's in-process transport.

``unknown_5xx=0.01,unknown_placed_lost=0.005,history_error=0.005,seed=7``: each name is a
per-request probability, drawn from the plan's seed and the request ordinal (deterministic for a
given process life). The faults act inside ``SimulatedVenue`` only; nothing here can reach
Bitfinex, and ``build_venue`` refuses the knob for the bitfinex venue.

Submit faults make an UNKNOWN attempt the ledger must resolve by itself, which is what the
soak bar's "injected UNKNOWN all auto-closed" measures. They target submits and history reads
only: a faulted cancel would surface as an organic-looking quarantine the report could not
attribute. Every injection is appended to the venue's durable log as a ``FaultInjected`` event.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

ENV_NAME = "BFX_SIM_FAULTS"

# The knobs and what each one means (apps/venue.py maps them onto the venue's fault kinds; the
# venue facade may be imported by that one module only).
FAULT_NAMES = ("unknown_5xx", "unknown_placed_lost", "unknown_not_placed_lost", "history_error")


@dataclass(frozen=True, slots=True)
class SimFaultSpec:
    rates: Mapping[str, float]
    seed: int = 0


def parse_sim_faults(raw: str) -> SimFaultSpec:
    """Parse the knob; an empty string is "no faults". Anything unreadable raises ValueError."""
    rates: dict[str, float] = {}
    seed: int | None = None
    for item in (raw.split(",") if raw.strip() else []):
        name, sep, value = item.strip().partition("=")
        if not sep or not name or not value:
            raise ValueError(f"{ENV_NAME} entries are NAME=VALUE, got {item!r}")
        if name == "seed":
            if seed is not None:
                raise ValueError(f"{ENV_NAME} names seed twice")
            try:
                seed = int(value)
            except ValueError:
                raise ValueError(f"{ENV_NAME} seed must be an integer, got {value!r}") from None
            if seed < 0:
                raise ValueError(f"{ENV_NAME} seed must not be negative")
            continue
        if name not in FAULT_NAMES:
            raise ValueError(
                f"{ENV_NAME} does not know {name!r}; use {', '.join(FAULT_NAMES)} and seed")
        if name in rates:
            raise ValueError(f"{ENV_NAME} names {name} twice")
        try:
            rate = float(value)
        except ValueError:
            raise ValueError(f"{ENV_NAME} {name} must be a number, got {value!r}") from None
        if not 0.0 <= rate <= 1.0:  # also false for nan
            raise ValueError(f"{ENV_NAME} {name} must be a probability in [0, 1], got {value!r}")
        rates[name] = rate
    return SimFaultSpec(rates=rates, seed=seed or 0)
