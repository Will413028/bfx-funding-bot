"""Immutable execution eligibility contracts.

The executor boundary accepts only :class:`ReadyToSubmit`; incomplete or
blocked candidates remain distinct values that cannot be submitted by mistake.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING
from uuid import UUID

from bfx_funding_bot.external.bitfinex.funding_rules import FundingAmountEvidence

if TYPE_CHECKING:
    from bfx_funding_bot.modules.ledger import CapitalAvailable
    from bfx_funding_bot.modules.marketfeed.funding_book import MarketSnapshot

from bfx_funding_bot.modules.strategy import DecisionPayload


class ExecutionPolicy(StrEnum):
    BOOK_GUARDED = "book_guarded"
    OPTIMIZER_SHADOW = "optimizer_shadow"
    OPTIMIZER_LIVE = "optimizer_live"


class DecisionOutcome(StrEnum):
    READY = "ready"
    BLOCKED = "blocked"
    NO_RECOMMENDATION = "no_recommendation"


class BlockReason(StrEnum):
    BOOK_FETCH_FAILED = "book_fetch_failed"
    BOOK_NOT_INITIALIZED = "book_not_initialized"
    BOOK_STALE = "book_stale"
    BOOK_SEQUENCE_INVALID = "book_sequence_invalid"
    BOOK_CHECKSUM_INVALID = "book_checksum_invalid"
    BOOK_VENUE_MAINTENANCE = "book_venue_maintenance"
    PERIOD_NOT_FOUND = "period_not_found"
    INSUFFICIENT_PERIOD_DEPTH = "insufficient_period_depth"
    FILL_MODEL_MISSING = "fill_model_missing"
    FILL_MODEL_LOW_CONFIDENCE = "fill_model_low_confidence"
    FILL_MODEL_SCOPE_MISMATCH = "fill_model_scope_mismatch"
    FILL_MODEL_UNVERSIONED = "fill_model_unversioned"
    OPTIMIZER_UNAVAILABLE = "optimizer_unavailable"
    SAFETY_GUARD_BLOCKED = "safety_guard_blocked"
    EXECUTION_AUDIT_UNAVAILABLE = "execution_audit_unavailable"


@dataclass(frozen=True, slots=True)
class GuardResult:
    allowed: bool
    guard_name: str
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class ReservationRef:
    """Immutable correlation between an audited decision and a venue offer.

    Bitfinex funding submits have no client-id field.  The reference therefore
    remains in internal request context and is bound to the venue offer id only
    after the acknowledgement is received.
    """

    execution_decision_id: str
    signal_correlation_id: UUID
    venue_offer_id: str | None = None

    def __post_init__(self) -> None:
        if not self.execution_decision_id.strip():
            raise ValueError("execution_decision_id must be non-empty")
        if self.venue_offer_id is not None and not self.venue_offer_id.strip():
            raise ValueError("venue_offer_id must be non-empty when bound")

    def bind_venue_offer(self, venue_offer_id: str) -> ReservationRef:
        if not venue_offer_id.strip():
            raise ValueError("venue_offer_id must be non-empty")
        if self.venue_offer_id is None:
            return ReservationRef(
                execution_decision_id=self.execution_decision_id,
                signal_correlation_id=self.signal_correlation_id,
                venue_offer_id=venue_offer_id,
            )
        if self.venue_offer_id != venue_offer_id:
            raise ValueError("reservation reference already bound to another venue offer")
        return self


@dataclass(frozen=True, slots=True)
class ReadyToSubmit:
    decision: DecisionPayload
    decision_id: str
    policy: ExecutionPolicy
    market_snapshot_id: str
    model_version: str | None
    evidence: Mapping[str, object]
    safety: GuardResult
    capital_view: CapitalAvailable | None = None
    market_snapshot: MarketSnapshot | None = None
    funding_amount_evidence: FundingAmountEvidence | None = None

    def __post_init__(self) -> None:
        if not self.decision_id.strip():
            raise ValueError("decision_id must be non-empty")

    @property
    def outcome(self) -> DecisionOutcome:
        return DecisionOutcome.READY

    def book_valid_at(self, now_ms: int) -> bool:
        snapshot = self.market_snapshot
        return (
            snapshot is not None
            and snapshot.snapshot_id == self.market_snapshot_id
            and snapshot.max_age_ms is not None
            and snapshot.max_age_ms > 0
            and snapshot.is_fresh(symbol=self.decision.symbol, now_ms=now_ms,
                                  max_age_ms=snapshot.max_age_ms)
        )


@dataclass(frozen=True, slots=True)
class BlockedExecution:
    decision_id: str
    candidate: DecisionPayload
    reason: BlockReason
    failed_dependency: str
    evidence: Mapping[str, object]

    @property
    def outcome(self) -> DecisionOutcome:
        return DecisionOutcome.BLOCKED


@dataclass(frozen=True, slots=True)
class NoRecommendation:
    decision_id: str
    candidate: DecisionPayload | None
    reason: BlockReason
    evidence: Mapping[str, object]

    @property
    def outcome(self) -> DecisionOutcome:
        return DecisionOutcome.NO_RECOMMENDATION
