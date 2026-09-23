"""Immutable venue-object records used by the serialized execution projector.

The records in this module deliberately contain no SQLAlchemy/session state.
They are the normalized boundary between Bitfinex wire responses and the
account-scoped entity projections.  A clean replay can therefore construct the
same values without contacting the venue.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from types import MappingProxyType
from typing import Any
from uuid import UUID


def _decimal(value: Decimal | int | float | str) -> Decimal:
    return value if isinstance(value, Decimal) else Decimal(str(value))


def _validate_offer_amounts(amount_original: Decimal, amount_remaining: Decimal) -> None:
    if amount_original < 0 or amount_remaining < 0:
        raise ValueError("offer amounts must be non-negative")
    if amount_remaining > amount_original:
        raise ValueError("amount_remaining exceeds amount_original")


def _freeze_metadata(value: Mapping[str, Any] | None) -> Mapping[str, Any]:
    if value is None:
        return MappingProxyType({})
    return MappingProxyType(dict(value))


@dataclass(frozen=True, slots=True)
class VenueOfferState:
    """Current belief about one venue funding offer.

    ``last_seen_event_seq`` is the local account-stream fence for this object;
    it is not a venue sequence and must only move forward.  ``is_terminal`` is
    derived from the normalized status unless an explicit value is supplied by
    a trusted projector/replay caller.
    """

    venue_offer_id: str
    symbol: str
    amount_original: Decimal
    amount_remaining: Decimal
    rate: Decimal | None
    period_days: int | None
    status: str
    mts_created: int
    mts_updated: int
    first_seen_event_seq: int
    last_seen_event_seq: int
    is_terminal: bool | None = None
    cid: int | None = None
    execution_decision_id: str | None = None
    signal_correlation_id: UUID | None = None
    offer_type: str | None = None
    flags: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.venue_offer_id:
            raise ValueError("venue_offer_id must be non-empty")
        if not self.symbol:
            raise ValueError("symbol must be non-empty")
        object.__setattr__(self, "amount_original", _decimal(self.amount_original))
        object.__setattr__(self, "amount_remaining", _decimal(self.amount_remaining))
        if self.rate is not None:
            object.__setattr__(self, "rate", _decimal(self.rate))
        _validate_offer_amounts(self.amount_original, self.amount_remaining)
        normalized_status = _normalize_status(self.status)
        object.__setattr__(self, "status", normalized_status)
        if self.is_terminal is None:
            object.__setattr__(self, "is_terminal", is_terminal_offer_status(normalized_status))
        object.__setattr__(self, "flags", _freeze_metadata(self.flags))


@dataclass(frozen=True, slots=True)
class VenueCreditState:
    """Current belief about one venue funding credit."""

    credit_id: str
    symbol: str
    amount: Decimal
    rate: Decimal | None
    period_days: int | None
    status: str
    mts_created: int | None
    mts_updated: int | None
    first_seen_event_seq: int
    last_seen_event_seq: int
    is_terminal: bool | None = None
    flags: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.credit_id:
            raise ValueError("credit_id must be non-empty")
        if not self.symbol:
            raise ValueError("symbol must be non-empty")
        object.__setattr__(self, "amount", _decimal(self.amount))
        if self.amount < 0:
            raise ValueError("credit amount must be non-negative")
        if self.rate is not None:
            object.__setattr__(self, "rate", _decimal(self.rate))
        normalized_status = _normalize_status(self.status)
        object.__setattr__(self, "status", normalized_status)
        if self.is_terminal is None:
            object.__setattr__(self, "is_terminal", is_terminal_credit_status(normalized_status))
        object.__setattr__(self, "flags", _freeze_metadata(self.flags))


@dataclass(frozen=True, slots=True)
class VenueOfferObservation:
    """Normalized offer included in a full-account venue snapshot."""

    venue_offer_id: str
    symbol: str
    amount_original: Decimal
    amount_remaining: Decimal
    rate: Decimal | None
    period_days: int | None
    status: str
    mts_created: int
    mts_updated: int
    cid: int | None = None
    execution_decision_id: str | None = None
    signal_correlation_id: UUID | None = None
    offer_type: str | None = None
    flags: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.venue_offer_id:
            raise ValueError("venue_offer_id must be non-empty")
        if not self.symbol:
            raise ValueError("symbol must be non-empty")
        object.__setattr__(self, "amount_original", _decimal(self.amount_original))
        object.__setattr__(self, "amount_remaining", _decimal(self.amount_remaining))
        _validate_offer_amounts(self.amount_original, self.amount_remaining)
        if self.rate is not None:
            object.__setattr__(self, "rate", _decimal(self.rate))
        object.__setattr__(self, "status", _normalize_status(self.status))
        object.__setattr__(self, "flags", _freeze_metadata(self.flags))


@dataclass(frozen=True, slots=True)
class VenueCreditObservation:
    """Normalized credit included in a full-account venue snapshot."""

    credit_id: str
    symbol: str
    amount: Decimal
    rate: Decimal | None
    period_days: int | None
    status: str
    mts_created: int | None = None
    mts_updated: int | None = None
    flags: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.credit_id:
            raise ValueError("credit_id must be non-empty")
        if not self.symbol:
            raise ValueError("symbol must be non-empty")
        object.__setattr__(self, "amount", _decimal(self.amount))
        if self.amount < 0:
            raise ValueError("credit amount must be non-negative")
        if self.rate is not None:
            object.__setattr__(self, "rate", _decimal(self.rate))
        object.__setattr__(self, "status", _normalize_status(self.status))
        object.__setattr__(self, "flags", _freeze_metadata(self.flags))


_TERMINAL_OFFER_STATUSES = frozenset({
    "absent",
    "cancelled",
    "canceled",
    "closed",
    "expired",
    "failed",
    "filled",
    "executed",
    "done",
    "released",
    "quarantined",
})
_TERMINAL_CREDIT_STATUSES = frozenset({
    "closed",
    "cancelled",
    "canceled",
    "expired",
    "returned",
    "repaid",
})


# Bitfinex appends detail to a status: "EXECUTED at 0.0148% (150.78)",
# "PARTIALLY FILLED at ...", "CANCELED was: PARTIALLY FILLED at ...". The state
# is the leading phrase; everything after the first separator is narrative.
# Without this, an offer that filled is recorded as the unknown status
# "executed_at_0.0148%_(150.78)" and can never be recognised as terminal.
_STATUS_DETAIL = re.compile(r"\s+(?:at|@)\s|\s+was:", re.IGNORECASE)


def _normalize_status(status: str) -> str:
    head = _STATUS_DETAIL.split(str(status).strip(), maxsplit=1)[0]
    return head.strip().lower().replace(" ", "_")


def normalize_venue_status(status: str) -> str:
    return _normalize_status(status)


def is_terminal_offer_status(status: str) -> bool:
    return _normalize_status(status) in _TERMINAL_OFFER_STATUSES


def is_terminal_credit_status(status: str) -> bool:
    return _normalize_status(status) in _TERMINAL_CREDIT_STATUSES
