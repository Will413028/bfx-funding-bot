"""Pure state machine: decide (commands, catch-up) and fold (apply), no transport."""
from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal

import pytest

from bfx_funding_bot.modules.simulated_venue._internal.decide import (
    Refusal,
    catch_up,
    decide_cancel,
    decide_cancel_all,
    decide_submit,
    fund_wallet,
)
from bfx_funding_bot.modules.simulated_venue._internal.state import VenueState, apply
from bfx_funding_bot.modules.simulated_venue.contracts import PublicTrade, SimulatedVenueConfig
from bfx_funding_bot.modules.simulated_venue.events import (
    CreditClosed,
    InterestPaid,
    LoanDrawn,
    OfferFilled,
    VenueEvent,
    event_from_payload,
    event_to_payload,
)
from tests.modules.simulated_venue.helpers import DAY, HOUR, T0, book, config, trade

D = Decimal
OFFSET = 5_400_000  # 01:30Z


class Machine:
    def __init__(self, cfg: SimulatedVenueConfig | None = None, funds: str = "1000") -> None:
        self.cfg = cfg or config()
        self.state = VenueState()
        self.log: list[VenueEvent] = []
        self.now = T0
        if funds != "0":
            self.emit(fund_wallet("UST", D(funds), T0))

    def emit(self, events: Sequence[VenueEvent] | Refusal) -> list[VenueEvent]:
        assert not isinstance(events, Refusal), events
        for event in events:
            apply(self.state, event)
        self.log.extend(events)
        return list(events)

    def place(self, amount: str = "150", rate: str = "0.0002", period: int = 2,
              asks: list[tuple[str, int, str]] | None = None) -> int:
        events = self.emit(decide_submit(
            self.state, self.cfg, now_ms=self.now, symbol="fUST", amount=D(amount),
            rate=D(rate), period=period, book=book("fUST", self.now, asks or []),
        ))
        return next(e.offer_id for e in events if hasattr(e, "offer_id"))

    def tick(self, to: int, trades: Sequence[PublicTrade] = ()) -> list[VenueEvent]:
        self.now = to
        return self.emit(catch_up(
            self.state, self.cfg, now_ms=to, trades={"fUST": list(trades)} if trades else {},
        ))


def test_placing_reserves_funds_and_freezes_the_queue() -> None:
    m = Machine()
    oid = m.place("300", asks=[("0.0001", 2, "500"), ("0.0003", 2, "900"), ("0.0001", 30, "7")])
    offer = m.state.offers[oid]
    assert offer.queue_ahead == D("500") and offer.status == "ACTIVE"
    assert m.state.available("UST") == D("700") and m.state.wallets["UST"].balance == D("1000")


@pytest.mark.parametrize(("kwargs", "why"), [
    ({"amount": "1001"}, "insufficient"),
    ({"amount": "149"}, "below minimum"),
    ({"period": 1}, "period"),
    ({"period": 121}, "period"),
    ({"rate": "0"}, "rate"),
    ({"symbol": "fXYZ"}, "symbol"),
])
def test_submit_validation_refuses_without_recording(kwargs: dict[str, str | int], why: str) -> None:
    m = Machine()
    args: dict[str, object] = {"symbol": "fUST", "amount": D("150"), "rate": D("0.0002"),
                               "period": 2}
    args.update({k: (D(v) if isinstance(v, str) and k in ("amount", "rate") else v)
                 for k, v in kwargs.items()})
    result = decide_submit(m.state, m.cfg, now_ms=T0, book=book(), **args)  # type: ignore[arg-type]
    assert isinstance(result, Refusal) and why in result.reason


def test_submit_without_a_book_is_refused_not_placed_on_an_empty_queue() -> None:
    m = Machine()
    result = decide_submit(m.state, m.cfg, now_ms=T0, symbol="fUST", amount=D("150"),
                           rate=D("0.0002"), period=2, book=None)
    assert isinstance(result, Refusal) and "book" in result.reason


def test_partial_then_full_fill_creates_trade_and_loan_with_opening_at_fill_time() -> None:
    m = Machine()
    oid = m.place("300", asks=[("0.0001", 2, "500")])
    # 400 traded: 500 ahead not yet consumed -> nothing.
    events = m.tick(T0 + HOUR, [trade(T0 + HOUR, "400")])
    assert not [e for e in events if isinstance(e, OfferFilled)]
    assert m.state.offers[oid].remaining == D("300")
    m.tick(T0 + 2 * HOUR, [trade(T0 + 2 * HOUR, "200")])  # cum 600 -> 100 into the offer
    offer = m.state.offers[oid]
    assert (offer.status, offer.remaining) == ("PARTIAL", D("200"))
    m.tick(T0 + 3 * HOUR, [trade(T0 + 3 * HOUR, "1000")])
    offer = m.state.offers[oid]
    assert (offer.status, offer.remaining, offer.was_partial) == ("EXECUTED", D("0"), True)
    assert offer.terminal_mts == T0 + 3 * HOUR
    fills = [e for e in m.log if isinstance(e, OfferFilled)]
    assert [(f.amount, f.mts) for f in fills] == [
        (D("100"), T0 + 2 * HOUR), (D("200"), T0 + 3 * HOUR)]
    assert [t.amount for t in m.state.trades] == [D("100"), D("200")]
    loans = list(m.state.lendings.values())
    assert [(lend.opening, lend.amount, lend.offer_id) for lend in loans] == [
        (T0 + 2 * HOUR, D("100"), oid), (T0 + 3 * HOUR, D("200"), oid)]
    assert m.state.available("UST") == D("700")  # 300 lent; nothing resting


def test_volume_at_another_period_or_before_placement_does_not_fill() -> None:
    m = Machine()
    oid = m.place("150", period=2)
    m.tick(T0 + HOUR, [trade(T0 + HOUR, "99999", period=30), trade(T0, "99999")])
    assert m.state.offers[oid].remaining == D("150")


def test_expiry_returns_principal_and_closes_at_opening_plus_period() -> None:
    m = Machine()
    m.place("150")
    m.tick(T0 + HOUR, [trade(T0 + HOUR, "150")])
    assert m.state.available("UST") == D("850")
    events = m.tick(T0 + HOUR + 2 * DAY - 1)
    assert not [e for e in events if isinstance(e, CreditClosed)]
    assert m.state.available("UST") < D("1000")
    events = m.tick(T0 + HOUR + 2 * DAY)
    (closed,) = [e for e in events if isinstance(e, CreditClosed)]
    assert closed.mts == T0 + HOUR + 2 * DAY and closed.reason == "expired"
    lending = m.state.lendings[("loan", closed.lending_id)]
    assert lending.status == "CLOSED (expired)" and lending.close_mts == closed.mts
    # principal is available again; only interest was added to the balance
    assert m.state.available("UST") == m.state.wallets["UST"].balance


def test_interest_is_paid_on_the_payout_cadence_net_of_fee() -> None:
    m = Machine(config(fee_rate=D("0.15")))
    m.place("150", rate="0.0002")
    m.tick(T0 + 2 * HOUR, [trade(T0 + 2 * HOUR, "150")])  # opens 02:00, after day-0 payout
    assert not [e for e in m.log if isinstance(e, InterestPaid)]
    m.tick(T0 + DAY + OFFSET - 1)
    assert not [e for e in m.log if isinstance(e, InterestPaid)]  # not before 01:30 next day
    m.tick(T0 + DAY + OFFSET)
    (paid,) = [e for e in m.log if isinstance(e, InterestPaid)]
    gross = D("150") * D("0.0002") * D(DAY + OFFSET - 2 * HOUR) / D(DAY)
    assert paid.mts == T0 + DAY + OFFSET
    assert paid.amount == (gross * D("0.85")).quantize(D("1E-8"), rounding="ROUND_DOWN")
    assert paid.balance == D("1000") + paid.amount
    m.tick(T0 + DAY + OFFSET + 3 * HOUR)
    assert len([e for e in m.log if isinstance(e, InterestPaid)]) == 1  # once a day, no more


def test_each_payout_pays_only_unpaid_time_so_total_interest_is_exact() -> None:
    m = Machine(config(fee_rate=D("0")))
    m.place("200.00000000", rate="0.0003")
    m.tick(T0 + HOUR, [trade(T0 + HOUR, "200")])
    m.tick(T0 + 6 * DAY)
    gross = D("200") * D("0.0003") * 2  # two days, then closed: nothing accrues after expiry
    paid = [e for e in m.log if isinstance(e, InterestPaid)]
    # day 0 01:30 pays the half hour since opening; the last payout pays the tail to expiry
    assert [p.mts for p in paid] == [T0 + k * DAY + OFFSET for k in (0, 1, 2)]
    # each payout is rounded down to 8 decimals, so the total is within 3e-8 below gross
    total = m.state.wallets["UST"].balance - D("1000")
    assert gross - D("0.00000003") <= total <= gross


def test_wallet_identity_balance_equals_deposits_plus_interest() -> None:
    m = Machine()
    m.place("200")
    m.tick(T0 + HOUR, [trade(T0 + HOUR, "200")])
    m.tick(T0 + 5 * DAY)
    interest = sum((e.amount for e in m.log if isinstance(e, InterestPaid)), D("0"))
    assert interest > 0
    assert m.state.wallets["UST"].balance == m.state.deposits["UST"] + interest


def test_own_offers_share_one_virtual_queue_volume_is_not_counted_twice() -> None:
    m = Machine()
    a = m.place("150", rate="0.0002")
    b = m.place("150", rate="0.0002")
    assert m.state.offers[b].ahead_ids == (a,)
    m.tick(T0 + HOUR, [trade(T0 + HOUR, "200")])
    assert m.state.offers[a].filled == D("150")
    assert m.state.offers[b].filled == D("50")  # 200 - 150 consumed by the offer ahead
    m.tick(T0 + 2 * HOUR, [trade(T0 + 2 * HOUR, "100")])
    assert m.state.offers[b].filled == D("150")


def test_an_offer_with_a_higher_rate_is_behind_and_one_placed_later_below_is_not_ahead() -> None:
    m = Machine()
    low = m.place("150", rate="0.0001")
    high = m.place("150", rate="0.0003")
    assert m.state.offers[high].ahead_ids == (low,) and m.state.offers[low].ahead_ids == ()
    m.tick(T0 + HOUR, [trade(T0 + HOUR, "150")])
    assert (m.state.offers[low].filled, m.state.offers[high].filled) == (D("150"), D("0"))


def test_volume_before_a_later_offer_is_placed_never_counts_for_it() -> None:
    m = Machine()
    m.place("150")
    m.tick(T0 + HOUR, [trade(T0 + HOUR, "150")])
    late = m.place("150")  # placed after the first offer was fully filled
    m.tick(T0 + 2 * HOUR)
    assert m.state.offers[late].filled == D("0")


def test_cancel_and_cancel_all_keep_what_was_filled() -> None:
    m = Machine()
    a = m.place("150")
    b = m.place("150", rate="0.0009")
    m.tick(T0 + HOUR, [trade(T0 + HOUR, "40")])
    assert isinstance(decide_cancel(m.state, now_ms=T0, offer_id=999), Refusal)
    m.emit(decide_cancel(m.state, now_ms=T0 + HOUR, offer_id=a))
    offer = m.state.offers[a]
    assert (offer.status, offer.was_partial, offer.filled) == ("CANCELED", True, D("40"))
    assert isinstance(decide_cancel(m.state, now_ms=T0, offer_id=a), Refusal)  # not active now
    assert [e.offer_id for e in decide_cancel_all(m.state, now_ms=T0, currency="UST")] == [b]
    assert decide_cancel_all(m.state, now_ms=T0, currency="USD") == []
    # 40 is lent out and b (150) still rests; the cancelled offer's unfilled part is free
    assert m.state.available("UST") == D("1000") - D("40") - D("150")


def test_borrower_draw_moves_the_loan_to_a_credit_keeping_opening_and_expiry() -> None:
    m = Machine(config(draw_after_ms=HOUR))
    m.place("150")
    m.tick(T0 + HOUR, [trade(T0 + HOUR, "150")])
    m.tick(T0 + 2 * HOUR)
    (draw,) = [e for e in m.log if isinstance(e, LoanDrawn)]
    loan = m.state.lendings[("loan", draw.loan_id)]
    credit = m.state.lendings[("credit", draw.credit_id)]
    assert loan.status == "CLOSED (used)" and credit.active
    assert (credit.opening, credit.expires_at, credit.amount) == (
        loan.opening, loan.expires_at, loan.amount)
    m.tick(T0 + HOUR + 2 * DAY)
    assert m.state.lendings[("credit", draw.credit_id)].status == "CLOSED (expired)"
    assert m.state.available("UST") == m.state.wallets["UST"].balance


def test_catch_up_over_a_long_gap_orders_every_change_by_time_and_stamps_it_on_schedule() -> None:
    m = Machine()
    m.place("150")
    trades = [trade(T0 + HOUR, "100"), trade(T0 + 3 * HOUR, "100")]
    events = m.tick(T0 + 10 * DAY, trades)
    stamps = [e.mts for e in events]
    assert stamps[0] == T0 + 10 * DAY  # the recorded market input leads its derived events
    assert stamps[1:] == sorted(stamps[1:])
    kinds = [type(e).__name__ for e in events[1:]]
    # fill 01:00, day-0 payout 01:30 (pays the half hour), fill 03:00, payouts ...
    assert kinds[:3] == ["OfferFilled", "InterestPaid", "OfferFilled"]
    assert kinds.count("CreditClosed") == 2 and kinds.count("InterestPaid") >= 3
    # the first loan (opened 01:00) expires at 2d+1h, before the second one's
    closes = [e.mts for e in events if isinstance(e, CreditClosed)]
    assert closes == [T0 + HOUR + 2 * DAY, T0 + 3 * HOUR + 2 * DAY]


def test_folding_the_log_reproduces_the_state_also_through_the_json_codec() -> None:
    m = Machine(config(draw_after_ms=HOUR))
    m.place("300", asks=[("0.0001", 2, "50")])
    m.place("150", rate="0.0004")
    m.tick(T0 + HOUR, [trade(T0 + HOUR, "130")])
    m.tick(T0 + 4 * DAY, [trade(T0 + DAY, "500")])
    for source in (m.log, [event_from_payload(event_to_payload(e)) for e in m.log]):
        replay = VenueState()
        for event in source:
            apply(replay, event)
        assert replay == m.state


def test_same_inputs_give_identical_events_independent_of_the_wall_clock() -> None:
    def run() -> list[VenueEvent]:
        m = Machine()
        m.place("150")
        m.tick(T0 + HOUR, [trade(T0 + HOUR, "150")])
        m.tick(T0 + 3 * DAY)
        return m.log

    assert run() == run()
    assert all(abs(e.mts - T0) < 4 * DAY for e in run())  # nothing stamped from time.time()


def test_fund_wallet_rejects_non_positive_amounts() -> None:
    with pytest.raises(ValueError):
        fund_wallet("UST", D("0"), T0)
