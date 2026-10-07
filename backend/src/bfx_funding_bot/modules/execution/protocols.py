"""Protocols + dataclasses for execution pipeline.

All concrete adapters (BitfinexLiveExecutor,
RestPollingFillTracker, hard/calibrated guards) conform to these.

AccountContext carries credentials + per-account allocation cap.  The daemon
constructs it only from the canonical ExchangeAccount UUID (serialized as a
lowercase hyphenated string for existing event contracts); there is no default
or process-global realm fallback.
"""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Protocol, runtime_checkable
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from bfx_funding_bot.external.bitfinex.credentials import Credentials
from bfx_funding_bot.modules.execution.contracts import (
    GuardResult,
    ReadyToSubmit,
    ReservationRef,
)
from bfx_funding_bot.modules.execution.submit_outcomes import (
    SubmitAcknowledged,
    SubmitOutcome,
    SubmitOutcomeKind,
)
from bfx_funding_bot.modules.strategy import DecisionPayload

__all__ = [
    "AccountContext",
    "CancelPort",
    "Credentials",
    "ExecutorPort",
    "FillTracker",
    "FundingCancelAllPort",
    "FundingCancelAllResult",
    "GuardResult",
    "GuardRule",
    "ReadyToSubmit",
    "SubmitOutcome",
    "SubmitOutcomeKind",
    "SubmittedOrder",
    "WriterLockHandle",
]


@dataclass(frozen=True, slots=True)
class AccountContext:
    """Per-account context bound to one canonical ExchangeAccount identity."""
    account_id: str
    credentials: Credentials
    allocation_cap_usdt: Decimal
    capital_cell_id: str | None = None
    # Only the command boundary supplies this; guards must reuse its replayed state.
    command_session: AsyncSession | None = None
    before_cancel_transport: Callable[[], Awaitable[None]] | None = None
    before_submit_transport: Callable[[], bool] | None = None


@dataclass(frozen=True, slots=True, init=False)
class SubmittedOrder:
    """Result envelope around one typed submit outcome.

    ``status`` is a read-only label derived from the outcome for metrics,
    tracing and the ORDER_SUBMIT event; control flow reads ``outcome_kind``.
    """

    venue_offer_id: str | None
    outcome: SubmitOutcome
    raw_response: Any | None
    reservation_ref: ReservationRef | None

    def __init__(
        self,
        *,
        outcome: SubmitOutcome,
        venue_offer_id: str | None = None,
        raw_response: Any | None = None,
        reservation_ref: ReservationRef | None = None,
    ) -> None:
        if isinstance(outcome, SubmitAcknowledged):
            if venue_offer_id is not None and venue_offer_id != outcome.venue_offer_id:
                raise ValueError("venue_offer_id conflicts with acknowledged outcome")
            venue_offer_id = outcome.venue_offer_id
        elif venue_offer_id is not None:
            raise ValueError(
                f"{outcome.kind.value} outcome cannot carry venue_offer_id"
            )
        if raw_response is None and hasattr(outcome, "raw_response"):
            raw_response = outcome.raw_response

        object.__setattr__(self, "venue_offer_id", venue_offer_id)
        object.__setattr__(self, "outcome", outcome)
        object.__setattr__(self, "raw_response", raw_response)
        object.__setattr__(self, "reservation_ref", reservation_ref)

    @property
    def outcome_kind(self) -> SubmitOutcomeKind:
        return self.outcome.kind

    @property
    def status(self) -> str:
        return _STATUS_LABELS[self.outcome_kind]


_STATUS_LABELS: dict[SubmitOutcomeKind, str] = {
    SubmitOutcomeKind.ACKNOWLEDGED: "submitted",
    SubmitOutcomeKind.REJECTED: "failed",
    SubmitOutcomeKind.UNKNOWN: "unknown",
    SubmitOutcomeKind.NOT_SENT: "not_sent",
}


class GuardRule(Protocol):
    """Pre-trade guard. Single-method, fail-closed convention."""
    name: str

    async def evaluate(
        self, decision: DecisionPayload, ctx: AccountContext,
    ) -> GuardResult: ...


class WriterLockHandle(Protocol):
    """Anything that can live-verify whether this process holds the writer lock."""
    async def verify_held(self) -> bool: ...


class ExecutorPort(Protocol):
    """Venue executor (Bitfinex live).

    ``reservation_ref`` is created by the account command gate and passed only
    to the venue executor behind it; a result must echo it back so the gate can
    attribute the outcome to its own intent.
    """
    async def submit(
        self, ready: ReadyToSubmit, ctx: AccountContext, *,
        reservation_ref: ReservationRef | None = None,
    ) -> SubmittedOrder: ...


@runtime_checkable
class CancelPort(Protocol):
    """Venue funding-offer cancel（只有 live executor 實作）。

    對應 BitfinexLiveExecutor.cancel：CancelRequested/CancelAcknowledged audit
    都在那一側；offer 結束由 ledger 觀測（WS foc 只是 venue hint），呼叫方不碰 ledger。
    """
    async def cancel(
        self,
        *,
        venue_offer_id: str,
        signal_correlation_id: UUID,
        account_id: str,
        ctx: AccountContext,
    ) -> None: ...


@dataclass(frozen=True, slots=True)
class FundingCancelAllResult:
    """The venue's answer to "cancel every funding offer in this currency".

    ``acknowledged`` means the venue accepted the request; the offers it
    cancelled are observed by the next reconcile, like any other cancel.
    ``rejected`` carries the venue's own status and text.
    """
    outcome: str  # "acknowledged" | "rejected"
    venue_status: str | None = None
    text: str | None = None


@runtime_checkable
class FundingCancelAllPort(Protocol):
    """Venue funding cancel-all for one currency -- the kill switch's only venue write.

    It bypasses the command gate on purpose: provenance, uncertainty and the
    trading state do not apply to "cancel everything", which is the one write
    that must still work when those projections are what is broken. Callers
    own the writer-lock check and the durable record of each attempt.
    """
    async def cancel_all_funding_offers(
        self, *, currency: str, ctx: AccountContext,
    ) -> FundingCancelAllResult: ...


class FillTracker(Protocol):
    """Polls venue for fill / status_change events. Daemon TaskGroup sub-task."""
    async def poll_loop(self, stop_event: asyncio.Event) -> None: ...
