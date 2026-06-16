"""SP2 api-keys endpoints. Thin: maps vault service results to HTTP. Every route
is gated by require_user and scoped to principal.user_id. {"data": ...} envelope."""
from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from bfx_funding_bot.core.auth import Principal, require_user
from bfx_funding_bot.core.crypto import VaultNotConfiguredError, load_kek
from bfx_funding_bot.external.bitfinex.auth_rest import BitfinexAuthREST
from bfx_funding_bot.external.bitfinex.errors import BitfinexAPIError, BitfinexShapeError
from bfx_funding_bot.modules.accounts import vault
from bfx_funding_bot.modules.accounts.tables import APIKey
from bfx_funding_bot.modules.api.deps import get_bitfinex_auth_rest, get_session
from bfx_funding_bot.modules.api.schemas import (
    ApiKeyResponse,
    CreateApiKeyRequest,
    VerifyResultResponse,
)


def _to_response(row: APIKey) -> dict[str, object]:
    return ApiKeyResponse(
        id=str(row.id),
        label=row.label,
        api_key=row.api_key,
        exchange_status=row.exchange_status,
        created_at=row.created_at.isoformat() if row.created_at else "",
        verified_at=row.verified_at.isoformat() if row.verified_at else None,
        last_verify_error=row.last_verify_error,
    ).model_dump(by_alias=True)


def _require_kek() -> bytes:
    try:
        return load_kek()
    except VaultNotConfiguredError as e:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="vault_not_configured"
        ) from e


def build_api_keys_router() -> APIRouter:
    router = APIRouter(prefix="/api/v1", tags=["api-keys"])

    @router.get("/api-keys")
    async def list_keys(
        user: Principal = Depends(require_user),  # noqa: B008
        session: AsyncSession = Depends(get_session),  # noqa: B008
    ) -> dict[str, object]:
        rows = await vault.list_api_keys(session, user_id=user.user_id)
        return {"data": [_to_response(r) for r in rows]}

    @router.post("/api-keys", status_code=status.HTTP_201_CREATED)
    async def create_key(
        body: CreateApiKeyRequest,
        user: Principal = Depends(require_user),  # noqa: B008
        session: AsyncSession = Depends(get_session),  # noqa: B008
    ) -> dict[str, object]:
        kek = _require_kek()
        try:
            row = await vault.create_api_key(
                session, user_id=user.user_id, label=body.label,
                api_key=body.api_key, api_secret=body.api_secret, kek=kek,
            )
        except (vault.KeyAlreadyExistsError, IntegrityError) as e:
            # KeyAlreadyExistsError: app-level pre-check. IntegrityError: the DB
            # unique index idx_api_keys_user_id fires on a concurrent duplicate
            # INSERT that slipped past the pre-check (race) -> still a 409.
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT, detail="key_already_exists"
            ) from e
        return {"data": _to_response(row)}

    @router.post("/api-keys/{key_id}/verify")
    async def verify_key(
        key_id: UUID,
        user: Principal = Depends(require_user),  # noqa: B008
        session: AsyncSession = Depends(get_session),  # noqa: B008
        client: BitfinexAuthREST = Depends(get_bitfinex_auth_rest),  # noqa: B008
    ) -> dict[str, object]:
        kek = _require_kek()
        try:
            row = await vault.verify_api_key(
                session, client, user_id=user.user_id, key_id=key_id, kek=kek,
            )
        except vault.VaultKeyMismatchError as e:
            # Stored ciphertext can't be decrypted with the active KEK (wrong/
            # rotated KEK or corruption). 503, not a raw 500.
            # NOTE: key_version-aware multi-KEK rotation is NOT implemented yet
            # (follow-up); for now this is a hard "vault misconfigured" signal.
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="vault_key_mismatch"
            ) from e
        except (BitfinexAPIError, BitfinexShapeError) as e:
            # transport error (status 0, re-raised by verify) OR malformed upstream
            # permissions response -> the exchange is unreachable/unusable, 502.
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY, detail="exchange_unreachable"
            ) from e
        if row is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not_found")
        return {"data": VerifyResultResponse(
            status=row.exchange_status, error=row.last_verify_error
        ).model_dump()}

    @router.delete("/api-keys/{key_id}", status_code=status.HTTP_204_NO_CONTENT)
    async def delete_key(
        key_id: UUID,
        user: Principal = Depends(require_user),  # noqa: B008
        session: AsyncSession = Depends(get_session),  # noqa: B008
    ) -> Response:
        deleted = await vault.delete_api_key(session, user_id=user.user_id, key_id=key_id)
        if not deleted:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not_found")
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    return router
