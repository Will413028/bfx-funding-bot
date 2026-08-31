"""Canonical exchange-account identity and account-scoped domain services.

The identity in this module is deliberately separate from Better Auth's user
identity.  A user is a principal; an ``ExchangeAccount`` is the money-domain
aggregate to which credentials, config drafts, and execution projections
belong.
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Literal, cast
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bfx_funding_bot.modules.accounts.tables import (
    AccountConfigDraft,
    ExchangeAccount,
    ExchangeAccountCredential,
    ExchangeAccountMembership,
)

AccountRole = Literal["owner", "operator", "viewer"]
AccountLifecycleStatus = Literal["active", "halted", "retired"]
CredentialLifecycleStatus = Literal["active", "revoked", "retired"]


class AccountDomainError(Exception):
    """Base class for account identity and authorization failures."""


class AccountNotFound(AccountDomainError):  # noqa: N818 - public domain name
    """The requested account does not exist in the account registry."""


class AccountRetired(AccountDomainError):  # noqa: N818 - public domain name
    """A command attempted to use an account that is permanently retired."""


class MembershipDenied(AccountDomainError):  # noqa: N818 - public domain name
    """The principal has no permitted membership for the requested account."""


class ActiveCredentialConflict(AccountDomainError):  # noqa: N818 - public domain name
    """An account already has an active credential for the venue."""


def account_id_canonical(value: UUID | str) -> str:
    """Return the lowercase hyphenated UUID string used as credential AAD.

    Legacy realm labels and the implicit ``default`` account are intentionally
    rejected instead of being normalized into an account identity.
    """
    try:
        return str(value if isinstance(value, UUID) else UUID(value))
    except (AttributeError, TypeError, ValueError) as exc:
        raise ValueError(f"account identity must be a UUID, got {value!r}") from exc


def validate_membership_role(value: str) -> AccountRole:
    """Validate and narrow a membership role to the closed domain set."""
    if value not in {"owner", "operator", "viewer"}:
        raise ValueError(f"membership role must be owner, operator, or viewer; got {value!r}")
    return cast(AccountRole, value)


def validate_account_lifecycle_status(value: str) -> AccountLifecycleStatus:
    """Validate and narrow an account lifecycle status."""
    if value not in {"active", "halted", "retired"}:
        raise ValueError(f"account lifecycle status is invalid: {value!r}")
    return cast(AccountLifecycleStatus, value)


def validate_credential_lifecycle_status(value: str) -> CredentialLifecycleStatus:
    """Validate and narrow a credential lifecycle status."""
    if value not in {"active", "revoked", "retired"}:
        raise ValueError(f"credential lifecycle status is invalid: {value!r}")
    return cast(CredentialLifecycleStatus, value)


def ensure_account_active(account: ExchangeAccount) -> None:
    """Reject commands targeting a retired account.

    Halted is a reversible operational state; callers that need to block
    trading while halted apply that policy at the execution gate.  Identity
    and draft-management operations only need the irreversible retired guard.
    """
    validate_account_lifecycle_status(account.lifecycle_status)
    if account.lifecycle_status == "retired":
        raise AccountRetired(str(account.id))


async def get_exchange_account(
    session: AsyncSession, *, exchange_account_id: UUID, for_command: bool = False
) -> ExchangeAccount:
    """Load one account by canonical UUID, optionally enforcing command use."""
    account = await session.scalar(
        select(ExchangeAccount).where(ExchangeAccount.id == exchange_account_id)
    )
    if account is None:
        raise AccountNotFound(str(exchange_account_id))
    if for_command:
        ensure_account_active(account)
    return account


async def grant_membership(
    session: AsyncSession,
    *,
    exchange_account_id: UUID,
    user_id: str,
    role: str,
) -> ExchangeAccountMembership:
    """Create or update one account membership with a validated role."""
    typed_role = validate_membership_role(role)
    await get_exchange_account(session, exchange_account_id=exchange_account_id)
    membership = await session.scalar(
        select(ExchangeAccountMembership).where(
            ExchangeAccountMembership.exchange_account_id == exchange_account_id,
            ExchangeAccountMembership.user_id == user_id,
        )
    )
    if membership is None:
        membership = ExchangeAccountMembership(
            exchange_account_id=exchange_account_id,
            user_id=user_id,
            role=typed_role,
        )
        session.add(membership)
    else:
        membership.role = typed_role
    await session.flush()
    return membership


async def create_exchange_account_credential(
    session: AsyncSession,
    *,
    exchange_account_id: UUID,
    venue: str,
    label: str,
    api_key: str,
    secret_ciphertext: bytes,
    secret_nonce: bytes,
    wrapped_dek: bytes,
    dek_nonce: bytes,
    key_version: int,
    lifecycle_status: str = "active",
) -> ExchangeAccountCredential:
    """Insert an account credential while enforcing active-key uniqueness."""
    typed_status = validate_credential_lifecycle_status(lifecycle_status)
    await get_exchange_account(
        session, exchange_account_id=exchange_account_id, for_command=True
    )
    if typed_status == "active":
        existing = await session.scalar(
            select(ExchangeAccountCredential).where(
                ExchangeAccountCredential.exchange_account_id == exchange_account_id,
                ExchangeAccountCredential.venue == venue,
                ExchangeAccountCredential.lifecycle_status == "active",
            )
        )
        if existing is not None:
            raise ActiveCredentialConflict(str(exchange_account_id))
    row = ExchangeAccountCredential(
        exchange_account_id=exchange_account_id,
        venue=venue,
        label=label,
        api_key=api_key,
        secret_ciphertext=secret_ciphertext,
        secret_nonce=secret_nonce,
        wrapped_dek=wrapped_dek,
        dek_nonce=dek_nonce,
        key_version=key_version,
        lifecycle_status=typed_status,
    )
    session.add(row)
    await session.flush()
    await session.refresh(row)
    return row


async def upsert_account_config_draft(
    session: AsyncSession,
    *,
    exchange_account_id: UUID,
    config: Mapping[str, object],
    source: str,
) -> AccountConfigDraft:
    """Create or revise the inert account-scoped config draft.

    Revision one is assigned on insert and increments exactly once for every
    successful update.  The account row is locked on PostgreSQL to serialize
    concurrent writers; SQLite ignores ``FOR UPDATE`` but remains suitable for
    the unit-level behavior tests.
    """
    account = await get_exchange_account(
        session, exchange_account_id=exchange_account_id, for_command=True
    )
    del account  # the lookup/guard above is the relevant side effect
    row = await session.scalar(
        select(AccountConfigDraft)
        .where(AccountConfigDraft.exchange_account_id == exchange_account_id)
        .with_for_update()
    )
    config_value = dict(config)
    if row is None:
        row = AccountConfigDraft(
            exchange_account_id=exchange_account_id,
            config=config_value,
            revision=1,
            source=source,
        )
        session.add(row)
    else:
        row.config = config_value
        row.revision += 1
        row.source = source
    await session.flush()
    await session.refresh(row)
    return row


__all__ = [
    "AccountDomainError",
    "AccountLifecycleStatus",
    "AccountNotFound",
    "AccountRetired",
    "AccountRole",
    "ActiveCredentialConflict",
    "CredentialLifecycleStatus",
    "MembershipDenied",
    "account_id_canonical",
    "create_exchange_account_credential",
    "ensure_account_active",
    "get_exchange_account",
    "grant_membership",
    "upsert_account_config_draft",
    "validate_account_lifecycle_status",
    "validate_credential_lifecycle_status",
    "validate_membership_role",
]
