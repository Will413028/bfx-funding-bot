"""Pydantic models for Phase 4 observability events.

對應 phase 4 design 鎖定的 envelope + payload schema。
Schema 改動 = 必須同步更新此 module; CP3 test 會 catch 違反。
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from bfx_funding_bot.core.telemetry import EventType, HealthCheckPayload, Level, Phase


class StrategyName(StrEnum):
    RATE_PERCENTILE = "rate_percentile"
    MEAN_REVERSION = "mean_reversion"
    ADAPTIVE_PERIOD = "adaptive_period"


class SignalDirection(StrEnum):
    POST = "post"
    SKIP = "skip"


class DecisionOutcome(StrEnum):
    POST = "post"
    SKIP = "skip"


class SkipReason(StrEnum):
    BELOW_THRESHOLD = "below_threshold"
    MAX_POSITION_CAP = "max_position_cap"
    SAFETY_BLOCK = "safety_block"
    INSUFFICIENT_BALANCE = "insufficient_balance"
    OTHER = "other"


class SignalPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    signal_score: float
    signal_direction: SignalDirection
    strategy_attributes: dict[str, Any] = Field(..., min_length=1)
    divergence_detail: dict[str, Any] | None = None
    # ── NEW (Phase 4.3 LOCF) ──
    is_stale: bool = False
    stale_seconds: int = 0
    budget_seconds: int  # required: every signal carries its cell's budget


class DecisionPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    decision_outcome: DecisionOutcome
    signal_correlation_id: UUID
    # Money terms are Decimal end-to-end (planner -> intent/attempt -> command
    # gate -> venue body): the amount's last 4 of 8 decimals are the submit's
    # only venue identity (D3a fingerprint), so no float may sit on this path.
    # JSON dumps (audit, stdout, safety snapshots) carry them as exact strings.
    offer_rate: Decimal | None = None
    offer_amount_usdt: Decimal | None = None
    offer_duration_days: int | None = None
    skip_reason: SkipReason | None = None
    skip_reason_detail: str | None = None
    # ── Phase 1 per-symbol: the offer currency this decision targets. ──
    # Guards read decision.symbol to pick the per-symbol ledger bucket.
    # MANDATORY (Task 11 — no default, so a decision can never silently land as
    # the legacy "fUSD"); every producer must set the cell's real symbol.
    symbol: str
    # ── NEW (Phase 4.3 LOCF): staleness dimension on the DURABLE decision record ──
    # SIGNAL carries this too but lands on the ephemeral stdout sink; persisting it
    # here (PG diagnostics) lets canary outcomes be sliced stale-vs-fresh via SQL.
    is_stale: bool = False
    stale_seconds: int = 0
    budget_seconds: int | None = None

    @model_validator(mode="after")
    def _check_outcome_fields(self) -> DecisionPayload:
        if self.decision_outcome == DecisionOutcome.POST:
            missing = [
                f for f in ("offer_rate", "offer_amount_usdt", "offer_duration_days")
                if getattr(self, f) is None
            ]
            if missing:
                raise ValueError(f"post decision requires {missing}")
        if self.decision_outcome == DecisionOutcome.SKIP and self.skip_reason is None:
            raise ValueError("skip decision requires skip_reason")
        return self


class OrderSubmitPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    cid: int
    offer_id: str | None  # paper: "paper_<uuid12>"; real: stringified int from venue; None when no venue id exists
    execution_decision_id: str = Field(..., min_length=1)
    signal_correlation_id: UUID
    offer_rate: Decimal  # echoes the submitted terms exactly (JSON: string)
    offer_amount_usdt: Decimal
    offer_duration_days: int
    is_simulated: bool
    status: Literal["submitted", "failed", "unknown", "not_sent"]
    failure_reason: str | None = None
    attempts: int = 1
    retry_total_ms: int | None = None

    @model_validator(mode="after")
    def _check_failed(self) -> OrderSubmitPayload:
        if not self.execution_decision_id.strip():
            raise ValueError("execution_decision_id must be non-empty")
        if self.status in {"failed", "unknown", "not_sent"} and not self.failure_reason:
            raise ValueError(f"status={self.status} requires failure_reason")
        return self


class OrderFillPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    cid: int
    offer_id: str
    signal_correlation_id: UUID
    fill_size_usdt: float
    fill_price: float
    is_simulated: bool


class OrderStatusChangePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    cid: int
    offer_id: str
    signal_correlation_id: UUID
    status: str  # "cancelled" / "expired" / "partially_filled"
    reason: str | None = None
    filled_size_delta_usdt: float | None = None
    is_simulated: bool


class ReservationClaimedPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    cid: int
    venue_offer_id: str
    size_usdt: float
    signal_correlation_id: UUID
    is_simulated: bool


class ReservationReleasedPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    cid: int
    venue_offer_id: str
    size_usdt: float
    reason: str  # "venue_cancel" / "user_cancel" / "expired" / "missing_from_venue"
    signal_correlation_id: UUID
    is_simulated: bool


class SafetyTriggerPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    guard_name: str
    reason: str
    decision_snapshot: dict[str, Any]


PayloadModel = Annotated[
    SignalPayload | DecisionPayload | HealthCheckPayload
    | OrderSubmitPayload | OrderFillPayload | OrderStatusChangePayload
    | ReservationClaimedPayload | ReservationReleasedPayload
    | SafetyTriggerPayload | dict[str, Any],
    Field(union_mode="left_to_right"),
]


class Envelope(BaseModel):
    model_config = ConfigDict(extra="forbid")
    timestamp: datetime
    level: Level
    phase: Phase
    strategy: StrategyName | None = None
    cell: str | None = None
    event_type: EventType
    correlation_id: UUID
    # Signal/health telemetry is emitted before the deployment layer binds a
    # money-domain account.  ``None`` means "not applicable"; it is never a
    # hidden realm or a ``default`` account.  Execution events built by
    # ``emit.py`` always provide the canonical UUID string.
    account_id: str | None = None
    payload: dict[str, Any]

    @model_validator(mode="after")
    def _check_strategy_cell_conditional(self) -> Envelope:
        if self.event_type != EventType.HEALTH_CHECK and (
            self.strategy is None or self.cell is None
        ):
            raise ValueError(
                f"event_type={self.event_type} requires strategy and cell"
            )
        return self

    @model_validator(mode="after")
    def _validate_payload(self) -> Envelope:
        if self.event_type in (EventType.SIGNAL, EventType.SIGNAL_DIVERGENCE):
            SignalPayload.model_validate(self.payload)
        elif self.event_type == EventType.DECISION:
            DecisionPayload.model_validate(self.payload)
        elif self.event_type == EventType.HEALTH_CHECK:
            HealthCheckPayload.model_validate(self.payload)
        elif self.event_type == EventType.ORDER_SUBMIT:
            OrderSubmitPayload.model_validate(self.payload)
        elif self.event_type == EventType.ORDER_FILL:
            OrderFillPayload.model_validate(self.payload)
        elif self.event_type == EventType.ORDER_STATUS_CHANGE:
            OrderStatusChangePayload.model_validate(self.payload)
        elif self.event_type == EventType.RESERVATION_CLAIMED:
            ReservationClaimedPayload.model_validate(self.payload)
        elif self.event_type == EventType.RESERVATION_RELEASED:
            ReservationReleasedPayload.model_validate(self.payload)
        elif self.event_type == EventType.SAFETY_TRIGGER:
            SafetyTriggerPayload.model_validate(self.payload)
        return self
