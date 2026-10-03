"""The shared fill rule: clamp(cumulative - queue_ahead, 0, amount)."""
from __future__ import annotations

from decimal import Decimal

import pytest
from hypothesis import given
from hypothesis import strategies as st

from bfx_funding_bot.modules.lending.tracking.book_replay import queue_ahead
from bfx_funding_bot.modules.lending.tracking.queue_fill import filled_amount, queue_ahead_of
from bfx_funding_bot.modules.marketfeed.book_period_coverage import BookAskSnapshot

D = Decimal


@pytest.mark.parametrize(("ahead", "amount", "cum", "expected"), [
    ("0", "150", "0", "0"),
    ("0", "150", "40", "40"),
    ("0", "150", "150", "150"),
    ("0", "150", "9999", "150"),          # clamped to the offer size
    ("500", "150", "500", "0"),            # volume exactly consumes the queue: nothing yet
    ("500", "150", "501", "1"),
    ("500", "150", "650", "150"),          # fully filled at exactly queue + amount
    ("500", "150", "649.99999999", "149.99999999"),
    ("500", "150", "100", "0"),            # never negative
])
def test_filled_amount_table(ahead: str, amount: str, cum: str, expected: str) -> None:
    assert filled_amount(
        queue_ahead=D(ahead), amount=D(amount), cumulative_volume=D(cum),
    ) == D(expected)


_DEC = st.decimals(min_value=0, max_value=10_000, places=4)


@pytest.mark.property
@given(ahead=_DEC, amount=st.decimals(min_value="0.0001", max_value=10_000, places=4),
       cum=_DEC, more=_DEC)
def test_filled_amount_is_bounded_monotone_and_full_exactly_at_the_old_threshold(
    ahead: Decimal, amount: Decimal, cum: Decimal, more: Decimal,
) -> None:
    got = filled_amount(queue_ahead=ahead, amount=amount, cumulative_volume=cum)
    assert D(0) <= got <= amount
    assert filled_amount(queue_ahead=ahead, amount=amount, cumulative_volume=cum + more) >= got
    # The research rule was `cumulative >= queue_ahead + amount`; both must agree.
    assert (got == amount) == (cum >= ahead + amount)


def test_queue_ahead_of_matches_book_replay_wrapper() -> None:
    snap = BookAskSnapshot("fUST", 0, (
        (D("0.00019"), 2, D("100")), (D("0.0002"), 2, D("200")),
        (D("0.00021"), 2, D("300")), (D("0.0002"), 30, D("999")),
    ))
    for period in (2, 30):
        for rate in ("0.00018", "0.0002", "0.00025"):
            assert queue_ahead_of(snap.asks, period_days=period, offer_rate=D(rate)) == \
                queue_ahead(snap, period_days=period, offer_rate=D(rate))
