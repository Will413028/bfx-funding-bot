"""Verify a Bitfinex key against the venue and activate it; the one-time operator bootstrap.

Both ask Bitfinex who the key belongs to and what it may do, as the operator rotation
does (``credential_rotation.KeyClient``); the secrets stay in ``modules.credentials``.
Bootstrap never rotates or replaces existing credentials.
"""
from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bfx_funding_bot.core.sealed_credential import SealedPayloadError, validate_credential
from bfx_funding_bot.external.bitfinex.auth_rest import KeyPermissions
from bfx_funding_bot.external.bitfinex.credentials import Credentials
from bfx_funding_bot.external.bitfinex.errors import BitfinexAPIError, credential_rejection
from bfx_funding_bot.modules.accounts.exchange_accounts import require_account_membership
from bfx_funding_bot.modules.accounts.tables import (
    ExchangeAccount,
    ExchangeAccountCredential,
    ExchangeAccountMembership,
)
from bfx_funding_bot.modules.credentials.vault import (
    create_account_credential,
    decrypt_credential,
    permission_error,
)
from bfx_funding_bot.modules.execution.credential_rotation import KeyClient
from bfx_funding_bot.modules.execution.protocols import AccountContext


async def verify_account_credential(
    session: AsyncSession,
    client: KeyClient,
    *,
    exchange_account_id: UUID,
    user_id: str,
    key_id: UUID,
    kek: bytes,
) -> ExchangeAccountCredential | None:
    """Verify an account credential using account UUID AAD and context."""
    await require_account_membership(
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
    ctx = AccountContext(
        account_id=str(exchange_account_id),
        credentials=decrypt_credential(row, exchange_account_id=exchange_account_id, kek=kek),
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


class BootstrapError(Exception):
    """Safe boundary: never carries database parameters or remote response text."""


async def bootstrap_credential(
    factory: async_sessionmaker[AsyncSession],
    *,
    exchange_account_id: UUID,
    owner_user_id: str,
    api_key: str,
    api_secret: str,
    kek: bytes,
    client: KeyClient,
    apply: bool = False,
) -> dict[str, str | bool]:
    """Verify against Bitfinex, then commit only with explicit operator apply.

    Requires the identity account/membership backfill already present. The
    account lock serializes bootstrap attempts; the vault's partial unique
    index also guards a concurrent activation. The key's Bitfinex user id is
    asked first and stored on the pending row: an active credential always
    names it (``ck_exchange_account_credentials_active_user_id``), so the row
    has it before verification promotes it. A key Bitfinex refuses is
    ``bootstrap_permissions_rejected`` whichever of the two calls refuses it.
    """
    # The rotation path's rule (printable ASCII, no whitespace, bounded): the key ends up
    # in a request header, so a stray newline or control character is refused here.
    try:
        validate_credential(api_key, api_secret)
    except SealedPayloadError:
        raise BootstrapError("bootstrap_input_invalid") from None
    if len(kek) != 32:
        raise BootstrapError("bootstrap_input_invalid")
    try:
        async with factory() as session, session.begin():
            account = await session.scalar(
                select(ExchangeAccount)
                .where(ExchangeAccount.id == exchange_account_id)
                .with_for_update()
            )
            if (
                account is None
                or account.venue != "bitfinex"
                or account.lifecycle_status == "retired"
            ):
                raise BootstrapError("bootstrap_account_invalid")
            owner = await session.scalar(
                select(ExchangeAccountMembership).where(
                    ExchangeAccountMembership.exchange_account_id == exchange_account_id,
                    ExchangeAccountMembership.user_id == owner_user_id,
                    ExchangeAccountMembership.role == "owner",
                )
            )
            if owner is None:
                raise BootstrapError("bootstrap_owner_required")
            existing = await session.scalar(
                select(ExchangeAccountCredential.id)
                .where(ExchangeAccountCredential.exchange_account_id == exchange_account_id)
                .limit(1)
            )
            if existing is not None:
                raise BootstrapError("bootstrap_credential_exists")
            try:
                bitfinex_user_id = await client.get_user_id(ctx=AccountContext(
                    account_id=str(exchange_account_id),
                    credentials=Credentials(api_key=api_key, api_secret=api_secret)))
            except Exception as exc:
                if credential_rejection(exc) is not None:
                    raise BootstrapError("bootstrap_permissions_rejected") from None
                raise
            row = await create_account_credential(
                session,
                exchange_account_id=exchange_account_id,
                user_id=owner_user_id,
                label="Operator bootstrap",
                api_key=api_key,
                api_secret=api_secret,
                kek=kek,
                bitfinex_user_id=bitfinex_user_id,
            )
            verified = await verify_account_credential(
                session,
                client,
                exchange_account_id=exchange_account_id,
                user_id=owner_user_id,
                key_id=row.id,
                kek=kek,
            )
            if verified is None or verified.lifecycle_status != "active":
                raise BootstrapError("bootstrap_permissions_rejected")
            if not apply:
                await session.rollback()
        return {"status": "verified", "applied": apply}
    except BootstrapError:
        raise
    except Exception:
        raise BootstrapError("bootstrap_failed") from None
