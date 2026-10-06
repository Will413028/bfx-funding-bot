"""Domain events emitted via DomainEventBus.

Distinct from `modules/marketfeed/schemas.py` Pydantic payload models:
- Domain events (這檔) = in-process bus payload, frozen dataclass
- *Payload models (schemas.py)   = event payload serialization (persisted via event_log)

Schema version 3 (serialized execution projector):
  - Added bitemporal Optional fields: occurred_at_ms, recorded_at_ms
  - Added monotonic Optional event_seq (bus attach)
  - Added venue-issued idempotency Optional venue_seq (WS SEQ on WS-sourced events;
    None for non-WS events kept for uniform schema)
  - Added CancelRequested as first-class cancel event

Migration: 4.3 legacy rows lack these fields; PG-sourced rows set them from event_log columns.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from types import MappingProxyType
from typing import Any, Literal
from uuid import UUID, uuid4

from bfx_funding_bot.modules.execution.contracts import ReservationRef
from bfx_funding_bot.modules.execution.event_store.entities import (
    VenueCreditObservation,
    VenueOfferObservation,
)
from bfx_funding_bot.modules.execution.submit_outcomes import SubmissionAttemptPayload

__SCHEMA_VERSION__ = 3

ManualUncertaintyResolutionAction = Literal[
    "accepted_external_exposure",
    "closed_at_venue",
]
MANUAL_UNCERTAINTY_RESOLUTION_ACTIONS: frozenset[
    ManualUncertaintyResolutionAction
] = frozenset({"accepted_external_exposure", "closed_at_venue"})

# Schema-evolution upcast value: each of the 5 reserve events gained a mandatory
# `symbol` after early event_log rows were written (the 4 position events in
# Phase 2; Intent/Failed in the fUSD-prereq work). Two consumers use it for those
# legacy rows — serialization.deserialize_event (injects it for any symbol-less
# payload) and store.rebuild_snapshot_from_log's tail fold (defaults a missing
# symbol). Every live event now carries an explicit symbol; the canary was
# fUST-only when those rows existed.
DEFAULT_RECONCILE_SYMBOL = "fUST"

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
    dataclass does NOT enforce the `str` annotation at runtime — e.g.
    deserialize_event building kwargs from a legacy `payload.get("symbol")` could
    land symbol=None and silently miscompute the per-symbol fold. Convert that
    into a hard failure (defense-in-depth alongside the deserialize upcaster).
    """
    if not getattr(ev, "symbol", None):
        raise TypeError(f"{type(ev).__name__} requires a non-empty `symbol`")


def _require_execution_decision_id(ev: object) -> None:
    """Reject intents that cannot be correlated to their pre-trade audit row."""
    decision_id = getattr(ev, "execution_decision_id", None)
    if not isinstance(decision_id, str) or not decision_id:
        raise TypeError(f"{type(ev).__name__} requires a non-empty `execution_decision_id`")


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


def _validate_resolution_event(ev: object) -> None:
    """Validate the common immutable audit fields on resolution events."""
    uncertainty_id = getattr(ev, "uncertainty_id", None)
    if not isinstance(uncertainty_id, UUID):
        raise TypeError(f"{type(ev).__name__} requires uncertainty_id UUID")
    for name in ("account_id", "environment", "symbol", "kind"):
        if not isinstance(getattr(ev, name, None), str) or not getattr(ev, name).strip():
            raise TypeError(f"{type(ev).__name__} requires non-empty {name}")
    reconcile_event_seq = getattr(ev, "reconcile_event_seq", None)
    if not isinstance(reconcile_event_seq, int) or reconcile_event_seq < 0:
        raise ValueError(f"{type(ev).__name__} requires non-negative reconcile_event_seq")
    for name in ("resolved_by_operator_id", "resolution_reason"):
        if not isinstance(getattr(ev, name, None), str) or not getattr(ev, name).strip():
            raise TypeError(f"{type(ev).__name__} requires non-empty {name}")
    evidence = getattr(ev, "resolution_evidence", None)
    if not isinstance(evidence, Mapping):
        raise TypeError(f"{type(ev).__name__} resolution_evidence must be an object")


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
class SnapshotCoverage:
    """Evidence that a venue observation covers the complete account scope.

    A snapshot is only authoritative for the dimensions marked complete.  The
    page counters are retained as audit metadata so an operator can distinguish
    an empty account from a truncated response.  History coverage is optional
    for an ordinary reconcile and is used by the UNKNOWN submit matcher.
    """

    active_offers_complete: bool
    active_credits_complete: bool
    wallets_complete: bool
    offer_history_complete: bool = False
    active_offer_pages: int = 1
    active_credit_pages: int = 1
    wallet_pages: int = 1
    offer_history_pages: int = 0
    offer_history_start_ms: int | None = None
    offer_history_end_ms: int | None = None
    offer_history_oldest_mts: int | None = None
    offer_history_newest_mts: int | None = None

    def __post_init__(self) -> None:
        for name in (
            "active_offer_pages",
            "active_credit_pages",
            "wallet_pages",
            "offer_history_pages",
        ):
            if getattr(self, name) < 0:
                raise ValueError(f"{name} must be non-negative")
        if self.active_offers_complete and self.active_offer_pages < 1:
            raise ValueError("complete active offers require at least one page")
        if self.active_credits_complete and self.active_credit_pages < 1:
            raise ValueError("complete active credits require at least one page")
        if self.wallets_complete and self.wallet_pages < 1:
            raise ValueError("complete wallets require at least one page")
        if self.offer_history_complete and self.offer_history_pages < 1:
            raise ValueError("complete offer history requires at least one page")
        if (
            self.offer_history_start_ms is not None
            and self.offer_history_end_ms is not None
            and self.offer_history_end_ms < self.offer_history_start_ms
        ):
            raise ValueError("offer history coverage fence is inverted")
        if self.offer_history_complete and (
            self.offer_history_start_ms is None or self.offer_history_end_ms is None
        ):
            raise ValueError("complete offer history requires a query fence")


@dataclass(frozen=True, slots=True)
class VenueSnapshotObserved:
    """Immutable full-account venue observation persisted in ``event_log``.

    Network calls finish before this event enters the account writer.  Every
    active object is carried in normalized form, including symbols not present
    in strategy configuration; projections therefore cannot silently omit
    exposure from an unexpected currency.
    """

    account_id: str
    environment: str
    query_started_at_ms: int
    query_finished_at_ms: int
    offers: tuple[VenueOfferObservation, ...]
    credits: tuple[VenueCreditObservation, ...]
    wallet_available: Mapping[str, Decimal]
    coverage: SnapshotCoverage
    offer_history: tuple[VenueOfferObservation, ...] = ()
    capital_query_id: str | None = None
    capital_command_fence: int | None = None
    capital_confirmation: Mapping[str, Any] | None = None
    capital_classification_digest: str | None = None
    occurred_at_ms: int | None = None
    event_id: UUID = field(default_factory=uuid4)
    schema_version: int = field(default=__SCHEMA_VERSION__, init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        if not self.account_id:
            raise ValueError("snapshot account_id must be non-empty")
        if not self.environment:
            raise ValueError("snapshot environment must be non-empty")
        if self.query_finished_at_ms < self.query_started_at_ms:
            raise ValueError("snapshot query_finished_at_ms precedes query_started_at_ms")
        object.__setattr__(self, "offers", tuple(self.offers))
        object.__setattr__(self, "credits", tuple(self.credits))
        object.__setattr__(self, "offer_history", tuple(self.offer_history))
        object.__setattr__(
            self,
            "wallet_available",
            MappingProxyType({
                str(symbol): Decimal(str(amount))
                for symbol, amount in self.wallet_available.items()
            }),
        )
        if any(amount < 0 for amount in self.wallet_available.values()):
            raise ValueError("wallet available amounts must be non-negative")
        if self.occurred_at_ms is None:
            object.__setattr__(self, "occurred_at_ms", self.query_finished_at_ms)


# The target architecture calls the immutable value ``FullAccountSnapshot``
# while the persisted domain event is ``VENUE_SNAPSHOT_OBSERVED``.  Keeping an
# alias avoids two subtly different snapshot models and makes the value usable
# by callers that do not care about the event-type spelling.
FullAccountSnapshot = VenueSnapshotObserved


@dataclass(frozen=True, slots=True)
class ReservationIntent:
    """A2 write-ahead intent — durable record BEFORE the venue REST submit.

    Persisted in txn1 so a crash between submit-call and outcome leaves a
    recoverable PENDING claim (resolved at boot in 3a-recovery). Carries no
    venue_offer_id (unknown until CLAIMED). Not published to the bus.

    `symbol` is the offer currency; MANDATORY (no default). `amount` is the
    native reserve size; `size_usdt` is the transitional alias (see
    _resolve_amount). Legacy rows predate `symbol` and are upcast to
    DEFAULT_RECONCILE_SYMBOL in deserialize_event.
    """
    symbol: str  # mandatory, FIRST (frozen+slots: non-default must precede defaulted)
    cid: int
    signal_correlation_id: UUID
    account_id: str
    is_simulated: bool
    execution_decision_id: str | None
    reservation_ref: ReservationRef | None = None
    submission_attempt: SubmissionAttemptPayload | Mapping[str, Any] | None = None
    capital_authorization: Mapping[str, Any] | None = None
    is_legacy_uncorrelated: bool = field(default=False, init=False)
    amount: Decimal | None = None
    size_usdt: Decimal | None = None  # transitional alias; mapped to amount
    venue_seq: int | None = None
    event_seq: int | None = None
    occurred_at_ms: int | None = None
    recorded_at_ms: int | None = None
    event_id: UUID = field(default_factory=uuid4)
    schema_version: int = field(default=__SCHEMA_VERSION__, init=False, repr=False, compare=False)
    def __post_init__(self) -> None:
        _require_symbol(self)
        if self.execution_decision_id is None:
            _require_execution_decision_id(self)
        else:
            _require_execution_decision_id(self)
            if self.reservation_ref is None:
                object.__setattr__(self, "reservation_ref", ReservationRef(
                    execution_decision_id=self.execution_decision_id,
                    cid=self.cid,
                    signal_correlation_id=self.signal_correlation_id,
                ))
            elif self.reservation_ref.execution_decision_id != self.execution_decision_id:
                raise TypeError("ReservationIntent reservation_ref decision id conflicts")
        attempt = self.submission_attempt
        if isinstance(attempt, Mapping):
            attempt = SubmissionAttemptPayload(**dict(attempt))
            object.__setattr__(self, "submission_attempt", attempt)
        elif attempt is not None and not isinstance(attempt, SubmissionAttemptPayload):
            raise TypeError("ReservationIntent submission_attempt has invalid type")
        if attempt is not None:
            if (
                attempt.execution_decision_id != self.execution_decision_id
                or str(attempt.account_id) != self.account_id
                or attempt.symbol != self.symbol
                or attempt.cid != self.cid
            ):
                raise TypeError("ReservationIntent submission_attempt identity conflicts")
            if (
                attempt.completed_at_ms is not None
                or attempt.outcome_kind is not None
                or attempt.outcome_reason is not None
                or attempt.venue_offer_id is not None
                or attempt.last_event_seq is not None
            ):
                raise TypeError("ReservationIntent requires a pending submission_attempt")
        _validate_reservation_ref(
            self,
            requires_venue_offer=False,
        )
        _resolve_amount(self)


@dataclass(frozen=True, slots=True)
class ReservationFailed:
    """A2 terminal outcome — venue REST submit failed; intent resolves to FAILED.

    Ledger effect: none (reserved untouched). Not published to the bus.
    `symbol`/`amount`/`size_usdt`: see ReservationIntent (symbol mandatory).
    """
    symbol: str  # mandatory, FIRST
    cid: int
    signal_correlation_id: UUID
    account_id: str
    is_simulated: bool
    reason: str  # e.g. "submit_failed"
    amount: Decimal | None = None
    size_usdt: Decimal | None = None  # transitional alias; mapped to amount
    venue_seq: int | None = None
    event_seq: int | None = None
    occurred_at_ms: int | None = None
    recorded_at_ms: int | None = None
    reservation_ref: ReservationRef | None = None
    is_legacy_uncorrelated: bool = field(default=False, init=False)
    event_id: UUID = field(default_factory=uuid4)
    schema_version: int = field(default=__SCHEMA_VERSION__, init=False, repr=False, compare=False)
    def __post_init__(self) -> None:
        _require_symbol(self)
        _validate_reservation_ref(
            self,
            requires_venue_offer=False,
        )
        _resolve_amount(self)


@dataclass(frozen=True, slots=True)
class ReservationUnknown:
    """Ambiguous submit outcome — the venue may already own the offer.

    This is deliberately distinct from ``ReservationFailed``.  The event keeps
    the intended amount reserved pessimistically until a later full-account
    observation proves an exact venue match or an operator records a
    not-accepted resolution.  It is never an instruction to retry the submit.
    """

    symbol: str  # mandatory, FIRST
    cid: int
    signal_correlation_id: UUID
    account_id: str
    is_simulated: bool
    reason: str
    amount: Decimal | None = None
    size_usdt: Decimal | None = None
    venue_seq: int | None = None
    event_seq: int | None = None
    occurred_at_ms: int | None = None
    recorded_at_ms: int | None = None
    reservation_ref: ReservationRef | None = None
    is_legacy_uncorrelated: bool = field(default=False, init=False)
    event_id: UUID = field(default_factory=uuid4)
    schema_version: int = field(default=__SCHEMA_VERSION__, init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        _require_symbol(self)
        _validate_reservation_ref(self, requires_venue_offer=False)
        _resolve_amount(self)


@dataclass(frozen=True, slots=True)
class VenueOfferQuarantined:
    """Active venue offer with no local submission provenance.

    The full object is already present in ``VenueSnapshotObserved`` and its
    entity projection.  This event is the explicit audit/block breadcrumb; it
    intentionally carries no synthetic CID or reservation reference and never
    triggers an automatic cancel.
    """

    venue_offer_id: str
    symbol: str
    amount: Decimal | None = None
    size_usdt: Decimal | None = None
    account_id: str = ""
    is_simulated: bool = False
    reason: str = "unattributed_active_offer"
    observed_at_ms: int = 0
    event_seq: int | None = None
    occurred_at_ms: int | None = None
    recorded_at_ms: int | None = None
    event_id: UUID = field(default_factory=uuid4)
    schema_version: int = field(default=__SCHEMA_VERSION__, init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        if not self.venue_offer_id:
            raise TypeError("VenueOfferQuarantined requires venue_offer_id")
        _require_symbol(self)
        if not self.account_id:
            raise TypeError("VenueOfferQuarantined requires account_id")
        _resolve_amount(self)
        if self.amount is not None and self.amount < 0:
            raise ValueError("quarantined offer amount must be non-negative")
        if self.occurred_at_ms is None:
            object.__setattr__(self, "occurred_at_ms", self.observed_at_ms)


@dataclass(frozen=True, slots=True)
class SubmitMatchedToVenueOffer:
    """Fresh complete reconcile evidence binds an UNKNOWN attempt to venue ID."""

    symbol: str
    cid: int
    venue_offer_id: str
    signal_correlation_id: UUID
    account_id: str
    is_simulated: bool
    venue_status: str
    matched_mts_created: int
    reconcile_event_seq: int
    amount: Decimal | None = None
    size_usdt: Decimal | None = None
    reservation_ref: ReservationRef | None = None
    is_legacy_uncorrelated: bool = field(default=False, init=False)
    event_seq: int | None = None
    occurred_at_ms: int | None = None
    recorded_at_ms: int | None = None
    event_id: UUID = field(default_factory=uuid4)
    schema_version: int = field(default=__SCHEMA_VERSION__, init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        _require_symbol(self)
        if not self.venue_offer_id:
            raise TypeError("SubmitMatchedToVenueOffer requires venue_offer_id")
        _validate_reservation_ref(self, requires_venue_offer=True)
        _resolve_amount(self)
        if self.reconcile_event_seq < 0:
            raise ValueError("reconcile_event_seq must be non-negative")


@dataclass(frozen=True, slots=True)
class UncertaintyBoundToVenueOffer:
    """Operator resolution binding one UNKNOWN attempt to an exact venue ID.

    This is a resolution fact, not a projection command.  The event writer
    verifies the referenced fresh snapshot and applies the attribution and
    uncertainty transition atomically.  Keeping the operator/evidence fields
    on the event makes the decision reproducible during a clean replay.
    """

    uncertainty_id: UUID
    account_id: str
    environment: str
    symbol: str
    kind: str
    venue_offer_id: str
    reconcile_event_seq: int
    resolved_by_operator_id: str
    resolution_reason: str
    resolution_evidence: Mapping[str, Any] = field(default_factory=dict)
    venue_status: str = "active"
    occurred_at_ms: int | None = None
    recorded_at_ms: int | None = None
    event_seq: int | None = None
    event_id: UUID = field(default_factory=uuid4)
    schema_version: int = field(default=__SCHEMA_VERSION__, init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        _validate_resolution_event(self)
        if not self.venue_offer_id.strip():
            raise TypeError("UncertaintyBoundToVenueOffer requires venue_offer_id")
        object.__setattr__(self, "resolution_evidence", dict(self.resolution_evidence))


@dataclass(frozen=True, slots=True)
class UncertaintyMarkedNotAccepted:
    """Operator resolution confirming an UNKNOWN request was not accepted."""

    uncertainty_id: UUID
    account_id: str
    environment: str
    symbol: str
    kind: str
    reconcile_event_seq: int
    resolved_by_operator_id: str
    resolution_reason: str
    resolution_evidence: Mapping[str, Any] = field(default_factory=dict)
    candidate_count: int = 0
    occurred_at_ms: int | None = None
    recorded_at_ms: int | None = None
    event_seq: int | None = None
    event_id: UUID = field(default_factory=uuid4)
    schema_version: int = field(default=__SCHEMA_VERSION__, init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        _validate_resolution_event(self)
        if self.candidate_count < 0:
            raise ValueError("candidate_count must be non-negative")
        object.__setattr__(self, "resolution_evidence", dict(self.resolution_evidence))


@dataclass(frozen=True, slots=True)
class UncertaintyManuallyResolved:
    """Operator resolution for an orphan/unsupported exposure or exception."""

    uncertainty_id: UUID
    account_id: str
    environment: str
    symbol: str
    kind: str
    reconcile_event_seq: int
    resolved_by_operator_id: str
    resolution_reason: str
    resolution_action: ManualUncertaintyResolutionAction
    resolution_evidence: Mapping[str, Any] = field(default_factory=dict)
    occurred_at_ms: int | None = None
    recorded_at_ms: int | None = None
    event_seq: int | None = None
    event_id: UUID = field(default_factory=uuid4)
    schema_version: int = field(default=__SCHEMA_VERSION__, init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        _validate_resolution_event(self)
        if (
            not isinstance(self.resolution_action, str)
            or self.resolution_action not in MANUAL_UNCERTAINTY_RESOLUTION_ACTIONS
        ):
            raise ValueError("unsupported manual uncertainty resolution_action")
        object.__setattr__(self, "resolution_evidence", dict(self.resolution_evidence))


# Descriptive aliases retained for callers that use the verb from the API
# action name.  They intentionally point at the same frozen event classes so
# serialization has one canonical registry entry per durable event type.
UncertaintyBindToVenueOffer = UncertaintyBoundToVenueOffer
UncertaintyNotAccepted = UncertaintyMarkedNotAccepted
ManualUncertaintyResolution = UncertaintyManuallyResolved


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
    event_seq: int | None = None
    occurred_at_ms: int | None = None
    recorded_at_ms: int | None = None
    reservation_ref: ReservationRef | None = None
    is_legacy_uncorrelated: bool = field(default=False, init=False)
    event_id: UUID = field(default_factory=uuid4)
    schema_version: int = field(default=__SCHEMA_VERSION__, init=False, repr=False, compare=False)
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
    event_seq: int | None = None
    occurred_at_ms: int | None = None
    recorded_at_ms: int | None = None
    reservation_ref: ReservationRef | None = None
    is_legacy_uncorrelated: bool = field(default=False, init=False)
    event_id: UUID = field(default_factory=uuid4)
    schema_version: int = field(default=__SCHEMA_VERSION__, init=False, repr=False, compare=False)
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
    event_seq: int | None = None
    occurred_at_ms: int | None = None
    recorded_at_ms: int | None = None
    reservation_ref: ReservationRef | None = None
    is_legacy_uncorrelated: bool = field(default=False, init=False)
    event_id: UUID = field(default_factory=uuid4)
    schema_version: int = field(default=__SCHEMA_VERSION__, init=False, repr=False, compare=False)
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
    event_seq: int | None = None
    occurred_at_ms: int | None = None  # close time (venue mts_last_payout)
    recorded_at_ms: int | None = None
    event_id: UUID = field(default_factory=uuid4)
    # Present only on events written after the 2026-09-27 parser fix. Without
    # them, rate / period_days / occurred_at_ms are unreliable: rate held the
    # period, period_days held mts_opening, and occurred_at_ms held mts_update,
    # which may equal mts_create on the close frame.
    mts_opening: int | None = None
    mts_last_payout: int | None = None
    schema_version: int = field(default=__SCHEMA_VERSION__, init=False, repr=False, compare=False)

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
    event_seq: int | None = None
    occurred_at_ms: int | None = None
    recorded_at_ms: int | None = None
    event_id: UUID = field(default_factory=uuid4)
    schema_version: int = field(default=__SCHEMA_VERSION__, init=False, repr=False, compare=False)


@dataclass(frozen=True, slots=True)
class PositionReconciled:
    """Derived per-symbol bus signal from a durable full-account observation.

    The event is intentionally not an event-log source of truth.  BootRecovery
    first persists ``VenueSnapshotObserved``; only after that transaction
    commits does it fan out one signal per symbol to the in-memory ledger and
    safety/PnL subscribers.  The durable projection is updated by the account
    writer, never by this bus-only signal.

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
    schema_version: int = field(default=__SCHEMA_VERSION__, init=False, repr=False, compare=False)

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
    event_seq: int | None = None
    occurred_at_ms: int | None = None
    recorded_at_ms: int | None = None
    event_id: UUID = field(default_factory=uuid4)
    schema_version: int = field(default=__SCHEMA_VERSION__, init=False, repr=False, compare=False)
