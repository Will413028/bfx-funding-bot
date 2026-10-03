"""Venue events: everything that ever changed the simulated venue, incl. market input.

State is a pure fold over these. Market input (public trades, book snapshots) is
recorded as events too, so a replay needs no network and no clock.
"""
from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from decimal import Decimal
from typing import Any


@dataclass(frozen=True, slots=True)
class TradeTick:
    mts: int
    amount: Decimal
    rate: Decimal
    period: int


@dataclass(frozen=True, slots=True)
class WalletFunded:
    currency: str
    amount: Decimal
    mts: int


@dataclass(frozen=True, slots=True)
class BookObserved:
    symbol: str
    captured_at_ms: int
    asks: tuple[tuple[Decimal, int, Decimal], ...]
    mts: int


@dataclass(frozen=True, slots=True)
class OfferPlaced:
    offer_id: int
    symbol: str
    amount: Decimal
    rate: Decimal
    period: int
    mts: int
    queue_ahead: Decimal  # public volume resting ahead, frozen from the book at placement


@dataclass(frozen=True, slots=True)
class TradesObserved:
    symbol: str
    through_ms: int
    trades: tuple[TradeTick, ...]
    mts: int


@dataclass(frozen=True, slots=True)
class OfferFilled:
    offer_id: int
    trade_id: int
    loan_id: int
    amount: Decimal
    rate: Decimal
    period: int
    mts: int


@dataclass(frozen=True, slots=True)
class LoanDrawn:
    loan_id: int
    credit_id: int
    mts: int


@dataclass(frozen=True, slots=True)
class OfferCanceled:
    offer_id: int
    mts: int


@dataclass(frozen=True, slots=True)
class CreditClosed:
    kind: str  # "loan" | "credit"
    lending_id: int
    mts: int
    reason: str  # "expired"


@dataclass(frozen=True, slots=True)
class InterestPaid:
    ledger_id: int
    currency: str
    amount: Decimal
    balance: Decimal  # funding wallet balance after the payout
    mts: int


type VenueEvent = (
    WalletFunded | BookObserved | OfferPlaced | TradesObserved | OfferFilled
    | LoanDrawn | OfferCanceled | CreditClosed | InterestPaid
)

_TYPES: dict[str, type] = {
    cls.__name__: cls
    for cls in (
        TradeTick, WalletFunded, BookObserved, OfferPlaced, TradesObserved, OfferFilled,
        LoanDrawn, OfferCanceled, CreditClosed, InterestPaid,
    )
}


def _encode(value: Any) -> Any:
    if isinstance(value, Decimal):
        return {"$dec": str(value)}
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {"$t": type(value).__name__, **{
            f.name: _encode(getattr(value, f.name)) for f in dataclasses.fields(value)
        }}
    if isinstance(value, tuple):
        return [_encode(v) for v in value]
    return value


def _decode(value: Any) -> Any:
    if isinstance(value, dict):
        if "$dec" in value:
            return Decimal(value["$dec"])
        cls = _TYPES[value["$t"]]
        return cls(**{k: _decode(v) for k, v in value.items() if k != "$t"})
    if isinstance(value, list):
        return tuple(_decode(v) for v in value)
    return value


def event_to_payload(event: VenueEvent) -> dict[str, Any]:
    """JSON-safe form of an event (the SQL store's `payload`)."""
    payload = _encode(event)
    assert isinstance(payload, dict)
    return payload


def event_from_payload(payload: dict[str, Any]) -> VenueEvent:
    event = _decode(payload)
    if type(event).__name__ not in _TYPES or type(event) is TradeTick:
        raise ValueError(f"not a venue event payload: {payload!r}")
    return event  # type: ignore[no-any-return]
