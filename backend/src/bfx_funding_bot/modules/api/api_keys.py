"""Account-scoped credential reads.

The UUID in ``/exchange-accounts/{exchange_account_id}`` is the only private
API scope. Credentials are lifecycle records and their plaintext secret is
never returned. The web API only lists them: a credential is written by the
operator CLI alone -- the one-time bootstrap (``scripts/bootstrap_account_credential.py``)
and ``bfx-op rotate-credential`` (ADR 2026-10-15 D4) -- and the database refuses
the web API's role any INSERT or UPDATE on them (36e59526f8cf).
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from bfx_funding_bot.modules.accounts.exchange_accounts import (
    AccountRetired,
    MembershipDenied,
    list_account_credentials,
)
from bfx_funding_bot.modules.accounts.tables import ExchangeAccountCredential
from bfx_funding_bot.modules.api.account_scope import (
    ExchangeAccountContext,
    require_account_member,
)
from bfx_funding_bot.modules.api.deps import get_session
from bfx_funding_bot.modules.api.ratelimit import shared_rate_limit_dependency
from bfx_funding_bot.modules.api.schemas import (
    AccountCredentialResponse,
    Envelope,
)


def _account_credential_to_response(
    row: ExchangeAccountCredential,
) -> AccountCredentialResponse:
    return AccountCredentialResponse(
        id=str(row.id),
        exchange_account_id=str(row.exchange_account_id),
        label=row.label,
        api_key=row.api_key,
        status=row.lifecycle_status,  # the CHECK and the response Literal agree
        created_at=row.created_at.isoformat() if row.created_at else "",
        verified_at=row.verified_at.isoformat(),
    )


def _scope_not_found(exc: Exception) -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not_found")


def build_api_keys_router() -> APIRouter:
    router = APIRouter(
        prefix="/api/v1", tags=["api-keys"],
        dependencies=[Depends(shared_rate_limit_dependency())],
    )

    @router.get("/exchange-accounts/{exchange_account_id}/credentials")
    async def list_account_keys(
        context: ExchangeAccountContext = Depends(require_account_member),  # noqa: B008
        session: AsyncSession = Depends(get_session),  # noqa: B008
    ) -> Envelope[list[AccountCredentialResponse]]:
        try:
            rows = await list_account_credentials(
                session,
                exchange_account_id=context.exchange_account_id,
                user_id=context.user_id,
            )
        except (AccountRetired, MembershipDenied) as exc:
            raise _scope_not_found(exc) from exc
        return Envelope(data=[_account_credential_to_response(row) for row in rows])

    return router
