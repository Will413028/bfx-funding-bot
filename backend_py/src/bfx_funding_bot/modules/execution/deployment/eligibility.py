"""Fail-closed audit gate between deployment candidates and the executor."""
from __future__ import annotations

import logging
import time
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Protocol

from bfx_funding_bot.modules.execution.audit import (
    AuditContext,
    ExecutionAuditUnavailable,
    ExecutionDecision,
    ExecutionDecisionRecorder,
)
from bfx_funding_bot.modules.execution.contracts import (
    BlockedExecution,
    BlockReason,
    DecisionOutcome,
    ExecutionPolicy,
    GuardResult,
    NoRecommendation,
    ReadyToSubmit,
)
from bfx_funding_bot.modules.execution.deployment.period_pricing import PriceDecision
from bfx_funding_bot.modules.marketfeed.funding_book import MarketSnapshot
from bfx_funding_bot.modules.marketfeed.schemas import DecisionPayload

log = logging.getLogger(__name__)


class _TradingReadiness(Protocol):
    def set_ready(self) -> None: ...
    def set_blocked(self, reason: BlockReason, dependency: str) -> None: ...


@dataclass(frozen=True, slots=True)
class _NormalizedFillEvidence:
    model_version: str
    artifact_hash: str
    fill_prob: Decimal
    expected_ttf_ms: int | None
    n_samples: int
    symbol: str
    period_agg: str
    horizon_h: int
    cutoff_ms: int


class ExecutionGate:
    """Audit every candidate and release only a committed READY decision."""

    def __init__(
        self,
        *,
        policy: ExecutionPolicy,
        audit: ExecutionDecisionRecorder,
        readiness: _TradingReadiness,
    ) -> None:
        self._policy = policy
        self._audit = audit
        self._readiness = readiness

    @property
    def policy(self) -> ExecutionPolicy:
        return self._policy

    async def prepare(
        self,
        candidate: DecisionPayload,
        *,
        decision_id: str,
        reconcile_id: str,
        snapshot: MarketSnapshot | None,
        price: PriceDecision | BlockedExecution,
        fill_evidence: object | None,
        safety: GuardResult,
        audit_context: AuditContext,
    ) -> ReadyToSubmit | BlockedExecution | NoRecommendation:
        normalized_fill_evidence = _normalize_fill_evidence(
            fill_evidence,
            candidate_symbol=candidate.symbol,
        )
        blocked = self._blocked_dependency(
            candidate=candidate,
            decision_id=decision_id,
            snapshot=snapshot,
            price=price,
            fill_evidence=normalized_fill_evidence,
            safety=safety,
        )
        if blocked is not None:
            return await self._audit_blocked(
                blocked=blocked,
                reconcile_id=reconcile_id,
                snapshot=snapshot,
                fill_evidence=normalized_fill_evidence,
                safety=safety,
                audit_context=audit_context,
            )

        assert isinstance(price, PriceDecision)
        assert snapshot is not None
        applied_candidate = candidate.model_copy(update={"offer_rate": float(price.rate)})
        audit_decision = self._audit_decision(
            candidate=candidate,
            decision_id=decision_id,
            reconcile_id=reconcile_id,
            outcome=DecisionOutcome.READY,
            reason=None,
            dependency=None,
            snapshot=snapshot,
            price=price,
            fill_evidence=normalized_fill_evidence,
            safety=safety,
            audit_context=audit_context,
        )
        try:
            await self._audit.record(audit_decision)
        except ExecutionAuditUnavailable:
            log.exception("execution_audit_unavailable decision_id=%s", decision_id)
            self._readiness.set_blocked(BlockReason.EXECUTION_AUDIT_UNAVAILABLE, "audit")
            return BlockedExecution(
                decision_id=decision_id,
                candidate=candidate,
                reason=BlockReason.EXECUTION_AUDIT_UNAVAILABLE,
                failed_dependency="audit",
                evidence={"policy": self._policy.value},
            )
        ready = ReadyToSubmit(
            decision=applied_candidate,
            decision_id=decision_id,
            policy=self._policy,
            market_snapshot_id=snapshot.snapshot_id,
            model_version=_model_value(normalized_fill_evidence),
            evidence={"price": dict(price.evidence), "branch": price.branch.value},
            safety=safety,
        )
        self._readiness.set_ready()
        return ready

    def _blocked_dependency(
        self,
        *,
        candidate: DecisionPayload,
        decision_id: str,
        snapshot: MarketSnapshot | None,
        price: PriceDecision | BlockedExecution,
        fill_evidence: _NormalizedFillEvidence | BlockReason | None,
        safety: GuardResult,
    ) -> BlockedExecution | None:
        if not safety.allowed:
            return _blocked(
                decision_id, candidate, BlockReason.SAFETY_GUARD_BLOCKED, safety.guard_name,
                {"guard_reason": safety.reason},
            )
        if snapshot is None:
            return _blocked(
                decision_id, candidate, BlockReason.BOOK_STALE, "market_snapshot", {},
            )
        if snapshot.symbol != candidate.symbol:
            return _blocked(
                decision_id,
                candidate,
                BlockReason.BOOK_STALE,
                "market_snapshot_symbol",
                {
                    "expected_symbol": candidate.symbol,
                    "actual_symbol": snapshot.symbol,
                },
            )
        if not snapshot.sequence_valid:
            return _blocked(
                decision_id, candidate, BlockReason.BOOK_SEQUENCE_INVALID, "market_snapshot", {},
            )
        if not snapshot.checksum_valid:
            return _blocked(
                decision_id, candidate, BlockReason.BOOK_CHECKSUM_INVALID, "market_snapshot", {},
            )
        if isinstance(price, BlockedExecution):
            return _blocked(
                decision_id, candidate, price.reason, price.failed_dependency, price.evidence,
            )
        if self._policy is ExecutionPolicy.OPTIMIZER_LIVE:
            if isinstance(fill_evidence, BlockReason):
                return _blocked(
                    decision_id,
                    candidate,
                    fill_evidence,
                    "fill_model",
                    _fill_evidence(fill_evidence),
                )
            if fill_evidence is None:
                return _blocked(
                    decision_id,
                    candidate,
                    BlockReason.FILL_MODEL_MISSING,
                    "fill_model",
                    {},
                )
        return None

    async def _audit_blocked(
        self,
        *,
        blocked: BlockedExecution,
        reconcile_id: str,
        snapshot: MarketSnapshot | None,
        fill_evidence: _NormalizedFillEvidence | BlockReason | None,
        safety: GuardResult,
        audit_context: AuditContext,
    ) -> BlockedExecution:
        audit_decision = self._audit_decision(
            candidate=blocked.candidate,
            decision_id=blocked.decision_id,
            reconcile_id=reconcile_id,
            outcome=DecisionOutcome.BLOCKED,
            reason=blocked.reason,
            dependency=blocked.failed_dependency,
            snapshot=snapshot,
            price=None,
            fill_evidence=fill_evidence,
            safety=safety,
            audit_context=audit_context,
        )
        try:
            await self._audit.record(audit_decision)
        except ExecutionAuditUnavailable:
            log.exception("execution_audit_unavailable decision_id=%s", blocked.decision_id)
            self._readiness.set_blocked(BlockReason.EXECUTION_AUDIT_UNAVAILABLE, "audit")
            return _blocked(
                blocked.decision_id,
                blocked.candidate,
                BlockReason.EXECUTION_AUDIT_UNAVAILABLE,
                "audit",
                {"original_reason": blocked.reason.value},
            )
        self._readiness.set_blocked(blocked.reason, blocked.failed_dependency)
        return blocked

    def _audit_decision(
        self,
        *,
        candidate: DecisionPayload,
        decision_id: str,
        reconcile_id: str,
        outcome: DecisionOutcome,
        reason: BlockReason | None,
        dependency: str | None,
        snapshot: MarketSnapshot | None,
        price: PriceDecision | None,
        fill_evidence: _NormalizedFillEvidence | BlockReason | None,
        safety: GuardResult,
        audit_context: AuditContext,
    ) -> ExecutionDecision:
        now_ms = int(time.time() * 1_000)
        signal_rate = _required_decimal(candidate.offer_rate, "offer_rate")
        amount = _required_decimal(candidate.offer_amount_usdt, "offer_amount_usdt")
        duration = candidate.offer_duration_days
        if duration is None:
            raise ValueError("POST candidate requires offer_duration_days")
        return ExecutionDecision(
            decision_id=decision_id,
            account_id=audit_context.account_id,
            deployment_environment=audit_context.deployment_environment,
            reconcile_id=reconcile_id,
            cell_id=audit_context.cell_id,
            symbol=audit_context.symbol,
            signal_correlation_id=audit_context.signal_correlation_id,
            outcome=outcome,
            reason_code=reason,
            failed_dependency=dependency,
            signal_rate=signal_rate,
            applied_rate=price.rate if price is not None else None,
            amount_usdt=amount,
            duration_days=duration,
            snapshot_id=snapshot.snapshot_id if snapshot is not None else None,
            snapshot_hash=snapshot.snapshot_id if snapshot is not None else None,
            snapshot_captured_at_ms=snapshot.captured_at_ms if snapshot is not None else None,
            snapshot_source=snapshot.source if snapshot is not None else None,
            snapshot_age_ms=None,
            model_version=_model_value(fill_evidence),
            model_hash=_model_hash(fill_evidence),
            model_evidence=_fill_evidence(fill_evidence),
            safety_result={
                "allowed": safety.allowed,
                "guard_name": safety.guard_name,
                "reason": safety.reason,
            },
            execution_policy=self._policy,
            service_version=audit_context.service_version,
            config_hash=audit_context.config_hash,
            occurred_at_ms=now_ms,
            recorded_at_ms=now_ms,
        )


def _blocked(
    decision_id: str,
    candidate: DecisionPayload,
    reason: BlockReason,
    dependency: str,
    evidence: Mapping[str, object],
) -> BlockedExecution:
    return BlockedExecution(
        decision_id=decision_id,
        candidate=candidate,
        reason=reason,
        failed_dependency=dependency,
        evidence=dict(evidence),
    )


def _model_value(fill_evidence: _NormalizedFillEvidence | BlockReason | None) -> str | None:
    if isinstance(fill_evidence, _NormalizedFillEvidence):
        return fill_evidence.model_version
    return None


def _model_hash(fill_evidence: _NormalizedFillEvidence | BlockReason | None) -> str | None:
    if isinstance(fill_evidence, _NormalizedFillEvidence):
        return fill_evidence.artifact_hash
    return None


def _fill_evidence(
    fill_evidence: _NormalizedFillEvidence | BlockReason | None,
) -> Mapping[str, object]:
    if isinstance(fill_evidence, _NormalizedFillEvidence):
        return {
            "fill_prob": str(fill_evidence.fill_prob),
            "expected_ttf_ms": fill_evidence.expected_ttf_ms,
            "n_samples": fill_evidence.n_samples,
            "symbol": fill_evidence.symbol,
            "period_agg": fill_evidence.period_agg,
            "horizon_h": fill_evidence.horizon_h,
            "cutoff_ms": fill_evidence.cutoff_ms,
        }
    if isinstance(fill_evidence, BlockReason):
        return {"unavailable_reason": fill_evidence.value}
    if fill_evidence is None:
        return {}
    raise AssertionError("unreachable")


def _normalize_fill_evidence(
    fill_evidence: object | None,
    *,
    candidate_symbol: str,
) -> _NormalizedFillEvidence | BlockReason | None:
    """Accept only the narrow future fill-model seam required for live optimizer use."""
    if fill_evidence is None:
        return None

    reason = getattr(fill_evidence, "reason", None)
    if isinstance(reason, str):
        if reason == "low_confidence":
            return BlockReason.FILL_MODEL_LOW_CONFIDENCE
        return BlockReason.FILL_MODEL_MISSING

    low_confidence = getattr(fill_evidence, "low_confidence", None)
    if low_confidence is True:
        return BlockReason.FILL_MODEL_LOW_CONFIDENCE
    if low_confidence is not None and low_confidence is not False:
        return BlockReason.FILL_MODEL_MISSING

    model_version = getattr(fill_evidence, "model_version", None)
    artifact_hash = getattr(fill_evidence, "artifact_hash", None)
    fill_prob = getattr(fill_evidence, "fill_prob", None)
    expected_ttf_ms = getattr(fill_evidence, "expected_ttf_ms", None)
    n_samples = getattr(fill_evidence, "n_samples", None)
    symbol = getattr(fill_evidence, "symbol", None)
    period_agg = getattr(fill_evidence, "period_agg", None)
    horizon_h = getattr(fill_evidence, "horizon_h", None)
    cutoff_ms = getattr(fill_evidence, "cutoff_ms", None)
    if (
        not isinstance(model_version, str)
        or not model_version
        or not isinstance(artifact_hash, str)
        or not artifact_hash
        or isinstance(fill_prob, bool)
        or not isinstance(fill_prob, int | float | Decimal)
        or isinstance(expected_ttf_ms, bool)
        or not isinstance(expected_ttf_ms, int | None)
        or isinstance(n_samples, bool)
        or not isinstance(n_samples, int)
        or n_samples < 0
        or not isinstance(symbol, str)
        or not symbol
        or symbol != candidate_symbol
        or not isinstance(period_agg, str)
        or not period_agg
        or isinstance(horizon_h, bool)
        or not isinstance(horizon_h, int)
        or horizon_h <= 0
        or isinstance(cutoff_ms, bool)
        or not isinstance(cutoff_ms, int)
        or cutoff_ms < 0
        or (expected_ttf_ms is not None and expected_ttf_ms < 0)
    ):
        return BlockReason.FILL_MODEL_MISSING
    try:
        normalized_fill_prob = Decimal(str(fill_prob))
    except (InvalidOperation, ValueError):
        return BlockReason.FILL_MODEL_MISSING
    if (
        not normalized_fill_prob.is_finite()
        or normalized_fill_prob < Decimal("0")
        or normalized_fill_prob > Decimal("1")
    ):
        return BlockReason.FILL_MODEL_MISSING
    return _NormalizedFillEvidence(
        model_version=model_version,
        artifact_hash=artifact_hash,
        fill_prob=normalized_fill_prob,
        expected_ttf_ms=expected_ttf_ms,
        n_samples=n_samples,
        symbol=symbol,
        period_agg=period_agg,
        horizon_h=horizon_h,
        cutoff_ms=cutoff_ms,
    )


def _required_decimal(value: float | None, name: str) -> Decimal:
    if value is None:
        raise ValueError(f"POST candidate requires {name}")
    return Decimal(str(value))
