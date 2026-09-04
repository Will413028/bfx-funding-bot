"""Explicit ExchangeAccount path scope and membership authorization.

Authentication identifies the Better Auth principal.  This module is the
authorization seam that binds that principal to the UUID in a request path;
no process-global account or legacy realm can select a private API response.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal
from uuid import UUID

from fastapi import Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from bfx_funding_bot.core.auth import Principal, require_operator
from bfx_funding_bot.core.settings import require_deployment_environment
from bfx_funding_bot.modules.accounts.exchange_accounts import (
    AccountNotFound,
    AccountRetired,
    AccountRole,
    MembershipDenied,
    get_exchange_account,
    require_account_membership,
)
from bfx_funding_bot.modules.api.deps import get_session


@dataclass(frozen=True, slots=True)
class ExchangeAccountContext:
    """The fully-authorized account scope passed to private handlers."""

    exchange_account_id: UUID
    user_id: str
    role: AccountRole
    lifecycle_status: Literal["active", "halted"]
    deployment_environment: str

    @property
    def can_write(self) -> bool:
        return self.role in {"owner", "operator"}


def _not_found() -> HTTPException:
    """Return one stable response for absent, retired, or unauthorized rows."""
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not_found")


def _deployment_environment() -> str:
    try:
        return require_deployment_environment()
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="deployment_environment_not_configured",
        ) from exc


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
        account = await get_exchange_account(
            session, exchange_account_id=exchange_account_id
        )
        membership = await require_account_membership(
            session,
            exchange_account_id=exchange_account_id,
            user_id=user.user_id,
        )
    except (AccountNotFound, AccountRetired, MembershipDenied) as exc:
        raise _not_found() from exc

    # ``require_account_membership`` already excludes retired accounts.  Keep
    # the narrowing explicit so the context cannot accidentally grow a command
    # path for a new lifecycle state without an authorization decision.
    if account.lifecycle_status not in {"active", "halted"}:
        raise _not_found()

    return ExchangeAccountContext(
        exchange_account_id=exchange_account_id,
        user_id=user.user_id,
        role=membership.role,  # type: ignore[arg-type]  # DB check narrows this set
        lifecycle_status=account.lifecycle_status,  # type: ignore[arg-type]
        deployment_environment=_deployment_environment(),
    )


def require_account_write(context: ExchangeAccountContext) -> None:
    """Fail closed for viewer mutations after the account path is authorized."""
    if not context.can_write:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="account_write_required",
        )


__all__ = [
    "ExchangeAccountContext",
    "require_account_member",
    "require_account_write",
]
