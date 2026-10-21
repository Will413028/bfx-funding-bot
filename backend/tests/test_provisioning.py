import pytest
from sqlalchemy import func, select

import bfx_funding_bot.modules.accounts.user_profile  # noqa: F401  register UserProfile
from bfx_funding_bot.core.db import session_scope
from bfx_funding_bot.modules.accounts.provisioning import ensure_user_profile
from bfx_funding_bot.modules.accounts.user_profile import UserProfile

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_creates_profile_when_absent(pg_session_factory):
    async with session_scope(pg_session_factory) as s:
        await ensure_user_profile(s, user_id="user_jit")
    async with session_scope(pg_session_factory) as s:
        count = await s.scalar(
            select(func.count()).select_from(UserProfile).where(UserProfile.user_id == "user_jit")
        )
    assert count == 1


@pytest.mark.asyncio
async def test_idempotent(pg_session_factory):
    async with session_scope(pg_session_factory) as s:
        await ensure_user_profile(s, user_id="user_dup")
    async with session_scope(pg_session_factory) as s:
        await ensure_user_profile(s, user_id="user_dup")
    async with session_scope(pg_session_factory) as s:
        count = await s.scalar(
            select(func.count()).select_from(UserProfile).where(UserProfile.user_id == "user_dup")
        )
    assert count == 1
