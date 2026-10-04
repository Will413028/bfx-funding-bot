"""Shared telemetry vocabulary and health payload."""
from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, model_validator


class Level(StrEnum):
    DEBUG = "debug"
    INFO = "info"
    WARN = "warn"
    ERROR = "error"
    CRITICAL = "critical"


class Phase(StrEnum):
    LIVE = "live"
    SHADOW = "shadow"


class EventType(StrEnum):
    SIGNAL = "signal"
    SIGNAL_DIVERGENCE = "signal_divergence"
    DECISION = "decision"
    SAFETY_TRIGGER = "safety_trigger"
    ORDER_SUBMIT = "order_submit"
    ORDER_FILL = "order_fill"
    ORDER_STATUS_CHANGE = "order_status_change"  # deprecated, removed in 4.4
    RESERVATION_INTENT = "reservation_intent"
    RESERVATION_CLAIMED = "reservation_claimed"
    RESERVATION_FAILED = "reservation_failed"
    RESERVATION_RELEASED = "reservation_released"
    CANCEL_REQUESTED = "cancel_requested"
    CANCEL_ACKNOWLEDGED = "cancel_acknowledged"  # NEW — Phase 4.4b prework D2
    HEALTH_CHECK = "health_check"


class HealthTarget(StrEnum):
    BITFINEX_WS = "bitfinex_ws"
    AUTH_WS = "auth_ws"
    BITFINEX_REST = "bitfinex_rest"
    DB = "db"
    REDIS = "redis"
    SIGNAL_PIPELINE = "signal_pipeline"
    EXECUTOR = "executor"
    SAFETY_CHAIN = "safety_chain"
    FILL_TRACKER = "fill_tracker"
    LEDGER = "ledger"
    RECONCILE = "reconcile"


class HealthStatus(StrEnum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    DOWN = "down"


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
