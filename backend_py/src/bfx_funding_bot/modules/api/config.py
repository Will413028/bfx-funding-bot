"""SP3 configs endpoints. Thin: maps config_service results to HTTP. Every route
is gated by require_user and scoped to principal.user_id. {"data": ...} envelope.
INERT: stored config is never read by the lending daemon (SP6 will consume it)."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from bfx_funding_bot.core.auth import Principal, require_user
from bfx_funding_bot.modules.accounts import config_service
from bfx_funding_bot.modules.accounts.tables import UserConfig
from bfx_funding_bot.modules.api.deps import get_session
from bfx_funding_bot.modules.api.schemas import StrategyConfigBody, UserConfigResponse


def _to_response(row: UserConfig) -> dict[str, object]:
    return UserConfigResponse(
        id=str(row.id),
        user_id=row.user_id,
        config=dict(row.config),
        created_at=row.created_at.isoformat() if row.created_at else "",
        updated_at=row.updated_at.isoformat() if row.updated_at else "",
    ).model_dump(by_alias=True)


def build_config_router() -> APIRouter:
    router = APIRouter(prefix="/api/v1", tags=["configs"])

    @router.get("/configs")
    async def get_config(
        user: Principal = Depends(require_user),  # noqa: B008
        session: AsyncSession = Depends(get_session),  # noqa: B008
    ) -> dict[str, object]:
        row = await config_service.get_user_config(session, user_id=user.user_id)
        if row is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not_found")
        return {"data": _to_response(row)}

    @router.put("/configs")
    async def put_config(
        body: StrategyConfigBody,
        user: Principal = Depends(require_user),  # noqa: B008
        session: AsyncSession = Depends(get_session),  # noqa: B008
    ) -> dict[str, object]:
        row = await config_service.upsert_user_config(
            session, user_id=user.user_id, config=body.model_dump(by_alias=True)
        )
        return {"data": _to_response(row)}

    @router.delete("/configs", status_code=status.HTTP_204_NO_CONTENT)
    async def delete_config(
        user: Principal = Depends(require_user),  # noqa: B008
        session: AsyncSession = Depends(get_session),  # noqa: B008
    ) -> Response:
        deleted = await config_service.delete_user_config(session, user_id=user.user_id)
        if not deleted:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not_found")
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    return router
