"""SP3 config service: get/upsert/delete the user's strategy-preference config.
No HTTP concerns — the config router maps these to endpoints/status codes.
INERT: the lending daemon never reads user_configs; this is FE-facing storage
and SP6 groundwork."""
from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bfx_funding_bot.modules.accounts.exchange_accounts import (
    require_account_membership,
)
from bfx_funding_bot.modules.accounts.exchange_accounts import (
    upsert_account_config_draft as upsert_account_config_draft_domain,
)
from bfx_funding_bot.modules.accounts.provisioning import ensure_user_profile
from bfx_funding_bot.modules.accounts.tables import AccountConfigDraft, UserConfig


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


async def get_account_config_draft(
    session: AsyncSession, *, exchange_account_id: UUID, user_id: str
) -> AccountConfigDraft | None:
    """Read an account draft after membership authorization."""
    await require_account_membership(
        session, exchange_account_id=exchange_account_id, user_id=user_id
    )
    result: AccountConfigDraft | None = await session.scalar(
        select(AccountConfigDraft).where(
            AccountConfigDraft.exchange_account_id == exchange_account_id
        )
    )
    return result


async def load_account_config_draft(
    session: AsyncSession, *, exchange_account_id: UUID
) -> AccountConfigDraft | None:
    """Load the account-owned draft for daemon bootstrap without user scope.

    A draft is optional because it is operator-facing desired state; the
    daemon must not silently treat an absent draft as applied configuration.
    """
    result: AccountConfigDraft | None = await session.scalar(
        select(AccountConfigDraft).where(
            AccountConfigDraft.exchange_account_id == exchange_account_id
        )
    )
    return result


async def upsert_account_config_draft_for_user(
    session: AsyncSession,
    *,
    exchange_account_id: UUID,
    user_id: str,
    config: dict[str, Any],
) -> AccountConfigDraft:
    """Write an account draft only for an owner/operator membership."""
    await require_account_membership(
        session, exchange_account_id=exchange_account_id, user_id=user_id, write=True
    )
    return await upsert_account_config_draft_domain(
        session,
        exchange_account_id=exchange_account_id,
        config=config,
        source="operator",
    )


async def delete_account_config_draft(
    session: AsyncSession, *, exchange_account_id: UUID, user_id: str
) -> bool:
    """Delete a draft; applied execution configuration is never affected."""
    await require_account_membership(
        session, exchange_account_id=exchange_account_id, user_id=user_id, write=True
    )
    row: AccountConfigDraft | None = await session.scalar(
        select(AccountConfigDraft).where(
            AccountConfigDraft.exchange_account_id == exchange_account_id
        )
    )
    if row is None:
        return False
    await session.delete(row)
    await session.flush()
    return True


__all__ = [
    "delete_account_config_draft",
    "delete_user_config",
    "get_account_config_draft",
    "get_user_config",
    "load_account_config_draft",
    "upsert_account_config_draft_for_user",
    "upsert_user_config",
]
