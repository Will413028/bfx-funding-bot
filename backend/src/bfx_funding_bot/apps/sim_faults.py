"""``BFX_SIM_FAULTS``: seeded fault injection for the simulated venue's in-process transport.

``unknown_5xx=0.01,unknown_placed_lost=0.005,history_error=0.005,seed=7``: each name is a
per-request probability, drawn from the plan's seed and the request's durable nonce. ``<name>_at``
(``unknown_5xx_at=3`` or ``=3+7``) instead makes the Nth request of that kind fault every time;
that ordinal restarts at 1 in each process life, so every generation of a soak injects again, which
is how a short soak is sure to see an injected UNKNOWN. The faults act inside ``SimulatedVenue`` only; nothing here can reach
Bitfinex, and ``build_venue`` refuses the knob for the bitfinex venue.

Submit faults make an UNKNOWN attempt the ledger must resolve by itself, which is what the
soak bar's "injected UNKNOWN all auto-closed" measures. They target submits and history reads
only: a faulted cancel would surface as an organic-looking quarantine the report could not
attribute. Every injection is appended to the venue's durable log as a ``FaultInjected`` event.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

ENV_NAME = "BFX_SIM_FAULTS"

# The one table of knobs: name -> (request kind, fault kind), as the venue's own string values
# (`FaultTarget`, `FaultKind`). apps/venue.py turns them into the enums; this module may not
# import the venue facade. Submit faults make an UNKNOWN attempt for the ledger to resolve;
# cancels are not faulted (a faulted cancel would surface as a quarantine the soak report could
# not attribute to an injection).
FAULT_KNOBS: dict[str, tuple[str, str]] = {
    "unknown_5xx": ("submit", "unknown_5xx_error"),
    "unknown_placed_lost": ("submit", "unknown_placed_lost"),
    "unknown_not_placed_lost": ("submit", "unknown_not_placed_lost"),
    "history_error": ("history", "history_error"),
}
FAULT_NAMES = tuple(FAULT_KNOBS)
ORDINAL_SUFFIX = "_at"  # ``unknown_5xx_at=3`` or ``=3+7``: the Nth request of that kind faults


@dataclass(frozen=True, slots=True)
class SimFaultSpec:
    rates: Mapping[str, float]
    seed: int = 0
    # knob name -> process ordinals (1-based, per request kind) that always fault. The ordinal
    # restarts at 1 in every process life, so each generation injects again.
    ordinals: Mapping[str, tuple[int, ...]] = field(default_factory=dict)


def parse_sim_faults(raw: str) -> SimFaultSpec:
    """Parse the knob; an empty string is "no faults". Anything unreadable raises ValueError."""
    rates: dict[str, float] = {}
    ordinals: dict[str, tuple[int, ...]] = {}
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
        if name.endswith(ORDINAL_SUFFIX) and name[:-len(ORDINAL_SUFFIX)] in FAULT_NAMES:
            knob = name[:-len(ORDINAL_SUFFIX)]
            if knob in ordinals:
                raise ValueError(f"{ENV_NAME} names {name} twice")
            try:
                picked = tuple(int(part) for part in value.split("+"))
            except ValueError:
                raise ValueError(
                    f"{ENV_NAME} {name} is ordinals joined by '+', got {value!r}") from None
            if any(n < 1 for n in picked) or len(set(picked)) != len(picked):
                raise ValueError(f"{ENV_NAME} {name} needs distinct ordinals >= 1, got {value!r}")
            ordinals[knob] = tuple(sorted(picked))
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
    return SimFaultSpec(rates=rates, seed=seed or 0, ordinals=ordinals)
