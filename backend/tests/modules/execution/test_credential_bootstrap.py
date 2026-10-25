from uuid import UUID

import pytest
import pytest_asyncio
from cryptography.exceptions import InvalidTag
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from bfx_funding_bot.core.crypto import Envelope, decrypt_secret_with_aad
from bfx_funding_bot.core.db import Base
from bfx_funding_bot.external.bitfinex.auth_rest import KeyPermissions
from bfx_funding_bot.external.bitfinex.errors import BitfinexAPIError
from bfx_funding_bot.modules.accounts.exchange_accounts import grant_membership
from bfx_funding_bot.modules.accounts.tables import ExchangeAccount, ExchangeAccountCredential
from bfx_funding_bot.modules.credentials.vault import load_account_credentials
from bfx_funding_bot.modules.execution import credential_bootstrap as bootstrap

ACCOUNT = UUID("550e8400-e29b-41d4-a716-446655440000")
KEK = bytes(range(32))


class Permissions:
    def __init__(self, scopes=None, error=None, user_error=None):
        self.scopes = scopes if scopes is not None else {"funding": (True, True)}
        self.error = error
        self.user_error = user_error
        self.calls: list[str] = []

    async def get_key_permissions(self, *, ctx):
        self.calls.append("permissions")
        assert ctx.account_id == str(ACCOUNT)
        assert ctx.credentials.api_key == "fixture-key"
        assert ctx.credentials.api_secret == "fixture-secret"
        if self.error:
            raise self.error
        return KeyPermissions(scopes=self.scopes)

    async def get_user_id(self, *, ctx):
        # Asked of the key being bootstrapped, never another.
        self.calls.append("user_id")
        assert ctx.account_id == str(ACCOUNT)
        assert ctx.credentials.api_key == "fixture-key"
        if self.user_error:
            raise self.user_error
        return 4242


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
        # The Bitfinex account the key belongs to, for later rotations to compare with.
        assert row.bitfinex_user_id == 4242
    assert await count(factory) == 1


@pytest.mark.asyncio
async def test_an_unreadable_user_id_leaves_no_row(factory):
    """The id is part of the bootstrap: without it a later rotation of a revoked key could
    not prove the account, so nothing is committed -- nor is any row created first."""
    client = Permissions(user_error=RuntimeError("fixture-secret"))
    with pytest.raises(bootstrap.BootstrapError) as error:
        await invoke(factory, apply=True, client=client)
    assert str(error.value) == "bootstrap_failed"
    assert "fixture-secret" not in str(error.value)
    assert client.calls == ["permissions", "user_id"]
    assert await count(factory) == 0


@pytest.mark.asyncio
async def test_the_key_is_written_active_once_bitfinex_answered(factory, monkeypatch):
    """No pending row is stored first and promoted: Bitfinex is asked (permissions, then the
    user id, as a rotation asks), and the verified key is inserted as the active credential
    naming that user."""
    created: list[tuple[str, int | None]] = []
    client = Permissions()
    real = bootstrap.create_active_credential

    async def spy(session, **kwargs):
        assert client.calls == ["permissions", "user_id"]
        row = await real(session, **kwargs)
        created.append((row.lifecycle_status, row.bitfinex_user_id))
        return row

    monkeypatch.setattr(bootstrap, "create_active_credential", spy)
    assert await invoke(factory, apply=True, client=client) == {"status": "verified",
                                                                 "applied": True}
    assert created == [("active", 4242)]
    async with factory() as session:
        row = await session.scalar(select(ExchangeAccountCredential))
        assert (row.lifecycle_status, row.bitfinex_user_id) == ("active", 4242)
        assert row.verified_at is not None


# The rotation's classification (credential_rotation.ask_bitfinex): the venue not answering
# is unavailable, any answer refuses.
REFUSALS = [
    BitfinexAPIError(500, "apikey: invalid", '["error",10100,"apikey: invalid"]'),
    BitfinexAPIError(401, "unauthorized", None),
    BitfinexAPIError(500, "nonce: small", '["error",10114,"nonce: small"]'),
]
NO_ANSWERS = [
    BitfinexAPIError(0, "transport error", None),
    BitfinexAPIError(429, "rate limited", None),
    BitfinexAPIError(503, "unavailable", None),
    BitfinexAPIError(500, "ready: invalid", '["error",11000,"ready: invalid"]'),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("call", ["permissions", "user_id"])
@pytest.mark.parametrize("error, code", [
    *((error, "bootstrap_permissions_rejected") for error in REFUSALS),
    *((error, "bootstrap_venue_unavailable") for error in NO_ANSWERS),
])
async def test_bitfinex_refusing_is_a_rejection_and_not_answering_a_failure(
        factory, call, error, code):
    """Either call may meet the venue's answer; a key it refuses is the operator's
    rejection, the venue being away is its own code: run it again later."""
    client = (Permissions(error=error) if call == "permissions"
              else Permissions(user_error=error))
    with pytest.raises(bootstrap.BootstrapError) as raised:
        await invoke(factory, apply=True, client=client)
    assert str(raised.value) == code
    assert await count(factory) == 0


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
@pytest.mark.parametrize("status", ["active", "retired"])
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


@pytest.mark.asyncio
@pytest.mark.parametrize("field, value", [
    ("api_key", "fixture-key\n"),         # a newline pasted from the terminal
    ("api_key", "fixture key"),           # whitespace inside
    ("api_secret", "fixture-secret\x00"),  # a control character
    ("api_secret", "   "),
])
async def test_a_key_the_rotation_path_would_refuse_is_refused_before_bitfinex(factory, field, value):
    """ASVS V1.3.3: the key goes into a request header; the first key gets the same check
    as every rotated one (core.sealed_credential.validate_credential)."""
    client = Permissions()
    with pytest.raises(bootstrap.BootstrapError) as error:
        await invoke(factory, apply=True, client=client, **{field: value})
    assert str(error.value) == "bootstrap_input_invalid"
    assert client.calls == [] and await count(factory) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("client, code, committed_nothing", [
    (Permissions(scopes={}), "bootstrap_permissions_rejected", True),
    (Permissions(error=BitfinexAPIError(0, "transport error", None)),
     "bootstrap_venue_unavailable", True),
    (Permissions(error=RuntimeError("fixture-secret")), "bootstrap_failed", False),
])
async def test_only_a_generic_failure_may_have_committed(factory, client, code, committed_nothing):
    """Every code but ``bootstrap_failed`` is raised before the write could commit, so the
    CLI may tell the operator nothing was applied."""
    with pytest.raises(bootstrap.BootstrapError) as raised:
        await invoke(factory, apply=True, client=client)
    assert (str(raised.value), raised.value.committed_nothing) == (code, committed_nothing)
