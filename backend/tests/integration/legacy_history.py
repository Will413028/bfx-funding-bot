"""Write legacy history for tests of what still reads the frozen event log.

The legacy runtime that appended events (the persister, the command journal, the boot
recovery) is gone (S1-8). Its readers are not: the archived execution history, the seed and
the capital comparison, the DR replay. Their tests plant the history with the event store's
own writer, as the owner, exactly as the runtime used to append it.
"""
from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bfx_funding_bot.core.db import session_scope
from bfx_funding_bot.modules.execution.event_store.store import PostgresEventStore
from bfx_funding_bot.modules.execution.event_store.writer import AccountEventWriter


async def append_legacy(
    session_factory: async_sessionmaker[AsyncSession],
    *events: object,
    environment: str = "ci",
    compatibility_mode: bool = False,
) -> list[bool]:
    """Append ``events`` in one committed transaction; True per event newly written.

    ``compatibility_mode`` relaxes the account-identity checks, as the legacy test-only
    persister did, for a history written before the account registry existed.
    """
    store = PostgresEventStore(deployment_environment=environment)
    writer = (
        AccountEventWriter(store=store, strict_identity=False, allow_missing_account=True)
        if compatibility_mode else AccountEventWriter(store=store)
    )
    async with session_scope(session_factory) as session:
        return [(await writer.append(session, event)).persisted for event in events]
