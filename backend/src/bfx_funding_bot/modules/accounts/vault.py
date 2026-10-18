"""SP2 vault service: orchestrates the api_keys table + envelope crypto. No HTTP
concerns — the api_keys router maps these to endpoints/status codes. verify also
takes an injected Bitfinex client so it stays unit-testable."""
from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Protocol
from uuid import UUID

from cryptography.exceptions import InvalidTag
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bfx_funding_bot.core.crypto import (
    Envelope,
    decrypt_secret,
    decrypt_secret_with_aad,
    encrypt_secret,
    encrypt_secret_with_aad,
)
from bfx_funding_bot.external.bitfinex.auth_rest import KeyPermissions
from bfx_funding_bot.external.bitfinex.errors import BitfinexAPIError
from bfx_funding_bot.modules.accounts.exchange_accounts import (
    AccountRetired,
    MembershipDenied,
    account_id_canonical,
    create_exchange_account_credential,
    get_exchange_account,
)
from bfx_funding_bot.modules.accounts.provisioning import ensure_user_profile
from bfx_funding_bot.modules.accounts.tables import (
    APIKey,
    ExchangeAccount,
    ExchangeAccountCredential,
    ExchangeAccountMembership,
)
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


class AccountCredentialNotConfiguredError(Exception):
    """An account has no unrevoked Bitfinex credential available for boot."""


# Scopes allowed to have write=True for a VERIFIED key. Anything else with
# write=True (withdraw, orders, a renamed dangerous scope, ...) fails closed.
_ALLOWED_WRITE_SCOPES = {"funding"}


class ActiveCredentialChangedError(Exception):
    """The account's active credential is not the one a rotation was asked to replace."""


def permission_error(perms: KeyPermissions) -> str | None:
    """Why a key's permissions may not trade, or None: funding write on, nothing else writable.

    Fail-closed default deny: a renamed or unknown write scope refuses the key too.
    """
    if not perms.can("funding", write=True):
        return "funding_write_required"
    offending = [
        scope for scope, (_read, write) in perms.scopes.items()
        if write and scope not in _ALLOWED_WRITE_SCOPES
    ]
    if not offending:
        return None
    if "withdraw" in offending:
        return "withdraw_must_be_disabled"
    return f"unexpected_write_scope:{offending[0]}"


async def _require_account_membership(
    session: AsyncSession,
    *,
    exchange_account_id: UUID,
    user_id: str,
    write: bool,
) -> None:
    account = await get_exchange_account(session, exchange_account_id=exchange_account_id)
    if account.lifecycle_status == "retired":
        raise AccountRetired(str(exchange_account_id))
    membership = await session.scalar(
        select(ExchangeAccountMembership).where(
            ExchangeAccountMembership.exchange_account_id == exchange_account_id,
            ExchangeAccountMembership.user_id == user_id,
        )
    )
    allowed = {"owner", "operator"} if write else {"owner", "operator", "viewer"}
    if membership is None or membership.role not in allowed:
        raise MembershipDenied(str(exchange_account_id))


async def list_account_credentials(
    session: AsyncSession, *, exchange_account_id: UUID, user_id: str
) -> Sequence[ExchangeAccountCredential]:
    """List credentials only after account membership has been checked."""
    await _require_account_membership(
        session, exchange_account_id=exchange_account_id, user_id=user_id, write=False
    )
    result = await session.scalars(
        select(ExchangeAccountCredential).where(
            ExchangeAccountCredential.exchange_account_id == exchange_account_id
        )
    )
    return list(result)


async def load_account_credentials(
    session: AsyncSession,
    *,
    exchange_account_id: UUID,
    kek: bytes,
) -> Credentials:
    """Load and decrypt the sole active Bitfinex credential for an account.

    The daemon uses this boundary at boot. It deliberately returns only the
    runtime ``Credentials`` value, never an ORM row or a legacy user-owned key,
    and fails closed when the account is not provisioned exactly once.
    """
    await get_exchange_account(
        session, exchange_account_id=exchange_account_id, for_command=True
    )
    rows = list(
        await session.scalars(
            select(ExchangeAccountCredential)
            .where(
                ExchangeAccountCredential.exchange_account_id == exchange_account_id,
                ExchangeAccountCredential.venue == "bitfinex",
                ExchangeAccountCredential.lifecycle_status == "active",
                ExchangeAccountCredential.verified_at.is_not(None),
                ExchangeAccountCredential.last_verify_error.is_(None),
            )
            .limit(2)
        )
    )
    if not rows:
        raise AccountCredentialNotConfiguredError(
            f"account {exchange_account_id} has no active Bitfinex credential"
        )
    if len(rows) != 1:
        raise AccountCredentialNotConfiguredError(
            f"account {exchange_account_id} has multiple active Bitfinex credentials"
        )
    row = rows[0]
    try:
        secret = decrypt_secret_with_aad(
            Envelope(
                secret_ciphertext=row.secret_ciphertext,
                secret_nonce=row.secret_nonce,
                wrapped_dek=row.wrapped_dek,
                dek_nonce=row.dek_nonce,
                key_version=row.key_version,
            ),
            aad=account_id_canonical(exchange_account_id),
            kek=kek,
        )
    except InvalidTag as exc:
        raise VaultKeyMismatchError(str(row.id)) from exc
    return Credentials(api_key=row.api_key, api_secret=secret)


async def create_account_credential(
    session: AsyncSession,
    *,
    exchange_account_id: UUID,
    user_id: str,
    label: str,
    api_key: str,
    api_secret: str,
    kek: bytes,
    bitfinex_user_id: int,
) -> ExchangeAccountCredential:
    """Create a pending credential encrypted with canonical ExchangeAccount AAD, naming the
    Bitfinex user the key belongs to: an active credential always has it (a CHECK), so it is
    stored before the row can be promoted."""
    await _require_account_membership(
        session, exchange_account_id=exchange_account_id, user_id=user_id, write=True
    )
    envelope = encrypt_secret_with_aad(
        api_secret, aad=account_id_canonical(exchange_account_id), kek=kek
    )
    return await create_exchange_account_credential(
        session,
        exchange_account_id=exchange_account_id,
        venue="bitfinex",
        label=label,
        api_key=api_key,
        secret_ciphertext=envelope.secret_ciphertext,
        secret_nonce=envelope.secret_nonce,
        wrapped_dek=envelope.wrapped_dek,
        dek_nonce=envelope.dek_nonce,
        key_version=envelope.key_version,
        bitfinex_user_id=bitfinex_user_id,
    )


async def rotate_active_credential(
    session: AsyncSession,
    *,
    exchange_account_id: UUID,
    replaces_credential_id: UUID,
    operator_request_id: UUID,
    label: str,
    api_key: str,
    api_secret: str,
    verified_at: datetime,
    kek: bytes,
    bitfinex_user_id: int,
) -> ExchangeAccountCredential:
    """Retire the active credential and make an already verified key the active one, naming
    the operator request that asked for it and the Bitfinex user the key belongs to.

    Both in the caller's transaction: either the account keeps its old active credential
    or it has exactly the new one. The account row is locked first, as promotion does in
    :func:`verify_account_credential`; the old row is retired and flushed before the new
    one is inserted, so the one-active partial unique index never sees two. Raises
    :class:`ActiveCredentialChangedError` when the active credential is not
    ``replaces_credential_id`` (or there is none).
    """
    await session.scalar(
        select(ExchangeAccount).where(ExchangeAccount.id == exchange_account_id).with_for_update()
    )
    current = await session.scalar(
        select(ExchangeAccountCredential).where(
            ExchangeAccountCredential.exchange_account_id == exchange_account_id,
            ExchangeAccountCredential.venue == "bitfinex",
            ExchangeAccountCredential.lifecycle_status == "active",
        )
    )
    if current is None or current.id != replaces_credential_id:
        raise ActiveCredentialChangedError(str(exchange_account_id))
    current.lifecycle_status = "retired"
    await session.flush()
    envelope = encrypt_secret_with_aad(
        api_secret, aad=account_id_canonical(exchange_account_id), kek=kek
    )
    return await create_exchange_account_credential(
        session,
        exchange_account_id=exchange_account_id,
        venue="bitfinex",
        label=label,
        api_key=api_key,
        secret_ciphertext=envelope.secret_ciphertext,
        secret_nonce=envelope.secret_nonce,
        wrapped_dek=envelope.wrapped_dek,
        dek_nonce=envelope.dek_nonce,
        key_version=envelope.key_version,
        lifecycle_status="active",
        verified_at=verified_at,
        operator_request_id=operator_request_id,
        bitfinex_user_id=bitfinex_user_id,
    )


async def verify_account_credential(
    session: AsyncSession,
    client: _PermissionsClient,
    *,
    exchange_account_id: UUID,
    user_id: str,
    key_id: UUID,
    kek: bytes,
) -> ExchangeAccountCredential | None:
    """Verify an account credential using account UUID AAD and context."""
    await _require_account_membership(
        session, exchange_account_id=exchange_account_id, user_id=user_id, write=True
    )
    row = await session.scalar(
        select(ExchangeAccountCredential).where(
            ExchangeAccountCredential.id == key_id,
            ExchangeAccountCredential.exchange_account_id == exchange_account_id,
        )
    )
    if row is None:
        return None
    # Revoked and retired rows are terminal audit records.  Verification must
    # never resurrect one into the sole active slot; rotation creates a new
    # pending row instead.  ``None`` tells the caller (the operator bootstrap)
    # that nothing was verified.
    if row.lifecycle_status in {"revoked", "retired"}:
        return None
    try:
        secret = decrypt_secret_with_aad(
            Envelope(
                secret_ciphertext=row.secret_ciphertext,
                secret_nonce=row.secret_nonce,
                wrapped_dek=row.wrapped_dek,
                dek_nonce=row.dek_nonce,
                key_version=row.key_version,
            ),
            aad=account_id_canonical(exchange_account_id),
            kek=kek,
        )
    except InvalidTag as exc:
        # Wrong/rotated KEK or ciphertext corruption must be a clean vault
        # boundary, never a decryption error escaping to the caller.
        raise VaultKeyMismatchError(str(key_id)) from exc
    ctx = AccountContext(
        account_id=str(exchange_account_id),
        credentials=Credentials(api_key=row.api_key, api_secret=secret),
    )
    perms: KeyPermissions | None
    try:
        perms = await client.get_key_permissions(ctx=ctx)
    except BitfinexAPIError as e:
        if e.status_code == 0 or e.status_code == 429 or e.status_code >= 500:
            raise
        perms = None  # the key was refused: recorded below, under the lock
    # Serialize every verdict for one account, then re-read the row: while Bitfinex was
    # answering, an operator rotation may have retired it (``rotate_active_credential``
    # takes the same lock), and a terminal row is never written over.  The partial unique
    # index stays the final race-proof guard for promotion.
    await session.scalar(
        select(ExchangeAccount)
        .where(ExchangeAccount.id == exchange_account_id)
        .with_for_update()
    )
    locked: ExchangeAccountCredential | None = await session.scalar(
        select(ExchangeAccountCredential)
        .where(
            ExchangeAccountCredential.id == key_id,
            ExchangeAccountCredential.exchange_account_id == exchange_account_id,
        )
        .execution_options(populate_existing=True)
    )
    if locked is None or locked.lifecycle_status in {"revoked", "retired"}:
        return None
    row = locked
    if perms is None:
        row.lifecycle_status = "revoked"
        row.verified_at = None
        row.last_verify_error = "invalid_credentials"
        await session.flush()
        return row
    refusal = permission_error(perms)
    if refusal is not None:
        row.lifecycle_status = "pending"
        row.verified_at = None
        row.last_verify_error = refusal
    else:
        competing = await session.scalar(
            select(ExchangeAccountCredential).where(
                ExchangeAccountCredential.exchange_account_id == exchange_account_id,
                ExchangeAccountCredential.venue == row.venue,
                ExchangeAccountCredential.lifecycle_status == "active",
                ExchangeAccountCredential.id != row.id,
            )
        )
        if competing is not None:
            row.lifecycle_status = "pending"
            row.verified_at = None
            row.last_verify_error = "active_credential_exists"
        else:
            row.lifecycle_status = "active"
            row.last_verify_error = None
            row.verified_at = datetime.now(UTC)
    await session.flush()
    return row


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
    "AccountCredentialNotConfiguredError",
    "ActiveCredentialChangedError",
    "Envelope",
    "KeyAlreadyExistsError",
    "VaultKeyMismatchError",
    "create_api_key",
    "delete_api_key",
    "list_account_credentials",
    "list_api_keys",
    "load_account_credentials",
    "permission_error",
    "rotate_active_credential",
    "verify_api_key",
]
