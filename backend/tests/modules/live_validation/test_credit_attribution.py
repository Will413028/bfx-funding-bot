"""Per-cell interest from venue credits, and its weekly reconciliation.

Credits, trades and payouts are the live account's (read 2026-09-27):
- 466642176 / 466642177: two identical credits, 150.76884612 fUST at
  0.00019999/day for 2 days, created 2026-09-25 15:30:46Z and repaid at
  15:44:48Z (842 s). Trade 432914136 filled our offer 5123273052.
- 466451710: expired credit at 0.0001482, opened 09-22 18:08:30Z, last payout
  09-24 18:08:31Z (its expiry).
- Daily payout 0.0518895 on 2 x 150.77 at 0.00020504/day (net ratio 0.84).
"""
from decimal import Decimal

from bfx_funding_bot.external.bitfinex.auth_rest import InterestPayment
from bfx_funding_bot.modules.live_validation.credit_attribution import (
    PAYOUT_LAG_MS,
    PAYOUT_SETTLE_MS,
    UNATTRIBUTED,
    CreditLifetime,
    TradeRecord,
    assign_cells,
    reconcile_week,
    weekly_totals,
)
from bfx_funding_bot.modules.live_validation.interest_ledger import MS_PER_DAY
from bfx_funding_bot.modules.live_validation.weekly_attribution import WEEK_MS

AMOUNT = Decimal("150.76884612")
RATE = Decimal("0.00019999")
CREATED = 1790350246000        # 2026-09-25 15:30:46Z
REPAID = 1790351088000         # 2026-09-25 15:44:48Z
MON = 1789948800000            # 2026-09-21, the week both credits fall in
DAY = MS_PER_DAY


def _credit(credit_id: str, *, closed: int | None = REPAID, opened: int = CREATED,
            amount: Decimal = AMOUNT, rate: Decimal = RATE, period: int = 2) -> CreditLifetime:
    return CreditLifetime(credit_id=credit_id, symbol="fUST", amount=amount, rate=rate,
                          period_days=period, mts_create=opened, opened_ms=opened,
                          closed_ms=closed)


def _trade(trade_id: int, offer_id: str) -> TradeRecord:
    return TradeRecord(trade_id=trade_id, symbol="fUST", mts_create=CREATED,
                       offer_id=offer_id, amount=AMOUNT, rate=RATE, period_days=2)


def test_early_repaid_credit_earns_its_fourteen_minutes() -> None:
    """Pre-fix CREDIT_CLOSED put the close at MTS_UPDATE == MTS_CREATE (0 days)
    and the fill path assumed 2 days; the venue says 842 seconds."""
    credit = _credit("466642176")
    cells = assign_cells([credit], [_trade(432914136, "5123273052")], {"5123273052": "fUST_p2"})
    assert cells.cell_by_credit == {"466642176": "fUST_p2"}
    totals = weekly_totals([credit], cells.cell_by_credit, now_ms=REPAID + DAY)
    week = totals["fUST_p2"][MON]
    held_days = Decimal(842_000) / Decimal(DAY)
    assert week.n_credits == 1
    assert week.gross_interest == AMOUNT * RATE * held_days
    assert Decimal("0.00029") < week.gross_interest < Decimal("0.00030")


def test_expired_credit_is_split_across_the_weeks_it_spans() -> None:
    opened, last_payout = 1790100510000, 1790273311000   # Tue 09-22 .. Thu 09-24
    credit = _credit("466451710", opened=opened, closed=last_payout,
                     amount=Decimal("100"), rate=Decimal("0.0001482"))
    totals = weekly_totals([credit], {}, now_ms=last_payout + DAY)
    assert list(totals) == [UNATTRIBUTED]
    week = totals[UNATTRIBUTED][MON]
    assert week.capital_days == Decimal("100") * Decimal(172_801_000) / Decimal(DAY)

    spanning = _credit("1", opened=MON + WEEK_MS - DAY, closed=MON + WEEK_MS + DAY,
                       amount=Decimal("100"), rate=Decimal("0.0002"))
    split = weekly_totals([spanning], {}, now_ms=MON + 3 * WEEK_MS)[UNATTRIBUTED]
    assert split[MON].gross_interest == split[MON + WEEK_MS].gross_interest == Decimal("0.02")
    assert (split[MON].n_credits, split[MON + WEEK_MS].n_credits) == (1, 0)


def test_open_credit_accrues_until_now() -> None:
    credit = _credit("9", closed=None)
    now = CREATED + DAY // 2
    week = weekly_totals([credit], {}, now_ms=now)[UNATTRIBUTED][MON]
    assert week.gross_interest == AMOUNT * RATE * Decimal("0.5")


def test_unmatched_and_foreign_credits_are_unattributed() -> None:
    lone = _credit("1", amount=Decimal("99"))
    foreign = _credit("2")
    cells = assign_cells([lone, foreign], [_trade(10, "not-ours")], {"5123273052": "fUST_p2"})
    assert cells.cell_by_credit == {"1": UNATTRIBUTED, "2": UNATTRIBUTED}
    assert cells.without_trade == {"1"} and cells.foreign_offer == {"2"}


def test_identical_credits_held_alike_pair_deterministically_without_ambiguity() -> None:
    """466642176/466642177 share amount, rate, period and creation instant, and
    were repaid together: whichever trade each is paired with, interest is equal."""
    credits = [_credit("466642177"), _credit("466642176")]
    trades = [_trade(432914137, "b"), _trade(432914136, "a")]
    cells = assign_cells(credits, trades, {"a": "fUST_p2", "b": "fUST_a30"})
    # lowest credit id <-> lowest trade id
    assert cells.cell_by_credit == {"466642176": "fUST_p2", "466642177": "fUST_a30"}
    assert not cells.ambiguous


def test_identical_keys_held_differently_across_cells_are_flagged() -> None:
    credits = [_credit("466642176"), _credit("466642177", closed=CREATED + DAY)]
    trades = [_trade(432914136, "a"), _trade(432914137, "b")]
    cells = assign_cells(credits, trades, {"a": "fUST_p2", "b": "fUST_a30"})
    assert cells.ambiguous == {"466642176", "466642177"}
    # still attributed, deterministically
    assert cells.cell_by_credit["466642176"] == "fUST_p2"
    same_cell = assign_cells(credits, trades, {"a": "fUST_p2", "b": "fUST_p2"})
    assert not same_cell.ambiguous


def test_loan_ids_sort_after_credit_ids() -> None:
    credits = [_credit("loan:5"), _credit("7")]
    cells = assign_cells(credits, [_trade(1, "a")], {"a": "fUST_p2"})
    assert cells.cell_by_credit == {"7": "fUST_p2", "loan:5": UNATTRIBUTED}


# ---- reconciliation --------------------------------------------------------

LIVE_DAILY_PAYOUT = Decimal("0.0518895")
LIVE_RATE = Decimal("0.00020504")


def _payout(mts: int, amount: Decimal = LIVE_DAILY_PAYOUT) -> InterestPayment:
    return InterestPayment(mts, "UST", "funding", mts, amount, Decimal("395"), "Margin Funding Payment")


def _week_of_payouts(week: int) -> list[InterestPayment]:
    """Paid at ~01:30Z each day for the day before: Tue..next Mon."""
    return [_payout(week + (i + 1) * DAY + 5_400_000) for i in range(7)]


def _two_credits_lent_all_week(week: int) -> list[CreditLifetime]:
    return [_credit(str(i), opened=week - DAY, closed=week + WEEK_MS + DAY, rate=LIVE_RATE)
            for i in (1, 2)]


def test_reconciles_within_tolerance_at_the_observed_fee_ratio() -> None:
    now = MON + WEEK_MS + PAYOUT_LAG_MS + PAYOUT_SETTLE_MS
    rec = reconcile_week(_two_credits_lent_all_week(MON), _week_of_payouts(MON),
                         currency="UST", week_start_ms=MON, now_ms=now)
    assert rec.payouts == 7
    assert rec.ledger_net == 7 * LIVE_DAILY_PAYOUT
    assert rec.credit_gross == 2 * AMOUNT * LIVE_RATE * 7
    assert rec.credit_net == rec.credit_gross * Decimal("0.85")
    # venue kept ~16% rather than 15%: +1.3%, inside the 5% band
    assert rec.diff_pct is not None and Decimal("1.0") < rec.diff_pct < Decimal("1.5")
    assert rec.complete and not rec.flagged


def test_payout_for_the_last_sunday_lands_next_tuesday_window() -> None:
    """A payout early on the Monday after is for Sunday and belongs to this
    week; the one on this week's Monday is for the previous Sunday."""
    payouts = [_payout(MON + 5_400_000, Decimal("9")), *_week_of_payouts(MON)]
    rec = reconcile_week([], payouts, currency="UST", week_start_ms=MON,
                         now_ms=MON + 3 * WEEK_MS)
    assert rec.payouts == 7


def test_missing_credit_is_flagged() -> None:
    now = MON + 2 * WEEK_MS
    rec = reconcile_week(_two_credits_lent_all_week(MON)[:1], _week_of_payouts(MON),
                         currency="UST", week_start_ms=MON, now_ms=now)
    assert rec.flagged and rec.diff < 0


def test_small_absolute_gap_is_not_flagged() -> None:
    tiny = [_payout(MON + DAY + 5_400_000, Decimal("0.004"))]
    rec = reconcile_week([], tiny, currency="UST", week_start_ms=MON, now_ms=MON + 2 * WEEK_MS)
    assert rec.diff == Decimal("-0.004") and not rec.flagged


def test_week_is_incomplete_until_its_shifted_payout_window_settles() -> None:
    before = MON + WEEK_MS + PAYOUT_LAG_MS + PAYOUT_SETTLE_MS - 1
    rec = reconcile_week(_two_credits_lent_all_week(MON), [], currency="UST",
                         week_start_ms=MON, now_ms=before)
    assert not rec.complete and not rec.flagged and rec.diff_pct is None
