"""Replay validation against our own offers: metrics, own-trade handling, verdicts."""
from __future__ import annotations

import gzip
from dataclasses import replace
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from scripts.report_replay_validation import (
    COVERAGE_MIN,
    COVERAGE_TOLERANCE_MS,
    DEFAULT_MAX_WAIT_MS,
    SAMPLE_FLOOR,
    BookSides,
    ExcludingTape,
    HistoryRow,
    OfferOutcome,
    OwnOffer,
    book_from_payload,
    bound_width_ms,
    build_offers,
    crossing_bids,
    evaluate,
    history_facts,
    is_covered,
    nearest_snapshot,
    offer_wait_ms,
    queue_error_ms,
    read_history,
    render,
    same_rate_queue,
    summarize,
    tape_has_day,
)

DAY = date(2026, 1, 10)
T0 = 1_768_003_200_000  # 2026-01-10T00:00:00Z
M = 60_000
H = 60 * M
W = DEFAULT_MAX_WAIT_MS
RATE = Decimal("0.0002")


def write_day(root: Path, symbol: str, day: date, rows: list[tuple]) -> None:
    folder = root / symbol
    folder.mkdir(parents=True, exist_ok=True)
    lines = ["id,mts,amount,rate,period"] + [",".join(str(v) for v in r) for r in rows]
    (folder / f"{day.isoformat()}.csv.gz").write_bytes(
        gzip.compress(("\n".join(lines) + "\n").encode(), mtime=0))


def own(first_fill: int | None = T0 + 30 * M, amount: str = "100", **kw: object) -> OwnOffer:
    return replace(OwnOffer(venue_offer_id="1", symbol="fUST", rate=RATE, period_days=2,
                            placed_mts=T0, amount=Decimal(amount), first_fill_mts=first_fill),
                   **kw)


def book(ts: int, asks: tuple = (), bids: tuple = ()) -> BookSides:
    return BookSides(ts, asks, bids)


def outcome(actual: int | None, opt: int | None, cons: int | None, queue: int | None = None,
            *, has_queue: bool = True, period: int = 2, wait: int = W) -> OfferOutcome:
    return OfferOutcome(offer=replace(own(), period_days=period), actual_ms=actual,
                        optimistic_ms=opt, conservative_ms=cons, queue_ms=queue,
                        has_queue_estimate=has_queue, wait_ms=wait)


@pytest.fixture
def tape_root(tmp_path: Path) -> Path:
    write_day(tmp_path, "fUST", DAY, [
        (100, T0 + 10 * M, -100.0, 0.0002, 2),   # same rate: optimistic and queue only
        (101, T0 + 30 * M, -100.0, 0.0002, 2),   # own trade at our rate
        (102, T0 + 2 * H, -100.0, 0.0003, 2),    # through our rate: conservative
    ])
    write_day(tmp_path, "fUST", date(2026, 1, 11), [])
    return tmp_path


def run(tape_root: Path, offer: OwnOffer, snaps: list[BookSides], own_ids: list[int]
        ) -> OfferOutcome:
    tape = ExcludingTape(tape_root, "fUST", own_ids)
    (result,) = evaluate([offer], tape_for=lambda _s: tape, snapshots={"fUST": snaps})
    return result


def test_trades_of_other_own_offers_leave_the_tape(tape_root: Path) -> None:
    snap = book(T0 - M, asks=((RATE, 2, Decimal("150")),))
    kept = run(tape_root, own(), [snap], own_ids=[])
    removed = run(tape_root, own(), [snap], own_ids=[101])
    # Queue 150 ahead: trade 100 leaves 50 of it, trade 101 would clear it at 30m.
    assert kept.queue_ms == 30 * M
    assert removed.queue_ms == 2 * H
    assert removed.optimistic_ms == 10 * M and removed.conservative_ms == 2 * H


def test_the_replayed_offers_own_trades_stay_as_demand(tape_root: Path) -> None:
    snap = book(T0 - M, asks=((RATE, 2, Decimal("150")),))
    result = run(tape_root, own(own_trade_ids=(101,)), [snap], own_ids=[101])
    assert result.queue_ms == 30 * M
    assert result.conservative_ms == 2 * H  # same-rate own fill never reaches it


def test_crossed_bids_fill_at_placement(tape_root: Path) -> None:
    snap = book(T0 - M, bids=((Decimal("0.00021"), 2, Decimal("40")),))
    result = run(tape_root, own(), [snap], own_ids=[])
    assert result.conservative_ms == 0 and result.optimistic_ms == 0


@pytest.mark.parametrize(("bids", "ts", "expected"), [
    (((Decimal("0.00021"), 2, Decimal("40")), (Decimal("0.0002"), 2, Decimal("5"))), T0 - M, "45"),
    (((Decimal("0.00019"), 2, Decimal("40")),), T0 - M, "0"),   # below our rate
    (((Decimal("0.00021"), 7, Decimal("40")),), T0 - M, "0"),   # bid longer than our offer
    (((Decimal("0.00021"), 2, Decimal("40")),), T0 + M, "0"),   # snapshot after placement
])
def test_crossing_bids(bids: tuple, ts: int, expected: str) -> None:
    assert crossing_bids(book(ts, bids=bids), own()) == Decimal(expected)


def test_cancelled_offer_is_replayed_only_until_its_cancel(tape_root: Path) -> None:
    offer = own(first_fill=None, cancel_mts=T0 + H)
    assert offer_wait_ms(offer, W) == H
    assert offer_wait_ms(own(cancel_mts=T0 + H), W) == W  # filled: cancel does not cut
    result = run(tape_root, offer, [], own_ids=[])
    assert result.wait_ms == H
    assert result.conservative_ms is None and is_covered(result)


def test_offers_with_unknown_end_or_past_the_tape_are_left_out(tape_root: Path) -> None:
    tape = ExcludingTape(tape_root, "fUST", [])
    unknown = own(first_fill=None, end_known=False)
    assert evaluate([unknown], tape_for=lambda _s: tape, snapshots={}) == []
    assert evaluate([own()], tape_for=lambda _s: tape, snapshots={},
                    tape_covers=lambda _end: False) == []
    assert tape_has_day(tape_root, [own()], T0 + W)
    assert not tape_has_day(tape_root, [own()], T0 + 2 * W)


def test_fill_stamped_in_the_acks_second_counts_as_immediate(tape_root: Path) -> None:
    assert run(tape_root, own(first_fill=T0 - 999), [], own_ids=[]).actual_ms == 0
    assert run(tape_root, own(first_fill=T0 - 1000), [], own_ids=[]).actual_ms is None


def test_actual_fill_after_the_wait_counts_as_no_fill(tape_root: Path) -> None:
    result = run(tape_root, own(first_fill=T0 + W + 1), [], own_ids=[])
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
    (0, COVERAGE_TOLERANCE_MS, 2 * H, True),           # within the tape's 1 s resolution
    (0, COVERAGE_TOLERANCE_MS + 1, 2 * H, False),
    (COVERAGE_TOLERANCE_MS, 0, 0, True),
    (COVERAGE_TOLERANCE_MS + 1, 0, 0, False),
])
def test_coverage(actual: int | None, opt: int | None, cons: int | None, covered: bool) -> None:
    assert is_covered(outcome(actual, opt, cons)) is covered


def test_width_and_error_pin_missing_fills_at_the_offers_wait() -> None:
    assert bound_width_ms(outcome(None, 10 * M, None)) == W - 10 * M
    assert bound_width_ms(outcome(None, 10 * M, None, wait=H)) == H - 10 * M
    assert queue_error_ms(outcome(30 * M, 0, 0, None)) == W - 30 * M
    assert queue_error_ms(outcome(30 * M, 0, 0, 10 * M)) == -20 * M


def test_same_rate_queue_counts_only_our_level_and_drops_ourselves_after_placement() -> None:
    asks = ((Decimal("0.0001"), 2, Decimal("999")), (RATE, 2, Decimal("150")),
            (RATE, 7, Decimal("70")))
    assert same_rate_queue(book(T0 - M, asks), own()) == Decimal("150")
    assert same_rate_queue(book(T0 + M, asks), own()) == Decimal("50")
    assert same_rate_queue(book(T0 + M, asks), own(amount="500")) == 0


def test_same_rate_queue_tolerates_float_rate_noise() -> None:
    snap = book(T0 - M, asks=((Decimal("0.00020000000000000001"), 2, Decimal("7")),))
    assert same_rate_queue(snap, own()) == Decimal("7")


def test_book_from_payload_reads_both_sides() -> None:
    parsed = book_from_payload(T0, {"asks": [[0.0002, 2, 3, 150.5]], "bids": [[0.0003, 7, 1, 9]]})
    assert parsed.asks == ((Decimal("0.0002"), 2, Decimal("150.5")),)
    assert parsed.bids == ((Decimal("0.0003"), 7, Decimal("9")),)


def test_nearest_snapshot_prefers_before_and_respects_tolerance() -> None:
    snaps = [book(T0 - 6 * M), book(T0 - 4 * M), book(T0 + M)]
    picked, only_after = nearest_snapshot(snaps, T0), nearest_snapshot(snaps[2:], T0)
    assert picked is not None and picked.captured_at_ms == T0 - 4 * M
    assert only_after is not None and only_after.captured_at_ms == T0 + M
    assert nearest_snapshot(snaps[:1], T0) is None


def test_history_facts_first_fill_and_clean_cancel(tmp_path: Path) -> None:
    csv_path = tmp_path / "h.csv"
    csv_path.write_text(
        "venue_offer_id,amount_original,amount_remaining,terminal_kind,mts_updated\n"
        "a,100,100,canceled,500\n"          # cancelled unfilled
        "b,100,60,canceled,700\n"           # partly filled, then cancelled
        "b,100,60,executed,650\n"
        "c,100,0,executed,300\n"
        "c,100,0,executed,200\n"
        "d,,5,canceled,\n"
        "e,100,60,canceled,400\n"          # partial fill seen only through the remainder
        "f,100,100,canceled,900\n"         # the last cancel observation wins
        "f,100,100,canceled,800\n", encoding="utf-8")
    facts = history_facts(read_history(csv_path))
    assert facts["a"].first_fill_mts is None and facts["a"].cancel_mts == 500
    assert facts["b"].first_fill_mts == 650
    assert facts["c"].first_fill_mts == 200 and facts["c"].cancel_mts is None
    assert "d" in facts and facts["d"].first_fill_mts is None
    assert facts["e"].first_fill_mts == 400
    assert facts["f"].cancel_mts == 900


def test_build_offers_takes_the_earliest_fill_from_trades_or_history() -> None:
    rows = [("1", "fUST", T0, RATE, 2, Decimal(100)), ("2", "fUST", T0, RATE, 2, Decimal(100)),
            ("3", "fUST", T0, RATE, 2, Decimal(100)), ("4", "fUST", T0, RATE, 2, Decimal(100))]
    trades = [(1, 900, T0 + 50), (1, 901, T0 + 40), (4, 902, T0 + 9)]
    history = history_facts([
        HistoryRow("1", Decimal(100), Decimal(0), "executed", T0 + 45),
        HistoryRow("4", Decimal(100), Decimal(0), "executed", T0 + 3),
        HistoryRow("2", Decimal(100), Decimal(0), "executed", T0 + 7),  # missing from trades
    ])
    one, two, three, four = build_offers(rows, trades, history)
    assert four.first_fill_mts == T0 + 3  # history earlier than the trade
    assert one.first_fill_mts == T0 + 40 and one.own_trade_ids == (900, 901)
    assert two.first_fill_mts == T0 + 7 and two.end_known
    assert three.first_fill_mts is None and not three.end_known


def test_coverage_denominator_is_every_offer_not_only_filled_ones() -> None:
    (bucket,) = summarize([outcome(30 * M, 10 * M, 2 * H), outcome(None, 10 * M, 2 * H)])
    assert bucket.offers == 2 and bucket.filled == 1
    assert bucket.coverage == 0.5


def test_floor_met_needs_coverage_and_queue_error() -> None:
    good = [outcome(30 * M, 10 * M, 2 * H, 40 * M)] * SAMPLE_FLOOR
    assert summarize(good)[0].passed
    slow_queue = [outcome(30 * M, 10 * M, 2 * H, 30 * M + 61 * M)] * SAMPLE_FLOOR
    assert not summarize(slow_queue)[0].passed
    n_bad = SAMPLE_FLOOR - int(SAMPLE_FLOOR * COVERAGE_MIN) + 1
    uncovered = good[: SAMPLE_FLOOR - n_bad] + [outcome(5 * M, 10 * M, 2 * H, 40 * M)] * n_bad
    assert not summarize(uncovered)[0].passed


def test_queue_error_only_over_offers_with_a_snapshot() -> None:
    rows = ([outcome(30 * M, 10 * M, 2 * H, 40 * M)] * SAMPLE_FLOOR
            + [outcome(30 * M, 10 * M, 2 * H, None, has_queue=False)] * SAMPLE_FLOOR)
    (bucket,) = summarize(rows)
    assert bucket.queue_n == SAMPLE_FLOOR
    assert bucket.queue_abs_error_median_ms == 10 * M


def test_below_floor_releases_on_width_only() -> None:
    narrow = [outcome(5 * M, 10 * M, 2 * H, period=7)]
    wide = [outcome(30 * M, 10 * M, 5 * H, period=7)]
    assert summarize(narrow)[0].passed and not summarize(narrow)[0].floor_met
    assert not summarize(wide)[0].passed


def test_render_lists_verdict_and_every_offer() -> None:
    rows = [outcome(30 * M, 10 * M, 2 * H, 40 * M)]
    text = render(summarize(rows), rows)
    assert "PASS (width only)" in text
    assert text.count("| 1 | 2026-01-10 00:00 |") == 1
