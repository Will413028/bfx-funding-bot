"""Pydantic models for Phase 4 observability events.

對應 phase 4 design 鎖定的 envelope + payload schema。
Schema 改動 = 必須同步更新此 module; CP3 test 會 catch 違反。
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from bfx_funding_bot.core.telemetry import EventType, HealthCheckPayload, Level, Phase
from bfx_funding_bot.modules.strategy import DecisionPayload, SignalPayload, StrategyName


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
