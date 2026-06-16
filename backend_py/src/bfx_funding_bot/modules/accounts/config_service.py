"""SP3 config service: get/upsert/delete the user's strategy-preference config.
No HTTP concerns — the config router maps these to endpoints/status codes.
INERT: the lending daemon never reads user_configs; this is FE-facing storage
and SP6 groundwork."""
from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bfx_funding_bot.modules.accounts.provisioning import ensure_user_profile
from bfx_funding_bot.modules.accounts.tables import UserConfig


async def get_user_config(session: AsyncSession, *, user_id: str) -> UserConfig | None:
    result: UserConfig | None = await session.scalar(
        select(UserConfig).where(UserConfig.user_id == user_id)
    )
    return result


async def upsert_user_config(
    session: AsyncSession, *, user_id: str, config: dict[str, Any]
) -> UserConfig:
    """Create the user's config row, or overwrite it if one exists (one row per
    user). ensure_user_profile first so the DB-level FK to user_profiles is
    satisfied on insert. The idx_user_configs_user_id UNIQUE is the race backstop
    for a rare concurrent double-insert (single operator in v1 -> no contention)."""
    await ensure_user_profile(session, user_id=user_id)
    row: UserConfig | None = await session.scalar(
        select(UserConfig).where(UserConfig.user_id == user_id)
    )
    if row is None:
        row = UserConfig(user_id=user_id, config=config)
        session.add(row)
    else:
        row.config = config
    await session.flush()
    await session.refresh(row)
    return row


async def delete_user_config(session: AsyncSession, *, user_id: str) -> bool:
    """Fetch-then-delete mirrors vault.py's delete_api_key: session.delete() +
    flush() avoids CursorResult.rowcount type ambiguity and is idiomatic for
    ORM-tracked rows."""
    row: UserConfig | None = await session.scalar(
        select(UserConfig).where(UserConfig.user_id == user_id)
    )
    if row is None:
        return False
    await session.delete(row)
    await session.flush()
    return True


__all__ = ["delete_user_config", "get_user_config", "upsert_user_config"]
