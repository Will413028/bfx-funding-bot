"""A live boot that refuses to run stops trading like any automatic stop.

Will, 2026-09-25: manual and automatic stops both cancel. A refused live boot
(the database is at another schema than this build, or an applied capital
policy is unreadable) is an automatic stop, so it takes the kill path:

1. ``HALTED`` (cause ``auto``, actor ``boot``) is written first. If even that
   cannot be written, nothing reaches the venue (the kill path's rule).
2. Best effort, the venue funding cancel-all for the configured currencies,
   through :class:`KillSwitch`, so it is audited in ``funding_cancel_all_audit``
   and alerted exactly like an operator's kill. It runs only when this build
   can read the credential vault *as the database is*: the vault's two tables
   must have exactly the columns this build declares, read from the catalog --
   the rows are never touched on a schema this build does not know. The
   single-writer lock is taken first; another writer holding it means that
   process owns the venue, and nothing is sent.
3. Whenever the cancel-all did not fully land (vault unreadable, no lock, venue
   refused or failed), a critical alert says that the venue may still hold
   funding offers and that they must be cancelled by hand.

The caller then refuses the boot.
"""
from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Iterable
from decimal import Decimal
from typing import Final
from uuid import UUID

from sqlalchemy import Table, inspect
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bfx_funding_bot.core.errors import WriterLockUnacquired
from bfx_funding_bot.core.writer_lock import WriterLock
from bfx_funding_bot.modules.accounts.tables import ExchangeAccount, ExchangeAccountCredential
from bfx_funding_bot.modules.execution.protocols import (
    AccountContext,
    Credentials,
    FundingCancelAllPort,
    WriterLockHandle,
)
from bfx_funding_bot.modules.execution.safety.kill_switch import KillResult, KillSwitch
from bfx_funding_bot.modules.execution.safety.trading_state import (
    CAUSE_AUTO,
    HALTED,
    TradingStateRepository,
)
from bfx_funding_bot.modules.observability import alerts

log = logging.getLogger(__name__)

BOOT_ACTOR: Final = "boot"
VENUE_OFFERS_MAY_REMAIN: Final = "venue_offers_may_remain"
_VAULT_TABLES: tuple[Table, ...] = (ExchangeAccount.__table__, ExchangeAccountCredential.__table__)  # type: ignore[assignment]
_ZERO = Decimal(0)


async def vault_readable(session: AsyncSession) -> bool:
    """Whether the credential vault has exactly the columns this build reads.

    Catalog only: on a schema this build does not know, the rows are not read
    until their tables are shown to be the ones this code was written for.
    """
    def check(connection) -> bool:  # type: ignore[no-untyped-def]
        inspector = inspect(connection)
        for table in _VAULT_TABLES:
            if not inspector.has_table(table.name):
                return False
            reflected = {column["name"] for column in inspector.get_columns(table.name)}
            if reflected != {column.name for column in table.columns}:
                return False
        return True

    return await session.run_sync(lambda sync: check(sync.connection()))


async def stop_refused_boot(
    *,
    session_factory: async_sessionmaker[AsyncSession],
    account_id: UUID,
    environment: str,
    configured_symbols: Iterable[str],
    reason: str,
    load_credentials: Callable[[AsyncSession], Awaitable[Credentials]],
    venue: Callable[[], FundingCancelAllPort],
    writer_lock: Callable[[], Awaitable[WriterLockHandle | None]],
    clock: Callable[[], int],
) -> KillResult | None:
    """HALTED/auto, then the best-effort cancel-all; alerts what is left by hand.

    Never raises: the caller is already failing the boot with the real error.
    Returns the kill result, or None when HALTED itself could not be written.
    """
    reason = reason[:500]
    trading = TradingStateRepository(session_factory, account_id=account_id,
                                     deployment_environment=environment)
    try:
        await trading.transition(HALTED, cause=CAUSE_AUTO, actor=BOOT_ACTOR, reason=reason,
                                 now_ms=clock())
    except Exception as exc:
        log.critical("boot_stop_halt_unwritten account=%s", account_id, exc_info=True)
        _manual(account_id, environment, reason,
                f"HALTED could not be written ({type(exc).__name__}); nothing was sent to the venue")
        return None

    ctx: AccountContext | None = None
    unavailable: str | None = None
    try:
        async with session_factory() as session:
            if not await vault_readable(session):
                unavailable = "credential vault tables differ from this build's; not read"
            else:
                ctx = AccountContext(account_id=str(account_id),
                                     credentials=await load_credentials(session),
                                     allocation_cap_usdt=_ZERO)
    except Exception as exc:
        unavailable = f"credentials unreadable ({type(exc).__name__})"
    lock: WriterLockHandle | None = None
    port: FundingCancelAllPort | None = None
    if ctx is not None:
        try:
            lock = await writer_lock()
            port = venue()
        except Exception as exc:
            unavailable = f"venue client or writer lock unavailable ({type(exc).__name__})"
            port = None
    try:
        kill = KillSwitch(
            trading_state=trading, session_factory=session_factory,
            ctx=ctx or AccountContext(account_id=str(account_id),
                                      credentials=Credentials(api_key="", api_secret=""),
                                      allocation_cap_usdt=_ZERO),
            configured_symbols=configured_symbols, venue=port, writer_lock=lock, clock=clock,
        )
        result = await kill.engage(cause=CAUSE_AUTO, actor=BOOT_ACTOR, reason=reason,
                                   when_already_halted="retry")
    except Exception as exc:
        log.critical("boot_stop_cancel_all_failed account=%s", account_id, exc_info=True)
        _manual(account_id, environment, reason, f"cancel-all did not run ({type(exc).__name__})")
        return None
    finally:
        release = getattr(lock, "release", None)
        if release is not None:
            try:
                await release()
            except Exception:
                log.exception("boot_stop_writer_lock_release_failed")
    if not result.complete:
        left = [(o.currency, o.phase, o.detail) for o in result.cancel_all
                if o.phase != "acknowledged"]
        _manual(account_id, environment, reason,
                unavailable or f"cancel-all incomplete: {left}; scope_error={result.scope_error}")
    return result


async def writer_lock_or_none(lock: WriterLock) -> WriterLock | None:
    """The single-writer lock, or None when another writer holds it: that
    process owns the venue, and this one sends nothing."""
    try:
        await lock.acquire()
    except WriterLockUnacquired:
        return None
    return lock


def _manual(account_id: UUID, environment: str, reason: str, detail: str) -> None:
    log.critical("venue_offers_may_remain account=%s env=%s reason=%s detail=%s",
                 account_id, environment, reason, detail)
    alerts.emit(VENUE_OFFERS_MAY_REMAIN, level=alerts.CRITICAL,
                message="the venue may still hold funding offers: cancel them by hand",
                account=str(account_id), environment=environment, reason=reason, detail=detail)


__all__ = ["BOOT_ACTOR", "VENUE_OFFERS_MAY_REMAIN", "stop_refused_boot", "vault_readable",
           "writer_lock_or_none"]
