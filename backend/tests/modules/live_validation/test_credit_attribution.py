"""Per-cell interest from venue credits, and its weekly reconciliation.

Credits, loans, trades and payouts are the live account's (read 2026-09-27):
- 466642176 / 466642177: two identical credits, 150.76884612 fUST at
  0.00019999/day for 2 days, created 2026-09-25 15:30:46Z and repaid at
  15:44:48Z (842 s). Trades 432914136 / 432914137 at the same instant;
  432914136 filled our offer 5123273052.
- 466451710: expired credit at 0.0001482, opened 09-22 18:08:30Z, last payout
  09-24 18:08:31Z (its expiry). It began as loan 61621685 (150.77638588,
  opened 18:08:30Z) and became this credit with a new id and MTS_CREATE;
  trade 432678437 is at 18:08:30Z.
- Trade 429585989 (2026-08-09 15:31:04Z, 391.4117332 at 0.00013545, 2 days)
  -> loan 60709535 (391.4117332 at 0.00014, opened 15:31:04Z) -> loans
  60709642 / 60709643 and credits 463464628 / 463464629 (161.24782943 each)
  and 463464632 (68.91607434), all at 0.00014000000000000001 and all with
  MTS_OPENING 15:31:04Z. The credits add up to the trade.
- Credit rates carry more digits than their trades': 463618273 0.0001239463 vs
  trade 429747154 0.00012395 (both 2026-08-12 09:10:47Z, 391.50517826);
  464183072 0.0001752507 vs 430314496 0.00017525; 464331043 0.0001232777 vs
  430466785 0.00012328.
- Daily payout 0.0518895 on 2 x 150.77 at 0.00020504/day (net ratio 0.84).
"""
from decimal import Decimal

import pytest

from bfx_funding_bot.external.bitfinex.auth_rest import InterestPayment
from bfx_funding_bot.modules.live_validation.credit_attribution import (
    NO_CELLS,
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
HOUR = 3_600_000


def _credit(credit_id: str, *, closed: int | None = REPAID, opened: int = CREATED,
            amount: Decimal = AMOUNT, rate: Decimal = RATE, period: int = 2,
            created: int | None = None) -> CreditLifetime:
    return CreditLifetime(credit_id=credit_id, symbol="fUST", amount=amount, rate=rate,
                          period_days=period, mts_create=opened if created is None else created,
                          opened_ms=opened, closed_ms=closed)


def _trade(trade_id: int, offer_id: str, *, at: int = CREATED, amount: Decimal = AMOUNT,
           rate: Decimal = RATE, period: int = 2) -> TradeRecord:
    return TradeRecord(trade_id=trade_id, symbol="fUST", mts_create=at,
                       offer_id=offer_id, amount=amount, rate=rate, period_days=period)


def test_early_repaid_credit_earns_its_fourteen_minutes() -> None:
    """Pre-fix CREDIT_CLOSED put the close at MTS_UPDATE == MTS_CREATE (0 days)
    and the fill path assumed 2 days; the venue says 842 seconds."""
    credit = _credit("466642176")
    cells = assign_cells([credit], [_trade(432914136, "5123273052")], {"5123273052": "fUST_p2"})
    assert cells.cell_by_credit == {"466642176": "fUST_p2"}
    totals = weekly_totals([credit], cells, now_ms=REPAID + DAY)
    week = totals["fUST_p2"][MON]
    held_days = Decimal(842_000) / Decimal(DAY)
    assert week.n_credits == 1
    assert week.gross_interest == AMOUNT * RATE * held_days
    assert Decimal("0.00029") < week.gross_interest < Decimal("0.00030")


def test_expired_credit_is_split_across_the_weeks_it_spans() -> None:
    opened, last_payout = 1790100510000, 1790273311000   # Tue 09-22 .. Thu 09-24
    credit = _credit("466451710", opened=opened, closed=last_payout,
                     amount=Decimal("100"), rate=Decimal("0.0001482"))
    totals = weekly_totals([credit], NO_CELLS, now_ms=last_payout + DAY)
    assert list(totals) == [UNATTRIBUTED]
    week = totals[UNATTRIBUTED][MON]
    assert week.capital_days == Decimal("100") * Decimal(172_801_000) / Decimal(DAY)

    spanning = _credit("1", opened=MON + WEEK_MS - DAY, closed=MON + WEEK_MS + DAY,
                       amount=Decimal("100"), rate=Decimal("0.0002"))
    split = weekly_totals([spanning], NO_CELLS, now_ms=MON + 3 * WEEK_MS)[UNATTRIBUTED]
    assert split[MON].gross_interest == split[MON + WEEK_MS].gross_interest == Decimal("0.02")
    assert (split[MON].n_credits, split[MON + WEEK_MS].n_credits) == (1, 0)


def test_open_credit_accrues_until_now() -> None:
    credit = _credit("9", closed=None)
    now = CREATED + DAY // 2
    week = weekly_totals([credit], NO_CELLS, now_ms=now)[UNATTRIBUTED][MON]
    assert week.gross_interest == AMOUNT * RATE * Decimal("0.5")


def test_unmatched_and_foreign_credits_are_unattributed() -> None:
    lone = _credit("1", amount=Decimal("99"), opened=CREATED + 1000)
    foreign = _credit("2")
    cells = assign_cells([lone, foreign], [_trade(10, "not-ours")], {"5123273052": "fUST_p2"})
    assert cells.cell_by_credit == {"1": UNATTRIBUTED, "2": UNATTRIBUTED}
    assert cells.without_trade == {"1"} and cells.foreign_offer == {"2"}


def test_identical_credits_held_alike_pair_deterministically_without_ambiguity() -> None:
    """466642176/466642177 share amount, rate, period and opening instant with
    trades 432914136/432914137, and were repaid together: whichever trade each
    is paired with, interest is equal."""
    credits = [_credit("466642177"), _credit("466642176")]
    trades = [_trade(432914137, "b"), _trade(432914136, "a")]
    cells = assign_cells(credits, trades, {"a": "fUST_p2", "b": "fUST_a30"})
    # lowest credit id <-> lowest trade id
    assert cells.cell_by_credit == {"466642176": "fUST_p2", "466642177": "fUST_a30"}
    assert not cells.ambiguous and not cells.shares
    totals = weekly_totals(credits, cells, now_ms=REPAID + DAY)
    assert totals["fUST_p2"][MON] == totals["fUST_a30"][MON]
    assert totals["fUST_p2"][MON].n_credits == 1


def test_identical_keys_held_differently_across_cells_are_flagged() -> None:
    credits = [_credit("466642176"), _credit("466642177", closed=CREATED + DAY)]
    trades = [_trade(432914136, "a"), _trade(432914137, "b")]
    cells = assign_cells(credits, trades, {"a": "fUST_p2", "b": "fUST_a30"})
    assert cells.ambiguous == {"466642176", "466642177"}
    # still attributed, deterministically
    assert cells.cell_by_credit["466642176"] == "fUST_p2"
    same_cell = assign_cells(credits, trades, {"a": "fUST_p2", "b": "fUST_p2"})
    assert not same_cell.ambiguous


# ---- the matching key: (symbol, period, MTS_OPENING) == trade MTS_CREATE ----

@pytest.mark.parametrize(("credit_id", "credit_rate", "trade_id", "trade_rate"), [
    ("463618273", Decimal("0.0001239463"), 429747154, Decimal("0.00012395")),
    ("464183072", Decimal("0.0001752507"), 430314496, Decimal("0.00017525")),
    ("464331043", Decimal("0.0001232777"), 430466785, Decimal("0.00012328")),
])
def test_credit_rate_precision_does_not_break_the_match(
        credit_id: str, credit_rate: Decimal, trade_id: int, trade_rate: Decimal) -> None:
    """The venue stores a credit's rate with more digits than its trade's; the
    old (amount, rate, ...) key left every such credit without a trade."""
    opened, amount = 1786525847000, Decimal("391.50517826")   # 2026-08-12 09:10:47Z
    credit = _credit(credit_id, opened=opened, closed=opened + 2 * DAY, amount=amount,
                     rate=credit_rate)
    trade = _trade(trade_id, "o", at=opened, amount=amount, rate=trade_rate)
    cells = assign_cells([credit], [trade], {"o": "fUST_p2"})
    assert cells.cell_by_credit == {credit_id: "fUST_p2"} and not cells.without_trade
    week = weekly_totals([credit], cells, now_ms=opened + 3 * DAY)["fUST_p2"]
    # interest at the credit's own rate: that is what the venue pays
    assert sum(w.gross_interest for w in week.values()) == amount * credit_rate * 2


AUG09 = 1786289464000                  # 2026-08-09 15:31:04Z
AUG09_TRADE = Decimal("391.4117332")
AUG09_CREDIT_RATE = Decimal("0.00014000000000000001")


def _aug09_chain() -> list[CreditLifetime]:
    """Trade 429585989's money: a loan, then three credits. Ids, amounts, rates
    and the opening are the venue's; when each record was created is not (the
    conversions are placed one hour apart), and the two intermediate loans'
    amounts are what the credits imply (the remainder still unused)."""
    closed = AUG09 + 2 * DAY
    rest1 = AUG09_TRADE - Decimal("161.24782943")            # 230.16390377
    rest2 = rest1 - Decimal("161.24782943")                  # 68.91607434
    def rec(cid: str, amount: Decimal, rate: Decimal, created: int, end: int) -> CreditLifetime:
        return _credit(cid, opened=AUG09, created=created, closed=end, amount=amount, rate=rate)
    return [
        rec("loan:60709535", AUG09_TRADE, Decimal("0.00014"), AUG09, AUG09 + HOUR),
        rec("463464628", Decimal("161.24782943"), AUG09_CREDIT_RATE, AUG09 + HOUR, closed),
        rec("loan:60709642", rest1, Decimal("0.00014"), AUG09 + HOUR, AUG09 + 2 * HOUR),
        rec("463464629", Decimal("161.24782943"), AUG09_CREDIT_RATE, AUG09 + 2 * HOUR, closed),
        rec("loan:60709643", rest2, Decimal("0.00014"), AUG09 + 2 * HOUR, AUG09 + 3 * HOUR),
        rec("463464632", Decimal("68.91607434"), AUG09_CREDIT_RATE, AUG09 + 3 * HOUR, closed),
    ]


def _credit_total(chain: list[CreditLifetime]) -> Decimal:
    return sum((c.amount for c in chain if not c.credit_id.startswith("loan:")), Decimal(0))


def test_loan_that_became_split_credits_stays_with_its_trade() -> None:
    chain = _aug09_chain()
    assert _credit_total(chain) == AUG09_TRADE
    trade = _trade(429585989, "o", at=AUG09, amount=AUG09_TRADE, rate=Decimal("0.00013545"))
    cells = assign_cells(chain, [trade], {"o": "fUST_p2"})
    assert set(cells.cell_by_credit.values()) == {"fUST_p2"}
    assert not (cells.without_trade or cells.ambiguous or cells.foreign_offer)

    totals = weekly_totals(chain, cells, now_ms=AUG09 + 3 * DAY)["fUST_p2"]
    # one fill, however many loans and credits it became
    assert sum(w.n_credits for w in totals.values()) == 1
    # the money is lent once: each record earns only from its own creation, so
    # the loan and the credits it turned into do not overlap
    assert sum(w.capital_days for w in totals.values()) == AUG09_TRADE * 2


def test_loan_converted_to_credit_with_new_id_keeps_the_trade() -> None:
    """Loan 61621685 (opened 09-22 18:08:30Z) became credit 466451710: same
    amount and opening, new id and MTS_CREATE. Trade 432678437 is at the opening."""
    opened, amount = 1790100510000, Decimal("150.77638588")
    loan = _credit("loan:61621685", opened=opened, closed=opened + HOUR, amount=amount,
                   rate=Decimal("0.0001482"))
    credit = _credit("466451710", opened=opened, created=opened + HOUR,
                     closed=1790273311000, amount=amount, rate=Decimal("0.0001482"))
    trade = _trade(432678437, "o", at=opened, amount=amount, rate=Decimal("0.0001482"))
    cells = assign_cells([loan, credit], [trade], {"o": "fUST_p2"})
    assert cells.cell_by_credit == {"loan:61621685": "fUST_p2", "466451710": "fUST_p2"}
    assert not cells.ambiguous


def test_trades_to_different_cells_at_one_instant_split_by_amount_and_flag() -> None:
    """A second fill (hypothetical, 100 at a30) at the 08-09 instant: exact sums
    first, then proportional over the trades large enough to be the source."""
    extra = _credit("loan:60709999", opened=AUG09, closed=AUG09 + 2 * DAY,
                    amount=Decimal("100"), rate=Decimal("0.00014"))
    chain = [*_aug09_chain(), extra]
    trades = [_trade(429585989, "a", at=AUG09, amount=AUG09_TRADE),
              _trade(429585990, "b", at=AUG09, amount=Decimal("100"))]
    cells = assign_cells(chain, trades, {"a": "fUST_p2", "b": "fUST_a30"})
    assert cells.ambiguous == {c.credit_id for c in chain}
    # exact: the first loan is trade a, the 100 loan is trade b, and credit
    # 463464628 + loan 60709642 add up to trade a again
    for cid in ("loan:60709535", "463464628", "loan:60709642"):
        assert cells.share_of(cid) == {"fUST_p2": 1}
    assert cells.share_of("loan:60709999") == {"fUST_a30": 1}
    # 463464629 (161.25) is larger than trade b, so it can only be trade a's
    assert cells.share_of("463464629") == {"fUST_p2": 1}
    # 68.92 fits either: split by trade size
    total = AUG09_TRADE + 100
    assert cells.share_of("463464632") == {"fUST_p2": AUG09_TRADE / total,
                                           "fUST_a30": Decimal(100) / total}
    totals = weekly_totals(chain, cells, now_ms=AUG09 + 3 * DAY)
    whole = sum((c.amount * c.held_ms(0, AUG09 + 3 * DAY, now_ms=0) for c in chain), Decimal(0))
    split = sum((w.capital_days for cell in totals.values() for w in cell.values()), Decimal(0))
    assert abs(split * DAY - whole) < Decimal("1e-12")    # shares conserve the amount
    assert {cell: sum(w.n_credits for w in weeks.values())
            for cell, weeks in totals.items()} == {"fUST_p2": 1, "fUST_a30": 1}


def test_loans_and_credits_at_other_instants_or_periods_do_not_match() -> None:
    credit = _credit("466642176")
    other_period = _trade(1, "a", period=30)
    other_instant = _trade(2, "a", at=CREATED + 1)
    cells = assign_cells([credit], [other_period, other_instant], {"a": "fUST_p2"})
    assert cells.without_trade == {"466642176"}


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
