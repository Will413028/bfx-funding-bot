"""Domain events published on the in-process DomainEventBus.

Frozen dataclasses; nothing persists them (the ledger journal is the capital record).
``occurred_at_ms`` is the venue/producer time and ``venue_seq`` the WS SEQ on
WS-sourced events (None otherwise).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Literal
from uuid import UUID, uuid4

from bfx_funding_bot.modules.execution.contracts import ReservationRef


def _resolve_amount(ev: object) -> None:
    """Reconcile transitional `size_usdt` with canonical `amount` on frozen events.

    Exactly one of the two must be provided by the caller. We mirror the value
    into BOTH attributes so `.amount` (canonical) and `.size_usdt` (legacy read
    path in the ledger) agree until all callsites migrate to `amount`.
    """
    amount = getattr(ev, "amount", None)
    size_usdt = getattr(ev, "size_usdt", None)
    if amount is None and size_usdt is None:
        raise TypeError(
            f"{type(ev).__name__} requires `amount` (or transitional `size_usdt`)"
        )
    if amount is not None and size_usdt is not None and amount != size_usdt:
        raise TypeError(
            f"{type(ev).__name__}: amount={amount!r} and size_usdt={size_usdt!r} "
            "disagree — pass only one"
        )
    if amount is None:
        object.__setattr__(ev, "amount", size_usdt)
    if size_usdt is None:
        object.__setattr__(ev, "size_usdt", amount)


def _require_symbol(ev: object) -> None:
    """Fail loud if `symbol` is missing/empty.

    `symbol` is a mandatory non-default field on the reserve events, but a frozen
    dataclass does NOT enforce the `str` annotation at runtime, so symbol=None or ""
    would construct silently. Convert that into a hard failure.
    """
    if not getattr(ev, "symbol", None):
        raise TypeError(f"{type(ev).__name__} requires a non-empty `symbol`")


def _validate_reservation_ref(
    ev: object,
    *,
    requires_venue_offer: bool,
) -> None:
    """Reject internally contradictory correlation data before it is persisted.

    Live producers must supply a reference. (The stored-row replay factory that let
    uncorrelated historical rows bypass this went with the event store, S1-8 PR-D.)
    """
    reference = getattr(ev, "reservation_ref", None)
    if reference is None:
        raise TypeError(
            f"{type(ev).__name__} requires reservation_ref",
        )
    typed_event: Any = ev
    if (
        reference.cid != typed_event.cid
        or reference.signal_correlation_id != typed_event.signal_correlation_id
    ):
        raise TypeError(f"{type(ev).__name__} reservation_ref correlation conflicts")
    if requires_venue_offer and reference.venue_offer_id != typed_event.venue_offer_id:
        raise TypeError(f"{type(ev).__name__} reservation_ref venue offer conflicts")


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
class ReservationClaimed:
    """Submit returned status ∈ {submitted, filled} — capital reserved at venue.

    Ledger effect: reserved[symbol] += amount (native units).

    `symbol` is the offer currency (e.g. "fUST"); MANDATORY (Task 11 — no
    default, so it can never silently land as the legacy "fUSD"). `amount` is
    the native reserve size; `size_usdt` is a transitional read alias +
    back-compat constructor kwarg kept until producers migrate (Phase 1
    per-symbol ledger work).
    """
    symbol: str  # mandatory, FIRST (frozen+slots: non-default must precede defaulted)
    cid: int
    venue_offer_id: str
    signal_correlation_id: UUID
    account_id: str
    is_simulated: bool
    amount: Decimal | None = None
    size_usdt: Decimal | None = None  # transitional: legacy producers; mapped to amount
    venue_seq: int | None = None
    occurred_at_ms: int | None = None
    reservation_ref: ReservationRef | None = None
    event_id: UUID = field(default_factory=uuid4)
    def __post_init__(self) -> None:
        _require_symbol(self)
        _validate_reservation_ref(
            self,
            requires_venue_offer=True,
        )
        _resolve_amount(self)


@dataclass(frozen=True, slots=True)
class OrderFilled:
    """Offer → credit transition (paper synchronous OR live WS `foc` EXECUTED).

    Ledger effect: reserved[symbol] -= amount; realized[symbol] += amount.
    `credit_id` is None for paper (no real credit) and for live (the `foc`
    EXECUTED frame carries no credit id; the fill is keyed by venue_offer_id).

    `symbol`/`amount`/`size_usdt`: see ReservationClaimed (symbol mandatory).
    """
    symbol: str  # mandatory, FIRST (frozen+slots: non-default must precede defaulted)
    cid: int
    venue_offer_id: str
    credit_id: str | None
    fill_rate: float
    signal_correlation_id: UUID
    account_id: str
    is_simulated: bool
    amount: Decimal | None = None
    size_usdt: Decimal | None = None  # transitional: legacy producers; mapped to amount
    venue_seq: int | None = None
    occurred_at_ms: int | None = None
    reservation_ref: ReservationRef | None = None
    event_id: UUID = field(default_factory=uuid4)
    def __post_init__(self) -> None:
        _require_symbol(self)
        _validate_reservation_ref(
            self,
            requires_venue_offer=True,
        )
        _resolve_amount(self)


@dataclass(frozen=True, slots=True)
class ReservationReleased:
    """Offer cancelled / expired without fill.

    Ledger effect: reserved[symbol] -= amount (floor at 0; emits warning + counts).
    `symbol`/`amount`/`size_usdt`: see ReservationClaimed (symbol mandatory).
    """
    symbol: str  # mandatory, FIRST (frozen+slots: non-default must precede defaulted)
    cid: int
    venue_offer_id: str
    reason: str  # "venue_cancel" / "user_cancel" / "expired" / "missing_from_venue"
    signal_correlation_id: UUID
    account_id: str
    is_simulated: bool
    amount: Decimal | None = None
    size_usdt: Decimal | None = None  # transitional: legacy producers; mapped to amount
    venue_seq: int | None = None
    occurred_at_ms: int | None = None
    reservation_ref: ReservationRef | None = None
    event_id: UUID = field(default_factory=uuid4)
    def __post_init__(self) -> None:
        _require_symbol(self)
        _validate_reservation_ref(
            self,
            requires_venue_offer=True,
        )
        _resolve_amount(self)


@dataclass(frozen=True, slots=True)
class CreditClosed:
    """Venue credit ended (matured or borrower returned early) — WS `fcc`.

    Ledger effect: NONE — audit/attribution-only. realized stays reconcile-owned
    (single-writer, ADR 2026-05-29); this event exists so attribution can stop
    assuming held-to-term when the borrower returned early. Carries no
    cid/venue_offer_id (the venue credit object has no offer linkage) — consumers
    join on (symbol, amount, mts_create ≈ fill time).
    """
    symbol: str
    credit_id: int
    amount: Decimal
    rate: float
    period_days: int
    mts_create: int
    account_id: str
    is_simulated: bool
    venue_seq: int | None = None
    occurred_at_ms: int | None = None  # close time (venue mts_last_payout)
    event_id: UUID = field(default_factory=uuid4)
    # Present only on events written after the 2026-09-27 parser fix. Without
    # them, rate / period_days / occurred_at_ms are unreliable: rate held the
    # period, period_days held mts_opening, and occurred_at_ms held mts_update,
    # which may equal mts_create on the close frame.
    mts_opening: int | None = None
    mts_last_payout: int | None = None

    @property
    def venue_fields_reliable(self) -> bool:
        return self.mts_last_payout is not None

    def __post_init__(self) -> None:
        _require_symbol(self)


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
    venue_response_text: str | None = None  # Bitfinex notification TEXT, index 7
    venue_seq: int | None = None  # always None — not WS-sourced
    occurred_at_ms: int | None = None
    event_id: UUID = field(default_factory=uuid4)
