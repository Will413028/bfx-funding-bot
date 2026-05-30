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

Migration: 4.3 legacy rows lack these fields; PG-sourced rows set them from event_log columns.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Literal
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
    """Offer → credit transition (paper synchronous OR live WS `foc` EXECUTED).

    Ledger effect: _reserved -= size_usdt; _realized += size_usdt.
    `credit_id` is None for paper (no real credit) and for live (the `foc`
    EXECUTED frame carries no credit id; the fill is keyed by venue_offer_id).
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
class PositionReconciled:
    """Periodic venue snapshot result — in-process pub/sub signal ONLY.

    NOT persisted to event_log. Emitted by BootRecovery / PeriodicReconcile
    after fetching /funding/offers, /funding/credits and /wallets. Drives the
    absolute set in PaperPositionLedger.on_position_reconciled(); the
    store.set_position_snapshot() direct write persists reserved/realized to
    position_state (available is in-memory only — not persisted).

    reserved_usdt  = Σ(active offers)  — venue snapshot, not event accumulation.
    realized_usdt  = Σ(active credits) — venue snapshot.
    available_usdt = funding-wallet available balance (deposit-wallet free funds).
    """
    account_id: str
    reserved_usdt: Decimal
    realized_usdt: Decimal
    available_usdt: Decimal
    n_offers: int
    n_credits: int
    occurred_at_ms: int


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
    rest_status: Literal["success", "already_terminal"]
    venue_response_text: str | None = None  # Bitfinex 9th element TEXT field
    venue_seq: int | None = None  # always None — not WS-sourced
    event_seq: int | None = None
    occurred_at_ms: int | None = None
    recorded_at_ms: int | None = None
