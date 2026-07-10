"""Pure sizing functions for the deployment reconciler.

No I/O, no venue calls — deterministic given inputs (testable in isolation).
"""
from __future__ import annotations

from decimal import ROUND_CEILING, Decimal


def effective_min_usdt(venue_floor_usd: Decimal, buffer_pct: Decimal) -> Decimal:
    """Smallest offer (in USDT) that clears the venue's USD-equiv minimum.

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

    # Degenerate-case relaxation: with a single active cell the concentration
    # cap buys no diversification (all cells run the same strategy per symbol)
    # and strands (1 − concentration_pct) × target at 0%. max() is
    # behavior-identical for ≥2 active cells.
    cap_per_cell = max(concentration_pct * target, target / len(active_cells))
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
