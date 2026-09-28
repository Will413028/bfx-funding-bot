"""Protocols + dataclasses for execution pipeline.

All concrete adapters (EchoPaperExecutor, BitfinexLiveExecutor 4.4,
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
    SubmitNotSent,
    SubmitOutcome,
    SubmitOutcomeKind,
    SubmitOutcomeUnknown,
    SubmitRejected,
    response_digest,
)
from bfx_funding_bot.modules.marketfeed.schemas import DecisionPayload

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
    """Result envelope with a typed outcome and a legacy status read view.

    New adapters should pass ``outcome``.  ``status=`` remains accepted for
    paper/legacy adapters during the staged migration, but is converted into a
    typed outcome at construction time.  In particular, an UNKNOWN outcome can
    only expose ``status == "unknown"``; it is never silently collapsed into
    the old ``"failed"`` value.
    """

    cid: int
    venue_offer_id: str | None
    outcome: SubmitOutcome
    raw_response: Any | None
    reservation_ref: ReservationRef | None
    _legacy_status: str | None

    def __init__(
        self,
        cid: int,
        venue_offer_id: str | None,
        status: str | None = None,
        raw_response: Any | None = None,
        reservation_ref: ReservationRef | None = None,
        *,
        outcome: SubmitOutcome | None = None,
        _legacy_status: str | None = None,
    ) -> None:
        typed_outcome_supplied = outcome is not None
        if outcome is None:
            outcome = _outcome_from_legacy_status(
                status,
                venue_offer_id=venue_offer_id,
                raw_response=raw_response,
            )
        else:
            expected_statuses = {
                SubmitOutcomeKind.ACKNOWLEDGED: {"submitted", "filled"},
                SubmitOutcomeKind.REJECTED: {"failed"},
                SubmitOutcomeKind.UNKNOWN: {"unknown"},
                SubmitOutcomeKind.NOT_SENT: {"not_sent"},
            }[outcome.kind]
            if status is not None and status not in expected_statuses:
                raise ValueError(
                    f"typed {outcome.kind.value} outcome conflicts with status={status!r}"
                )
            replace_legacy_statuses = (
                {"weird_venue_string"}
                if outcome.kind is SubmitOutcomeKind.UNKNOWN
                else set()
            )
            if (
                _legacy_status is not None
                and _legacy_status not in expected_statuses | replace_legacy_statuses
            ):
                # ``_legacy_status`` is populated only by dataclasses.replace on
                # a legacy result.  Preserve the historical diagnostic string
                # during replacement, while still rejecting success-shaped
                # values on an UNKNOWN outcome.
                raise ValueError(
                    f"typed {outcome.kind.value} outcome conflicts with "
                    f"legacy status={_legacy_status!r}"
                )
            if (
                isinstance(outcome, SubmitAcknowledged)
                and venue_offer_id is not None
                and venue_offer_id != outcome.venue_offer_id
            ):
                raise ValueError("venue_offer_id conflicts with acknowledged outcome")
            if isinstance(outcome, SubmitAcknowledged) and venue_offer_id is None:
                venue_offer_id = outcome.venue_offer_id
            if raw_response is None and hasattr(outcome, "raw_response"):
                raw_response = outcome.raw_response

        if not isinstance(outcome, SubmitAcknowledged) and venue_offer_id is not None:
            raise ValueError(
                f"{outcome.kind.value} outcome cannot carry venue_offer_id"
            )

        object.__setattr__(self, "cid", cid)
        object.__setattr__(self, "venue_offer_id", venue_offer_id)
        object.__setattr__(self, "outcome", outcome)
        object.__setattr__(self, "raw_response", raw_response)
        object.__setattr__(self, "reservation_ref", reservation_ref)
        # ``filled`` is a paper-only compatibility value; all real submit
        # outcomes derive to submitted/failed/unknown/not_sent below.
        compatibility = (
            _legacy_status
            if _legacy_status is not None
            else (
                status
                if (
                    not typed_outcome_supplied
                    and (
                        status == "weird_venue_string"
                        or (status == "filled" and isinstance(outcome, SubmitAcknowledged))
                    )
                )
                else None
            )
        )
        object.__setattr__(self, "_legacy_status", compatibility)

    @property
    def outcome_kind(self) -> SubmitOutcomeKind:
        return self.outcome.kind

    @property
    def status(self) -> str:
        if self._legacy_status is not None:
            return self._legacy_status
        return {
            SubmitOutcomeKind.ACKNOWLEDGED: "submitted",
            SubmitOutcomeKind.REJECTED: "failed",
            SubmitOutcomeKind.UNKNOWN: "unknown",
            SubmitOutcomeKind.NOT_SENT: "not_sent",
        }[self.outcome_kind]


def _outcome_from_legacy_status(
    status: str | None,
    *,
    venue_offer_id: str | None,
    raw_response: Any | None,
) -> SubmitOutcome:
    """Upcast pre-typed executor values without losing ambiguity semantics."""
    if status in {"submitted", "filled"} and venue_offer_id is not None:
        return SubmitAcknowledged(venue_offer_id=venue_offer_id, raw_response=raw_response)
    if status == "failed":
        return SubmitRejected(reason="legacy_submit_failed", raw_response=raw_response)
    if status == "not_sent":
        return SubmitNotSent(reason="legacy_not_sent")
    if status == "unknown":
        return SubmitOutcomeUnknown(
            reason="legacy_unknown",
            transport_started=True,
            raw_response_digest=(response_digest(raw_response) if raw_response is not None else None),
        )
    # Unknown legacy status values are themselves ambiguous.  Preserve the
    # string only for observability compatibility, while exposing UNKNOWN to
    # new control-flow code.
    return SubmitOutcomeUnknown(
        reason="legacy_unrecognized_status",
        transport_started=True,
        raw_response_digest=(response_digest(raw_response) if raw_response is not None else None),
    )


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
    """Venue executor (Echo paper / Bitfinex live).

    cid is centralized by ReservationEmittingMiddleware (A2: same cid for INTENT
    + outcome). It is threaded down through the chain; executors use it when
    provided and fall back to deterministic generation only for direct callers.
    """
    async def submit(
        self, ready: ReadyToSubmit, ctx: AccountContext, *, cid: int | None = None,
        reservation_ref: ReservationRef | None = None,
    ) -> SubmittedOrder: ...


@runtime_checkable
class CancelPort(Protocol):
    """Venue funding-offer cancel（只有 live executor 實作；paper 無 venue offer）。

    對應 BitfinexLiveExecutor.cancel：CancelRequested/CancelAcknowledged audit
    與 release 路徑（WS foc → ReservationReleased）都在那一側，呼叫方不碰 ledger。
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
