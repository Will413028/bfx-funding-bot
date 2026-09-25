"""Pure sizing functions for the deployment reconciler.

No I/O, no venue calls — deterministic given inputs (testable in isolation).
"""
from __future__ import annotations

from decimal import ROUND_CEILING, ROUND_DOWN, Decimal
from math import nextafter

from bfx_funding_bot.modules.execution.capital_repository import CapitalView


def venue_amount(amount: Decimal) -> Decimal:
    """Downward native amount quantization, including the legacy float boundary."""
    bounded = amount.quantize(Decimal("0.00000001"), rounding=ROUND_DOWN)
    wire = float(bounded)
    while Decimal(str(wire)) > bounded:
        wire = nextafter(wire, 0.0)
    return Decimal(str(wire))


def allocate_capital(*, views: dict[str, CapitalView], min_fill: Decimal) -> dict[str, Decimal]:
    """Allocate one consistent canonical budget, emptiest cell first."""
    if not views:
        return {}
    first = next(iter(views.values()))
    remaining = first.budget.spendable
    fills: dict[str, Decimal] = {}
    for cell in sorted(views, key=lambda name: (views[name].snapshot.cell_exposure, name)):
        view = views[cell]
        if (view.applied, view.snapshot_seq, view.budget.spendable) != (
            first.applied, first.snapshot_seq, first.budget.spendable,
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


def effective_min_usdt(venue_floor_usd: Decimal, buffer_pct: Decimal) -> Decimal:
    """Historical simulation helper, never live funding-rule authority.

    v1: static — the buffer absorbs USDT de-peg + precision (assumes
    USDT >= 1 - buffer). No USDT/USD ticker fetch (deferred, future venue-call).
    ceil(150 * 1.02) = 153.
    """
    raw = venue_floor_usd * (Decimal("1") + buffer_pct)
    return raw.quantize(Decimal("1"), rounding=ROUND_CEILING)


def allocate_gap(
    *,
    target: Decimal,
    current_exposure: Decimal,
    deployed: dict[str, Decimal],
    active_cells: list[str],
    concentration_pct: Decimal,
    min_fill: Decimal,
    available_headroom: Decimal = Decimal("Infinity"),
) -> dict[str, Decimal]:
    """Distribute the funding gap across active cells.

    Greedy emptiest-first (balances per-cell deployment over time), each cell
    capped at concentration_pct * target. Fills below min_fill are dropped to
    avoid sub-minimum dust (would be rejected by the venue minimum anyway).
    Total allocated <= gap, so the global allocation cap is never exceeded.

    The gap is clamped to available_headroom (= venue free balance − buffer) so
    the reconciler never sizes an offer larger than the funds physically present;
    default Infinity = no balance constraint (only the policy cap binds).
    cap_per_cell stays bound to the POLICY target, not the balance-clamped gap.
    """
    gap = min(target - current_exposure, available_headroom)
    if gap < min_fill or not active_cells:
        return {}

    # Even a lone active cell must respect the configured concentration limit.
    cap_per_cell = concentration_pct * target
    ordered = sorted(active_cells, key=lambda c: (deployed.get(c, Decimal("0")), c))

    fills: dict[str, Decimal] = {}
    remaining = gap
    for cell in ordered:
        if remaining < min_fill:
            break
        headroom = cap_per_cell - deployed.get(cell, Decimal("0"))
        fill = min(remaining, headroom)
        if fill >= min_fill:
            fills[cell] = fill
            remaining -= fill
    return fills
