"""A live boot that refuses to run stops trading: HALTED/auto, then an alert.

A refused live boot (the database is at another schema than this build, or an
applied capital policy is unreadable) is an automatic stop, so it writes
``HALTED`` (cause ``auto``, actor ``boot``). Lending envelope ADR 2026-09-25
D2/D3: an automatic reaction never cancels offers it cannot prove are its own,
and this process cannot -- it refuses to build the command gate that would. So
nothing is sent to the venue: the managed offers already there stay inside the
envelope they were placed under and nothing new is placed. A critical alert
says so; the operator's kill (a venue cancel-all) is the way to pull them.

The caller then refuses the boot.
"""
from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Final
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bfx_funding_bot.modules.execution.safety.trading_state import (
    CAUSE_AUTO,
    HALTED,
    TradingState,
    TradingStateRepository,
)
from bfx_funding_bot.modules.observability import alerts

log = logging.getLogger(__name__)

BOOT_ACTOR: Final = "boot"
VENUE_OFFERS_MAY_REMAIN: Final = "venue_offers_may_remain"


async def stop_refused_boot(
    *,
    session_factory: async_sessionmaker[AsyncSession],
    account_id: UUID,
    environment: str,
    reason: str,
    clock: Callable[[], int],
) -> TradingState | None:
    """HALTED/auto, then the alert. Never raises: the caller is already failing
    the boot with the real error. Returns the state, or None when HALTED itself
    could not be written."""
    reason = reason[:500]
    trading = TradingStateRepository(session_factory, account_id=account_id,
                                     deployment_environment=environment)
    try:
        result = await trading.transition(HALTED, cause=CAUSE_AUTO, actor=BOOT_ACTOR,
                                          reason=reason, now_ms=clock())
    except Exception as exc:
        log.critical("boot_stop_halt_unwritten account=%s", account_id, exc_info=True)
        _alert(account_id, environment, reason,
               f"HALTED could not be written ({type(exc).__name__})")
        return None
    _alert(account_id, environment, reason,
           "HALTED; managed offers already at the venue were left in place")
    return result.state


def _alert(account_id: UUID, environment: str, reason: str, detail: str) -> None:
    log.critical("venue_offers_may_remain account=%s env=%s reason=%s detail=%s",
                 account_id, environment, reason, detail)
    alerts.emit(VENUE_OFFERS_MAY_REMAIN, level=alerts.CRITICAL,
                message="the bot refused to boot; its offers may still be at the venue "
                        "(the kill switch cancels them)",
                account=str(account_id), environment=environment, reason=reason, detail=detail)


__all__ = ["BOOT_ACTOR", "VENUE_OFFERS_MAY_REMAIN", "stop_refused_boot"]
