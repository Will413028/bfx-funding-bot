from __future__ import annotations

from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import bfx_funding_bot.modules.accounts.tables  # noqa: F401
from bfx_funding_bot.core.db import Base
from bfx_funding_bot.modules.accounts.exchange_accounts import (
    AccountRetired,
    account_id_canonical,
    ensure_account_active,
    upsert_account_config_draft,
    validate_membership_role,
)
from bfx_funding_bot.modules.accounts.tables import (
    AccountConfigDraft,
    ExchangeAccount,
    ExchangeAccountCredential,
    ExchangeAccountMembership,
)


@pytest_asyncio.fixture
async def session(sqlite_engine):
    async with sqlite_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(sqlite_engine, expire_on_commit=False)
    async with factory() as session:
        yield session


def _account(*, account_id: UUID | None = None, status: str = "active") -> ExchangeAccount:
    return ExchangeAccount(
        id=account_id or uuid4(),
        venue="bitfinex",
        label="Primary",
        lifecycle_status=status,
    )


def _credential(account_id: UUID, *, status: str = "active") -> ExchangeAccountCredential:
    return ExchangeAccountCredential(
        exchange_account_id=account_id,
        venue="bitfinex",
        label="funding",
        api_key="public",
        secret_ciphertext=b"ciphertext",
        secret_nonce=b"nonce",
        wrapped_dek=b"wrapped",
        dek_nonce=b"dek-nonce",
        key_version=1,
        lifecycle_status=status,
    )


def test_account_id_is_assigned_once_and_cannot_be_changed(session: AsyncSession) -> None:
    account = _account()
    assert isinstance(account.id, UUID)


@pytest.mark.asyncio
async def test_persisted_account_id_is_immutable(session: AsyncSession) -> None:
    account = _account()
    session.add(account)
    await session.flush()

    with pytest.raises(ValueError, match="immutable"):
        account.id = uuid4()


def test_membership_role_is_closed_set() -> None:
    assert validate_membership_role("owner") == "owner"
    assert validate_membership_role("operator") == "operator"
    assert validate_membership_role("viewer") == "viewer"
    with pytest.raises(ValueError, match="role"):
        validate_membership_role("admin")


def test_uuid_canonicalization_is_stable_for_aad() -> None:
    raw = "550E8400-E29B-41D4-A716-446655440000"
    assert account_id_canonical(UUID(raw)) == "550e8400-e29b-41d4-a716-446655440000"
    assert account_id_canonical(raw) == "550e8400-e29b-41d4-a716-446655440000"
    with pytest.raises(ValueError, match="UUID"):
        account_id_canonical("legacy-default")


@pytest.mark.asyncio
async def test_only_one_active_credential_per_account_and_venue(session: AsyncSession) -> None:
    account = _account()
    session.add(account)
    await session.flush()
    session.add_all([_credential(account.id), _credential(account.id)])

    with pytest.raises(IntegrityError):
        await session.flush()


@pytest.mark.asyncio
async def test_config_revision_increments_on_each_update(session: AsyncSession) -> None:
    account = _account()
    session.add(account)
    await session.flush()

    first = await upsert_account_config_draft(
        session,
        exchange_account_id=account.id,
        config={"period_days": 2},
        source="operator",
    )
    first_revision = first.revision
    second = await upsert_account_config_draft(
        session,
        exchange_account_id=account.id,
        config={"period_days": 7},
        source="operator",
    )

    assert isinstance(first, AccountConfigDraft)
    assert second.id == first.id
    assert first_revision == 1
    assert second.revision == 2
    assert second.config == {"period_days": 7}

    persisted = await session.scalar(
        select(AccountConfigDraft).where(AccountConfigDraft.id == first.id)
    )
    assert persisted is not None
    assert persisted.revision == 2


@pytest.mark.asyncio
async def test_retired_account_rejects_new_config_commands(session: AsyncSession) -> None:
    account = _account(status="retired")
    session.add(account)
    await session.flush()

    with pytest.raises(AccountRetired):
        ensure_account_active(account)

    with pytest.raises(AccountRetired):
        await upsert_account_config_draft(
            session,
            exchange_account_id=account.id,
            config={"period_days": 2},
            source="operator",
        )


@pytest.mark.asyncio
async def test_membership_has_composite_identity(session: AsyncSession) -> None:
    account = _account()
    session.add(account)
    await session.flush()
    membership = ExchangeAccountMembership(
        exchange_account_id=account.id,
        user_id="operator-1",
        role="operator",
    )
    session.add(membership)
    await session.flush()
    assert (membership.exchange_account_id, membership.user_id) == (account.id, "operator-1")
