from uuid import UUID

import pytest
import pytest_asyncio
from cryptography.exceptions import InvalidTag
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from bfx_funding_bot.core.crypto import Envelope, decrypt_secret_with_aad
from bfx_funding_bot.core.db import Base
from bfx_funding_bot.external.bitfinex.auth_rest import KeyPermissions
from bfx_funding_bot.modules.accounts import credential_bootstrap as bootstrap
from bfx_funding_bot.modules.accounts.exchange_accounts import grant_membership
from bfx_funding_bot.modules.accounts.tables import ExchangeAccount, ExchangeAccountCredential
from bfx_funding_bot.modules.accounts.vault import load_account_credentials

ACCOUNT = UUID("550e8400-e29b-41d4-a716-446655440000")
KEK = bytes(range(32))


class Permissions:
    def __init__(self, scopes=None, error=None):
        self.scopes = scopes if scopes is not None else {"funding": (True, True)}
        self.error = error

    async def get_key_permissions(self, *, ctx):
        assert ctx.account_id == str(ACCOUNT)
        assert ctx.credentials.api_key == "fixture-key"
        assert ctx.credentials.api_secret == "fixture-secret"
        if self.error:
            raise self.error
        return KeyPermissions(scopes=self.scopes)


@pytest_asyncio.fixture
async def factory(sqlite_engine):
    async with sqlite_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(sqlite_engine, expire_on_commit=False)
    async with factory.begin() as session:
        session.add(
            ExchangeAccount(
                id=ACCOUNT, venue="bitfinex", label="Primary", lifecycle_status="active"
            )
        )
        await session.flush()
        await grant_membership(session, exchange_account_id=ACCOUNT, user_id="owner", role="owner")
        await grant_membership(
            session, exchange_account_id=ACCOUNT, user_id="operator", role="operator"
        )
    return factory


async def invoke(factory, **overrides):
    args = {
        "exchange_account_id": ACCOUNT,
        "owner_user_id": "owner",
        "api_key": "fixture-key",
        "api_secret": "fixture-secret",
        "kek": KEK,
        "client": Permissions(),
        "apply": False,
    }
    args.update(overrides)
    return await bootstrap.bootstrap_credential(factory, **args)


async def count(factory):
    async with factory() as session:
        return await session.scalar(select(func.count()).select_from(ExchangeAccountCredential))


@pytest.mark.asyncio
async def test_dry_run_verifies_without_retaining_credential(factory):
    report = await invoke(factory)
    assert report == {"status": "verified", "applied": False}
    assert await count(factory) == 0


@pytest.mark.asyncio
async def test_apply_commits_uuid_bound_loadable_credential(factory):
    assert await invoke(factory, apply=True) == {"status": "verified", "applied": True}
    async with factory() as session:
        credentials = await load_account_credentials(session, exchange_account_id=ACCOUNT, kek=KEK)
        assert credentials.api_secret == "fixture-secret"
        row = await session.scalar(select(ExchangeAccountCredential))
        envelope = Envelope(
            row.secret_ciphertext, row.secret_nonce, row.wrapped_dek, row.dek_nonce, row.key_version
        )
        with pytest.raises(InvalidTag):
            decrypt_secret_with_aad(envelope, aad="owner", kek=KEK)
    assert await count(factory) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "scopes",
    [
        {},
        {"funding": (True, False)},
        {"funding": (True, True), "withdraw": (False, True)},
        {"funding": (True, True), "unknown": (False, True)},
    ],
)
async def test_denied_permissions_leave_no_row(factory, scopes):
    with pytest.raises(bootstrap.BootstrapError):
        await invoke(factory, apply=True, client=Permissions(scopes))
    assert await count(factory) == 0


@pytest.mark.asyncio
async def test_remote_failure_rolls_back_and_suppresses_secret(factory):
    with pytest.raises(bootstrap.BootstrapError) as error:
        await invoke(factory, apply=True, client=Permissions(error=RuntimeError("fixture-secret")))
    assert "fixture-secret" not in str(error.value)
    assert await count(factory) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("owner", ["missing", "operator"])
async def test_requires_owner_not_merely_operator(factory, owner):
    with pytest.raises(bootstrap.BootstrapError):
        await invoke(factory, apply=True, owner_user_id=owner)
    assert await count(factory) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["active", "pending", "revoked", "retired"])
async def test_existing_credential_is_not_overwritten(factory, status):
    await invoke(factory, apply=True)
    async with factory.begin() as session:
        row = await session.scalar(select(ExchangeAccountCredential))
        row.lifecycle_status = status
        ciphertext = row.secret_ciphertext
    with pytest.raises(bootstrap.BootstrapError):
        await invoke(factory, apply=True, api_secret="replacement")
    assert await count(factory) == 1
    async with factory() as session:
        row = await session.scalar(select(ExchangeAccountCredential))
        assert row.lifecycle_status == status
        assert row.secret_ciphertext == ciphertext


@pytest.mark.asyncio
@pytest.mark.parametrize("field,value", [("api_key", ""), ("api_secret", "  "), ("kek", b"bad")])
async def test_invalid_secret_input_does_not_write(factory, field, value):
    with pytest.raises(bootstrap.BootstrapError):
        await invoke(factory, apply=True, **{field: value})
    assert await count(factory) == 0
