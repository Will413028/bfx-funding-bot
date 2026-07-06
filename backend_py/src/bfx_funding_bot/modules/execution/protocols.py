"""Protocols + dataclasses for execution pipeline.

All concrete adapters (EchoPaperExecutor, BitfinexLiveExecutor 4.4,
RestPollingFillTracker, hard/calibrated guards) conform to these.

AccountContext carries credentials + per-account allocation cap; 4.2 always
account_id='default' (single hardcoded account from env). Phase 5+ SaaS
extends to per-tenant context loaded from vault.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Protocol, runtime_checkable
from uuid import UUID

from bfx_funding_bot.modules.marketfeed.schemas import DecisionPayload


@dataclass(frozen=True, slots=True)
class Credentials:
    """4.2: loaded from env BFX_API_KEY / BFX_API_SECRET.
    Phase 5 SaaS: decrypted from vault per-account.
    """
    api_key: str
    api_secret: str


@dataclass(frozen=True, slots=True)
class AccountContext:
    """Per-account context. 4.2: hardcoded account_id='default'."""
    account_id: str
    credentials: Credentials
    allocation_cap_usdt: Decimal


@dataclass(frozen=True, slots=True)
class GuardResult:
    allowed: bool
    guard_name: str
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class SubmittedOrder:
    cid: int
    venue_offer_id: str | None     # paper: "paper_<uuid12>"; real: str(int) from venue; None on failed submit
    status: str                    # "submitted" / "failed" / "filled" (paper synchronous)
    raw_response: dict[str, Any] | None    # debug audit; None for paper


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
        self, decision: DecisionPayload, ctx: AccountContext, *, cid: int | None = None,
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
