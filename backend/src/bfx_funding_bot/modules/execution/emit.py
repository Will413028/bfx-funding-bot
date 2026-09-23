"""Event emit helpers for order/safety events.

All helpers build a fully-validated Envelope (Pydantic) and pass the dict
representation to the event_sink emit port. Validation failure raises ValueError
before emit — catches bad shapes at the call site.
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal, Protocol
from uuid import UUID

from bfx_funding_bot.modules.execution.contracts import ReadyToSubmit
from bfx_funding_bot.modules.execution.protocols import (
    AccountContext,
)
from bfx_funding_bot.modules.marketfeed.schemas import (
    DecisionPayload,
    Envelope,
    EventType,
    Level,
    OrderFillPayload,
    OrderSubmitPayload,
    Phase,
    SafetyTriggerPayload,
    StrategyName,
)


class _EventSink(Protocol):
    """Operational telemetry port (→ structured-stdout StdoutEventSink)."""

    async def emit(self, event: dict[str, Any]) -> None: ...


class _DiagnosticsProtocol(Protocol):
    """Forensic diagnostics port (→ PG DiagnosticsSink)."""

    async def emit(self, event: dict[str, Any]) -> None: ...


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


async def emit_order_submit(
    *,
    event_sink: _EventSink,
    phase: Phase,
    strategy: StrategyName,
    cell: str,
    ready: ReadyToSubmit,
    ctx: AccountContext,
    cid: int,
    offer_id: str | None,
    is_simulated: bool,
    status: str,
    failure_reason: str | None = None,
    attempts: int = 1,
    retry_total_ms: int | None = None,
) -> None:
    decision = ready.decision
    payload = OrderSubmitPayload(
        cid=cid,
        offer_id=offer_id,
        execution_decision_id=ready.decision_id,
        signal_correlation_id=decision.signal_correlation_id,
        offer_rate=decision.offer_rate or 0.0,
        offer_amount_usdt=decision.offer_amount_usdt or 0.0,
        offer_duration_days=decision.offer_duration_days or 0,
        is_simulated=is_simulated,
        status=status,
        failure_reason=failure_reason,
        attempts=attempts,
        retry_total_ms=retry_total_ms,
    )
    env = Envelope(
        timestamp=_now_iso(),
        level=Level.INFO if status == "submitted" else Level.ERROR,
        phase=phase,
        strategy=strategy,
        cell=cell,
        event_type=EventType.ORDER_SUBMIT,
        correlation_id=decision.signal_correlation_id,
        account_id=ctx.account_id,
        payload=payload.model_dump(mode="json"),
    )
    await event_sink.emit(env.model_dump(mode="json"))


async def emit_order_fill(
    *,
    event_sink: _EventSink,
    phase: Phase,
    strategy: StrategyName,
    cell: str,
    decision: DecisionPayload,
    ctx: AccountContext,
    cid: int,
    offer_id: str,
    fill_size_usdt: float,
    fill_price: float,
    is_simulated: bool,
) -> None:
    payload = OrderFillPayload(
        cid=cid,
        offer_id=offer_id,
        signal_correlation_id=decision.signal_correlation_id,
        fill_size_usdt=fill_size_usdt,
        fill_price=fill_price,
        is_simulated=is_simulated,
    )
    env = Envelope(
        timestamp=_now_iso(),
        level=Level.INFO,
        phase=phase,
        strategy=strategy,
        cell=cell,
        event_type=EventType.ORDER_FILL,
        correlation_id=decision.signal_correlation_id,
        account_id=ctx.account_id,
        payload=payload.model_dump(mode="json"),
    )
    await event_sink.emit(env.model_dump(mode="json"))


async def emit_safety_trigger(
    *,
    diagnostics: _DiagnosticsProtocol,
    phase: Phase,
    strategy: StrategyName | None,
    cell: str | None,
    correlation_id: UUID,
    account_id: str,
    level: Literal["warn", "critical"],
    guard_name: str,
    reason: str,
    decision_snapshot: dict[str, Any],
) -> None:
    payload = SafetyTriggerPayload(
        guard_name=guard_name,
        reason=reason,
        decision_snapshot=decision_snapshot,
    )
    env = Envelope(
        timestamp=_now_iso(),
        level=Level(level),
        phase=phase,
        strategy=strategy,
        cell=cell,
        event_type=EventType.SAFETY_TRIGGER,
        correlation_id=correlation_id,
        account_id=account_id,
        payload=payload.model_dump(mode="json"),
    )
    await diagnostics.emit(env.model_dump(mode="json"))
