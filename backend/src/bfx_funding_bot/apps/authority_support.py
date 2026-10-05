"""Which capital authorities each process may run under (the one place that says so).

A process refuses to boot on an authority outside its set (``core.authority``), so a
database switched to ``ledger`` never meets a process that still writes ``legacy``. The sets
are per venue and per realm; there is no build-wide set. The database's epoch picks one of
them (ADR 2026-10-02 D2); only the owner appends it (the S1-7 switch).

* ``bitfinex`` runs either authority: legacy until the switch, the ledger after it.
* ``simulated`` is born on the ledger: the legacy authority was never built for it.
* The web API has no simulated counterpart and reads either authority.
* The owner's policy script follows the database's realm; every known realm holds either.

A switched database must also hold the seed (H-1, 2026-10-05): the real venue in the realm
that has legacy history (``prod``) refuses a ``ledger`` boot until every configured scope has
its ``legacy_seed`` observation (``require_ledger_seed``). Without it the first runtime basis
is a ``baseline`` that disowns every resting offer and open credit, and the seed could never
run afterwards (it refuses a non-empty ledger). A simulation database (``shadow``) is
bootstrapped on the ledger without a seed and a ``ci`` database holds no legacy history, so
neither needs one.
"""
from __future__ import annotations

from typing import Final

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bfx_funding_bot.core.authority import Authority, AuthorityMismatch
from bfx_funding_bot.core.venue import Venue
from bfx_funding_bot.modules.ledger import Scope
from bfx_funding_bot.modules.ledger.tables import LedgerObservationRow

SUPPORTED_BY_VENUE: Final[dict[Venue, frozenset[Authority]]] = {
    "bitfinex": frozenset({"legacy", "ledger"}),
    "simulated": frozenset({"ledger"}),
}
WEBAPI_SUPPORTED: Final[frozenset[Authority]] = frozenset({"legacy", "ledger"})
_POLICY_SCRIPT_BY_REALM: Final[dict[str, frozenset[Authority]]] = {
    "prod": frozenset({"legacy", "ledger"}),
    "shadow": frozenset({"legacy", "ledger"}),
    "ci": frozenset({"legacy", "ledger"}),
}
# The realms whose database holds legacy history: a ledger boot there needs the seed.
_SEEDED_REALMS: Final[frozenset[str]] = frozenset({"prod"})


def supported_for_venue(venue: Venue) -> frozenset[Authority]:
    return SUPPORTED_BY_VENUE[venue]


def supported_for_policy_script(realm: str) -> frozenset[Authority]:
    """A realm outside the known three has no set: the script refuses it."""
    try:
        return _POLICY_SCRIPT_BY_REALM[realm]
    except KeyError:
        raise ValueError(f"no capital authority is supported for realm {realm!r}") from None


def ledger_boot_needs_seed(venue: Venue, realm: str) -> bool:
    """A real (non-simulated) venue in a realm with legacy history boots the ledger only
    over the seed."""
    return venue != "simulated" and realm in _SEEDED_REALMS


async def require_ledger_seed(
    session: AsyncSession, *, authority: Authority, venue: Venue, scopes: tuple[Scope, ...]
) -> None:
    """Refuse (AuthorityMismatch) a ``ledger`` boot of a real venue in a seeded realm while
    any of ``scopes`` lacks its ``legacy_seed`` observation; every other boot passes."""
    if authority != "ledger":
        return
    for scope in scopes:
        if not ledger_boot_needs_seed(venue, scope.deployment_environment):
            continue
        seeded = await session.scalar(
            select(LedgerObservationRow.id).where(
                LedgerObservationRow.exchange_account_id == scope.exchange_account_id,
                LedgerObservationRow.deployment_environment == scope.deployment_environment,
                LedgerObservationRow.origin == "legacy_seed",
            ).limit(1)
        )
        if seeded is None:
            raise AuthorityMismatch(
                f"ledger_seed_missing scope={scope.exchange_account_id}:"
                f"{scope.deployment_environment}"
            )
