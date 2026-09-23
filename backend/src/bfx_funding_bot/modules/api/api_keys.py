"""Account-scoped credential endpoints.

The UUID in ``/exchange-accounts/{exchange_account_id}`` is the only private
API scope. Credentials are lifecycle records and their plaintext secret is
never returned.
"""
from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from bfx_funding_bot.core.crypto import VaultNotConfiguredError, load_kek
from bfx_funding_bot.external.bitfinex.auth_rest import BitfinexAuthREST
from bfx_funding_bot.external.bitfinex.errors import BitfinexAPIError, BitfinexShapeError
from bfx_funding_bot.modules.accounts import vault
from bfx_funding_bot.modules.accounts.exchange_accounts import (
    AccountRetired,
    ActiveCredentialConflict,
    MembershipDenied,
)
from bfx_funding_bot.modules.accounts.tables import ExchangeAccountCredential
from bfx_funding_bot.modules.api.account_scope import (
    ExchangeAccountContext,
    require_account_member,
    require_account_write,
)
from bfx_funding_bot.modules.api.deps import get_bitfinex_auth_rest, get_session
from bfx_funding_bot.modules.api.ratelimit import shared_rate_limit_dependency
from bfx_funding_bot.modules.api.schemas import (
    AccountCredentialResponse,
    CreateApiKeyRequest,
)


def _require_kek() -> bytes:
    try:
        return load_kek()
    except VaultNotConfiguredError as e:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="vault_not_configured"
        ) from e


def _credential_status(row: ExchangeAccountCredential) -> str:
    if row.lifecycle_status != "active":
        return row.lifecycle_status
    if row.last_verify_error:
        return "failed"
    if row.verified_at is not None:
        return "verified"
    return "unverified"


def _account_credential_to_response(
    row: ExchangeAccountCredential,
) -> dict[str, object]:
    return AccountCredentialResponse(
        id=str(row.id),
        exchange_account_id=str(row.exchange_account_id),
        label=row.label,
        api_key=row.api_key,
        status=_credential_status(row),
        created_at=row.created_at.isoformat() if row.created_at else "",
        verified_at=row.verified_at.isoformat() if row.verified_at else None,
        last_verify_error=row.last_verify_error,
    ).model_dump(by_alias=True)


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
    ) -> dict[str, object]:
        try:
            rows = await vault.list_account_credentials(
                session,
                exchange_account_id=context.exchange_account_id,
                user_id=context.user_id,
            )
        except (AccountRetired, MembershipDenied) as exc:
            raise _scope_not_found(exc) from exc
        return {"data": [_account_credential_to_response(row) for row in rows]}

    @router.post(
        "/exchange-accounts/{exchange_account_id}/credentials",
        status_code=status.HTTP_201_CREATED,
    )
    async def create_account_key(
        body: CreateApiKeyRequest,
        context: ExchangeAccountContext = Depends(require_account_member),  # noqa: B008
        session: AsyncSession = Depends(get_session),  # noqa: B008
    ) -> dict[str, object]:
        require_account_write(context)
        kek = _require_kek()
        try:
            row = await vault.create_account_credential(
                session,
                exchange_account_id=context.exchange_account_id,
                user_id=context.user_id,
                label=body.label,
                api_key=body.api_key,
                api_secret=body.api_secret,
                kek=kek,
            )
        except (ActiveCredentialConflict, IntegrityError) as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="credential_already_exists",
            ) from exc
        except (AccountRetired, MembershipDenied) as exc:
            raise _scope_not_found(exc) from exc
        return {"data": _account_credential_to_response(row)}

    @router.post("/exchange-accounts/{exchange_account_id}/credentials/{key_id}/verify")
    async def verify_account_key(
        key_id: UUID,
        context: ExchangeAccountContext = Depends(require_account_member),  # noqa: B008
        session: AsyncSession = Depends(get_session),  # noqa: B008
        client: BitfinexAuthREST = Depends(get_bitfinex_auth_rest),  # noqa: B008
    ) -> dict[str, object]:
        require_account_write(context)
        kek = _require_kek()
        try:
            row = await vault.verify_account_credential(
                session,
                client,
                exchange_account_id=context.exchange_account_id,
                user_id=context.user_id,
                key_id=key_id,
                kek=kek,
            )
        except vault.VaultKeyMismatchError as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="vault_key_mismatch",
            ) from exc
        except (BitfinexAPIError, BitfinexShapeError) as exc:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="exchange_unreachable",
            ) from exc
        except (AccountRetired, MembershipDenied) as exc:
            raise _scope_not_found(exc) from exc
        if row is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not_found")
        return {
            "data": {
                "status": _credential_status(row),
                "error": row.last_verify_error,
            }
        }

    @router.delete(
        "/exchange-accounts/{exchange_account_id}/credentials/{key_id}",
        status_code=status.HTTP_204_NO_CONTENT,
    )
    async def delete_account_key(
        key_id: UUID,
        context: ExchangeAccountContext = Depends(require_account_member),  # noqa: B008
        session: AsyncSession = Depends(get_session),  # noqa: B008
    ) -> Response:
        require_account_write(context)
        try:
            deleted = await vault.delete_account_credential(
                session,
                exchange_account_id=context.exchange_account_id,
                user_id=context.user_id,
                key_id=key_id,
            )
        except (AccountRetired, MembershipDenied) as exc:
            raise _scope_not_found(exc) from exc
        if not deleted:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not_found")
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    return router
