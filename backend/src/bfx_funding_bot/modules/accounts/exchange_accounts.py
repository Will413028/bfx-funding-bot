"""Canonical exchange-account identity and account-scoped domain services.

The identity in this module is deliberately separate from Better Auth's user
identity.  A user is a principal; an ``ExchangeAccount`` is the money-domain
aggregate to which credentials, config drafts, and execution projections
belong.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Literal, cast
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bfx_funding_bot.core.account_identity import account_id_canonical
from bfx_funding_bot.modules.accounts.tables import (
    AccountConfigDraft,
    ExchangeAccount,
    ExchangeAccountCredential,
    ExchangeAccountMembership,
)

AccountRole = Literal["owner", "operator", "viewer"]
AccountLifecycleStatus = Literal["active", "halted", "retired"]
CredentialLifecycleStatus = Literal["active", "retired"]


class AccountDomainError(Exception):
    """Base class for account identity and authorization failures."""


class AccountNotFound(AccountDomainError):  # noqa: N818 - public domain name
    """The requested account does not exist in the account registry."""


class AccountRetired(AccountDomainError):  # noqa: N818 - public domain name
    """A command attempted to use an account that is permanently retired."""


class MembershipDenied(AccountDomainError):  # noqa: N818 - public domain name
    """The principal has no permitted membership for the requested account."""


def account_id_uuid_or_none(value: UUID | str) -> UUID | None:
    """Parse the UUID alias carried by the transitional event contract.

    During Halt 1, domain events retain their historical ``account_id: str``
    field so existing serializers and synthetic unit realms remain readable.
    A daemon boot supplies the canonical UUID string, so writers can populate
    the durable UUID owner column without consulting a process-global mapping.
    Legacy-only test/audit rows deliberately return ``None``; the contract
    migration's NOT NULL/FK gate is the final production guard against them.
    """
    try:
        return value if isinstance(value, UUID) else UUID(value)
    except (AttributeError, TypeError, ValueError):
        return None


def account_scope_clause(
    session: AsyncSession,
    *,
    account_id: UUID | str,
    exchange_account_column: Any,
    legacy_account_column: Any,
) -> Any:
    """Build the owner predicate for a post-cutover account-scoped query.

    PostgreSQL production queries are UUID-only.  The SQLite branch exists
    solely for the repository's historical synthetic-realm unit fixtures,
    which intentionally do not run the PostgreSQL contract migration.  It is
    never reachable for a canonical daemon account and is not a production
    dual-read fallback.
    """
    exchange_account_id = account_id_uuid_or_none(account_id)
    if exchange_account_id is not None:
        return exchange_account_column == exchange_account_id
    bind = getattr(session, "bind", None)
    if bind is None or bind.dialect.name == "sqlite":
        return legacy_account_column == str(account_id)
    raise ValueError("production account scope must be a canonical ExchangeAccount UUID")


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


async def require_account_membership(
    session: AsyncSession,
    *,
    exchange_account_id: UUID,
    user_id: str,
    write: bool = False,
) -> ExchangeAccountMembership:
    """Return a membership or fail closed for the requested operation."""
    account = await get_exchange_account(session, exchange_account_id=exchange_account_id)
    if account.lifecycle_status == "retired":
        raise AccountRetired(str(exchange_account_id))
    membership = await session.scalar(
        select(ExchangeAccountMembership).where(
            ExchangeAccountMembership.exchange_account_id == exchange_account_id,
            ExchangeAccountMembership.user_id == user_id,
        )
    )
    allowed_roles = {"owner", "operator"} if write else {"owner", "operator", "viewer"}
    if membership is None or membership.role not in allowed_roles:
        raise MembershipDenied(str(exchange_account_id))
    return membership


async def list_account_credentials(
    session: AsyncSession, *, exchange_account_id: UUID, user_id: str
) -> Sequence[ExchangeAccountCredential]:
    """List credentials only after account membership has been checked."""
    await require_account_membership(
        session, exchange_account_id=exchange_account_id, user_id=user_id, write=False
    )
    result = await session.scalars(
        select(ExchangeAccountCredential).where(
            ExchangeAccountCredential.exchange_account_id == exchange_account_id
        )
    )
    return list(result)


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
    "CredentialLifecycleStatus",
    "MembershipDenied",
    "account_id_canonical",
    "account_id_uuid_or_none",
    "account_scope_clause",
    "ensure_account_active",
    "get_exchange_account",
    "grant_membership",
    "list_account_credentials",
    "require_account_membership",
    "upsert_account_config_draft",
    "validate_account_lifecycle_status",
    "validate_membership_role",
]
