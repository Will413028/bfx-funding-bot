"""Tape replay: one hypothetical offer against the recorded public funding trades."""
from __future__ import annotations

import gzip
from datetime import date
from decimal import Decimal
from pathlib import Path

import numpy as np
import pytest

from bfx_funding_bot.modules.backtest.engine import BacktestIncomplete
from bfx_funding_bot.modules.backtest.replay import (
    HypotheticalOffer,
    TapeDays,
    replay_offer,
)

DAY = date(2026, 1, 10)
T0 = 1_768_003_200_000  # 2026-01-10T00:00:00Z
H = 3_600_000

# id, mts, amount, rate, period. Offer under test: rate 0.0002, period 7, amount 100.
GOLDEN_ROWS = [
    (1, T0, -500.0, 0.0003, 2),          # at t0 itself: not after placement
    (2, T0 + 1 * H, -40.0, 0.00025, 7),  # through our rate, compatible -> fills 40
    (3, T0 + 2 * H, 30.0, 0.00030, 7),   # positive = lender took a bid: never ours
    (4, T0 + 3 * H, -50.0, 0.00030, 30),  # PERIOD 30 > 7: incompatible
    (5, T0 + 4 * H, -50.0, 0.00015, 7),  # below our rate: borrower took cheaper asks
    (6, T0 + 5 * H, -25.0, 0.0002, 7),   # exactly our rate: queue-dependent
    (7, T0 + 6 * H, -45.0, 0.00021, 2),  # PERIOD 2 <= 7: compatible -> cum 85
    (8, T0 + 7 * H, -60.0, 0.00022, 7),  # cum 145 >= 100 -> conservative full fill
]


def write_day(root: Path, symbol: str, day: date, rows: list[tuple]) -> None:
    folder = root / symbol
    folder.mkdir(parents=True, exist_ok=True)
    lines = ["id,mts,amount,rate,period"] + [",".join(str(v) for v in r) for r in rows]
    (folder / f"{day.isoformat()}.csv.gz").write_bytes(
        gzip.compress(("\n".join(lines) + "\n").encode(), mtime=0)
    )


@pytest.fixture
def tape(tmp_path: Path) -> TapeDays:
    write_day(tmp_path, "fUST", DAY, GOLDEN_ROWS)
    write_day(tmp_path, "fUST", date(2026, 1, 11), [
        (10, T0 + 26 * H, -1.0, 0.0001, 2),
        (9, T0 + 30 * H, -999.0, 0.0009, 7),  # after every wait window below
    ])
    return TapeDays(tmp_path, "fUST")


def offer(rate: str = "0.0002", period: int = 7, amount: str = "100") -> HypotheticalOffer:
    return HypotheticalOffer(rate=Decimal(rate), period_days=period, placed_mts=T0,
                             amount=Decimal(amount))


def test_golden_conservative_and_optimistic(tape: TapeDays) -> None:
    r = replay_offer(offer(), tape, max_wait_ms=24 * H)
    assert r.conservative.filled == Decimal("100")
    assert r.conservative.full_fill_mts == T0 + 7 * H
    assert r.conservative.first_fill_mts == T0 + 1 * H
    # Optimistic counts the same-rate 25 too: 40 + 25 + 45 = 110 >= 100 at trade 7.
    assert r.optimistic.filled == Decimal("100")
    assert r.optimistic.full_fill_mts == T0 + 6 * H
    assert r.queue_estimate is None


def test_partial_fill_when_volume_short(tape: TapeDays) -> None:
    r = replay_offer(offer(amount="1000"), tape, max_wait_ms=24 * H)
    assert r.conservative.filled == Decimal("145")
    assert r.conservative.full_fill_mts is None
    assert r.optimistic.filled == Decimal("170")


def test_short_offer_period_excludes_longer_public_periods(tape: TapeDays) -> None:
    r = replay_offer(offer(period=2, amount="1000"), tape, max_wait_ms=24 * H)
    assert r.conservative.filled == Decimal("45")


def test_queue_estimate_charges_same_rate_queue(tape: TapeDays) -> None:
    r = replay_offer(offer(amount="1000"), tape, max_wait_ms=24 * H,
                     same_rate_queue_ahead=Decimal("10"))
    assert r.queue_estimate is not None
    assert r.queue_estimate.filled == Decimal("160")  # 145 + (25 - 10)


def test_unfilled_has_no_fill_times(tape: TapeDays) -> None:
    r = replay_offer(offer(rate="0.001"), tape, max_wait_ms=24 * H)
    assert r.conservative.filled == 0
    assert r.conservative.first_fill_mts is None
    assert r.optimistic.full_fill_mts is None


def test_window_crosses_into_next_day_file(tape: TapeDays) -> None:
    r = replay_offer(offer(rate="0.0001", period=2, amount="1000"), tape, max_wait_ms=27 * H)
    # trade 7 (45) + day-2 trade 10 at the offer's own rate (optimistic only)
    assert r.conservative.filled == Decimal("45")
    assert r.optimistic.filled == Decimal("46")


def test_missing_day_file_is_incomplete(tape: TapeDays) -> None:
    with pytest.raises(BacktestIncomplete) as exc:
        replay_offer(offer(), tape, max_wait_ms=50 * H)  # needs 2026-01-12
    assert exc.value.reason == "tape_day_missing"


def test_day_cache_is_bounded(tmp_path: Path) -> None:
    for d in range(1, 8):
        write_day(tmp_path, "fUSD", date(2026, 1, d), [(d, 0, -1.0, 0.0001, 2)])
    days = TapeDays(tmp_path, "fUSD", cache_days=3)
    for d in range(1, 8):
        days.day(date(2026, 1, d))
    assert days.cached_days() <= 3


def test_float_sum_a_hair_short_still_counts_as_full_fill(tmp_path: Path) -> None:
    rows = [(i, T0 + (i + 1) * 60_000, -0.1, 0.0003, 2) for i in range(10)]
    write_day(tmp_path, "fUST", DAY, rows)
    r = replay_offer(offer(period=2, amount="1"), TapeDays(tmp_path, "fUST"), max_wait_ms=H)
    assert np.cumsum([0.1] * 10)[-1] < 1.0  # the float trap this guards
    assert r.conservative.filled == Decimal("1")
    assert r.conservative.full_fill_mts == T0 + 10 * 60_000


def test_large_offer_float_error_still_counts_as_full_fill(tmp_path: Path) -> None:
    rows = [(i, T0 + (i + 1) * 1_000, -1000.12345678, 0.0003, 2) for i in range(1000)]
    write_day(tmp_path, "fUSD", DAY, rows)
    amount = Decimal("1000.12345678") * 1000
    r = replay_offer(offer(period=2, amount=str(amount)), TapeDays(tmp_path, "fUSD"),
                     max_wait_ms=H)
    assert r.conservative.filled == amount
    assert r.conservative.full_fill_mts == T0 + 1000 * 1_000


def test_unsorted_day_file_is_rejected(tmp_path: Path) -> None:
    write_day(tmp_path, "fUST", DAY, [(1, T0 + 2 * H, -1.0, 0.0003, 2),
                                      (2, T0 + 1 * H, -1.0, 0.0003, 2)])
    with pytest.raises(ValueError, match="not sorted"):
        TapeDays(tmp_path, "fUST").day(DAY)


def test_negative_queue_ahead_is_rejected(tape: TapeDays) -> None:
    with pytest.raises(ValueError, match="same_rate_queue_ahead"):
        replay_offer(offer(), tape, max_wait_ms=H, same_rate_queue_ahead=Decimal("-1"))


def test_crossing_bid_fills_every_bound_at_placement(tape: TapeDays) -> None:
    result = replay_offer(offer(), tape, max_wait_ms=24 * H, same_rate_queue_ahead=Decimal(0),
                          crossing_bid_amount=Decimal("60"))
    for path in (result.conservative, result.optimistic, result.queue_estimate):
        assert path is not None and path.first_fill_mts == T0
    # 60 crossed at t0, then 40 more through our rate at t0+1h completes the 100.
    assert result.conservative.full_fill_mts == T0 + 1 * H


def test_crossing_bid_larger_than_offer_fills_in_full_at_once(tape: TapeDays) -> None:
    result = replay_offer(offer(), tape, max_wait_ms=24 * H, crossing_bid_amount=Decimal("500"))
    assert result.conservative.full_fill_mts == T0
    assert result.conservative.filled == Decimal("100")


def test_negative_crossing_bid_is_rejected(tape: TapeDays) -> None:
    with pytest.raises(ValueError, match="crossing_bid_amount"):
        replay_offer(offer(), tape, max_wait_ms=H, crossing_bid_amount=Decimal("-1"))
