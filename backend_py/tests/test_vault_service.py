import base64

import pytest
from sqlalchemy import select

import bfx_funding_bot.modules.accounts.user_profile  # noqa: F401
from bfx_funding_bot.core.crypto import decrypt_secret
from bfx_funding_bot.core.db import session_scope
from bfx_funding_bot.modules.accounts.tables import APIKey
from bfx_funding_bot.modules.accounts.vault import (
    Envelope,
    KeyAlreadyExistsError,
    create_api_key,
    delete_api_key,
    list_api_keys,
)

pytestmark = pytest.mark.integration

_KEK = base64.b64decode(base64.b64encode(bytes(range(32))))


@pytest.mark.asyncio
async def test_create_encrypts_and_provisions(pg_session_factory):
    async with session_scope(pg_session_factory) as s:
        row = await create_api_key(
            s, user_id="u_create", label="main",
            api_key="PUB", api_secret="my-secret", kek=_KEK,
        )
        rid = row.id
    async with session_scope(pg_session_factory) as s:
        got = await s.scalar(select(APIKey).where(APIKey.id == rid))
        assert got is not None
        assert got.api_key == "PUB"
        assert got.exchange_status == "unverified"
        # secret is recoverable only via envelope
        env = Envelope(
            secret_ciphertext=got.secret_ciphertext, secret_nonce=got.secret_nonce,
            wrapped_dek=got.wrapped_dek, dek_nonce=got.dek_nonce, key_version=got.key_version,
        )
        assert decrypt_secret(env, user_id="u_create", kek=_KEK) == "my-secret"


@pytest.mark.asyncio
async def test_create_duplicate_raises(pg_session_factory):
    async with session_scope(pg_session_factory) as s:
        await create_api_key(s, user_id="u_dup", label="a", api_key="P", api_secret="s", kek=_KEK)
    async with session_scope(pg_session_factory) as s:
        with pytest.raises(KeyAlreadyExistsError):
            await create_api_key(s, user_id="u_dup", label="b", api_key="P2", api_secret="s2", kek=_KEK)


@pytest.mark.asyncio
async def test_list_scoped_to_user(pg_session_factory):
    async with session_scope(pg_session_factory) as s:
        await create_api_key(s, user_id="u_list_a", label="a", api_key="P", api_secret="s", kek=_KEK)
        await create_api_key(s, user_id="u_list_b", label="b", api_key="P", api_secret="s", kek=_KEK)
    async with session_scope(pg_session_factory) as s:
        rows = await list_api_keys(s, user_id="u_list_a")
    assert len(rows) == 1
    assert rows[0].user_id == "u_list_a"


@pytest.mark.asyncio
async def test_delete_returns_false_for_other_user(pg_session_factory):
    async with session_scope(pg_session_factory) as s:
        row = await create_api_key(s, user_id="u_del", label="a", api_key="P", api_secret="s", kek=_KEK)
        rid = row.id
    async with session_scope(pg_session_factory) as s:
        assert await delete_api_key(s, user_id="someone_else", key_id=rid) is False
    async with session_scope(pg_session_factory) as s:
        assert await delete_api_key(s, user_id="u_del", key_id=rid) is True
    async with session_scope(pg_session_factory) as s:
        assert await list_api_keys(s, user_id="u_del") == []
