"""Pure commands: (state, input) -> events. The caller commits events, then folds them.

`catch_up` is the venue's only source of time-driven change: fills from public
trades, loan draws, credit expiry and interest payouts, each stamped with its own
scheduled time and emitted in time order.
"""
from __future__ import annotations

import copy
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal

from bfx_funding_bot.modules.lending.tracking.queue_fill import filled_amount, queue_ahead_of
from bfx_funding_bot.modules.simulated_venue._internal.state import (
    DAY_MS,
    ZERO,
    Lending,
    VenueState,
    apply,
    currency_of,
)
from bfx_funding_bot.modules.simulated_venue.contracts import (
    BookSnapshot,
    PublicTrade,
    SimulatedVenueConfig,
)
from bfx_funding_bot.modules.simulated_venue.events import (
    BookObserved,
    CreditClosed,
    InterestPaid,
    LoanDrawn,
    OfferCanceled,
    OfferFilled,
    OfferPlaced,
    TradesObserved,
    TradeTick,
    VenueEvent,
    WalletFunded,
)

_CENT_SATOSHI = Decimal("1E-8")
_PRIO_TRADE, _PRIO_DRAW, _PRIO_EXPIRY, _PRIO_PAYOUT = 0, 1, 2, 3


@dataclass(frozen=True, slots=True)
class NoMarketData:
    """The simulator lacks a usable book. Not a venue answer: an input gap of ours."""

    reason: str


@dataclass(frozen=True, slots=True)
class Refusal:
    """The venue refuses a command; nothing is recorded."""

    reason: str


def fund_wallet(currency: str, amount: Decimal, now_ms: int) -> list[VenueEvent]:
    if amount <= 0:
        raise ValueError("funding amount must be positive")
    return [WalletFunded(currency, amount, now_ms)]


def decide_submit(
    state: VenueState, config: SimulatedVenueConfig, *, now_ms: int, symbol: str,
    amount: Decimal, rate: Decimal, period: int, book: BookSnapshot | None,
) -> list[VenueEvent] | Refusal | NoMarketData:
    if symbol not in config.symbols:
        return Refusal(f"symbol: invalid ({symbol})")
    if not config.min_period_days <= period <= config.max_period_days:
        return Refusal(f"period: invalid ({period})")
    if rate <= 0:
        return Refusal("rate: invalid")
    if amount < config.min_offer_amount:
        return Refusal(f"amount: below minimum {config.min_offer_amount}")
    currency = currency_of(symbol)
    if amount > state.available(currency):
        return Refusal("amount: insufficient balance")
    if book is None:
        # Never place on an assumed-empty queue: that is a silent optimistic fill.
        return NoMarketData("no book snapshot for the queue position")
    if now_ms - book.captured_at_ms > config.max_book_age_ms:
        return NoMarketData(
            f"book snapshot is {now_ms - book.captured_at_ms} ms old "
            f"(max {config.max_book_age_ms})")
    stamp = now_ms - now_ms % 1000 if config.whole_second_offer_mts else now_ms
    return [
        BookObserved(symbol, book.captured_at_ms, book.asks, now_ms),
        OfferPlaced(
            offer_id=state.next_id("offer", config.id_base), symbol=symbol, amount=amount,
            rate=rate, period=period, mts=stamp,
            queue_ahead=queue_ahead_of(book.asks, period_days=period, offer_rate=rate),
        ),
    ]


def decide_cancel(state: VenueState, *, now_ms: int, offer_id: int) -> list[VenueEvent] | Refusal:
    offer = state.offers.get(offer_id)
    if offer is None or not offer.resting:
        return Refusal("offer not found or not active")
    return [OfferCanceled(offer_id, now_ms)]


def decide_cancel_all(state: VenueState, *, now_ms: int, currency: str) -> list[VenueEvent]:
    return [
        OfferCanceled(o.offer_id, now_ms)
        for symbol in sorted({o.symbol for o in state.offers.values() if o.resting})
        if currency_of(symbol) == currency
        for o in state.resting_offers(symbol)
    ]


def _next_payout_after(ms: int, offset_ms: int) -> int:
    day = (ms - offset_ms) // DAY_MS
    return (day + 1) * DAY_MS + offset_ms


def _accrual(lending: Lending, payout_ms: int) -> Decimal:
    end = payout_ms if lending.close_mts is None else min(payout_ms, lending.close_mts)
    span = max(0, end - lending.paid_through)
    return lending.amount * lending.rate * Decimal(span) / Decimal(DAY_MS)


def _owed(lending: Lending) -> bool:
    return lending.active or (lending.close_mts or 0) > lending.paid_through


def _due_payout(
    state: VenueState, config: SimulatedVenueConfig, currency: str,
) -> int | None:
    start = min(
        (lend.paid_through for lend in state.lendings.values()
         if currency_of(lend.symbol) == currency and _owed(lend)),
        default=None,
    )
    if start is None:
        return None
    last = state.wallets[currency].last_payout_ms
    return _next_payout_after(max(start, last), config.payout_offset_ms)


def _fills_for_tick(
    work: VenueState, symbol: str, tick: TradeTick, config: SimulatedVenueConfig,
) -> list[OfferFilled]:
    key = (symbol, tick.period)
    work.volume_cum[key] = work.volume_cum.get(key, ZERO) + tick.amount
    out: list[OfferFilled] = []
    for offer in work.resting_offers(symbol, tick.period):
        own_ahead = sum(
            (work.offers[i].filled for i in offer.ahead_ids), ZERO,
        ) - offer.ahead_filled_mark
        total = filled_amount(
            queue_ahead=offer.queue_ahead + own_ahead, amount=offer.amount_original,
            cumulative_volume=work.volume_cum[key] - offer.cum_mark,
        )
        delta = total - offer.filled
        if delta <= 0:
            continue
        event = OfferFilled(
            offer_id=offer.offer_id, trade_id=work.next_id("trade", config.id_base),
            loan_id=work.next_id("loan", config.id_base), amount=delta, rate=offer.rate,
            period=offer.period, mts=tick.mts,
        )
        apply(work, event)
        out.append(event)
    return out


type _Candidate = tuple[int, int, int, str, object]


def _candidates(
    work: VenueState, config: SimulatedVenueConfig, ticks: list[tuple[int, str, TradeTick]],
    cursor: int, now_ms: int,
) -> list[_Candidate]:
    """Scheduled items due by `now_ms`: (mts, priority, id, label, payload)."""
    out: list[_Candidate] = []
    if cursor < len(ticks):
        mts, symbol, tick = ticks[cursor]
        out.append((mts, _PRIO_TRADE, 0, symbol, tick))
    for lend in work.lendings.values():
        if not lend.active:
            continue
        if lend.kind == "loan" and config.draw_after_ms is not None:
            draw_at = min(lend.opening + config.draw_after_ms, lend.expires_at)
            out.append((draw_at, _PRIO_DRAW, lend.lending_id, "loan", lend))
        out.append((lend.expires_at, _PRIO_EXPIRY, lend.lending_id, lend.kind, lend))
    for currency in sorted(work.wallets):
        due = _due_payout(work, config, currency)
        if due is not None:
            out.append((due, _PRIO_PAYOUT, 0, currency, None))
    return [c for c in out if c[0] <= now_ms]


def catch_up(
    state: VenueState, config: SimulatedVenueConfig, *, now_ms: int,
    trades: Mapping[str, Sequence[PublicTrade]],
) -> list[VenueEvent]:
    """Every scheduled change with `mts <= now_ms`, in time order.

    `trades[symbol]` are the public trades fetched for `(market_through, now_ms]`;
    only periods with a resting offer are kept and recorded.
    """
    events: list[VenueEvent] = []
    ticks: list[tuple[int, str, TradeTick]] = []
    for symbol in sorted(trades):
        periods = {o.period for o in state.resting_offers(symbol)}
        kept = tuple(sorted(
            (TradeTick(t.mts, t.amount, t.rate, t.period) for t in trades[symbol]
             if t.period in periods and state.market_through.get(symbol, 0) < t.mts <= now_ms),
            key=lambda t: (t.mts, t.period, t.amount, t.rate),
        ))
        if kept:
            events.append(TradesObserved(symbol, now_ms, kept, now_ms))
            ticks.extend((t.mts, symbol, t) for t in kept)
    ticks.sort(key=lambda item: (item[0], item[1]))

    if not ticks and not _candidates(state, config, ticks, 0, now_ms):
        return events  # nothing is due: no copy of the (growing) state per request
    work = copy.deepcopy(state)
    cursor = 0
    while True:
        candidates = _candidates(work, config, ticks, cursor, now_ms)
        if not candidates:
            break
        mts, prio, _, label, payload = min(candidates, key=lambda c: c[:4])
        new: list[VenueEvent]
        if prio == _PRIO_TRADE:
            cursor += 1
            assert isinstance(payload, TradeTick)
            new = list(_fills_for_tick(work, label, payload, config))
        elif prio == _PRIO_DRAW:
            assert isinstance(payload, Lending)
            new = [LoanDrawn(
                loan_id=payload.lending_id,
                credit_id=work.next_id("credit", config.id_base), mts=mts,
            )]
        elif prio == _PRIO_EXPIRY:
            assert isinstance(payload, Lending)
            new = [CreditClosed(payload.kind, payload.lending_id, mts, "expired")]
        else:
            new = [_pay_interest(work, config, label, mts)]
        for event in new:
            if prio != _PRIO_TRADE:  # tick fills were already folded by _fills_for_tick
                apply(work, event)
            events.append(event)
    return events


def _pay_interest(
    work: VenueState, config: SimulatedVenueConfig, currency: str, mts: int,
) -> InterestPaid:
    gross = sum(
        (_accrual(lend, mts) for lend in work.lendings.values()
         if currency_of(lend.symbol) == currency and _owed(lend)),
        ZERO,
    )
    net = (gross * (Decimal(1) - config.fee_rate)).quantize(_CENT_SATOSHI, rounding=ROUND_DOWN)
    return InterestPaid(
        ledger_id=work.next_id("ledger", config.id_base), currency=currency, amount=net,
        balance=work.wallets[currency].balance + net, mts=mts,
    )
