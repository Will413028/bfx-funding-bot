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
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Protocol, runtime_checkable
from uuid import UUID

from bfx_funding_bot.modules.execution.contracts import (
    GuardResult,
    ReadyToSubmit,
    ReservationRef,
)
from bfx_funding_bot.modules.marketfeed.schemas import DecisionPayload

__all__ = [
    "AccountContext",
    "CancelPort",
    "Credentials",
    "ExecutorPort",
    "FillTracker",
    "GuardResult",
    "GuardRule",
    "ReadyToSubmit",
    "SubmittedOrder",
    "WriterLockHandle",
]


@dataclass(frozen=True, slots=True)
class Credentials:
    """Runtime Bitfinex credential decrypted from the account vault at boot."""
    api_key: str
    api_secret: str


@dataclass(frozen=True, slots=True)
class AccountContext:
    """Per-account context bound to one canonical ExchangeAccount identity."""
    account_id: str
    credentials: Credentials
    allocation_cap_usdt: Decimal


@dataclass(frozen=True, slots=True)
class SubmittedOrder:
    cid: int
    venue_offer_id: str | None     # paper: "paper_<uuid12>"; real: str(int) from venue; None on failed submit
    status: str                    # "submitted" / "failed" / "filled" (paper synchronous)
    raw_response: dict[str, Any] | None    # debug audit; None for paper
    reservation_ref: ReservationRef | None = None


class GuardRule(Protocol):
    """Pre-trade guard. Single-method, fail-closed convention."""
    name: str
    is_calibrated: bool   # True = L2 (enabled=false by default in 4.2)

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


class FillTracker(Protocol):
    """Polls venue for fill / status_change events. Daemon TaskGroup sub-task."""
    async def poll_loop(self, stop_event: asyncio.Event) -> None: ...
