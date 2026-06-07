"""SP2 vault service: orchestrates the api_keys table + envelope crypto. No HTTP
concerns — the api_keys router maps these to endpoints/status codes. verify also
takes an injected Bitfinex client so it stays unit-testable."""
from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from decimal import Decimal
from typing import Protocol
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bfx_funding_bot.core.crypto import Envelope, decrypt_secret, encrypt_secret
from bfx_funding_bot.external.bitfinex.auth_rest import KeyPermissions
from bfx_funding_bot.external.bitfinex.errors import BitfinexAPIError
from bfx_funding_bot.modules.accounts.provisioning import ensure_user_profile
from bfx_funding_bot.modules.accounts.tables import APIKey
from bfx_funding_bot.modules.execution.protocols import AccountContext, Credentials


class KeyAlreadyExistsError(Exception):
    """User already has a key (single-key-per-user invariant)."""


async def list_api_keys(session: AsyncSession, *, user_id: str) -> Sequence[APIKey]:
    result = await session.scalars(select(APIKey).where(APIKey.user_id == user_id))
    return list(result)


async def create_api_key(
    session: AsyncSession, *, user_id: str, label: str,
    api_key: str, api_secret: str, kek: bytes,
) -> APIKey:
    existing = await session.scalar(select(APIKey).where(APIKey.user_id == user_id))
    if existing is not None:
        raise KeyAlreadyExistsError()
    await ensure_user_profile(session, user_id=user_id)
    env = encrypt_secret(api_secret, user_id=user_id, kek=kek)
    row = APIKey(
        user_id=user_id, label=label, api_key=api_key,
        secret_ciphertext=env.secret_ciphertext, secret_nonce=env.secret_nonce,
        wrapped_dek=env.wrapped_dek, dek_nonce=env.dek_nonce, key_version=env.key_version,
    )
    session.add(row)
    await session.flush()
    await session.refresh(row)
    return row


async def delete_api_key(session: AsyncSession, *, user_id: str, key_id: UUID) -> bool:
    row = await session.scalar(
        select(APIKey).where(APIKey.id == key_id, APIKey.user_id == user_id)
    )
    if row is None:
        return False
    await session.delete(row)
    await session.flush()
    return True


class _PermissionsClient(Protocol):
    async def get_key_permissions(self, *, ctx: AccountContext) -> KeyPermissions: ...


async def verify_api_key(
    session: AsyncSession, client: _PermissionsClient, *,
    user_id: str, key_id: UUID, kek: bytes,
) -> APIKey | None:
    """Load the user's key, decrypt, check Bitfinex permissions fail-closed
    (funding write on AND withdraw off), persist the verdict. Returns None if no
    such key for this user (router -> 404). Re-raises BitfinexAPIError with
    status_code==0 (transport) so the router can map it to 502; any other
    Bitfinex API error marks the key failed/invalid_credentials."""
    row = await session.scalar(
        select(APIKey).where(APIKey.id == key_id, APIKey.user_id == user_id)
    )
    if row is None:
        return None

    secret = decrypt_secret(
        Envelope(
            secret_ciphertext=row.secret_ciphertext, secret_nonce=row.secret_nonce,
            wrapped_dek=row.wrapped_dek, dek_nonce=row.dek_nonce, key_version=row.key_version,
        ),
        user_id=user_id, kek=kek,
    )
    ctx = AccountContext(
        account_id=str(row.id),
        credentials=Credentials(api_key=row.api_key, api_secret=secret),
        allocation_cap_usdt=Decimal("0"),
    )

    try:
        perms = await client.get_key_permissions(ctx=ctx)
    except BitfinexAPIError as e:
        if e.status_code == 0:
            raise  # transport/unreachable -> router 502, status unchanged
        row.exchange_status = "failed"
        row.last_verify_error = "invalid_credentials"
        await session.flush()
        return row

    if not perms.can("funding", write=True):
        row.exchange_status = "failed"
        row.last_verify_error = "funding_write_required"
    elif perms.can("withdraw", write=True):
        row.exchange_status = "failed"
        row.last_verify_error = "withdraw_must_be_disabled"
    else:
        row.exchange_status = "verified"
        row.verified_at = datetime.now(UTC)
        row.last_verify_error = None
    await session.flush()
    return row


__all__ = [
    "Envelope",
    "KeyAlreadyExistsError",
    "create_api_key",
    "delete_api_key",
    "list_api_keys",
    "verify_api_key",
]
