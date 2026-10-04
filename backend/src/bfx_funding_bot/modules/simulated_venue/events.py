"""Venue events: everything that ever changed the simulated venue, incl. market input.

State is a pure fold over these. Market input (public trades, book snapshots) is
recorded as events too, so a replay needs no network and no clock.

Persisted form: `{"event_type": <stable name>, "schema_version": <int>, "data": {...}}`.
The stable `event_type` string, not the Python class name, is the contract, and the
decoder upcasts old versions step by step or refuses what it does not know (same
convention as `modules/execution/events.py`). The version belongs to the event TYPE: every type
starts at 1 and a type that changes shape bumps its own number and registers its own upcaster.
Adding a type therefore never touches the log that already exists: a reader that predates the new
type finds it unknown and refuses the log (it never misreads it), and every older row keeps the
version it was written with.
"""
from __future__ import annotations

import dataclasses
from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, ClassVar

SCHEMA_VERSION = 1  # the first version of a type (each class below names its own default)


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


@dataclass(frozen=True, slots=True)
class FaultInjected:
    """One injected venue fault (soak only), recorded BEFORE its effect, so a restart or a
    lost response never hides it: the soak report tells injected from organic UNKNOWN by it.

    `target` is the request kind (`submit`, `history`, ...), `request_ordinal` its 1-based
    count in this process, `nonce` the request's accepted nonce. A submit also records what the
    request asked for (`symbol`, `amount`, `rate`, `period`; None for other targets), so the
    report matches an injection to the attempt that sent it by content, not by time alone.
    """

    event_type: ClassVar[str] = "fault_injected"
    fault_kind: str
    target: str
    request_ordinal: int
    nonce: int
    symbol: str | None
    amount: Decimal | None
    rate: Decimal | None
    period: int | None
    mts: int
    schema_version: int = field(default=SCHEMA_VERSION, init=False, repr=False, compare=False)


@dataclass(frozen=True, slots=True)
class InternalFailureRecorded:
    """The simulator itself failed (never a venue answer): durable, so the soak report reads it
    from the database across restarts and crashes. `detail` is cut to `DETAIL_LIMIT` chars."""

    event_type: ClassVar[str] = "internal_failure_recorded"
    kind: str
    detail: str
    mts: int
    schema_version: int = field(default=SCHEMA_VERSION, init=False, repr=False, compare=False)


@dataclass(frozen=True, slots=True)
class UnexpectedRequestRecorded:
    """A request the venue does not route (wrong host, method or path). `url` is cut to
    `DETAIL_LIMIT` chars."""

    event_type: ClassVar[str] = "unexpected_request_recorded"
    method: str
    url: str
    mts: int
    schema_version: int = field(default=SCHEMA_VERSION, init=False, repr=False, compare=False)


DETAIL_LIMIT = 300


type VenueEvent = (
    WalletFunded | BookObserved | OfferPlaced | TradesObserved | OfferFilled
    | LoanDrawn | OfferCanceled | CreditClosed | InterestPaid | NonceAdvanced | FaultInjected
    | InternalFailureRecorded | UnexpectedRequestRecorded
)

_EVENTS: dict[str, type] = {
    cls.event_type: cls
    for cls in (
        WalletFunded, BookObserved, OfferPlaced, TradesObserved, OfferFilled,
        LoanDrawn, OfferCanceled, CreditClosed, InterestPaid, NonceAdvanced, FaultInjected,
        InternalFailureRecorded, UnexpectedRequestRecorded,
    )
}
# The current version of each type: only a type that changes shape moves its own number.
_VERSIONS: dict[str, int] = dict.fromkeys(_EVENTS, SCHEMA_VERSION)
_NESTED: dict[str, type] = {"trade_tick": TradeTick}

# (event_type, from_version) -> data of from_version + 1. Empty while every type is at v1;
# a type that changes shape adds its upcaster here instead of rewriting stored events.
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
    current = _VERSIONS[event_type]
    while version < current:
        upcast = _UPCASTERS.get((event_type, version))
        if upcast is None:
            raise UnknownEventVersionError(f"no upcaster for {event_type} v{version}")
        data, version = upcast(data), version + 1
    if version != current:
        raise UnknownEventVersionError(
            f"{event_type} schema_version {version} is newer than {current}")
    return cls(**{k: _decode(v) for k, v in data.items()})  # type: ignore[no-any-return]
