"""Account-scoped strategy configuration draft endpoints.

The draft is user-editable control-plane input. It is not applied daemon state
and is always addressed through an explicit ExchangeAccount UUID.
"""
from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from bfx_funding_bot.modules.accounts import config_service
from bfx_funding_bot.modules.accounts.exchange_accounts import AccountRetired, MembershipDenied
from bfx_funding_bot.modules.accounts.tables import AccountConfigDraft
from bfx_funding_bot.modules.api.account_scope import (
    ExchangeAccountContext,
    require_account_member,
    require_account_write,
)
from bfx_funding_bot.modules.api.deps import get_session
from bfx_funding_bot.modules.api.ratelimit import shared_rate_limit_dependency
from bfx_funding_bot.modules.api.schemas import (
    AccountConfigDraftResponse,
    Envelope,
    StrategyConfigBody,
    field_paths,
)

logger = logging.getLogger(__name__)


def _stored_config(stored: Any) -> StrategyConfigBody | None:
    """The stored draft, or None when it is not exactly a ``StrategyConfigBody``.

    The PUT stores ``StrategyConfigBody.model_dump(by_alias=True)``, so a draft that does not
    parse, or does not dump back to what is stored (a key this schema no longer has), was
    written under another schema. It is reported as unreadable rather than failing the read,
    so the console still shows that a draft exists and can overwrite it; the log names the
    fields, never their values.
    """
    try:
        config = StrategyConfigBody.model_validate(stored)
    except ValidationError as exc:
        logger.error("config_draft_unreadable: %s", field_paths(exc))
        return None
    dumped = config.model_dump(by_alias=True)
    if dumped != stored:
        logger.error("config_draft_unreadable: %s", sorted(
            key for key in set(dumped) | set(stored) if dumped.get(key) != stored.get(key)))
        return None
    return config


def _account_to_response(row: AccountConfigDraft) -> Envelope[AccountConfigDraftResponse]:
    config = _stored_config(row.config)
    return Envelope(data=AccountConfigDraftResponse(
        id=str(row.id),
        exchange_account_id=str(row.exchange_account_id),
        config=config,
        config_error=None if config is not None else "config_unreadable",
        revision=row.revision,
        source=row.source,
        created_at=row.created_at.isoformat() if row.created_at else "",
        updated_at=row.updated_at.isoformat() if row.updated_at else "",
    ))


def _scope_not_found(exc: Exception) -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not_found")


def build_config_router() -> APIRouter:
    router = APIRouter(
        prefix="/api/v1", tags=["configs"],
        dependencies=[Depends(shared_rate_limit_dependency())],
    )

    @router.get("/exchange-accounts/{exchange_account_id}/config-draft")
    async def get_account_config(
        context: ExchangeAccountContext = Depends(require_account_member),  # noqa: B008
        session: AsyncSession = Depends(get_session),  # noqa: B008
    ) -> Envelope[AccountConfigDraftResponse]:
        try:
            row = await config_service.get_account_config_draft(
                session,
                exchange_account_id=context.exchange_account_id,
                user_id=context.user_id,
            )
        except (AccountRetired, MembershipDenied) as exc:
            raise _scope_not_found(exc) from exc
        if row is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not_found")
        return _account_to_response(row)

    @router.put("/exchange-accounts/{exchange_account_id}/config-draft")
    async def put_account_config(
        body: StrategyConfigBody,
        context: ExchangeAccountContext = Depends(require_account_member),  # noqa: B008
        session: AsyncSession = Depends(get_session),  # noqa: B008
    ) -> Envelope[AccountConfigDraftResponse]:
        require_account_write(context)
        try:
            row = await config_service.upsert_account_config_draft_for_user(
                session,
                exchange_account_id=context.exchange_account_id,
                user_id=context.user_id,
                config=body.model_dump(by_alias=True),
            )
        except (AccountRetired, MembershipDenied) as exc:
            raise _scope_not_found(exc) from exc
        return _account_to_response(row)

    @router.delete(
        "/exchange-accounts/{exchange_account_id}/config-draft",
        status_code=status.HTTP_204_NO_CONTENT,
    )
    async def delete_account_config(
        context: ExchangeAccountContext = Depends(require_account_member),  # noqa: B008
        session: AsyncSession = Depends(get_session),  # noqa: B008
    ) -> Response:
        require_account_write(context)
        try:
            deleted = await config_service.delete_account_config_draft(
                session,
                exchange_account_id=context.exchange_account_id,
                user_id=context.user_id,
            )
        except (AccountRetired, MembershipDenied) as exc:
            raise _scope_not_found(exc) from exc
        if not deleted:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not_found")
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    return router
