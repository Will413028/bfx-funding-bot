"""Per-symbol conservation of lent capital between two accepted bases, by venue id.

Pure: no storage, no clock. Between the previous accepted basis P and this one C
a symbol's lending may rise only by what offers filled in the interval, and
every fill must show up as new lending. Both sides are reconciled by venue id,
so no time window is applied to either and nothing is counted twice:

* new lending = the sum over credit keys of ``max(0, C - P)``. A key is one
  lending (the venue's (symbol, period, opening) when known, else the credit id),
  so a loan the venue replaces by credits of the same total is not new lending.
  A decrease (a loan ending or shrinking) is never an anomaly and never offsets
  an increase.
* fills = per offer id, what it filled in the interval: its remaining in P (its
  original amount when it is new) less its remaining in C or at its end. Each
  fill is own or foreign (an offer is foreign only when nothing ties it to this
  bot, quarantine members included). A fill whose evidence disagrees with
  itself (a negative amount, an unknown end or original amount, or the funding
  trades of the observation) is a *conflict*.

``unexplained = new lending - fills``. A conflict, or ``|unexplained| >
LEDGER_EPSILON`` in either direction, is ``unexplained_lending`` (lending no fill
explains, or fills no lending shows). Otherwise it is ``foreign_lending`` when
foreign offers filled more than the epsilon (an alert, not a halt), else
``conserved``. No previous row for the symbol is a ``baseline``.

This deliberately replaces the legacy ``LedgerConservation`` offered-drop
heuristic, which could not tell a cancel from a fill; they are not comparable
case by case.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
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
class OfferFill:
    """What one offer filled in the interval; ``conflict`` when its evidence disagrees."""

    filled: Decimal
    foreign: bool
    conflict: bool = False


@dataclass(frozen=True, slots=True)
class ConservationVerdict:
    """``lent_unexplained`` is new lending less fills (signed); ``foreign_executed`` is what
    foreign offers filled; ``fill_conflicts`` counts fills whose evidence disagrees. A
    baseline records nothing."""

    conservation: Conservation
    lent_unexplained: Decimal
    foreign_executed: Decimal
    fill_conflicts: int


@dataclass(frozen=True, slots=True)
class SymbolConservation:
    symbol: str
    conservation: Conservation
    lent_unexplained: Decimal
    foreign_executed: Decimal
    fill_conflicts: int


@dataclass(frozen=True, slots=True)
class AcceptedConservation:
    """The per-symbol verdicts the latest accepted basis of a scope stored."""

    basis_id: UUID
    query_id: UUID
    symbols: tuple[SymbolConservation, ...]


def new_lending(previous: Mapping[str, Decimal], current: Mapping[str, Decimal]) -> Decimal:
    """Sum of the increases per credit key; decreases and ended keys are ignored."""
    return sum(
        (max(Decimal(0), amount - previous.get(key, Decimal(0))) for key, amount in current.items()),
        Decimal(0),
    )


def conservation_verdict(
    previous: Mapping[str, Decimal] | None,
    current: Mapping[str, Decimal],
    fills: Sequence[OfferFill],
    *,
    epsilon: Decimal = LEDGER_EPSILON,
) -> ConservationVerdict:
    """The verdict of ``current`` credits and the interval's ``fills`` against ``previous``.

    ``previous`` is the symbol's credit amounts per key in the previous accepted
    basis, None when it had no row for the symbol (new currency, first basis): there
    is nothing to compare against.
    """
    if previous is None:
        return ConservationVerdict("baseline", Decimal(0), Decimal(0), 0)
    conflicts = sum(1 for fill in fills if fill.conflict or fill.filled < 0)
    filled = sum((fill.filled for fill in fills if not (fill.conflict or fill.filled < 0)), Decimal(0))
    foreign = sum(
        (fill.filled for fill in fills if fill.foreign and not (fill.conflict or fill.filled < 0)),
        Decimal(0),
    )
    unexplained = new_lending(previous, current) - filled
    if conflicts or abs(unexplained) > epsilon:
        return ConservationVerdict("unexplained_lending", unexplained, foreign, conflicts)
    if foreign > epsilon:
        return ConservationVerdict("foreign_lending", unexplained, foreign, 0)
    return ConservationVerdict("conserved", unexplained, foreign, 0)


__all__ = [
    "CONSERVATION_VERDICTS",
    "LEDGER_EPSILON",
    "AcceptedConservation",
    "Conservation",
    "ConservationVerdict",
    "OfferFill",
    "SymbolConservation",
    "conservation_verdict",
    "new_lending",
]
