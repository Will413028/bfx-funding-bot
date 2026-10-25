"""The one-time operator bootstrap: verify an account's first Bitfinex key and activate it.

It has the operator rotation's shape (``credential_rotation``): Bitfinex is asked who the
key belongs to and what it may do (``credential_rotation.ask_bitfinex``) with no
transaction open, and the verified key is then written as the active credential in one
short transaction; the secrets stay in ``modules.credentials``. Bootstrap never rotates
or replaces existing credentials.
"""
from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bfx_funding_bot.core.sealed_credential import SealedPayloadError, validate_credential
from bfx_funding_bot.external.bitfinex.credentials import Credentials
from bfx_funding_bot.external.bitfinex.errors import BitfinexAPIError
from bfx_funding_bot.modules.accounts.tables import (
    ExchangeAccount,
    ExchangeAccountCredential,
    ExchangeAccountMembership,
)
from bfx_funding_bot.modules.credentials.vault import create_active_credential
from bfx_funding_bot.modules.execution.credential_rotation import (
    KeyClient,
    Refused,
    ask_bitfinex,
)
from bfx_funding_bot.modules.execution.protocols import AccountContext

# The one code that may follow a commit: the write's own commit, or anything unforeseen.
BOOTSTRAP_FAILED = "bootstrap_failed"


class BootstrapError(Exception):
    """Safe boundary: never carries database parameters or remote response text, only one
    of the bounded ``bootstrap_*`` codes."""

    @property
    def committed_nothing(self) -> bool:
        """Every code but :data:`BOOTSTRAP_FAILED` is raised before the write can commit."""
        return str(self) != BOOTSTRAP_FAILED


async def _check_account(
    session: AsyncSession, *, exchange_account_id: UUID, owner_user_id: str, lock: bool
) -> None:
    """The account can take its first credential: a Bitfinex account that is not retired,
    ``owner_user_id`` is its owner, and it has no credential in any state."""
    query = select(ExchangeAccount).where(ExchangeAccount.id == exchange_account_id)
    account = await session.scalar(query.with_for_update() if lock else query)
    if account is None or account.venue != "bitfinex" or account.lifecycle_status == "retired":
        raise BootstrapError("bootstrap_account_invalid")
    owner = await session.scalar(
        select(ExchangeAccountMembership.user_id).where(
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

    Requires the identity account/membership backfill already present. The account is
    checked before the key is sent anywhere, Bitfinex is asked with no transaction open,
    and the account is checked again under its lock in the transaction that inserts the
    key as active: a credential that appeared while Bitfinex answered is never joined
    (the vault's one-active partial unique index also guards a concurrent activation).
    A key Bitfinex refuses, or one that may do more than funding, is
    ``bootstrap_permissions_rejected``; the venue not answering is
    ``bootstrap_venue_unavailable``.
    Without ``apply`` the insert is rolled back.
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
        async with factory() as session:  # read only: closing rolls it back
            await _check_account(session, exchange_account_id=exchange_account_id,
                                 owner_user_id=owner_user_id, lock=False)
        try:
            bitfinex_user_id = await ask_bitfinex(client, AccountContext(
                account_id=str(exchange_account_id),
                credentials=Credentials(api_key=api_key, api_secret=api_secret)))
        except BitfinexAPIError:
            # ask_bitfinex lets one through only when the venue did not answer.
            raise BootstrapError("bootstrap_venue_unavailable") from None
        if isinstance(bitfinex_user_id, Refused):
            raise BootstrapError("bootstrap_permissions_rejected")
        async with factory() as session, session.begin():
            await _check_account(session, exchange_account_id=exchange_account_id,
                                 owner_user_id=owner_user_id, lock=True)
            await create_active_credential(
                session,
                exchange_account_id=exchange_account_id,
                label="Operator bootstrap",
                api_key=api_key,
                api_secret=api_secret,
                kek=kek,
                bitfinex_user_id=bitfinex_user_id,
                verified_at=datetime.now(UTC),
            )
            if not apply:
                await session.rollback()
        return {"status": "verified", "applied": apply}
    except BootstrapError:
        raise
    except Exception:
        raise BootstrapError(BOOTSTRAP_FAILED) from None
