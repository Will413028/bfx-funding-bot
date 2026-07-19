"""SP5 plan gating — require_plan dependency factory. Mechanism only in v1.

PLAN_TIERS orders the plans; require_plan("pro") rejects any principal whose
user_profiles.plan ranks below "pro" with 403 plan_required. A missing profile
row counts as "free" (matches the column server_default — JIT provisioning may
not have run yet). v1 policy (Will, 2026-07-19): no route carries a deny rule;
SP6 multi-tenant wires the actual tier map onto endpoints.
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable

from fastapi import Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bfx_funding_bot.core.auth import Principal, require_user
from bfx_funding_bot.modules.accounts.user_profile import UserProfile
from bfx_funding_bot.modules.api.deps import get_session

PLAN_TIERS: dict[str, int] = {"free": 0, "pro": 1, "operator": 2}
_DEFAULT_PLAN = "free"


def require_plan(minimum: str) -> Callable[..., Awaitable[None]]:
    if minimum not in PLAN_TIERS:
        raise ValueError(f"unknown plan tier: {minimum!r}")
    floor = PLAN_TIERS[minimum]

    async def _check(
        user: Principal = Depends(require_user),  # noqa: B008
        session: AsyncSession = Depends(get_session),  # noqa: B008
    ) -> None:
        row = (
            await session.execute(
                select(UserProfile.plan).where(UserProfile.user_id == user.user_id)
            )
        ).scalar_one_or_none()
        plan = row if row is not None else _DEFAULT_PLAN
        # Unknown plan strings rank as free (fail-closed for gated routes,
        # fail-open for free-tier ones) rather than crashing the request.
        if PLAN_TIERS.get(plan, 0) < floor:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN, detail="plan_required"
            )

    return _check
