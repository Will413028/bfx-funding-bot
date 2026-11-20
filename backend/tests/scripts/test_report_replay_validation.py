"""Replay validation against our own offers: metrics, own-trade exclusion, verdicts."""
from __future__ import annotations

import gzip
from dataclasses import replace
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from bfx_funding_bot.modules.market.book_period_coverage import BookAskSnapshot
from scripts.report_replay_validation import (
    COVERAGE_MIN,
    DEFAULT_MAX_WAIT_MS,
    SAMPLE_FLOOR,
    ExcludingTape,
    OfferOutcome,
    OwnOffer,
    bound_width_ms,
    evaluate,
    is_covered,
    nearest_snapshot,
    queue_error_ms,
    render,
    same_rate_queue,
    summarize,
    tape_has_day,
)

DAY = date(2026, 1, 10)
T0 = 1_768_003_200_000  # 2026-01-10T00:00:00Z
M = 60_000
H = 60 * M
RATE = Decimal("0.0002")


def write_day(root: Path, symbol: str, day: date, rows: list[tuple]) -> None:
    folder = root / symbol
    folder.mkdir(parents=True, exist_ok=True)
    lines = ["id,mts,amount,rate,period"] + [",".join(str(v) for v in r) for r in rows]
    (folder / f"{day.isoformat()}.csv.gz").write_bytes(
        gzip.compress(("\n".join(lines) + "\n").encode(), mtime=0))


def own(first_fill: int | None = T0 + 30 * M, amount: str = "100") -> OwnOffer:
    return OwnOffer(venue_offer_id="1", symbol="fUST", rate=RATE, period_days=2,
                    placed_mts=T0, amount=Decimal(amount), first_fill_mts=first_fill)


def outcome(actual: int | None, opt: int | None, cons: int | None, queue: int | None = None,
            *, has_queue: bool = True, period: int = 2) -> OfferOutcome:
    return OfferOutcome(offer=replace(own(), period_days=period), actual_ms=actual,
                        optimistic_ms=opt, conservative_ms=cons, queue_ms=queue,
                        has_queue_estimate=has_queue)


@pytest.fixture
def tape_root(tmp_path: Path) -> Path:
    write_day(tmp_path, "fUST", DAY, [
        (100, T0 + 10 * M, -100.0, 0.0002, 2),   # same rate: optimistic and queue only
        (101, T0 + 30 * M, -100.0, 0.0002, 2),   # our own trade at our rate
        (102, T0 + 2 * H, -100.0, 0.0003, 2),    # through our rate: conservative
    ])
    write_day(tmp_path, "fUST", date(2026, 1, 11), [])
    return tmp_path


def test_own_trades_are_removed_from_the_tape(tape_root: Path) -> None:
    snap = BookAskSnapshot("fUST", T0 - M, ((RATE, 2, Decimal("150")),))
    with_own = evaluate([own()], tape_for=lambda s: ExcludingTape(tape_root, s, []),
                        snapshots={"fUST": [snap]})[0]
    without = evaluate([own()], tape_for=lambda s: ExcludingTape(tape_root, s, [101]),
                       snapshots={"fUST": [snap]})[0]
    # Queue 150 ahead: trade 100 leaves 50 of it, our own trade 101 would clear it at 30m.
    assert with_own.queue_ms == 30 * M
    assert without.queue_ms == 2 * H
    assert without.optimistic_ms == 10 * M
    assert without.conservative_ms == 2 * H
    assert without.actual_ms == 30 * M


def test_offers_whose_wait_passes_the_tape_end_are_skipped(tape_root: Path) -> None:
    rows = evaluate([own()], tape_for=lambda s: ExcludingTape(tape_root, s, []),
                    snapshots={}, tape_covers=lambda _end: False)
    assert rows == []
    assert tape_has_day(tape_root, [own()], T0 + DEFAULT_MAX_WAIT_MS)
    assert not tape_has_day(tape_root, [own()], T0 + 2 * DEFAULT_MAX_WAIT_MS)


def test_same_rate_queue_tolerates_float_rate_noise() -> None:
    snap = BookAskSnapshot("fUST", T0 - M, ((Decimal("0.00020000000000000001"), 2, Decimal("7")),))
    assert same_rate_queue(snap, own()) == Decimal("7")


def test_actual_fill_after_the_wait_counts_as_no_fill(tape_root: Path) -> None:
    late = own(first_fill=T0 + DEFAULT_MAX_WAIT_MS + 1)
    result = evaluate([late], tape_for=lambda s: ExcludingTape(tape_root, s, []),
                      snapshots={})[0]
    assert result.actual_ms is None
    assert not result.has_queue_estimate


@pytest.mark.parametrize(("actual", "opt", "cons", "covered"), [
    (30 * M, 10 * M, 2 * H, True),
    (10 * M, 10 * M, 10 * M, True),       # endpoints included
    (5 * M, 10 * M, 2 * H, False),        # filled before the optimistic bound
    (3 * H, 10 * M, 2 * H, False),        # later than the conservative bound
    (3 * H, 10 * M, None, True),          # conservative never filled: open upper end
    (30 * M, None, None, False),          # replay never fills, reality did
    (None, 10 * M, None, True),           # no fill and conservative agrees
    (None, 10 * M, 2 * H, False),         # conservative filled, reality did not
])
def test_coverage(actual: int | None, opt: int | None, cons: int | None, covered: bool) -> None:
    assert is_covered(outcome(actual, opt, cons)) is covered


def test_width_and_error_pin_missing_fills_at_the_wait() -> None:
    w = DEFAULT_MAX_WAIT_MS
    assert bound_width_ms(outcome(None, 10 * M, None), w) == w - 10 * M
    assert queue_error_ms(outcome(30 * M, 0, 0, None), w) == w - 30 * M
    assert queue_error_ms(outcome(30 * M, 0, 0, 10 * M), w) == -20 * M


def test_same_rate_queue_counts_only_our_level_and_drops_ourselves_after_placement() -> None:
    asks = ((Decimal("0.0001"), 2, Decimal("999")), (RATE, 2, Decimal("150")),
            (RATE, 7, Decimal("70")))
    before = BookAskSnapshot("fUST", T0 - M, asks)
    after = BookAskSnapshot("fUST", T0 + M, asks)
    assert same_rate_queue(before, own()) == Decimal("150")
    assert same_rate_queue(after, own()) == Decimal("50")
    assert same_rate_queue(after, own(amount="500")) == 0


def test_nearest_snapshot_prefers_before_and_respects_tolerance() -> None:
    snaps = [BookAskSnapshot("fUST", T0 - 6 * M, ()), BookAskSnapshot("fUST", T0 - 4 * M, ()),
             BookAskSnapshot("fUST", T0 + M, ())]
    picked, only_after = nearest_snapshot(snaps, T0), nearest_snapshot(snaps[2:], T0)
    assert picked is not None and picked.captured_at_ms == T0 - 4 * M
    assert only_after is not None and only_after.captured_at_ms == T0 + M
    assert nearest_snapshot(snaps[:1], T0) is None


def test_coverage_denominator_is_every_offer_not_only_filled_ones() -> None:
    rows = [outcome(30 * M, 10 * M, 2 * H), outcome(None, 10 * M, 2 * H)]
    (bucket,) = summarize(rows)
    assert bucket.offers == 2 and bucket.filled == 1
    assert bucket.coverage == 0.5


def test_floor_met_needs_coverage_and_queue_error() -> None:
    good = [outcome(30 * M, 10 * M, 2 * H, 40 * M)] * SAMPLE_FLOOR
    assert summarize(good)[0].passed
    slow_queue = [outcome(30 * M, 10 * M, 2 * H, 30 * M + 61 * M)] * SAMPLE_FLOOR
    assert not summarize(slow_queue)[0].passed
    n_bad = SAMPLE_FLOOR - int(SAMPLE_FLOOR * COVERAGE_MIN) + 1
    uncovered = (good[: SAMPLE_FLOOR - n_bad]
                 + [outcome(5 * M, 10 * M, 2 * H, 40 * M)] * n_bad)
    assert not summarize(uncovered)[0].passed


def test_queue_error_only_over_offers_with_a_snapshot() -> None:
    rows = ([outcome(30 * M, 10 * M, 2 * H, 40 * M)] * SAMPLE_FLOOR
            + [outcome(30 * M, 10 * M, 2 * H, None, has_queue=False)] * SAMPLE_FLOOR)
    (bucket,) = summarize(rows)
    assert bucket.queue_n == SAMPLE_FLOOR
    assert bucket.queue_abs_error_median_ms == 10 * M


def test_below_floor_releases_on_width_only() -> None:
    narrow = [outcome(5 * M, 10 * M, 2 * H, period=7)]  # uncovered, but narrow bounds
    wide = [outcome(30 * M, 10 * M, 5 * H, period=7)]
    assert summarize(narrow)[0].passed and not summarize(narrow)[0].floor_met
    assert not summarize(wide)[0].passed


def test_render_lists_verdict_and_every_offer() -> None:
    rows = [outcome(30 * M, 10 * M, 2 * H, 40 * M)]
    text = render(summarize(rows), rows)
    assert "PASS (width only)" in text
    assert text.count("| 1 | 2026-01-10 00:00 |") == 1
