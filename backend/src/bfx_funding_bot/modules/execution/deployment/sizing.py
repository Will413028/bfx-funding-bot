"""Pure sizing functions for the deployment reconciler.

No I/O, no venue calls — deterministic given inputs (testable in isolation).
"""
from __future__ import annotations

from decimal import ROUND_DOWN, Decimal
from math import nextafter

from bfx_funding_bot.modules.ledger import CapitalAvailable


def venue_amount(amount: Decimal) -> Decimal:
    """Downward native amount quantization, including the legacy float boundary."""
    bounded = amount.quantize(Decimal("0.00000001"), rounding=ROUND_DOWN)
    wire = float(bounded)
    while Decimal(str(wire)) > bounded:
        wire = nextafter(wire, 0.0)
    return Decimal(str(wire))


def allocate_capital(*, views: dict[str, CapitalAvailable], min_fill: Decimal) -> dict[str, Decimal]:
    """Allocate one consistent canonical budget, emptiest cell first."""
    if not views:
        return {}
    first = next(iter(views.values()))
    remaining = first.budget.spendable
    fills: dict[str, Decimal] = {}
    for cell in sorted(views, key=lambda name: (views[name].snapshot.cell_exposure, name)):
        view = views[cell]
        if (view.applied, view.basis_token, view.budget.spendable) != (
            first.applied, first.basis_token, first.budget.spendable,
        ):
            raise ValueError("inconsistent capital views")
        limit = view.budget.max_new_offer
        ceiling = view.applied.policy.max_offer_amount
        if ceiling is not None:
            # Size within the policy's absolute per-offer ceiling (T9) rather than
            # sizing an offer the pre-trade guard would refuse on every tick.
            limit = min(limit, ceiling)
        amount = venue_amount(min(remaining, limit))
        if amount >= min_fill:
            fills[cell] = amount
            remaining -= amount
    return fills

