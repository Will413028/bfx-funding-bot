"""Venue events: everything that ever changed the simulated venue, incl. market input.

State is a pure fold over these. Market input (public trades, book snapshots) is
recorded as events too, so a replay needs no network and no clock.

Persisted form: `{"event_type": <stable name>, "schema_version": <int>, "data": {...}}`.
The stable `event_type` string, not the Python class name, is the contract, and the
decoder upcasts old versions step by step or refuses what it does not know (same
convention as `modules/execution/events.py`). Version 1 is the first.
"""
from __future__ import annotations

import dataclasses
from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, ClassVar

SCHEMA_VERSION = 1


class UnknownEventVersionError(ValueError):
    """A persisted event has an unknown type or a schema version we cannot read."""


@dataclass(frozen=True, slots=True)
class TradeTick:
    mts: int
    amount: Decimal
    rate: Decimal
    period: int


@dataclass(frozen=True, slots=True)
class WalletFunded:
    event_type: ClassVar[str] = "wallet_funded"
    currency: str
    amount: Decimal
    mts: int
    schema_version: int = field(default=SCHEMA_VERSION, init=False, repr=False, compare=False)


@dataclass(frozen=True, slots=True)
class BookObserved:
    event_type: ClassVar[str] = "book_observed"
    symbol: str
    captured_at_ms: int
    asks: tuple[tuple[Decimal, int, Decimal], ...]
    mts: int
    schema_version: int = field(default=SCHEMA_VERSION, init=False, repr=False, compare=False)


@dataclass(frozen=True, slots=True)
class OfferPlaced:
    event_type: ClassVar[str] = "offer_placed"
    offer_id: int
    symbol: str
    amount: Decimal
    rate: Decimal
    period: int
    mts: int
    queue_ahead: Decimal  # public volume resting ahead, frozen from the book at placement
    schema_version: int = field(default=SCHEMA_VERSION, init=False, repr=False, compare=False)


@dataclass(frozen=True, slots=True)
class TradesObserved:
    event_type: ClassVar[str] = "trades_observed"
    symbol: str
    through_ms: int
    trades: tuple[TradeTick, ...]
    mts: int
    schema_version: int = field(default=SCHEMA_VERSION, init=False, repr=False, compare=False)


@dataclass(frozen=True, slots=True)
class OfferFilled:
    event_type: ClassVar[str] = "offer_filled"
    offer_id: int
    trade_id: int
    loan_id: int
    amount: Decimal
    rate: Decimal
    period: int
    mts: int
    schema_version: int = field(default=SCHEMA_VERSION, init=False, repr=False, compare=False)


@dataclass(frozen=True, slots=True)
class LoanDrawn:
    event_type: ClassVar[str] = "loan_drawn"
    loan_id: int
    credit_id: int
    mts: int
    schema_version: int = field(default=SCHEMA_VERSION, init=False, repr=False, compare=False)


@dataclass(frozen=True, slots=True)
class OfferCanceled:
    event_type: ClassVar[str] = "offer_canceled"
    offer_id: int
    mts: int
    schema_version: int = field(default=SCHEMA_VERSION, init=False, repr=False, compare=False)


@dataclass(frozen=True, slots=True)
class CreditClosed:
    event_type: ClassVar[str] = "credit_closed"
    kind: str  # "loan" | "credit"
    lending_id: int
    mts: int
    reason: str  # "expired"
    schema_version: int = field(default=SCHEMA_VERSION, init=False, repr=False, compare=False)


@dataclass(frozen=True, slots=True)
class InterestPaid:
    event_type: ClassVar[str] = "interest_paid"
    ledger_id: int
    currency: str
    amount: Decimal
    balance: Decimal  # funding wallet balance after the payout
    mts: int
    schema_version: int = field(default=SCHEMA_VERSION, init=False, repr=False, compare=False)


@dataclass(frozen=True, slots=True)
class NonceAdvanced:
    """The highest nonce the venue accepted for the key: durable, like on Bitfinex."""

    event_type: ClassVar[str] = "nonce_advanced"
    nonce: int
    mts: int
    schema_version: int = field(default=SCHEMA_VERSION, init=False, repr=False, compare=False)


type VenueEvent = (
    WalletFunded | BookObserved | OfferPlaced | TradesObserved | OfferFilled
    | LoanDrawn | OfferCanceled | CreditClosed | InterestPaid | NonceAdvanced
)

_EVENTS: dict[str, type] = {
    cls.event_type: cls
    for cls in (
        WalletFunded, BookObserved, OfferPlaced, TradesObserved, OfferFilled,
        LoanDrawn, OfferCanceled, CreditClosed, InterestPaid, NonceAdvanced,
    )
}
_NESTED: dict[str, type] = {"trade_tick": TradeTick}

# (event_type, from_version) -> data of from_version + 1. Empty while only v1 exists;
# a future schema change adds its upcaster here instead of rewriting stored events.
_UPCASTERS: dict[tuple[str, int], Callable[[dict[str, Any]], dict[str, Any]]] = {}


def _encode(value: Any) -> Any:
    if isinstance(value, Decimal):
        return {"$dec": str(value)}
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        name = next(n for n, c in _NESTED.items() if c is type(value))
        return {"$t": name, **{f.name: _encode(getattr(value, f.name))
                               for f in dataclasses.fields(value)}}
    if isinstance(value, tuple):
        return [_encode(v) for v in value]
    return value


def _decode(value: Any) -> Any:
    if isinstance(value, dict):
        if "$dec" in value:
            return Decimal(value["$dec"])
        if "$t" in value:
            cls = _NESTED.get(value["$t"])
            if cls is None:
                raise UnknownEventVersionError(f"unknown nested type {value['$t']!r}")
            return cls(**{k: _decode(v) for k, v in value.items() if k != "$t"})
        raise UnknownEventVersionError(f"unreadable value {value!r}")
    if isinstance(value, list):
        return tuple(_decode(v) for v in value)
    return value


def event_to_payload(event: VenueEvent) -> dict[str, Any]:
    """JSON-safe form of an event (the SQL store's `payload`)."""
    return {
        "event_type": type(event).event_type,
        "schema_version": event.schema_version,
        "data": {f.name: _encode(getattr(event, f.name))
                 for f in dataclasses.fields(event) if f.init},
    }


def event_from_payload(payload: dict[str, Any]) -> VenueEvent:
    """Decode, upcasting old versions; unknown types and versions are refused."""
    event_type, version = payload.get("event_type"), payload.get("schema_version")
    cls = _EVENTS.get(event_type) if isinstance(event_type, str) else None
    if (cls is None or not isinstance(event_type, str)
            or isinstance(version, bool) or not isinstance(version, int)):
        raise UnknownEventVersionError(f"not a venue event payload: {payload!r}")
    data = dict(payload.get("data", {}))
    while version < SCHEMA_VERSION:
        upcast = _UPCASTERS.get((event_type, version))
        if upcast is None:
            raise UnknownEventVersionError(f"no upcaster for {event_type} v{version}")
        data, version = upcast(data), version + 1
    if version != SCHEMA_VERSION:
        raise UnknownEventVersionError(
            f"{event_type} schema_version {version} is newer than {SCHEMA_VERSION}")
    return cls(**{k: _decode(v) for k, v in data.items()})  # type: ignore[no-any-return]
