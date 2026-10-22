"""The key in use is checked by the rotation's rule (9g): funding write only, its stored user.

The client is a stand-in for ``BitfinexAuthREST`` returning the venue's parsed answers; the
credential rows are real (SQLite, the ORM tables).

Mutation checks (one at a time; revert after each):

* skip ``permission_error`` in ``CredentialCompliance.check``: the withdraw and the
  missing-funding cases pass silently, ``test_a_key_that_may_*`` fails.
* skip the user id comparison: ``test_a_key_of_another_bitfinex_user_*`` fails.
* compare the running key with no active row (or drop the comparison):
  ``test_a_key_that_is_no_longer_the_active_one_is_superseded_*`` fails.
* turn a ``BitfinexShapeError`` from either read into a ``CredentialNoncompliantError``:
  ``test_an_answer_that_does_not_parse_is_no_verdict[*]`` fails.
"""
from __future__ import annotations

import importlib
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from bfx_funding_bot.core.crypto import encrypt_secret_with_aad
from bfx_funding_bot.core.db import Base
from bfx_funding_bot.external.bitfinex.auth_rest import KeyPermissions
from bfx_funding_bot.external.bitfinex.errors import (
    BitfinexAPIError,
    BitfinexShapeError,
    VenueCredentialRejectedError,
)
from bfx_funding_bot.modules.accounts.tables import ExchangeAccount, ExchangeAccountCredential
from bfx_funding_bot.modules.execution.credential_compliance import (
    CredentialCompliance,
    CredentialNoncompliantError,
    CredentialSupersededError,
)
from bfx_funding_bot.modules.execution.protocols import AccountContext, Credentials

importlib.import_module("bfx_funding_bot.apps.schema")

pytestmark = pytest.mark.asyncio

ACCOUNT = UUID("aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee")
KEY, SECRET = "FAKE-BOOT-KEY-000000", "FAKE-BOOT-SECRET-not-real-000000"
KEK = bytes(range(32))
USER = 4242
COMPLIANT = {"funding": (True, True), "wallets": (True, False), "withdraw": (False, False)}


class Client:
    """Bitfinex's answers for the key: its permissions and its user id (or errors)."""

    def __init__(self, scopes: Any = None, user: Any = USER) -> None:
        self.scopes = COMPLIANT if scopes is None else scopes
        self.user = user
        self.asked: list[str] = []

    async def get_key_permissions(self, *, ctx: AccountContext) -> KeyPermissions:
        assert ctx.credentials.api_key == KEY
        self.asked.append("permissions")
        if isinstance(self.scopes, BaseException):
            raise self.scopes
        return KeyPermissions(scopes=self.scopes)

    async def get_user_id(self, *, ctx: AccountContext) -> int:
        self.asked.append("user")
        if isinstance(self.user, BaseException):
            raise self.user
        return int(self.user)


@pytest.fixture
async def engine(tmp_path: Any) -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/compliance.db")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    envelope = encrypt_secret_with_aad(SECRET, aad=str(ACCOUNT), kek=KEK)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory.begin() as session:
        session.add(ExchangeAccount(id=ACCOUNT, venue="bitfinex", label="test",
                                    lifecycle_status="active"))
        await session.flush()
        session.add(ExchangeAccountCredential(
            exchange_account_id=ACCOUNT, venue="bitfinex", label="boot", api_key=KEY,
            secret_ciphertext=envelope.secret_ciphertext, secret_nonce=envelope.secret_nonce,
            wrapped_dek=envelope.wrapped_dek, dek_nonce=envelope.dek_nonce,
            key_version=envelope.key_version, lifecycle_status="active",
            verified_at=datetime(2026, 1, 1, tzinfo=UTC), bitfinex_user_id=USER))
    try:
        yield engine
    finally:
        await engine.dispose()


def _check(engine: AsyncEngine, client: Client, *, key: str = KEY) -> CredentialCompliance:
    return CredentialCompliance(
        session_factory=async_sessionmaker(engine, expire_on_commit=False),
        exchange_account_id=ACCOUNT, client=client,
        ctx=AccountContext(account_id=str(ACCOUNT), credentials=Credentials(key, SECRET)))


async def test_a_funding_only_key_of_the_stored_user_complies(engine) -> None:
    client = Client()
    await _check(engine, client).check()
    assert client.asked == ["permissions", "user"]


@pytest.mark.parametrize(("scopes", "reason"), [
    ({**COMPLIANT, "withdraw": (True, True)}, "withdraw_must_be_disabled"),
    ({**COMPLIANT, "funding": (True, False)}, "funding_write_required"),
    ({**COMPLIANT, "orders": (True, True)}, "unexpected_write_scope:orders"),
], ids=["withdraw", "no_funding_write", "orders"])
async def test_a_key_that_may_write_anything_but_funding_does_not_comply(
        engine, scopes, reason) -> None:
    client = Client(scopes)
    with pytest.raises(CredentialNoncompliantError) as refused:
        await _check(engine, client).check()
    assert refused.value.reason == reason
    assert str(refused.value) == f"credential_noncompliant:{reason}"
    assert client.asked == ["permissions"]  # the identity is not asked of a refused key


async def test_a_key_of_another_bitfinex_user_does_not_comply(engine) -> None:
    with pytest.raises(CredentialNoncompliantError) as refused:
        await _check(engine, Client(user=9999)).check()
    assert refused.value.reason == "credential_account_mismatch"


@pytest.mark.parametrize("client", [
    Client(scopes=BitfinexShapeError("permission row malformed")),
    Client(user=BitfinexShapeError("user info id is not a positive int: str")),
], ids=["permissions", "user"])
async def test_an_answer_that_does_not_parse_is_no_verdict(engine, client) -> None:
    """Non-compliance is a well-formed answer that breaks the rule; an unparseable one, from
    either read, propagates for the caller to retry."""
    error = client.scopes if isinstance(client.scopes, BaseException) else client.user
    with pytest.raises(BitfinexShapeError) as raised:
        await _check(engine, client).check()
    assert raised.value is error


async def test_a_key_that_is_no_longer_the_active_one_is_superseded_not_refused(
        engine) -> None:
    """A rotation committed another key: not a verdict on this one, nothing is asked."""
    client = Client()
    with pytest.raises(CredentialSupersededError) as superseded:
        await _check(engine, client, key="FAKE-OTHER-KEY").check()
    assert superseded.value.reason == "replaced_by_rotation"
    assert not isinstance(superseded.value, CredentialNoncompliantError)
    assert client.asked == []


async def test_no_active_credential_at_all_is_superseded_too(engine) -> None:
    async with async_sessionmaker(engine).begin() as session:
        await session.execute(update(ExchangeAccountCredential).values(
            lifecycle_status="retired"))
    client = Client()
    with pytest.raises(CredentialSupersededError) as superseded:
        await _check(engine, client).check()
    assert superseded.value.reason == "no_active_credential"
    assert client.asked == []


@pytest.mark.parametrize("error", [
    VenueCredentialRejectedError(500, "x", '["error",10100,"apikey: invalid"]'),
    BitfinexAPIError(0, "transport error"),
], ids=["refused", "unanswered"])
async def test_what_the_venue_answers_is_left_to_the_caller(engine, error) -> None:
    with pytest.raises(type(error)) as raised:
        await _check(engine, Client(error)).check()
    assert raised.value is error
