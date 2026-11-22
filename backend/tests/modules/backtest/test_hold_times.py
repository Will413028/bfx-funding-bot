"""How long borrowers actually held our credits, against the two replay repayment scenarios."""
from __future__ import annotations

from decimal import Decimal

import pytest

from bfx_funding_bot.external.bitfinex.funding_candle_row import FundingCandle
from bfx_funding_bot.modules.backtest.hold_times import (
    Bootstrap,
    EndedCredit,
    group_holds,
    loans_that_became_credits,
    measure_holds,
    rate_gap,
    repaid_after_drop,
    summarize_holds,
)

H = 3_600_000
T0 = 1_768_003_200_000  # 2026-01-10T00:00:00Z
RATE = Decimal("0.0002")
MARGIN = Decimal("0.2")


def _reference(closes: dict[int, str], hours: int = 120, default: str = "0.0002") -> list[FundingCandle]:
    return [FundingCandle(symbol="fUST", timeframe="1h", period_agg="p2", mts=T0 + i * H,
                          close=Decimal(closes.get(i, default))) for i in range(hours)]


def _credit(credit_id: int, *, opened_h: float, held_h: float, amount: str = "100",
            rate: Decimal = RATE, period_days: int = 2) -> EndedCredit:
    opened = T0 + int(opened_h * H)
    return EndedCredit(credit_id=credit_id, kind="credit", symbol="fUST", rate=rate,
                       period_days=period_days, amount=Decimal(amount), opened_mts=opened,
                       closed_mts=opened + int(held_h * H))


def test_a_credit_held_to_term_is_not_early_and_counts_in_full() -> None:
    rows = measure_holds([_credit(1, opened_h=1, held_h=48)], _reference({}), MARGIN)
    assert rows[0].early is False
    assert rows[0].held_ms == rows[0].term_ms == 48 * H
    assert rows[0].predicted_ms == 48 * H
    summary = summarize_holds(rows, MARGIN)
    assert (summary.n, summary.n_early) == (1, 0)
    assert summary.held_fraction == 1
    assert summary.position is None  # the two scenarios agree: nothing to place


def test_a_close_within_the_tolerance_of_term_end_is_not_early() -> None:
    rows = measure_holds([_credit(1, opened_h=1, held_h=47.5), _credit(2, opened_h=1, held_h=46.5)],
                         _reference({}), MARGIN)
    assert [row.early for row in rows] == [False, True]


def test_early_repayment_is_measured_against_the_market_the_borrower_saw() -> None:
    # The market halves in hour 10 (known at +11h). The borrower repays at +12h.
    reference = _reference(dict.fromkeys(range(10, 120), "0.0001"))
    rows = measure_holds([_credit(1, opened_h=2, held_h=10)], reference, MARGIN)
    row = rows[0]
    assert row.early is True
    assert row.drop_at_close == Decimal("0.5")
    # The refinance rule would have ended the lock at the +11h close: 9h in.
    assert row.predicted_ms == 9 * H


def test_position_places_reality_between_the_two_scenarios() -> None:
    # The market drops at once: the refinance scenario predicts about 1h of 48h for both.
    reference = _reference(dict.fromkeys(range(0, 120), "0.0001"))
    credits = [_credit(1, opened_h=1, held_h=48), _credit(2, opened_h=1, held_h=1)]
    summary = summarize_holds(measure_holds(credits, reference, MARGIN), MARGIN)
    assert summary.n_early == 1
    assert summary.early_with_drop == 1
    predicted = Decimal(1) / Decimal(48)
    actual = (Decimal(48) + Decimal(1)) / Decimal(96)
    assert summary.predicted_fraction == predicted
    assert summary.held_fraction == actual
    assert summary.position == (actual - predicted) / (1 - predicted)
    assert Decimal("0.4") < summary.position < Decimal("0.6")


def test_held_fraction_is_weighted_by_amount_and_term() -> None:
    credits = [_credit(1, opened_h=1, held_h=48, amount="300"),
               _credit(2, opened_h=1, held_h=12, amount="100")]
    summary = summarize_holds(measure_holds(credits, _reference({}), MARGIN), MARGIN)
    assert summary.held_fraction == (Decimal(300 * 48 + 100 * 12)) / Decimal(400 * 48)


def test_a_credit_the_reference_does_not_cover_is_kept_out_of_the_comparison() -> None:
    credits = [_credit(1, opened_h=1, held_h=48), _credit(2, opened_h=500, held_h=3)]
    rows = measure_holds(credits, _reference({}), MARGIN)
    assert rows[1].predicted_ms is None and rows[1].drop_at_close is None
    summary = summarize_holds(rows, MARGIN)
    assert (summary.n, summary.n_compared) == (2, 1)


def test_a_credit_that_closes_before_it_opens_is_refused() -> None:
    with pytest.raises(ValueError, match="closes before it opens"):
        measure_holds([_credit(1, opened_h=5, held_h=-1)], _reference({}), MARGIN)


def test_a_close_after_the_term_end_counts_as_the_full_term_only() -> None:
    rows = measure_holds([_credit(1, opened_h=1, held_h=50)], _reference({}), MARGIN)
    assert rows[0].held_ms == 48 * H and rows[0].early is False
    assert summarize_holds(rows, MARGIN).held_fraction == 1


def test_the_market_at_the_close_is_the_last_finished_hour() -> None:
    # Hour 12 (still open when the credit closes at +12h30) is low; hour 11 was not.
    reference = _reference({12: "0.0001"})
    row = measure_holds([_credit(1, opened_h=2, held_h=10.5)], reference, MARGIN)[0]
    assert row.drop_at_close == 0


def test_only_a_drop_beyond_the_margin_counts_as_a_refinance_motive() -> None:
    def early_with_drop(close: str) -> int:
        reference = _reference(dict.fromkeys(range(0, 120), close))
        rows = measure_holds([_credit(1, opened_h=2, held_h=10)], reference, MARGIN)
        return summarize_holds(rows, MARGIN).early_with_drop

    assert early_with_drop("0.00019") == 0  # 5% under the rate
    assert early_with_drop("0.00016") == 0  # exactly the margin: the rule needs more
    assert early_with_drop("0.00015") == 1


def test_the_rule_is_not_run_where_it_cannot_be_judged() -> None:
    low = dict.fromkeys(range(0, 120), "0.0001")
    # An FRR-marked credit (rate 0) has no trigger a close could be under.
    frr = measure_holds([_credit(1, opened_h=2, held_h=5, rate=Decimal(0))], _reference(low), MARGIN)
    assert frr[0].predicted_ms is None
    # A 30-day credit on a 5-day series: 25 days of candles that could fire the rule are
    # missing, so it is left out whether or not the rule fired on the days that exist.
    long_credit = _credit(2, opened_h=2, held_h=3, period_days=30)
    assert measure_holds([long_credit], _reference({}), MARGIN)[0].predicted_ms is None
    assert measure_holds([long_credit], _reference(low), MARGIN)[0].predicted_ms is None


def test_a_hole_in_the_series_keeps_the_credit_out_of_the_comparison() -> None:
    full = _reference({})
    credit = _credit(1, opened_h=1, held_h=1)
    assert measure_holds([credit], full, MARGIN)[0].predicted_ms == 48 * H
    # Hours 3..59 are missing: most of the term has no candle that could fire the rule.
    holed = [c for c in full if not T0 + 3 * H <= c.mts < T0 + 60 * H]
    assert measure_holds([credit], holed, MARGIN)[0].predicted_ms is None
    # A few missing hours are tolerated (the series drops isolated slots).
    nearly = [c for c in full if c.mts not in (T0 + 7 * H, T0 + 20 * H)]
    assert measure_holds([credit], nearly, MARGIN)[0].predicted_ms == 48 * H


def test_a_loan_that_opened_with_a_same_shaped_credit_is_flagged() -> None:
    # The 2026-11-21 shape: the venue lists the lend as a loan, then as a credit that keeps
    # the lend's opening time. The loan row ends at the conversion, long before the credit.
    loan = EndedCredit(credit_id=1, kind="loan", symbol="fUST", rate=RATE, period_days=2,
                       amount=Decimal("100"), opened_mts=T0, closed_mts=T0 + 2 * H)
    used = EndedCredit(credit_id=2, kind="credit", symbol="fUST", rate=RATE, period_days=2,
                       amount=Decimal("100"), opened_mts=T0 + 5_000, closed_mts=T0 + 48 * H)
    other = EndedCredit(credit_id=3, kind="credit", symbol="fUST", rate=RATE, period_days=2,
                        amount=Decimal("250"), opened_mts=T0, closed_mts=T0 + 48 * H)
    assert loans_that_became_credits([loan, used, other]) == 1
    assert loans_that_became_credits([loan, other]) == 0
    # A credit that only opens when the loan ends is a new lend, not the same one.
    later = EndedCredit(credit_id=5, kind="credit", symbol="fUST", rate=RATE, period_days=2,
                        amount=Decimal("100"), opened_mts=T0 + 2 * H, closed_mts=T0 + 50 * H)
    assert loans_that_became_credits([loan, later]) == 0
    # Two loans cannot both have become the one credit.
    twin = EndedCredit(credit_id=4, kind="loan", symbol="fUST", rate=RATE, period_days=2,
                       amount=Decimal("100"), opened_mts=T0, closed_mts=T0 + 2 * H)
    assert loans_that_became_credits([loan, twin, used]) == 1
    # A loan that outlasts the credit did not turn into it.
    short = EndedCredit(credit_id=6, kind="credit", symbol="fUST", rate=RATE, period_days=2,
                        amount=Decimal("100"), opened_mts=T0, closed_mts=T0 + H)
    assert loans_that_became_credits([loan, short]) == 0


def test_terms_are_never_pooled() -> None:
    credits = [_credit(1, opened_h=1, held_h=48), _credit(2, opened_h=1, held_h=24),
               _credit(3, opened_h=1, held_h=84, period_days=7)]
    groups = group_holds(measure_holds(credits, _reference({}, hours=200), MARGIN))
    assert {key: [row.credit.credit_id for row in rows] for key, rows in groups.items()} == {
        ("credit", 2): [1, 2], ("credit", 7): [3]}
    # The median is of each credit's own share of its term.
    assert summarize_holds(groups["credit", 2], MARGIN).median_held_share == Decimal("0.75")
    assert summarize_holds(groups["credit", 7], MARGIN).median_held_share == Decimal("0.5")
    assert summarize_holds([], MARGIN).median_held_share is None


def test_repaid_after_a_drop_counts_only_credits_that_saw_the_drop_first() -> None:
    # The market halves in hour 10: the close under the trigger is known at +11h.
    reference = _reference(dict.fromkeys(range(10, 120), "0.0001"))
    followed = _credit(1, opened_h=2, held_h=10)  # repaid 1h after the drop
    to_term = _credit(2, opened_h=2, held_h=48)  # saw the drop, ran to its term
    left_first = _credit(3, opened_h=2, held_h=5)  # repaid before the drop
    much_later = _credit(4, opened_h=2, held_h=40)  # early, but 31h after the drop
    loan = EndedCredit(credit_id=5, kind="loan", symbol="fUST", rate=RATE, period_days=2,
                       amount=Decimal("100"), opened_mts=T0 + 2 * H, closed_mts=T0 + 12 * H)
    rows = measure_holds([followed, to_term, left_first, much_later, loan], reference, MARGIN)
    follow = repaid_after_drop(rows)
    assert (follow.n_dropped, follow.n_repaid_after) == (3, 1)
    assert follow.share == Decimal(1) / 3
    # A drop 9h before the term runs out: the credit ending on schedule did not follow it.
    late_drop = _reference(dict.fromkeys(range(40, 120), "0.0001"))
    matured = repaid_after_drop(measure_holds([to_term], late_drop, MARGIN))
    assert (matured.n_dropped, matured.n_repaid_after, matured.share) == (1, 0, Decimal(0))
    # A 7-day credit repaid on its first day, reported with three days of candles: the
    # drop is judged on the hours it was out, not on a term the candles do not reach.
    long_early = _credit(6, opened_h=2, held_h=12, period_days=7)
    short_series = _reference(dict.fromkeys(range(10, 72), "0.0001"), hours=72)
    row = measure_holds([long_early], short_series, MARGIN)[0]
    assert row.predicted_ms is None and row.drop_ms == 9 * H
    assert repaid_after_drop([row]).share == 1
    # Hours the candles do not cover cannot show a drop.
    assert measure_holds([long_early], _reference({}, hours=5), MARGIN)[0].drop_ms is None
    # No drop at all: nothing to divide by, not a zero.
    flat = repaid_after_drop(measure_holds([followed, to_term], _reference({}), MARGIN))
    assert (flat.n_dropped, flat.share) == (0, None)


def _long_and_base() -> list[EndedCredit]:
    return [
        # 7-day credit over two 2-day credits: 100 at 0.0002 then 300 at 0.0004, 48h each.
        _credit(1, opened_h=0, held_h=100, rate=Decimal("0.0005"), period_days=7),
        _credit(11, opened_h=0, held_h=48, rate=Decimal("0.0002")),
        _credit(12, opened_h=48, held_h=48, amount="300", rate=Decimal("0.0004")),
        # 7-day credit a 2-day one only partly overlaps (38h of its 48h).
        _credit(2, opened_h=200, held_h=50, amount="300", rate=Decimal("0.0003"), period_days=7),
        _credit(13, opened_h=190, held_h=48, rate=Decimal("0.0002")),
        # An FRR-marked 2-day credit over the first long one: not a comparison rate.
        _credit(14, opened_h=0, held_h=48, amount="1000", rate=Decimal(0)),
        # A 7-day credit lent with no 2-day credit alongside.
        _credit(3, opened_h=500, held_h=30, rate=Decimal("0.0009"), period_days=7),
    ]


def test_the_rate_gap_is_weighted_by_amount_and_time() -> None:
    rows = measure_holds(_long_and_base(), [], MARGIN)
    gap = rate_gap(rows, period_days=7, min_sample=3, bootstrap=Bootstrap(resamples=200))
    assert (gap.n_long, gap.n_included, gap.n_excluded) == (3, 2, 1)
    # Credit 1: 0.0005 - (100*0.0002 + 300*0.0004)/400 = 0.00015, weight 100 x 100h.
    # Credit 2: 0.0003 - 0.0002 = 0.0001, weight 300 x 50h.
    assert gap.gap == Decimal("0.00012")
    assert gap.ci_low is not None and gap.ci_high is not None
    assert Decimal("0.0001") <= gap.ci_low <= gap.gap <= gap.ci_high <= Decimal("0.00015")
    # Same seed, same interval.
    again = rate_gap(rows, period_days=7, min_sample=3, bootstrap=Bootstrap(resamples=200))
    assert (again.ci_low, again.ci_high) == (gap.ci_low, gap.ci_high)


def test_the_rate_gap_is_not_reported_under_the_sample_threshold() -> None:
    rows = measure_holds(_long_and_base(), [], MARGIN)
    gap = rate_gap(rows, period_days=7)
    assert gap.min_sample == 20
    assert (gap.n_long, gap.n_included) == (3, 2)
    assert (gap.gap, gap.ci_low, gap.ci_high) == (None, None, None)


def test_the_rate_gap_needs_an_included_credit_and_ignores_loans() -> None:
    alone = _credit(3, opened_h=500, held_h=30, rate=Decimal("0.0009"), period_days=7)
    loan = EndedCredit(credit_id=9, kind="loan", symbol="fUST", rate=RATE, period_days=7,
                       amount=Decimal("100"), opened_mts=T0, closed_mts=T0 + 10 * H)
    gap = rate_gap(measure_holds([alone, loan], [], MARGIN), period_days=7, min_sample=1)
    assert (gap.n_long, gap.n_included, gap.n_excluded) == (1, 0, 1)
    assert (gap.gap, gap.ci_low, gap.ci_high) == (None, None, None)


def test_a_long_credit_without_a_fixed_rate_is_not_scored() -> None:
    frr = _credit(4, opened_h=0, held_h=90, rate=Decimal(0), period_days=7)
    gap = rate_gap(measure_holds([*_long_and_base(), frr], [], MARGIN),
                   period_days=7, min_sample=3, bootstrap=Bootstrap(resamples=200))
    assert (gap.n_long, gap.n_included, gap.gap) == (3, 2, Decimal("0.00012"))


def test_the_rate_gap_waits_for_the_two_day_credits_alongside_to_end() -> None:
    rows = measure_holds(_long_and_base(), [], MARGIN)
    last_close = T0 + 530 * H
    early = rate_gap(rows, period_days=7, min_sample=3, bootstrap=Bootstrap(resamples=200),
                     as_of_mts=last_close + 47 * H)
    assert (early.settled, early.gap, early.ci_low) == (False, None, None)
    assert early.n_included == 2  # still counted, only not reported
    later = rate_gap(rows, period_days=7, min_sample=3, bootstrap=Bootstrap(resamples=200),
                     as_of_mts=last_close + 48 * H)
    assert (later.settled, later.gap) == (True, Decimal("0.00012"))


def test_the_long_term_must_be_longer_than_the_base() -> None:
    with pytest.raises(ValueError, match="over 2 days"):
        rate_gap([], period_days=2)
