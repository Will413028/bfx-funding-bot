"""Immutable values for the durable pre-trade execution audit."""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal

from bfx_funding_bot.modules.accounts.exchange_accounts import account_id_uuid_or_none
from bfx_funding_bot.modules.execution.contracts import (
    BlockReason,
    DecisionOutcome,
    ExecutionPolicy,
)


@dataclass(frozen=True, slots=True)
class AuditContext:
    """Stable execution identity supplied by the gate for every audit row."""

    account_id: str
    deployment_environment: str
    reconcile_id: str
    cell_id: str
    symbol: str
    signal_correlation_id: str
    service_version: str
    config_hash: str


@dataclass(frozen=True, slots=True)
class ExecutionDecision:
    """All evidence required to reconstruct one pre-trade decision."""

    decision_id: str
    account_id: str
    deployment_environment: str
    reconcile_id: str
    cell_id: str
    symbol: str
    signal_correlation_id: str
    outcome: DecisionOutcome
    reason_code: BlockReason | None
    failed_dependency: str | None
    signal_rate: Decimal
    applied_rate: Decimal | None
    amount_usdt: Decimal
    duration_days: int
    snapshot_id: str | None
    snapshot_hash: str | None
    snapshot_captured_at_ms: int | None
    snapshot_source: str | None
    snapshot_age_ms: int | None
    model_version: str | None
    model_hash: str | None
    model_evidence: Mapping[str, object]
    safety_result: Mapping[str, object]
    execution_policy: ExecutionPolicy
    service_version: str
    config_hash: str
    occurred_at_ms: int
    recorded_at_ms: int

    def persistence_values(self) -> dict[str, object]:
        """Return primitive, stable values for the append-only SQL row."""
        return {
            "decision_id": self.decision_id,
            "account_id": self.account_id,
            "exchange_account_id": account_id_uuid_or_none(self.account_id),
            "deployment_environment": self.deployment_environment,
            "reconcile_id": self.reconcile_id,
            "cell_id": self.cell_id,
            "symbol": self.symbol,
            "signal_correlation_id": self.signal_correlation_id,
            "outcome": self.outcome.value,
            "reason_code": self.reason_code.value if self.reason_code is not None else None,
            "failed_dependency": self.failed_dependency,
            "signal_rate": self.signal_rate,
            "applied_rate": self.applied_rate,
            "amount_usdt": self.amount_usdt,
            "duration_days": self.duration_days,
            "snapshot_id": self.snapshot_id,
            "snapshot_hash": self.snapshot_hash,
            "snapshot_captured_at_ms": self.snapshot_captured_at_ms,
            "snapshot_source": self.snapshot_source,
            "snapshot_age_ms": self.snapshot_age_ms,
            "model_version": self.model_version,
            "model_hash": self.model_hash,
            "model_evidence": dict(self.model_evidence),
            "safety_result": dict(self.safety_result),
            "execution_policy": self.execution_policy.value,
            "service_version": self.service_version,
            "config_hash": self.config_hash,
            "occurred_at_ms": self.occurred_at_ms,
            "recorded_at_ms": self.recorded_at_ms,
        }
