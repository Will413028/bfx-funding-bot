"""Immutable execution eligibility contracts.

The executor boundary accepts only :class:`ReadyToSubmit`; incomplete or
blocked candidates remain distinct values that cannot be submitted by mistake.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum

from bfx_funding_bot.modules.marketfeed.schemas import DecisionPayload


class ExecutionPolicy(StrEnum):
    PAPER = "paper"
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
    PERIOD_NOT_FOUND = "period_not_found"
    INSUFFICIENT_PERIOD_DEPTH = "insufficient_period_depth"
    FILL_MODEL_MISSING = "fill_model_missing"
    FILL_MODEL_LOW_CONFIDENCE = "fill_model_low_confidence"
    OPTIMIZER_UNAVAILABLE = "optimizer_unavailable"
    SAFETY_GUARD_BLOCKED = "safety_guard_blocked"
    EXECUTION_AUDIT_UNAVAILABLE = "execution_audit_unavailable"


@dataclass(frozen=True, slots=True)
class GuardResult:
    allowed: bool
    guard_name: str
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class ReadyToSubmit:
    decision: DecisionPayload
    decision_id: str
    policy: ExecutionPolicy
    market_snapshot_id: str
    model_version: str | None
    evidence: Mapping[str, object]
    safety: GuardResult

    @property
    def outcome(self) -> DecisionOutcome:
        return DecisionOutcome.READY


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
