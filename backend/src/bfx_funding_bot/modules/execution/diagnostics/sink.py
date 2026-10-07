"""DiagnosticsSink — forensic events (DECISION / SAFETY_TRIGGER / CANCEL_AUDIT)
to the `diagnostics` table.

Implements the `emit(dict)` port (same interface as StdoutEventSink). Best-effort: a persistence
failure is swallowed + logged to stdout and NEVER propagates — forensic writes
must not block trading (spec §240-241). Each write opens its OWN txn via
session_scope (never the command txn). Non-forensic / unknown event types are
silently dropped — operational telemetry goes to StdoutEventSink (3c).
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Protocol

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bfx_funding_bot.core.db import session_scope
from bfx_funding_bot.core.telemetry import EventType
from bfx_funding_bot.modules.accounts.exchange_accounts import account_id_uuid_or_none
from bfx_funding_bot.modules.execution.diagnostics.tables import DiagnosticsRow
from bfx_funding_bot.modules.execution.events import (
    CancelAcknowledged,
    CancelRequested,
)

log = logging.getLogger(__name__)


class DiagnosticKind(StrEnum):
    DECISION = "decision"
    SAFETY_TRIGGER = "safety_trigger"
    CANCEL_AUDIT = "cancel_audit"


# Single source of truth for "what is forensic". Anything not here is dropped.
# CANCEL_AUDIT is emitted only via handle_cancel_*() (typed bus path), never via emit().
_KIND_BY_EVENT_TYPE: dict[str, DiagnosticKind] = {
    EventType.DECISION.value: DiagnosticKind.DECISION,
    EventType.SAFETY_TRIGGER.value: DiagnosticKind.SAFETY_TRIGGER,
}


def _occurred_at(event: dict[str, Any]) -> datetime:
    ts = event.get("timestamp")
    if isinstance(ts, str):
        try:
            dt = datetime.fromisoformat(ts)
        except ValueError:
            pass
        else:
            return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)
    return datetime.now(UTC)


class _MetricsHook(Protocol):
    """Structural port for DaemonMetrics (observability/metrics.py) — keeps the
    forensic sink import-free of the metrics module and test doubles trivial."""

    def observe_diagnostic_event(self, event: dict[str, Any]) -> None: ...


class DiagnosticsSink:
    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        account_id: str,
        deployment_environment: str,
        metrics: _MetricsHook | None = None,
    ) -> None:
        self._sf = session_factory
        self._account_id = account_id
        self._env = deployment_environment
        self._metrics = metrics

    async def emit(self, event: dict[str, Any]) -> None:
        """Persists only forensic kinds; lenient field extraction (NO full
        Envelope validation — its strategy/cell rule over-constrains forensic
        storage, e.g. smoke_boot SAFETY_TRIGGER)."""
        kind = _KIND_BY_EVENT_TYPE.get(str(event.get("event_type")))
        if kind is None:
            return  # operational / unknown — not forensic, dropped
        # Four Golden Signals hook (bfx_diagnostic_events_total — safety guard
        # trips / decision rate). Observe-only + fail-open: a broken hook must
        # never block the forensic write, let alone the calling trade path.
        if self._metrics is not None:
            try:
                self._metrics.observe_diagnostic_event(event)
            except Exception:
                log.debug("diagnostics_metrics_hook_failed", exc_info=True)
        # account_id is sink-authoritative (mirror deployment_environment=self._env):
        # the real signal-engine decision event dict carries no account_id, so relying
        # on event.get(..., "default") would mis-tag every decision row. The sink
        # knows its account scope and mirrors the daemon's canonical UUID alias.
        await self._insert(
            kind=kind,
            account_id=self._account_id,
            payload=event,
            occurred_at=_occurred_at(event),
        )

    async def handle_cancel_requested(self, event: CancelRequested) -> None:
        await self._insert(
            kind=DiagnosticKind.CANCEL_AUDIT,
            account_id=self._account_id,
            payload={
                "event_type": EventType.CANCEL_REQUESTED.value,
                "venue_offer_id": event.venue_offer_id,
                "requested_at_ms": event.requested_at_ms,
                "signal_correlation_id": str(event.signal_correlation_id),
            },
            occurred_at=datetime.fromtimestamp(event.requested_at_ms / 1000, tz=UTC),
        )

    async def handle_cancel_acknowledged(self, event: CancelAcknowledged) -> None:
        await self._insert(
            kind=DiagnosticKind.CANCEL_AUDIT,
            account_id=self._account_id,
            payload={
                "event_type": EventType.CANCEL_ACKNOWLEDGED.value,
                "venue_offer_id": event.venue_offer_id,
                "acknowledged_at_ms": event.acknowledged_at_ms,
                "rest_status": event.rest_status,
                "venue_response_text": event.venue_response_text,
                "signal_correlation_id": str(event.signal_correlation_id),
            },
            occurred_at=datetime.fromtimestamp(event.acknowledged_at_ms / 1000, tz=UTC),
        )

    async def _insert(
        self,
        *,
        kind: DiagnosticKind,
        account_id: str,
        payload: dict[str, Any],
        occurred_at: datetime,
    ) -> None:
        try:
            async with session_scope(self._sf) as session:
                session.add(DiagnosticsRow(
                    account_id=account_id,
                    exchange_account_id=account_id_uuid_or_none(account_id),
                    deployment_environment=self._env,
                    kind=kind.value,
                    payload=payload,
                    occurred_at=occurred_at,
                ))
        except Exception:
            log.warning(
                "diagnostics_persist_failed kind=%s account_id=%s",
                kind.value, account_id, exc_info=True,
            )


class NoopDiagnosticsSink:
    """Null object — chain tests / contexts that don't exercise diagnostics."""

    async def emit(self, event: dict[str, Any]) -> None:
        return None

    async def handle_cancel_requested(self, event: CancelRequested) -> None:
        return None

    async def handle_cancel_acknowledged(self, event: CancelAcknowledged) -> None:
        return None
