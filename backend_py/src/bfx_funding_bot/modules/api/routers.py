"""SP1 standalone web-API router. Mounted on the main.py app (separate from the
real-money daemon's healthz app). Every endpoint is gated by require_user.
Responses use the FE envelope {"data": ...}.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends

from bfx_funding_bot.core.auth import Principal, require_user


def build_router() -> APIRouter:
    router = APIRouter(prefix="/api/v1", tags=["api"])

    @router.get("/profile")
    async def get_profile(user: Principal = Depends(require_user)) -> dict[str, object]:  # noqa: B008
        return {
            "data": {
                "userId": user.user_id,
                "email": user.email,
                "role": user.role,
            }
        }

    return router
