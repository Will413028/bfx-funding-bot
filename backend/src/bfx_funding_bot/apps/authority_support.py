"""Which capital authorities each process may run under (the one place that says so).

A process refuses to boot on an authority outside its set (``core.authority``), so a
database switched to ``ledger`` never meets a process that still writes ``legacy``. The sets
are per venue and per realm; there is no build-wide set.

* ``bitfinex`` writes the legacy authority until S1-7 switches it.
* ``simulated`` is born on the ledger: the legacy authority was never built for it.
* The web API has no simulated counterpart and reads the legacy authority.
* The owner's policy script follows the database's realm: ``prod`` is still legacy, a
  simulation or CI database (``shadow``, ``ci``) may hold either.
"""
from __future__ import annotations

from typing import Final

from bfx_funding_bot.core.authority import Authority
from bfx_funding_bot.core.venue import Venue

SUPPORTED_BY_VENUE: Final[dict[Venue, frozenset[Authority]]] = {
    "bitfinex": frozenset({"legacy"}),
    "simulated": frozenset({"ledger"}),
}
WEBAPI_SUPPORTED: Final[frozenset[Authority]] = frozenset({"legacy"})
_POLICY_SCRIPT_BY_REALM: Final[dict[str, frozenset[Authority]]] = {
    "prod": frozenset({"legacy"}),
    "shadow": frozenset({"legacy", "ledger"}),
    "ci": frozenset({"legacy", "ledger"}),
}


def supported_for_venue(venue: Venue) -> frozenset[Authority]:
    return SUPPORTED_BY_VENUE[venue]


def supported_for_policy_script(realm: str) -> frozenset[Authority]:
    """A realm outside the known three has no set: the script refuses it."""
    try:
        return _POLICY_SCRIPT_BY_REALM[realm]
    except KeyError:
        raise ValueError(f"no capital authority is supported for realm {realm!r}") from None
