"""Per-symbol conservation of lent capital between two accepted bases.

Pure: no storage, no clock. A symbol's lent amount may fall (a loan ended) and
may rise by what the venue's offers lost (a fill: the offers of a basis are every
venue offer, ours or foreign). What is left is lending no observed offer
accounts for. Lending envelope D2: the account may also carry foreign offers,
and one can be placed and filled between two accepted bases without ever being
seen; the offer history shows it executed. So the remainder is first set
against ``foreign_executed`` (the symbol's unattributed offers that ended after
the previous accepted query began): fully covered is foreign lending (an
alert), anything beyond is unexplained lending (a protection trigger).

The rule is the legacy ``execution.safety.protection.LedgerConservation``; the
ledger cannot import execution, so it is re-stated here, the epsilon lives here
(execution imports it) and a differential test pins the two. Unlike legacy there
is no carried delta: acceptance compares the two accepted bases directly. What
legacy had in its ledger before a snapshot and a basis lacks are the offers this
bot placed since the previous basis: ``placed`` adds them to the previous offered
amount (an offer placed and filled between two bases drops its original amount
to its remainder, which is a fill, not new lending).
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Literal
from uuid import UUID

# Same tolerance as the reconcile divergence report.
LEDGER_EPSILON = Decimal("0.01")

type Conservation = Literal["baseline", "conserved", "foreign_lending", "unexplained_lending"]
CONSERVATION_VERDICTS: tuple[Conservation, ...] = (
    "baseline",
    "conserved",
    "foreign_lending",
    "unexplained_lending",
)


@dataclass(frozen=True, slots=True)
class SymbolFlow:
    """One symbol of one accepted basis.

    ``offered`` is every venue offer (managed and foreign); ``lent`` is the
    symbol's credits.
    """

    offered: Decimal
    lent: Decimal


@dataclass(frozen=True, slots=True)
class ConservationVerdict:
    """``lent_unexplained`` is the lent increase no offer drop explains (never negative);
    ``foreign_executed`` is what foreign offers that ended since the previous accepted
    query lent. A baseline records neither."""

    conservation: Conservation
    lent_unexplained: Decimal
    foreign_executed: Decimal


@dataclass(frozen=True, slots=True)
class SymbolConservation:
    symbol: str
    conservation: Conservation
    lent_unexplained: Decimal
    foreign_executed: Decimal


@dataclass(frozen=True, slots=True)
class AcceptedConservation:
    """The per-symbol verdicts the latest accepted basis of a scope stored."""

    basis_id: UUID
    query_id: UUID
    symbols: tuple[SymbolConservation, ...]


def conservation_verdict(
    previous: SymbolFlow | None,
    current: SymbolFlow,
    foreign_executed: Decimal,
    placed: Decimal,
    *,
    epsilon: Decimal = LEDGER_EPSILON,
) -> ConservationVerdict:
    """The verdict of ``current`` against the previous accepted basis of the symbol.

    ``placed`` is the original amount of the offers this bot placed since that
    basis and that the current observation reflects (none of them in the previous
    basis's offers). No previous row for the symbol (new currency, first basis)
    only establishes a baseline: there is nothing to compare against.
    """
    if previous is None:
        return ConservationVerdict("baseline", Decimal(0), Decimal(0))
    offered_change = current.offered - (previous.offered + placed)
    lent_change = current.lent - previous.lent
    unexplained = lent_change - max(Decimal(0), -offered_change)
    lent_unexplained = max(Decimal(0), unexplained)
    if unexplained <= epsilon:
        return ConservationVerdict("conserved", lent_unexplained, foreign_executed)
    if unexplained - foreign_executed > epsilon:
        return ConservationVerdict("unexplained_lending", lent_unexplained, foreign_executed)
    return ConservationVerdict("foreign_lending", lent_unexplained, foreign_executed)


__all__ = [
    "CONSERVATION_VERDICTS",
    "LEDGER_EPSILON",
    "AcceptedConservation",
    "Conservation",
    "ConservationVerdict",
    "SymbolConservation",
    "SymbolFlow",
    "conservation_verdict",
]
