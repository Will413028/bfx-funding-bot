"""The shared fill rule: queue position plus traded volume decide a fill.

Dependency-free on purpose (plain numbers and `(rate, period, amount)` tuples):
research (`book_replay`) and the simulated venue apply the same rule, and
neither may drag the other's imports along.

An offer resting behind `queue_ahead` of other lenders' volume receives
`clamp(cumulative_volume - queue_ahead, 0, amount)` once `cumulative_volume`
has traded at its period since placement. Fully filled (`== amount`) happens at
exactly the instant `book_replay` has always declared a fill; the clamp adds
the partial fills in between.
"""
from __future__ import annotations

from collections.abc import Iterable
from decimal import Decimal


def filled_amount(
    *, queue_ahead: Decimal, amount: Decimal, cumulative_volume: Decimal,
) -> Decimal:
    """Amount of an offer of size `amount` filled after `cumulative_volume` traded."""
    if cumulative_volume <= queue_ahead:
        return Decimal("0")
    return min(cumulative_volume - queue_ahead, amount)


def queue_ahead_of(
    asks: Iterable[tuple[Decimal, int, Decimal]], *, period_days: int, offer_rate: Decimal,
) -> Decimal:
    """Ask amount at `period_days` resting at or below `offer_rate` (FIFO: same rate counts)."""
    return sum(
        (amount for rate, period, amount in asks
         if period == period_days and rate <= offer_rate),
        Decimal("0"),
    )
