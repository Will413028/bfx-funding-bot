"""One-time operator bootstrap; never rotates or replaces existing credentials."""

from typing import Protocol
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bfx_funding_bot.core.sealed_credential import SealedPayloadError, validate_credential
from bfx_funding_bot.external.bitfinex.auth_rest import KeyPermissions
from bfx_funding_bot.external.bitfinex.errors import credential_rejection
from bfx_funding_bot.modules.accounts.tables import (
    ExchangeAccount,
    ExchangeAccountCredential,
    ExchangeAccountMembership,
)
from bfx_funding_bot.modules.accounts.vault import (
    create_account_credential,
    verify_account_credential,
)
from bfx_funding_bot.modules.execution.protocols import AccountContext, Credentials


class PermissionsClient(Protocol):
    async def get_key_permissions(self, *, ctx: AccountContext) -> KeyPermissions: ...

    async def get_user_id(self, *, ctx: AccountContext) -> int: ...


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
    client: PermissionsClient,
    apply: bool = False,
) -> dict[str, str | bool]:
    """Verify through the vault, then commit only with explicit operator apply.

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
