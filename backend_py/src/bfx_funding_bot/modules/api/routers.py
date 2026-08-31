"""SP1 standalone web-API router. Mounted on the main.py app (separate from the
real-money daemon's healthz app). Every endpoint is gated by require_operator.
Responses use the FE envelope {"data": ...}.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends

from bfx_funding_bot.core.auth import Principal, require_operator
from bfx_funding_bot.modules.api.ratelimit import shared_rate_limit_dependency


def build_router() -> APIRouter:
    router = APIRouter(
        prefix="/api/v1", tags=["api"],
        dependencies=[Depends(shared_rate_limit_dependency())],
    )

    @router.get("/profile")
    async def get_profile(user: Principal = Depends(require_operator)) -> dict[str, object]:  # noqa: B008
        return {
            "data": {
                "userId": user.user_id,
                "email": user.email,
                "role": user.role,
            }
        }

    return router
