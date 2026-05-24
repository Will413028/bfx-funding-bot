"""Domain events emitted via DomainEventBus.

Distinct from `modules/marketfeed/schemas.py` Pydantic payload models:
- Domain events (這檔) = in-process bus payload, frozen dataclass
- *Payload models (schemas.py)   = event payload serialization (persisted via event_log)

Schema version 2 (Phase 4.4a):
  - Added bitemporal Optional fields: occurred_at_ms, recorded_at_ms
  - Added monotonic Optional event_seq (bus attach)
  - Added venue-issued idempotency Optional venue_seq (WS SEQ on WS-sourced events;
    None for non-WS events kept for uniform schema)
  - Added CancelRequested as first-class cancel event

Migration: 4.3 Axiom rows lack these fields; upcaster_chain v1→v2 fills None.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from uuid import UUID

__SCHEMA_VERSION__ = 2


@dataclass(frozen=True, slots=True)
class ReservationIntent:
    """A2 write-ahead intent — durable record BEFORE the venue REST submit.

    Persisted in txn1 so a crash between submit-call and outcome leaves a
    recoverable PENDING claim (resolved at boot in 3a-recovery). Carries no
    venue_offer_id (unknown until CLAIMED). Not published to the bus
    (in-memory ledger/registry track CLAIMED+, not PENDING).
    """
    cid: int
    size_usdt: Decimal
    signal_correlation_id: UUID
    account_id: str
    is_simulated: bool
    venue_seq: int | None = None
    event_seq: int | None = None
    occurred_at_ms: int | None = None
    recorded_at_ms: int | None = None


@dataclass(frozen=True, slots=True)
class ReservationFailed:
    """A2 terminal outcome — venue REST submit failed; intent resolves to FAILED.

    Ledger effect: none (reserved untouched — capital was never committed).
    Not published to the bus (no in-memory subscriber needs it).
    """
    cid: int
    size_usdt: Decimal
    signal_correlation_id: UUID
    account_id: str
    is_simulated: bool
    reason: str  # e.g. "submit_failed"
    venue_seq: int | None = None
    event_seq: int | None = None
    occurred_at_ms: int | None = None
    recorded_at_ms: int | None = None


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
    venue_seq: int | None = None
    event_seq: int | None = None
    occurred_at_ms: int | None = None
    recorded_at_ms: int | None = None


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
    venue_seq: int | None = None
    event_seq: int | None = None
    occurred_at_ms: int | None = None
    recorded_at_ms: int | None = None


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
    venue_seq: int | None = None
    event_seq: int | None = None
    occurred_at_ms: int | None = None
    recorded_at_ms: int | None = None


@dataclass(frozen=True, slots=True)
class CancelRequested:
    """User-initiated cancel request on pending offer.

    Ledger effect: dequeues offer from pending; signals venue cancel via API.
    """
    venue_offer_id: str
    requested_at_ms: int
    signal_correlation_id: UUID
    account_id: str
    venue_seq: int | None = None
    event_seq: int | None = None
    occurred_at_ms: int | None = None
    recorded_at_ms: int | None = None


@dataclass(frozen=True, slots=True)
class CancelAcknowledged:
    """Bitfinex REST cancel API returned (success OR already-terminal).

    Audit-only event — ledger/registry NOT subscribers. Single SoT for
    state mutation remains ws_dispatcher (publishes ReservationReleased
    on WS foc). Provides REST-leg debug breadcrumb separable from intent
    (CancelRequested) and outcome (ReservationReleased).

    Ledger effect: none (audit).
    Registry effect: none (audit).
    """
    venue_offer_id: str
    acknowledged_at_ms: int
    signal_correlation_id: UUID
    account_id: str
    rest_status: str  # "success" | "already_terminal"
    venue_response_text: str | None = None  # Bitfinex 9th element TEXT field
    venue_seq: int | None = None  # always None — not WS-sourced
    event_seq: int | None = None
    occurred_at_ms: int | None = None
    recorded_at_ms: int | None = None
