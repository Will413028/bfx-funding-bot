"""SP1 standalone web-API router. Mounted on the main.py app (separate from the
real-money daemon's healthz app). Every endpoint is gated by require_operator.
Responses use the FE envelope {"data": ...}.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bfx_funding_bot.core.auth import Principal, require_operator
from bfx_funding_bot.modules.accounts.tables import (
    ExchangeAccount,
    ExchangeAccountMembership,
)
from bfx_funding_bot.modules.api.deps import get_session
from bfx_funding_bot.modules.api.ratelimit import shared_rate_limit_dependency
from bfx_funding_bot.modules.api.schemas import ExchangeAccountResponse


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

    @router.get("/exchange-accounts")
    async def list_exchange_accounts(
        user: Principal = Depends(require_operator),  # noqa: B008
        session: AsyncSession = Depends(get_session),  # noqa: B008
    ) -> dict[str, object]:
        """Bootstrap the explicit account selector for the operator console."""
        rows = (
            await session.execute(
                select(ExchangeAccount, ExchangeAccountMembership.role)
                .join(
                    ExchangeAccountMembership,
                    ExchangeAccountMembership.exchange_account_id == ExchangeAccount.id,
                )
                .where(
                    ExchangeAccountMembership.user_id == user.user_id,
                    ExchangeAccount.lifecycle_status != "retired",
                )
                .order_by(ExchangeAccount.label, ExchangeAccount.id)
            )
        ).all()
        return {
            "data": [
                ExchangeAccountResponse(
                    exchange_account_id=str(account.id),
                    venue=account.venue,
                    label=account.label,
                    lifecycle_status=account.lifecycle_status,
                    role=role,
                ).model_dump(by_alias=True)
                for account, role in rows
            ]
        }

    return router
