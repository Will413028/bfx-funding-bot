"""Explicit ExchangeAccount path scope and membership authorization.

Authentication identifies the Better Auth principal.  This module is the
authorization seam that binds that principal to the UUID in a request path;
no process-global account or legacy realm can select a private API response.
"""
from __future__ import annotations

from uuid import UUID

from fastapi import Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from bfx_funding_bot.core.auth import Principal, require_operator
from bfx_funding_bot.core.security_log import ACCESS_DENIED, security_event
from bfx_funding_bot.modules.api.deps import get_session
from bfx_funding_bot.modules.execution.operator_commands import (
    OperatorCommandError,
    OperatorContext,
    require_write,
    resolve_context,
)

# The context private handlers receive; the operator CLI resolves the same one.
ExchangeAccountContext = OperatorContext

_STATUS = {
    "not_found": status.HTTP_404_NOT_FOUND,
    "forbidden": status.HTTP_403_FORBIDDEN,
    "conflict": status.HTTP_409_CONFLICT,
    "invalid": status.HTTP_422_UNPROCESSABLE_CONTENT,
    "unavailable": status.HTTP_503_SERVICE_UNAVAILABLE,
}


def command_error(exc: OperatorCommandError, *,
                  context: ExchangeAccountContext | None = None) -> HTTPException:
    """An operator command's refusal as the HTTP status of its kind and its code; a
    ``forbidden`` one (a viewer's write, another principal's identity) is logged as
    ``access_denied``."""
    if exc.kind == "forbidden":
        security_event(ACCESS_DENIED, reason=exc.code,
                       user_id=context.user_id if context else None,
                       exchange_account_id=str(context.exchange_account_id) if context else None)
    return HTTPException(status_code=_STATUS[exc.kind], detail=exc.code)


async def require_account_member(
    exchange_account_id: UUID,
    user: Principal = Depends(require_operator),  # noqa: B008
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> ExchangeAccountContext:
    """Authorize one explicit account path and return its immutable context.

    Account existence, retired lifecycle, and membership failures deliberately
    collapse to ``404 not_found`` so an operator cannot enumerate other
    accounts.  The operator JWT is evaluated by FastAPI before this dependency
    reaches the database-backed account checks.
    """
    try:
        return await resolve_context(
            session, exchange_account_id=exchange_account_id, user_id=user.user_id)
    except OperatorCommandError as exc:
        if exc.kind == "not_found":
            # Absent, retired and foreign accounts look alike to the caller, not here.
            security_event(ACCESS_DENIED, reason="account_not_found_or_not_member", user_id=user.user_id,
                           exchange_account_id=str(exchange_account_id))
        raise command_error(exc) from exc


def require_account_write(context: ExchangeAccountContext) -> None:
    """Fail closed for viewer mutations after the account path is authorized."""
    try:
        require_write(context)
    except OperatorCommandError as exc:
        raise command_error(exc, context=context) from exc


__all__ = [
    "ExchangeAccountContext",
    "command_error",
    "require_account_member",
    "require_account_write",
]
