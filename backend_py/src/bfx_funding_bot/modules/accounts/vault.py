"""SP2 vault service: orchestrates the api_keys table + envelope crypto. No HTTP
concerns — the api_keys router maps these to endpoints/status codes. verify also
takes an injected Bitfinex client so it stays unit-testable."""
from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from decimal import Decimal
from typing import Protocol
from uuid import UUID

from cryptography.exceptions import InvalidTag
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


class VaultKeyMismatchError(Exception):
    """The stored ciphertext could not be decrypted with the active KEK.

    Raised when AES-GCM authentication fails (wrong/rotated KEK or corrupted
    record). The router maps this to 503 (vault_key_mismatch) rather than letting
    the raw crypto InvalidTag bubble up as an unhandled 500.

    NOTE: key_version-aware multi-KEK rotation is intentionally NOT implemented
    yet (follow-up). This only turns the crash into a clean domain error.
    """


# Scopes allowed to have write=True for a VERIFIED key. Anything else with
# write=True (withdraw, orders, a renamed dangerous scope, ...) fails closed.
_ALLOWED_WRITE_SCOPES = {"funding"}


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

    try:
        secret = decrypt_secret(
            Envelope(
                secret_ciphertext=row.secret_ciphertext, secret_nonce=row.secret_nonce,
                wrapped_dek=row.wrapped_dek, dek_nonce=row.dek_nonce, key_version=row.key_version,
            ),
            user_id=user_id, kek=kek,
        )
    except InvalidTag as e:
        # Wrong/rotated KEK or corrupted ciphertext: AES-GCM auth fails. Surface
        # a domain error (router -> 503) instead of an unhandled 500. Status
        # unchanged. (key_version-aware multi-KEK rotation: follow-up.)
        raise VaultKeyMismatchError(str(key_id)) from e
    ctx = AccountContext(
        account_id=str(row.id),
        credentials=Credentials(api_key=row.api_key, api_secret=secret),
        allocation_cap_usdt=Decimal("0"),
    )

    try:
        perms = await client.get_key_permissions(ctx=ctx)
    except BitfinexAPIError as e:
        # Transient: transport (0), rate limit (429), server/maintenance (5xx).
        # Re-raise -> router 502, stored status UNCHANGED (don't demote a good
        # key just because the exchange is temporarily unavailable). Only genuine
        # client/credential errors (other 4xx: 400/401/403) demote the row.
        if e.status_code == 0 or e.status_code == 429 or e.status_code >= 500:
            raise
        row.exchange_status = "failed"
        row.last_verify_error = "invalid_credentials"
        await session.flush()
        return row

    # Fail-closed default-deny: VERIFIED iff funding-write is ON AND no scope
    # other than the allowed funding scope has write=True. A renamed/unknown
    # dangerous write scope must NOT slip through as verified.
    if not perms.can("funding", write=True):
        row.exchange_status = "failed"
        row.last_verify_error = "funding_write_required"
    else:
        offending = [
            scope for scope, (_read, write) in perms.scopes.items()
            if write and scope not in _ALLOWED_WRITE_SCOPES
        ]
        if offending:
            row.exchange_status = "failed"
            if "withdraw" in offending:
                row.last_verify_error = "withdraw_must_be_disabled"
            else:
                row.last_verify_error = f"unexpected_write_scope:{offending[0]}"
        else:
            row.exchange_status = "verified"
            row.verified_at = datetime.now(UTC)
            row.last_verify_error = None
    await session.flush()
    return row


__all__ = [
    "Envelope",
    "KeyAlreadyExistsError",
    "VaultKeyMismatchError",
    "create_api_key",
    "delete_api_key",
    "list_api_keys",
    "verify_api_key",
]
