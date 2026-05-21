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
from typing import Any, Protocol

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


class ExecutorPort(Protocol):
    """Venue executor (Echo paper / Bitfinex live)."""
    async def submit(
        self, decision: DecisionPayload, ctx: AccountContext,
    ) -> SubmittedOrder: ...


class FillTracker(Protocol):
    """Polls venue for fill / status_change events. Daemon TaskGroup sub-task."""
    async def poll_loop(self, stop_event: asyncio.Event) -> None: ...
