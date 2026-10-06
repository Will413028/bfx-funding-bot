"""Immutable venue-object observations carried by ``VenueSnapshotObserved``.

The records contain no SQLAlchemy/session state: the normalized boundary between Bitfinex wire
responses and the snapshot event the live executor publishes. The legacy projector that folded
them into entity state went with the event store (S1-8 PR-D).
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
    # The originating trade's instant; kept when a loan turns into credits with
    # new ids and MTS_CREATE. None in snapshots recorded before 2026-09-27.
    mts_opening: int | None = None

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


# Bitfinex appends detail to a status: "EXECUTED at 0.0148% (150.78)",
# "PARTIALLY FILLED at ...", "CANCELED was: PARTIALLY FILLED at ...". The state
# is the leading phrase; everything after the first separator is narrative.
# Without this, an offer that filled is recorded as the unknown status
# "executed_at_0.0148%_(150.78)" and can never be recognised as terminal.
_STATUS_DETAIL = re.compile(r"\s+(?:at|@)\s|\s+was:", re.IGNORECASE)


def _normalize_status(status: str) -> str:
    head = _STATUS_DETAIL.split(str(status).strip(), maxsplit=1)[0]
    return head.strip().lower().replace(" ", "_")
