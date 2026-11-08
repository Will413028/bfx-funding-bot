"""Venue state: a pure fold over `VenueEvent`s. No clock, no I/O, no randomness."""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Literal

from bfx_funding_bot.modules.simulated_venue.contracts import BookSnapshot
from bfx_funding_bot.modules.simulated_venue.events import (
    BookObserved,
    CreditClosed,
    InterestPaid,
    LoanDrawn,
    NonceAdvanced,
    OfferCanceled,
    OfferFilled,
    OfferPlaced,
    TradesObserved,
    TradeTick,
    VenueEvent,
    WalletFunded,
)

DAY_MS = 86_400_000
# One disjoint id range per entity kind (`id_base + slot * ID_STRIDE`), so an id of one
# kind can never equal an id of another and a join on the wrong kind cannot pass by luck.
ID_STRIDE = 10_000_000
ID_SLOT = {"offer": 0, "trade": 1, "loan": 2, "credit": 3, "ledger": 4}
ZERO = Decimal("0")

OfferStatus = Literal["ACTIVE", "PARTIAL", "EXECUTED", "CANCELED"]
LendingStatus = Literal["ACTIVE", "CLOSED (expired)", "CLOSED (used)"]
RESTING: tuple[OfferStatus, ...] = ("ACTIVE", "PARTIAL")


def currency_of(symbol: str) -> str:
    return symbol[1:]


@dataclass(slots=True)
class Offer:
    offer_id: int
    symbol: str
    amount_original: Decimal
    remaining: Decimal
    rate: Decimal
    period: int
    mts_create: int
    mts_update: int
    queue_ahead: Decimal
    cum_mark: Decimal  # public volume at this (symbol, period) when the offer was placed
    ahead_ids: tuple[int, ...]  # own resting offers ahead of this one at placement
    ahead_filled_mark: Decimal  # what those had already filled at placement
    placed_ms: int  # the real placement instant (`mts_create` may be stamped to the second)
    status: OfferStatus = "ACTIVE"
    was_partial: bool = False
    terminal_mts: int | None = None

    @property
    def resting(self) -> bool:
        return self.status in RESTING

    @property
    def filled(self) -> Decimal:
        return self.amount_original - self.remaining


@dataclass(slots=True)
class Lending:
    kind: Literal["loan", "credit"]
    lending_id: int
    symbol: str
    offer_id: int
    amount: Decimal
    rate: Decimal
    period: int
    mts_create: int
    mts_update: int
    opening: int
    paid_through: int
    status: LendingStatus = "ACTIVE"
    close_mts: int | None = None

    @property
    def active(self) -> bool:
        return self.status == "ACTIVE"

    @property
    def expires_at(self) -> int:
        return self.opening + self.period * DAY_MS


@dataclass(frozen=True, slots=True)
class Trade:
    trade_id: int
    symbol: str
    mts: int
    offer_id: int
    amount: Decimal
    rate: Decimal
    period: int


@dataclass(frozen=True, slots=True)
class LedgerEntry:
    ledger_id: int
    currency: str
    mts: int
    amount: Decimal
    balance: Decimal


@dataclass(slots=True)
class Wallet:
    balance: Decimal
    last_payout_ms: int


@dataclass(slots=True)
class VenueState:
    wallets: dict[str, Wallet] = field(default_factory=dict)
    offers: dict[int, Offer] = field(default_factory=dict)
    lendings: dict[tuple[str, int], Lending] = field(default_factory=dict)
    trades: list[Trade] = field(default_factory=list)
    ledger: list[LedgerEntry] = field(default_factory=list)
    volume_cum: dict[tuple[str, int], Decimal] = field(default_factory=dict)
    market_through: dict[str, int] = field(default_factory=dict)
    books: dict[str, BookSnapshot] = field(default_factory=dict)
    counters: dict[str, int] = field(default_factory=dict)
    high_water_ms: int = 0
    deposits: dict[str, Decimal] = field(default_factory=dict)
    last_nonce: int = 0

    def next_id(self, kind: str, id_base: int) -> int:
        floor = id_base + ID_SLOT[kind] * ID_STRIDE
        issued = max(self.counters.get(kind, floor), floor) + 1
        if issued >= floor + ID_STRIDE:
            raise OverflowError(f"{kind} id space exhausted")
        return issued

    def resting_offers(self, symbol: str, period: int | None = None) -> list[Offer]:
        return sorted(
            (o for o in self.offers.values()
             if o.resting and o.symbol == symbol and (period is None or o.period == period)),
            key=lambda o: o.offer_id,
        )

    def reserved(self, currency: str) -> Decimal:
        """Funds in resting offers plus principal lent out."""
        resting = sum(
            (o.remaining for o in self.offers.values()
             if o.resting and currency_of(o.symbol) == currency), ZERO,
        )
        lent = sum(
            (lend.amount for lend in self.lendings.values()
             if lend.active and currency_of(lend.symbol) == currency), ZERO,
        )
        return resting + lent

    def available(self, currency: str) -> Decimal:
        wallet = self.wallets.get(currency)
        return ZERO if wallet is None else wallet.balance - self.reserved(currency)


def _bump(state: VenueState, kind: str, value: int) -> None:
    state.counters[kind] = max(state.counters.get(kind, 0), value)


def apply(state: VenueState, event: VenueEvent) -> None:
    """Fold one event into `state` in place."""
    state.high_water_ms = max(state.high_water_ms, event.mts)
    match event:
        case WalletFunded():
            wallet = state.wallets.get(event.currency)
            if wallet is None:
                state.wallets[event.currency] = Wallet(event.amount, event.mts)
            else:
                wallet.balance += event.amount
            state.deposits[event.currency] = state.deposits.get(event.currency, ZERO) + event.amount
        case NonceAdvanced():
            state.last_nonce = max(state.last_nonce, event.nonce)
        case BookObserved():
            state.books[event.symbol] = BookSnapshot(event.symbol, event.captured_at_ms, event.asks)
        case OfferPlaced():
            _place(state, event)
        case TradesObserved():
            for tick in event.trades:
                count_tick(state, event.symbol, tick)
            state.market_through[event.symbol] = max(
                state.market_through.get(event.symbol, 0), event.through_ms,
            )
        case OfferFilled():
            _fill(state, event)
        case LoanDrawn():
            _draw(state, event)
        case OfferCanceled():
            offer = state.offers[event.offer_id]
            offer.was_partial = offer.status == "PARTIAL"
            offer.status = "CANCELED"
            offer.mts_update = offer.terminal_mts = event.mts
        case CreditClosed():
            lending = state.lendings[(event.kind, event.lending_id)]
            lending.status = "CLOSED (expired)"
            lending.mts_update = lending.close_mts = event.mts
        case InterestPaid():
            _interest(state, event)


def count_tick(state: VenueState, symbol: str, tick: TradeTick) -> None:
    """Add one public trade to the (symbol, period) volume, for the offers it can fill.

    A trade that executed before an offer's real placement instant, but reaches the venue
    after it (the feed's watermark trails `now`), is no volume for that offer: its mark moves
    with the counter, as if the trade had been seen before the offer was placed.
    """
    key = (symbol, tick.period)
    state.volume_cum[key] = state.volume_cum.get(key, ZERO) + tick.amount
    for offer in state.resting_offers(symbol, tick.period):
        if tick.mts <= offer.placed_ms:
            offer.cum_mark += tick.amount


def _place(state: VenueState, event: OfferPlaced) -> None:
    ahead = [
        o for o in state.resting_offers(event.symbol, event.period) if o.rate <= event.rate
    ]
    # Other resting offers of the symbol still wait for the public trades in
    # `(market_through, watermark]`: placing must not move that cursor past them. A symbol
    # with nothing resting starts its cursor at the real placement instant.
    nothing_else_rests = not state.resting_offers(event.symbol)
    state.offers[event.offer_id] = Offer(
        offer_id=event.offer_id, symbol=event.symbol, amount_original=event.amount,
        remaining=event.amount, rate=event.rate, period=event.period,
        mts_create=event.mts, mts_update=event.mts, queue_ahead=event.queue_ahead,
        cum_mark=state.volume_cum.get((event.symbol, event.period), ZERO),
        ahead_ids=tuple(o.offer_id for o in ahead),
        ahead_filled_mark=sum((o.filled for o in ahead), ZERO),
        placed_ms=state.high_water_ms,
    )
    if nothing_else_rests:
        state.market_through[event.symbol] = max(
            state.market_through.get(event.symbol, 0), state.high_water_ms,
        )
    else:
        state.market_through.setdefault(event.symbol, state.high_water_ms)
    _bump(state, "offer", event.offer_id)


def _fill(state: VenueState, event: OfferFilled) -> None:
    offer = state.offers[event.offer_id]
    offer.remaining -= event.amount
    offer.mts_update = event.mts
    if offer.remaining == 0:
        offer.was_partial = offer.status == "PARTIAL"
        offer.status = "EXECUTED"
        offer.terminal_mts = event.mts
    else:
        offer.status = "PARTIAL"
    state.trades.append(Trade(
        event.trade_id, offer.symbol, event.mts, event.offer_id, event.amount,
        event.rate, event.period,
    ))
    state.lendings[("loan", event.loan_id)] = Lending(
        kind="loan", lending_id=event.loan_id, symbol=offer.symbol, offer_id=event.offer_id,
        amount=event.amount, rate=event.rate, period=event.period, mts_create=event.mts,
        mts_update=event.mts, opening=event.mts, paid_through=event.mts,
    )
    _bump(state, "trade", event.trade_id)
    _bump(state, "loan", event.loan_id)


def _draw(state: VenueState, event: LoanDrawn) -> None:
    loan = state.lendings[("loan", event.loan_id)]
    loan.status = "CLOSED (used)"
    loan.mts_update = loan.close_mts = event.mts
    state.lendings[("credit", event.credit_id)] = Lending(
        kind="credit", lending_id=event.credit_id, symbol=loan.symbol, offer_id=loan.offer_id,
        amount=loan.amount, rate=loan.rate, period=loan.period, mts_create=event.mts,
        mts_update=event.mts, opening=loan.opening, paid_through=event.mts,
    )
    _bump(state, "credit", event.credit_id)


def _interest(state: VenueState, event: InterestPaid) -> None:
    wallet = state.wallets[event.currency]
    wallet.balance += event.amount
    wallet.last_payout_ms = event.mts
    state.ledger.append(LedgerEntry(
        event.ledger_id, event.currency, event.mts, event.amount, wallet.balance,
    ))
    for lending in state.lendings.values():
        if currency_of(lending.symbol) == event.currency:
            end = event.mts if lending.close_mts is None else min(event.mts, lending.close_mts)
            lending.paid_through = max(lending.paid_through, end)
    _bump(state, "ledger", event.ledger_id)
