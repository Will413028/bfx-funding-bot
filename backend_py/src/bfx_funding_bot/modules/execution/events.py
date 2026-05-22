"""Domain events emitted via DomainEventBus.

Distinct from `modules/marketfeed/schemas.py` Pydantic payload models:
- Domain events (這檔) = in-process bus payload, frozen dataclass
- *Payload models (schemas.py)   = Axiom event payload serialization

AxiomEventSink translates domain event → Pydantic payload → axiom.emit().
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from uuid import UUID


@dataclass(frozen=True, slots=True)
class ReservationClaimed:
    """Submit returned status ∈ {submitted, filled} — capital reserved at venue.

    Ledger effect: _reserved += size_usdt.
    """
    cid: int
    venue_offer_id: str
    size_usdt: Decimal
    signal_correlation_id: UUID
    account_id: str
    is_simulated: bool


@dataclass(frozen=True, slots=True)
class OrderFilled:
    """Offer → credit transition (paper synchronous OR live WS `fcn`).

    Ledger effect: _reserved -= size_usdt; _realized += size_usdt.
    `credit_id` None for paper (no real credit); populated for 4.4 live.
    """
    cid: int
    venue_offer_id: str
    credit_id: str | None
    size_usdt: Decimal
    fill_rate: float
    signal_correlation_id: UUID
    account_id: str
    is_simulated: bool


@dataclass(frozen=True, slots=True)
class ReservationReleased:
    """Offer cancelled / expired without fill.

    Ledger effect: _reserved -= size_usdt (floor at 0; emits warning + counts).
    """
    cid: int
    venue_offer_id: str
    size_usdt: Decimal
    reason: str  # "venue_cancel" / "user_cancel" / "expired" / "missing_from_venue"
    signal_correlation_id: UUID
    account_id: str
    is_simulated: bool
