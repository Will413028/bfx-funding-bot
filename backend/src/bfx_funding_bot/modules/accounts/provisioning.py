"""JIT provisioning of the app-side user_profiles row. Shared by SP2 vault and
later SP3+ endpoints: any authenticated write ensures the principal's profile
exists before inserting owned rows.
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bfx_funding_bot.modules.accounts.user_profile import UserProfile


async def ensure_user_profile(session: AsyncSession, *, user_id: str, plan: str = "free") -> None:
    """Insert a user_profiles row for user_id if absent. Idempotent; flushes so
    a subsequent FK insert in the same transaction sees the row."""
    existing = await session.scalar(
        select(UserProfile.id).where(UserProfile.user_id == user_id)
    )
    if existing is None:
        session.add(UserProfile(user_id=user_id, plan=plan))
        await session.flush()
