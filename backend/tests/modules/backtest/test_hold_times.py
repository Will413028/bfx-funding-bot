"""How long borrowers actually held our credits, against the two replay repayment scenarios."""
from __future__ import annotations

from decimal import Decimal

import pytest

from bfx_funding_bot.external.bitfinex.funding_candle_row import FundingCandle
from bfx_funding_bot.modules.backtest.hold_times import (
    EndedCredit,
    loans_that_became_credits,
    measure_holds,
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
    # A 30-day credit: the series ends 5 days in without the rule firing, so the other
    # 25 days of candles that could still have fired it are missing.
    long_credit = _credit(2, opened_h=2, held_h=3, period_days=30)
    assert measure_holds([long_credit], _reference({}), MARGIN)[0].predicted_ms is None
    # Had the rule fired inside the loaded range, the prediction stands.
    assert measure_holds([long_credit], _reference(low), MARGIN)[0].predicted_ms == H


def test_a_loan_that_ends_as_a_same_shaped_credit_opens_is_flagged() -> None:
    loan = EndedCredit(credit_id=1, kind="loan", symbol="fUST", rate=RATE, period_days=2,
                       amount=Decimal("100"), opened_mts=T0, closed_mts=T0 + 2 * H)
    used = EndedCredit(credit_id=2, kind="credit", symbol="fUST", rate=RATE, period_days=2,
                       amount=Decimal("100"), opened_mts=T0 + 2 * H + 5_000, closed_mts=T0 + 48 * H)
    other = EndedCredit(credit_id=3, kind="credit", symbol="fUST", rate=RATE, period_days=2,
                        amount=Decimal("250"), opened_mts=T0 + 2 * H, closed_mts=T0 + 48 * H)
    assert loans_that_became_credits([loan, used, other]) == 1
    assert loans_that_became_credits([loan, other]) == 0
