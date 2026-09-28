"""Fail-closed audit gate between deployment candidates and the executor."""

from __future__ import annotations

import logging
import time
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from math import isfinite
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
from bfx_funding_bot.modules.strategy import DecisionPayload

log = logging.getLogger(__name__)


class _TradingReadiness(Protocol):
    def set_ready(self) -> None: ...
    def set_blocked(self, reason: BlockReason, dependency: str) -> None: ...


class _ExecutionEvents(Protocol):
    async def emit_execution_event(
        self,
        event_name: str,
        *,
        level: str,
        decision_id: str,
        reconcile_id: str,
        symbol: str,
        cell: str,
        policy: str,
        outcome: str,
        reason_code: str | None,
        evidence: dict[str, object],
    ) -> None: ...


class _ExecutionMetrics(Protocol):
    def observe_execution_decision(
        self,
        *,
        outcome: str,
        reason: str,
        policy: str,
    ) -> None: ...
    def observe_audit_persist_failure(self) -> None: ...
    def observe_execution_gate_duration(self, *, seconds: float) -> None: ...
    def observe_book_snapshot(self, *, result: str) -> None: ...
    def observe_book_snapshot_age(self, *, age_seconds: float) -> None: ...


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



# Reasons a caller's own book lookup may report; anything else is not a book
# fault and must not pass through this path.
_BOOK_BLOCK_REASONS = frozenset(
    {
        BlockReason.BOOK_NOT_INITIALIZED,
        BlockReason.BOOK_SEQUENCE_INVALID,
        BlockReason.BOOK_CHECKSUM_INVALID,
        BlockReason.BOOK_STALE,
        BlockReason.BOOK_VENUE_MAINTENANCE,
    }
)

class ExecutionGate:
    """Audit every candidate and release only a committed READY decision."""

    def __init__(
        self,
        *,
        policy: ExecutionPolicy,
        audit: ExecutionDecisionRecorder,
        readiness: _TradingReadiness,
        events: _ExecutionEvents | None = None,
        metrics: _ExecutionMetrics | None = None,
    ) -> None:
        self._policy = policy
        self._audit = audit
        self._readiness = readiness
        self._events = events
        self._metrics = metrics

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
        optimizer_evidence: Mapping[str, object] | None = None,
        optimizer_block_reason: BlockReason | None = None,
        expected_period_agg: str | None = None,
        expected_horizon_h: int | None = None,
        expected_model_version: str | None = None,
        expected_artifact_hash: str | None = None,
    ) -> ReadyToSubmit | BlockedExecution | NoRecommendation:
        started = time.perf_counter()
        try:
            return await self._prepare(
                candidate,
                decision_id=decision_id,
                reconcile_id=reconcile_id,
                snapshot=snapshot,
                price=price,
                fill_evidence=fill_evidence,
                safety=safety,
                audit_context=audit_context,
                optimizer_evidence=optimizer_evidence,
                optimizer_block_reason=optimizer_block_reason,
                expected_period_agg=expected_period_agg,
                expected_horizon_h=expected_horizon_h,
                expected_model_version=expected_model_version,
                expected_artifact_hash=expected_artifact_hash,
            )
        finally:
            if self._metrics is not None:
                try:
                    self._metrics.observe_execution_gate_duration(
                        seconds=time.perf_counter() - started,
                    )
                except Exception:
                    log.debug("execution_gate_duration_metric_failed", exc_info=True)

    async def _prepare(
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
        optimizer_evidence: Mapping[str, object] | None,
        optimizer_block_reason: BlockReason | None,
        expected_period_agg: str | None,
        expected_horizon_h: int | None,
        expected_model_version: str | None,
        expected_artifact_hash: str | None,
    ) -> ReadyToSubmit | BlockedExecution | NoRecommendation:
        normalized_fill_evidence = _normalize_fill_evidence(
            fill_evidence,
            candidate_symbol=candidate.symbol,
            expected_period_agg=expected_period_agg,
            expected_horizon_h=expected_horizon_h,
            expected_model_version=expected_model_version,
            expected_artifact_hash=expected_artifact_hash,
        )
        if self._metrics is not None:
            try:
                if snapshot is None:
                    self._metrics.observe_book_snapshot(result="unavailable")
                else:
                    self._metrics.observe_book_snapshot(result="valid")
                    self._metrics.observe_book_snapshot_age(
                        age_seconds=max((time.time() * 1_000 - snapshot.captured_at_ms) / 1_000, 0),
                    )
            except Exception:
                log.debug("funding_book_metric_failed", exc_info=True)
        blocked = self._blocked_dependency(
            candidate=candidate,
            decision_id=decision_id,
            snapshot=snapshot,
            price=price,
            fill_evidence=normalized_fill_evidence,
            safety=safety,
            optimizer_block_reason=optimizer_block_reason,
        )
        if blocked is not None:
            result = await self._audit_blocked(
                blocked=blocked,
                reconcile_id=reconcile_id,
                snapshot=snapshot,
                fill_evidence=normalized_fill_evidence,
                safety=safety,
                audit_context=audit_context,
                optimizer_evidence=optimizer_evidence,
            )
            await self._observe_result(result, reconcile_id, audit_context)
            return result

        assert isinstance(price, PriceDecision)
        assert snapshot is not None
        applied_candidate = candidate.model_copy(update={"offer_rate": price.rate})
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
            optimizer_evidence=optimizer_evidence,
        )
        try:
            await self._audit.record(audit_decision)
        except ExecutionAuditUnavailable:
            log.exception("execution_audit_unavailable decision_id=%s", decision_id)
            if self._metrics is not None:
                try:
                    self._metrics.observe_audit_persist_failure()
                except Exception:
                    log.debug("execution_audit_failure_metric_failed", exc_info=True)
            self._readiness.set_blocked(BlockReason.EXECUTION_AUDIT_UNAVAILABLE, "audit")
            result = BlockedExecution(
                decision_id=decision_id,
                candidate=candidate,
                reason=BlockReason.EXECUTION_AUDIT_UNAVAILABLE,
                failed_dependency="audit",
                evidence={"policy": self._policy.value},
            )
            await self._observe_result(result, reconcile_id, audit_context)
            return result
        ready = ReadyToSubmit(
            decision=applied_candidate,
            decision_id=decision_id,
            policy=self._policy,
            market_snapshot_id=snapshot.snapshot_id,
            market_snapshot=snapshot,
            model_version=_model_value(normalized_fill_evidence),
            evidence={"price": dict(price.evidence), "branch": price.branch.value},
            safety=safety,
        )
        self._readiness.set_ready()
        await self._observe_result(ready, reconcile_id, audit_context)
        return ready

    async def _observe_result(
        self,
        result: ReadyToSubmit | BlockedExecution | NoRecommendation,
        reconcile_id: str,
        context: AuditContext,
    ) -> None:
        reason = result.reason.value if not isinstance(result, ReadyToSubmit) else "none"
        if self._metrics is not None:
            try:
                self._metrics.observe_execution_decision(
                    outcome=result.outcome.value,
                    reason=reason,
                    policy=self._policy.value,
                )
            except Exception:
                log.debug("execution_decision_metric_failed", exc_info=True)
        events = self._events
        if events is None:
            return
        evidence: dict[str, object] = {}
        if isinstance(result, BlockedExecution):
            evidence["dependency"] = result.failed_dependency
        elif isinstance(result, ReadyToSubmit):
            evidence["snapshot_id"] = result.market_snapshot_id
            branch = result.evidence.get("branch")
            if isinstance(branch, str):
                evidence["branch"] = branch
        try:

            async def emit(event_name: str, level: str) -> None:
                await events.emit_execution_event(
                    event_name,
                    level=level,
                    decision_id=result.decision_id,
                    reconcile_id=reconcile_id,
                    symbol=context.symbol,
                    cell=context.cell_id,
                    policy=self._policy.value,
                    outcome=result.outcome.value,
                    reason_code=None if reason == "none" else reason,
                    evidence=evidence,
                )

            await emit(
                "funding.execution.eligibility",
                "info" if isinstance(result, ReadyToSubmit) else "warn",
            )
            if isinstance(result, BlockedExecution):
                await emit("funding.execution.blocked", "warn")
                if result.reason in {
                    BlockReason.BOOK_FETCH_FAILED,
                    BlockReason.BOOK_NOT_INITIALIZED,
                    BlockReason.BOOK_STALE,
                    BlockReason.BOOK_SEQUENCE_INVALID,
                    BlockReason.BOOK_CHECKSUM_INVALID,
                }:
                    await emit("funding.book.snapshot_invalid", "warn")
                if result.reason in {
                    BlockReason.FILL_MODEL_MISSING,
                    BlockReason.FILL_MODEL_LOW_CONFIDENCE,
                    BlockReason.FILL_MODEL_SCOPE_MISMATCH,
                    BlockReason.FILL_MODEL_UNVERSIONED,
                }:
                    await emit("funding.fill_model.unavailable", "warn")
        except Exception:
            log.debug("execution_event_emit_failed", exc_info=True)

    def _blocked_dependency(
        self,
        *,
        candidate: DecisionPayload,
        decision_id: str,
        snapshot: MarketSnapshot | None,
        price: PriceDecision | BlockedExecution,
        fill_evidence: _NormalizedFillEvidence | BlockReason | None,
        safety: GuardResult,
        optimizer_block_reason: BlockReason | None,
    ) -> BlockedExecution | None:
        if not safety.allowed:
            return _blocked(
                decision_id,
                candidate,
                BlockReason.SAFETY_GUARD_BLOCKED,
                safety.guard_name,
                {"guard_reason": safety.reason},
            )
        if snapshot is None:
            # The caller that looked the book up knows which fault withheld it;
            # a single collapsed "stale" here would throw that away and leave
            # the operator unable to tell a book that never qualified from one
            # that aged out.
            if isinstance(price, BlockedExecution) and price.reason in _BOOK_BLOCK_REASONS:
                return _blocked(
                    decision_id,
                    candidate,
                    price.reason,
                    price.failed_dependency,
                    price.evidence,
                )
            return _blocked(
                decision_id,
                candidate,
                BlockReason.BOOK_STALE,
                "market_snapshot",
                {},
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
                decision_id,
                candidate,
                BlockReason.BOOK_SEQUENCE_INVALID,
                "market_snapshot",
                {},
            )
        if not snapshot.checksum_valid:
            return _blocked(
                decision_id,
                candidate,
                BlockReason.BOOK_CHECKSUM_INVALID,
                "market_snapshot",
                {},
            )
        if isinstance(price, BlockedExecution):
            return _blocked(
                decision_id,
                candidate,
                price.reason,
                price.failed_dependency,
                price.evidence,
            )
        if self._policy is ExecutionPolicy.OPTIMIZER_LIVE:
            if optimizer_block_reason is not None:
                return _blocked(
                    decision_id,
                    candidate,
                    optimizer_block_reason,
                    "optimizer",
                    {"optimizer_outcome": "no_recommendation"},
                )
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
        optimizer_evidence: Mapping[str, object] | None,
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
            optimizer_evidence=optimizer_evidence,
        )
        try:
            await self._audit.record(audit_decision)
        except ExecutionAuditUnavailable:
            log.exception("execution_audit_unavailable decision_id=%s", blocked.decision_id)
            if self._metrics is not None:
                try:
                    self._metrics.observe_audit_persist_failure()
                except Exception:
                    log.debug("execution_audit_failure_metric_failed", exc_info=True)
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
        optimizer_evidence: Mapping[str, object] | None,
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
            strategy=audit_context.strategy,
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
            model_evidence=_model_evidence(fill_evidence, optimizer_evidence),
            safety_result={
                "allowed": safety.allowed,
                "guard_name": safety.guard_name,
                "reason": safety.reason,
                "book_validity": None if snapshot is None else {
                    "snapshot_id": snapshot.snapshot_id, "symbol": snapshot.symbol,
                    "captured_at_ms": snapshot.captured_at_ms, "max_age_ms": snapshot.max_age_ms,
                    "sequence_valid": snapshot.sequence_valid, "checksum_valid": snapshot.checksum_valid,
                },
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


def _model_evidence(
    fill_evidence: _NormalizedFillEvidence | BlockReason | None,
    optimizer_evidence: Mapping[str, object] | None,
) -> Mapping[str, object]:
    evidence = dict(_fill_evidence(fill_evidence))
    if optimizer_evidence is not None:
        evidence["optimizer"] = _copy_evidence(optimizer_evidence)
    return evidence


def _copy_evidence(value: object) -> object:
    if isinstance(value, Mapping):
        copied: dict[str, object] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise TypeError("audit evidence keys must be strings")
            copied[key] = _copy_evidence(item)
        return copied
    if isinstance(value, tuple | list):
        return [_copy_evidence(item) for item in value]
    if value is None or isinstance(value, bool | int | str):
        return value
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise TypeError("audit evidence decimals must be finite")
        return str(value)
    if isinstance(value, float):
        if not isfinite(value):
            raise TypeError("audit evidence floats must be finite")
        return value
    raise TypeError(f"unsupported audit evidence value: {type(value).__name__}")


def _normalize_fill_evidence(
    fill_evidence: object | None,
    *,
    candidate_symbol: str,
    expected_period_agg: str | None = None,
    expected_horizon_h: int | None = None,
    expected_model_version: str | None = None,
    expected_artifact_hash: str | None = None,
) -> _NormalizedFillEvidence | BlockReason | None:
    """Accept only the narrow future fill-model seam required for live optimizer use."""
    if fill_evidence is None:
        return None

    reason = getattr(fill_evidence, "reason", None)
    if isinstance(reason, str):
        return _fill_model_block_reason(reason)

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
    if (
        expected_period_agg is not None and period_agg != expected_period_agg
    ) or (
        expected_horizon_h is not None and horizon_h != expected_horizon_h
    ) or (
        expected_model_version is not None and model_version != expected_model_version
    ) or (
        expected_artifact_hash is not None and artifact_hash != expected_artifact_hash
    ):
        return BlockReason.FILL_MODEL_SCOPE_MISMATCH
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


def _fill_model_block_reason(reason: str) -> BlockReason:
    return {
        "missing": BlockReason.FILL_MODEL_MISSING,
        "low_confidence": BlockReason.FILL_MODEL_LOW_CONFIDENCE,
        "scope_mismatch": BlockReason.FILL_MODEL_SCOPE_MISMATCH,
        "unversioned": BlockReason.FILL_MODEL_UNVERSIONED,
    }.get(reason, BlockReason.FILL_MODEL_MISSING)


def _required_decimal(value: Decimal | None, name: str) -> Decimal:
    if value is None:
        raise ValueError(f"POST candidate requires {name}")
    return value
