"""Domain events published on the in-process DomainEventBus.

Frozen dataclasses; nothing persists them (the ledger journal is the capital record).
``occurred_at_ms`` is the venue/producer time and ``venue_seq`` the WS SEQ on
WS-sourced events (None otherwise).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Literal
from uuid import UUID, uuid4


def _resolve_position_fields(ev: object) -> None:
    """Reconcile transitional `*_usdt` with canonical reserved/realized/available.

    For each of the three buckets, exactly one of (canonical, `_usdt` alias)
    must be supplied; we mirror into both so old (`.reserved_usdt`) and new
    (`.reserved`) read paths agree until callsites migrate.
    """
    for canonical, legacy in (
        ("reserved", "reserved_usdt"),
        ("realized", "realized_usdt"),
        ("available", "available_usdt"),
    ):
        c_val = getattr(ev, canonical, None)
        l_val = getattr(ev, legacy, None)
        if c_val is None and l_val is None:
            raise TypeError(
                f"{type(ev).__name__} requires `{canonical}` "
                f"(or transitional `{legacy}`)"
            )
        if c_val is not None and l_val is not None and c_val != l_val:
            raise TypeError(
                f"{type(ev).__name__}: {canonical}={c_val!r} and {legacy}={l_val!r} "
                "disagree — pass only one"
            )
        if c_val is None:
            object.__setattr__(ev, canonical, l_val)
        if l_val is None:
            object.__setattr__(ev, legacy, c_val)


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
    occurred_at_ms: int | None = None
    event_id: UUID = field(default_factory=uuid4)


@dataclass(frozen=True, slots=True)
class PositionReconciled:
    """Derived per-symbol bus signal from a durable full-account observation.

    The ledger observation cycle publishes one per symbol only after the
    observation it was derived from has been accepted and committed (see
    ``ledger_cycle_effects``); it feeds the safety/PnL subscribers and is never a
    source of truth.

    `symbol` is the offer currency; MANDATORY (never silently ``fUSD``).
    reserved/realized/available are canonical native fields; `*_usdt` are
    transitional read aliases for consumers that have not migrated yet.

    reserved  = Σ(active offers in `symbol`)  — venue snapshot, not accumulation.
    realized  = Σ(active credits in `symbol`) — venue snapshot.
    available = funding-wallet available balance for `symbol`'s currency.
    """
    symbol: str  # mandatory, FIRST (frozen+slots: non-default must precede defaulted)
    account_id: str
    n_offers: int
    n_credits: int
    occurred_at_ms: int
    reserved: Decimal | None = None
    realized: Decimal | None = None
    available: Decimal | None = None
    reserved_usdt: Decimal | None = None  # transitional alias of reserved
    realized_usdt: Decimal | None = None  # transitional alias of realized
    available_usdt: Decimal | None = None  # transitional alias of available
    event_id: UUID = field(default_factory=uuid4)

    def __post_init__(self) -> None:
        _resolve_position_fields(self)


@dataclass(frozen=True, slots=True)
class CancelAcknowledged:
    """Bitfinex REST cancel API returned (success OR already-terminal).

    Audit-only event: its subscribers are the diagnostics sink (a ``cancel_audit``
    row) and the domain-event counter; it changes no capital state. The offer's
    close is observed by the ledger (the WS `foc` only becomes a venue hint).
    Provides a REST-leg debug breadcrumb separable from the intent (CancelRequested).
    """
    venue_offer_id: str
    acknowledged_at_ms: int
    signal_correlation_id: UUID
    account_id: str
    rest_status: Literal["success", "already_terminal"]
    venue_response_text: str | None = None  # Bitfinex notification TEXT, index 7
    venue_seq: int | None = None  # always None — not WS-sourced
    occurred_at_ms: int | None = None
    event_id: UUID = field(default_factory=uuid4)
