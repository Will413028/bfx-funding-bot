"""Pydantic models for Phase 4 observability events.

對應 phase4-roadmap-design.md line 92-227 lock 的 envelope + payload schema。
Schema 改動 = 必須同步更新此 module; CP3 test 會 catch 違反。
"""
from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Level(StrEnum):
    DEBUG = "debug"
    INFO = "info"
    WARN = "warn"
    ERROR = "error"
    CRITICAL = "critical"


class Phase(StrEnum):
    PAPER = "paper"
    SHADOW = "shadow"
    CANARY = "canary"


class StrategyName(StrEnum):
    RATE_PERCENTILE = "rate_percentile"
    MEAN_REVERSION = "mean_reversion"


class EventType(StrEnum):
    SIGNAL = "signal"
    SIGNAL_DIVERGENCE = "signal_divergence"
    DECISION = "decision"
    SAFETY_TRIGGER = "safety_trigger"
    ORDER_SUBMIT = "order_submit"
    ORDER_FILL = "order_fill"
    ORDER_STATUS_CHANGE = "order_status_change"  # deprecated, removed in 4.4
    RESERVATION_CLAIMED = "reservation_claimed"
    RESERVATION_RELEASED = "reservation_released"
    HEALTH_CHECK = "health_check"


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


class HealthTarget(StrEnum):
    BITFINEX_WS = "bitfinex_ws"
    BITFINEX_REST = "bitfinex_rest"
    DB = "db"
    REDIS = "redis"
    SIGNAL_PIPELINE = "signal_pipeline"
    EXECUTOR = "executor"
    SAFETY_CHAIN = "safety_chain"
    FILL_TRACKER = "fill_tracker"
    LEDGER = "ledger"


class HealthStatus(StrEnum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    DOWN = "down"


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
    offer_rate: float | None = None
    offer_amount_usdt: float | None = None
    offer_duration_days: int | None = None
    skip_reason: SkipReason | None = None
    skip_reason_detail: str | None = None

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


class HealthCheckPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    check_target: HealthTarget
    status: HealthStatus
    last_msg_age_ms: int | None = None
    reconnect_count_last_hour: int | None = None
    latency_ms: int | None = None
    error_message: str | None = None
    # ── NEW (Phase 4.3 LOCF) ──
    reason: str | None = None  # "stale_exceeded", "connection_lost", "db_unavailable", "task_hung"
    stale_seconds: int | None = None  # populated when reason="stale_exceeded"
    budget_seconds: int | None = None  # populated when reason="stale_exceeded"

    @model_validator(mode="after")
    def _conditional_required(self) -> HealthCheckPayload:
        if self.check_target == HealthTarget.BITFINEX_WS and (
            self.last_msg_age_ms is None or self.reconnect_count_last_hour is None
        ):
            raise ValueError(
                "bitfinex_ws check requires last_msg_age_ms + reconnect_count_last_hour"
            )
        if (
            self.check_target in (HealthTarget.BITFINEX_REST, HealthTarget.DB)
            and self.latency_ms is None
        ):
            raise ValueError(f"{self.check_target} check requires latency_ms")
        if self.status in (HealthStatus.DEGRADED, HealthStatus.DOWN) and not self.error_message:
            raise ValueError(f"status={self.status} requires error_message")
        return self


class OrderSubmitPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    cid: int
    offer_id: str | None  # paper: "paper_<uuid12>"; real: stringified int from venue; None on failed submit
    signal_correlation_id: UUID
    offer_rate: float
    offer_amount_usdt: float
    offer_duration_days: int
    is_simulated: bool
    status: str  # "submitted" / "failed"
    failure_reason: str | None = None
    attempts: int = 1
    retry_total_ms: int | None = None

    @model_validator(mode="after")
    def _check_failed(self) -> OrderSubmitPayload:
        if self.status == "failed" and not self.failure_reason:
            raise ValueError("status=failed requires failure_reason")
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
    account_id: str = "default"
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
