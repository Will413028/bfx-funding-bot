"""Fail-closed audit gate between deployment candidates and the executor."""
from __future__ import annotations

import logging
import time
from collections.abc import Mapping
from decimal import Decimal
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
        blocked = self._blocked_dependency(
            candidate=candidate,
            decision_id=decision_id,
            snapshot=snapshot,
            price=price,
            fill_evidence=fill_evidence,
            safety=safety,
        )
        if blocked is not None:
            return await self._audit_blocked(
                blocked=blocked,
                reconcile_id=reconcile_id,
                snapshot=snapshot,
                fill_evidence=fill_evidence,
                safety=safety,
                audit_context=audit_context,
            )

        assert isinstance(price, PriceDecision)
        assert snapshot is not None
        applied_candidate = candidate.model_copy(update={"offer_rate": float(price.rate)})
        ready = ReadyToSubmit(
            decision=applied_candidate,
            decision_id=decision_id,
            policy=self._policy,
            market_snapshot_id=snapshot.snapshot_id,
            model_version=_model_value(fill_evidence, "model_version"),
            evidence={"price": dict(price.evidence), "branch": price.branch.value},
            safety=safety,
        )
        audit_decision = self._audit_decision(
            candidate=candidate,
            decision_id=decision_id,
            reconcile_id=reconcile_id,
            outcome=DecisionOutcome.READY,
            reason=None,
            dependency=None,
            snapshot=snapshot,
            price=price,
            fill_evidence=fill_evidence,
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
        self._readiness.set_ready()
        return ready

    def _blocked_dependency(
        self,
        *,
        candidate: DecisionPayload,
        decision_id: str,
        snapshot: MarketSnapshot | None,
        price: PriceDecision | BlockedExecution,
        fill_evidence: object | None,
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
            unavailable_reason = _unavailable_fill_reason(fill_evidence)
            if unavailable_reason is not None:
                return _blocked(
                    decision_id,
                    candidate,
                    unavailable_reason,
                    "fill_model",
                    _fill_evidence(fill_evidence),
                )
        return None

    async def _audit_blocked(
        self,
        *,
        blocked: BlockedExecution,
        reconcile_id: str,
        snapshot: MarketSnapshot | None,
        fill_evidence: object | None,
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
        fill_evidence: object | None,
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
            model_version=_model_value(fill_evidence, "model_version"),
            model_hash=_model_value(fill_evidence, "artifact_hash"),
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


def _model_value(fill_evidence: object | None, name: str) -> str | None:
    value = getattr(fill_evidence, name, None)
    return str(value) if value is not None else None


def _fill_evidence(fill_evidence: object | None) -> Mapping[str, object]:
    if fill_evidence is None:
        return {}
    values = getattr(fill_evidence, "__dict__", None)
    if isinstance(values, dict):
        return {key: value for key, value in values.items() if _json_scalar(value)}
    reason = getattr(fill_evidence, "reason", None)
    return {"reason": str(reason)} if reason is not None else {}


def _json_scalar(value: object) -> bool:
    return isinstance(value, str | int | float | bool) or value is None


def _unavailable_fill_reason(fill_evidence: object | None) -> BlockReason | None:
    if fill_evidence is None:
        return BlockReason.FILL_MODEL_MISSING
    reason = getattr(fill_evidence, "reason", None)
    if reason == "low_confidence":
        return BlockReason.FILL_MODEL_LOW_CONFIDENCE
    if reason is not None:
        return BlockReason.FILL_MODEL_MISSING
    return None


def _required_decimal(value: float | None, name: str) -> Decimal:
    if value is None:
        raise ValueError(f"POST candidate requires {name}")
    return Decimal(str(value))
