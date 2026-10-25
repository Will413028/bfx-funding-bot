"""``credential_bootstrap.verify_account_credential``: the permission verdict the operator bootstrap promotes on.

These were the web API's verify-route tests until 9c-2b removed the route; the bootstrap
still verifies through this function, so its verdicts are tested here directly.
"""
from __future__ import annotations

import uuid
from uuid import UUID

import pytest
import pytest_asyncio
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import async_sessionmaker

from bfx_funding_bot.core.db import Base
from bfx_funding_bot.external.bitfinex.auth_rest import KeyPermissions
from bfx_funding_bot.external.bitfinex.errors import BitfinexAPIError, BitfinexShapeError
from bfx_funding_bot.modules.accounts.exchange_accounts import MembershipDenied, grant_membership
from bfx_funding_bot.modules.accounts.tables import ExchangeAccount, ExchangeAccountCredential
from bfx_funding_bot.modules.credentials import vault
from bfx_funding_bot.modules.execution import credential_bootstrap

ACCOUNT = UUID("550e8400-e29b-41d4-a716-446655440000")
KEK = bytes(range(32))
FUNDING_ONLY = {"funding": (True, True), "withdraw": (False, False)}


class Client:
    def __init__(self, scopes=None, error=None):
        self.scopes = FUNDING_ONLY if scopes is None else scopes
        self.error = error

    async def get_key_permissions(self, *, ctx):
        if self.error is not None:
            raise self.error
        return KeyPermissions(scopes=self.scopes)


@pytest_asyncio.fixture
async def factory(sqlite_engine):
    async with sqlite_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(sqlite_engine, expire_on_commit=False)
    async with factory.begin() as session:
        session.add(ExchangeAccount(id=ACCOUNT, venue="bitfinex", label="Primary",
                                    lifecycle_status="active"))
        await session.flush()
        await grant_membership(session, exchange_account_id=ACCOUNT, user_id="owner",
                               role="owner")
    return factory


async def create(factory, api_key: str = "P") -> UUID:
    async with factory.begin() as session:
        row = await vault.create_account_credential(
            session, exchange_account_id=ACCOUNT, user_id="owner", label="a", api_key=api_key,
            api_secret="S", kek=KEK, bitfinex_user_id=4242)
        return row.id


async def verify(factory, key_id: UUID, client: Client, *, user_id: str = "owner",
                 kek: bytes = KEK) -> ExchangeAccountCredential | None:
    async with factory.begin() as session:
        return await credential_bootstrap.verify_account_credential(
            session, client, exchange_account_id=ACCOUNT, user_id=user_id, key_id=key_id, kek=kek)


async def stored(factory, key_id: UUID) -> ExchangeAccountCredential:
    async with factory() as session:
        row = await session.scalar(select(ExchangeAccountCredential).where(
            ExchangeAccountCredential.id == key_id))
        assert row is not None
        return row


@pytest.mark.asyncio
async def test_a_funding_only_key_becomes_active(factory) -> None:
    row = await verify(factory, await create(factory), Client())
    assert row is not None
    assert (row.lifecycle_status, row.last_verify_error, row.bitfinex_user_id) == (
        "active", None, 4242)
    assert row.verified_at is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("scopes, error", [
    ({"funding": (True, True), "withdraw": (False, True)}, "withdraw_must_be_disabled"),
    # Fail closed: a renamed or unknown write scope beside funding write is not funding-only.
    ({"funding": (True, True), "orders": (True, True), "withdrawals": (False, True)},
     "unexpected_write_scope"),
])
async def test_a_key_that_can_do_more_than_funding_stays_pending(factory, scopes, error) -> None:
    key_id = await create(factory)
    row = await verify(factory, key_id, Client(scopes))
    assert row is not None
    assert row.lifecycle_status == "pending"
    assert row.last_verify_error is not None and row.last_verify_error.startswith(error)
    assert (await stored(factory, key_id)).last_verify_error == row.last_verify_error


@pytest.mark.asyncio
async def test_a_second_verified_key_stays_pending_beside_the_active_one(factory) -> None:
    await verify(factory, await create(factory), Client())
    second = await verify(factory, await create(factory, "P2"), Client())
    assert second is not None
    assert (second.lifecycle_status, second.last_verify_error) == (
        "pending", "active_credential_exists")


@pytest.mark.asyncio
async def test_an_unknown_credential_is_none(factory) -> None:
    assert await verify(factory, uuid.uuid4(), Client()) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [
    BitfinexAPIError(status_code=0, message="transport", raw=None),
    BitfinexShapeError("malformed permissions response"),
])
async def test_no_answer_from_bitfinex_raises_and_writes_nothing(factory, error) -> None:
    key_id = await create(factory)
    with pytest.raises(type(error)):
        await verify(factory, key_id, Client(error=error))
    assert (await stored(factory, key_id)).lifecycle_status == "pending"


@pytest.mark.asyncio
@pytest.mark.parametrize("status_code", [429, 503])
async def test_a_transient_error_does_not_demote_an_active_key(factory, status_code) -> None:
    key_id = await create(factory)
    await verify(factory, key_id, Client())
    with pytest.raises(BitfinexAPIError):
        await verify(factory, key_id, Client(error=BitfinexAPIError(
            status_code=status_code, message="transient", raw=None)))
    assert (await stored(factory, key_id)).lifecycle_status == "active"


@pytest.mark.asyncio
async def test_a_refused_key_is_revoked(factory) -> None:
    key_id = await create(factory)
    await verify(factory, key_id, Client())
    row = await verify(factory, key_id, Client(error=BitfinexAPIError(
        status_code=401, message="unauthorized", raw=None)))
    assert row is not None
    assert (row.lifecycle_status, row.last_verify_error) == ("revoked", "invalid_credentials")


@pytest.mark.asyncio
async def test_a_wrong_kek_is_a_key_mismatch(factory) -> None:
    key_id = await create(factory)
    with pytest.raises(vault.VaultKeyMismatchError):
        await verify(factory, key_id, Client(), kek=bytes(range(31, -1, -1)))


@pytest.mark.asyncio
async def test_a_retired_credential_is_never_verified_again(factory) -> None:
    key_id = await create(factory)
    async with factory.begin() as session:
        await session.execute(update(ExchangeAccountCredential).where(
            ExchangeAccountCredential.id == key_id).values(lifecycle_status="retired"))
    assert await verify(factory, key_id, Client()) is None
    assert (await stored(factory, key_id)).lifecycle_status == "retired"


@pytest.mark.asyncio
async def test_another_users_credential_is_refused(factory) -> None:
    key_id = await create(factory)
    with pytest.raises(MembershipDenied):
        await verify(factory, key_id, Client(), user_id="user_xyz")
    assert (await stored(factory, key_id)).lifecycle_status == "pending"
