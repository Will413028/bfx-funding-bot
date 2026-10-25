"""Exchange credential secrets: envelope encryption, decryption and the key-permission policy.

Only the bot and the operator CLI import this module; the webapi reads credential
metadata through ``accounts.exchange_accounts`` and is forbidden from reaching here
(``control-plane-exposure`` in pyproject.toml).
"""
from __future__ import annotations

from datetime import datetime
from uuid import UUID

from cryptography.exceptions import InvalidTag
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bfx_funding_bot.core.crypto import (
    Envelope,
    decrypt_secret_with_aad,
    encrypt_secret_with_aad,
)
from bfx_funding_bot.external.bitfinex.auth_rest import KeyPermissions
from bfx_funding_bot.external.bitfinex.credentials import Credentials
from bfx_funding_bot.modules.accounts.exchange_accounts import (
    account_id_canonical,
    create_exchange_account_credential,
    get_exchange_account,
)
from bfx_funding_bot.modules.accounts.tables import (
    ExchangeAccount,
    ExchangeAccountCredential,
)


class VaultKeyMismatchError(Exception):
    """The stored ciphertext could not be decrypted with the active KEK.

    Raised when AES-GCM authentication fails (wrong KEK or corrupted record) instead of
    the raw ``InvalidTag``; the bot refuses to boot on it. One KEK decrypts every row:
    a new KEK comes with a new Bitfinex key through ``bfx-op rotate-credential``, never a
    re-wrap (ADR 2026-08-31 Amendment 2026-10-22, which also names when KMS and
    ``key_version``-aware decryption come in: the first key that is not the owner's).
    """


class AccountCredentialNotConfiguredError(Exception):
    """An account has no unrevoked Bitfinex credential available for boot."""


class ActiveCredentialChangedError(Exception):
    """The account's active credential is not the one a rotation was asked to replace."""


# Scopes allowed to have write=True for a VERIFIED key. Anything else with
# write=True (withdraw, orders, a renamed dangerous scope, ...) fails closed.
_ALLOWED_WRITE_SCOPES = {"funding"}


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


def decrypt_credential(
    row: ExchangeAccountCredential, *, exchange_account_id: UUID, kek: bytes
) -> Credentials:
    """Decrypt one credential row under its account's canonical UUID as AAD.

    Wrong KEK or ciphertext corruption is a clean :class:`VaultKeyMismatchError`,
    never a decryption error escaping to the caller.
    """
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
    return decrypt_credential(row, exchange_account_id=exchange_account_id, kek=kek)


async def create_active_credential(
    session: AsyncSession,
    *,
    exchange_account_id: UUID,
    label: str,
    api_key: str,
    api_secret: str,
    kek: bytes,
    bitfinex_user_id: int,
    verified_at: datetime,
) -> ExchangeAccountCredential:
    """Insert an already verified key as the account's active credential, encrypted with
    canonical ExchangeAccount AAD and naming the Bitfinex user it belongs to.

    The caller (``execution.credential_bootstrap``) asked Bitfinex first and holds the
    account lock; the one-active partial unique index refuses a second active row.
    """
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
    or it has exactly the new one. The account row is locked first, as the bootstrap's
    write does (``execution.credential_bootstrap``); the old row is retired
    and flushed before the new one is inserted, so the one-active partial unique index
    never sees two. Raises
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
