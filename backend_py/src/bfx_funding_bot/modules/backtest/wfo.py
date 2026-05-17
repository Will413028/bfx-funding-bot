"""Walk-forward optimization window generator for Phase 3b-WFO.

Generates calendar-aligned (train, test) window pairs from a candle list.
Each window is anchored to UTC month boundaries so they're deterministic
across runs and easy to reason about in results reports.

Used by the WFO matrix runner (run_phase3b_wfo_matrix.py) and the
run_cell_wfo orchestration helper in matrix.py.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from bfx_funding_bot.modules.candles.schemas import FundingCandle


@dataclass(frozen=True)
class WfoWindow:
    """One walk-forward (train, test) pair, mts boundaries inclusive."""
    train_start_mts: int
    train_end_mts: int  # inclusive, last ms before test_start_mts
    test_start_mts: int
    test_end_mts: int  # inclusive, last ms of test month


def _month_start_mts(year: int, month: int) -> int:
    """First ms of YYYY-MM-01 00:00:00 UTC."""
    return int(datetime(year, month, 1, tzinfo=UTC).timestamp() * 1000)


def _advance_months(year: int, month: int, delta_months: int) -> tuple[int, int]:
    """Return (year, month) advanced by delta_months."""
    total = (year * 12 + (month - 1)) + delta_months
    return total // 12, total % 12 + 1


def compute_wfo_windows(
    candles: list[FundingCandle],
    train_months: int = 3,
    test_months: int = 1,
    step_months: int = 1,
    min_candles_per_segment: int = 200,
) -> list[WfoWindow]:
    """Generate calendar-aligned WFO windows for a candle stream.

    Args:
        candles: Non-empty candle list. Order doesn't matter; sorted internally.
        train_months: Number of UTC months in each train portion. Default 3.
        test_months: Number of UTC months in each test portion. Default 1.
        step_months: How many UTC months to advance between successive windows. Default 1.
        min_candles_per_segment: Skip windows where train OR test has fewer
            candles than this. Default 200 (~1 week of 1h candles).

    Returns:
        List of WfoWindow in chronological order. Empty if no window survives
        the min-candles filter.

    Raises:
        ValueError if candles list is empty.
    """
    if not candles:
        raise ValueError("compute_wfo_windows: candles list is empty")

    sorted_candles = sorted(candles, key=lambda c: c.mts)
    series_start_mts = sorted_candles[0].mts
    series_end_mts = sorted_candles[-1].mts

    series_start_dt = datetime.fromtimestamp(series_start_mts / 1000, UTC)
    # Anchor first window's train_start to the first month boundary at-or-after series start
    if series_start_dt.day == 1 and series_start_dt.hour == 0 and series_start_dt.minute == 0:
        anchor_year, anchor_month = series_start_dt.year, series_start_dt.month
    else:
        anchor_year, anchor_month = _advance_months(series_start_dt.year, series_start_dt.month, 1)

    windows: list[WfoWindow] = []
    window_idx = 0
    while True:
        train_start_year, train_start_month = _advance_months(
            anchor_year, anchor_month, window_idx * step_months
        )
        test_start_year, test_start_month = _advance_months(
            train_start_year, train_start_month, train_months
        )
        test_end_excl_year, test_end_excl_month = _advance_months(
            test_start_year, test_start_month, test_months
        )

        train_start_mts = _month_start_mts(train_start_year, train_start_month)
        test_start_mts = _month_start_mts(test_start_year, test_start_month)
        test_end_excl_mts = _month_start_mts(test_end_excl_year, test_end_excl_month)

        if test_end_excl_mts > series_end_mts + 1:
            break

        train_end_mts = test_start_mts - 1
        test_end_mts = test_end_excl_mts - 1

        train_n = sum(1 for c in sorted_candles if train_start_mts <= c.mts <= train_end_mts)
        test_n = sum(1 for c in sorted_candles if test_start_mts <= c.mts <= test_end_mts)

        if train_n >= min_candles_per_segment and test_n >= min_candles_per_segment:
            windows.append(WfoWindow(
                train_start_mts=train_start_mts,
                train_end_mts=train_end_mts,
                test_start_mts=test_start_mts,
                test_end_mts=test_end_mts,
            ))

        window_idx += 1
        if window_idx > 1000:  # paranoia guard
            break

    return windows
