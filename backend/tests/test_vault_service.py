import base64
from dataclasses import dataclass
from uuid import UUID

import pytest
from sqlalchemy import select

import bfx_funding_bot.modules.accounts.user_profile  # noqa: F401
from bfx_funding_bot.core.crypto import decrypt_secret
from bfx_funding_bot.core.db import session_scope
from bfx_funding_bot.external.bitfinex.auth_rest import KeyPermissions
from bfx_funding_bot.external.bitfinex.errors import BitfinexAPIError
from bfx_funding_bot.modules.accounts.tables import APIKey
from bfx_funding_bot.modules.accounts.vault import (
    Envelope,
    KeyAlreadyExistsError,
    create_api_key,
    delete_api_key,
    list_api_keys,
    verify_api_key,
)
from tests.conftest import ensure_auth_user

pytestmark = pytest.mark.integration

_KEK = base64.b64decode(base64.b64encode(bytes(range(32))))


async def _create_key(
    session, *, user_id: str, label: str, api_key: str, api_secret: str,
):
    await ensure_auth_user(session, user_id)
    return await create_api_key(
        session,
        user_id=user_id,
        label=label,
        api_key=api_key,
        api_secret=api_secret,
        kek=_KEK,
    )


@pytest.mark.asyncio
async def test_create_encrypts_and_provisions(pg_session_factory):
    async with session_scope(pg_session_factory) as s:
        row = await _create_key(
            s, user_id="u_create", label="main",
            api_key="PUB", api_secret="my-secret",
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
        await _create_key(s, user_id="u_dup", label="a", api_key="P", api_secret="s")
    async with session_scope(pg_session_factory) as s:
        with pytest.raises(KeyAlreadyExistsError):
            await _create_key(s, user_id="u_dup", label="b", api_key="P2", api_secret="s2")


@pytest.mark.asyncio
async def test_list_scoped_to_user(pg_session_factory):
    async with session_scope(pg_session_factory) as s:
        await _create_key(s, user_id="u_list_a", label="a", api_key="P", api_secret="s")
        await _create_key(s, user_id="u_list_b", label="b", api_key="P", api_secret="s")
    async with session_scope(pg_session_factory) as s:
        rows = await list_api_keys(s, user_id="u_list_a")
    assert len(rows) == 1
    assert rows[0].user_id == "u_list_a"


@pytest.mark.asyncio
async def test_delete_returns_false_for_other_user(pg_session_factory):
    async with session_scope(pg_session_factory) as s:
        row = await _create_key(s, user_id="u_del", label="a", api_key="P", api_secret="s")
        rid = row.id
    async with session_scope(pg_session_factory) as s:
        assert await delete_api_key(s, user_id="someone_else", key_id=rid) is False
    async with session_scope(pg_session_factory) as s:
        assert await delete_api_key(s, user_id="u_del", key_id=rid) is True
    async with session_scope(pg_session_factory) as s:
        assert await list_api_keys(s, user_id="u_del") == []


# ---------------------------------------------------------------------------
# verify_api_key tests
# ---------------------------------------------------------------------------


@dataclass
class _FakeClient:
    """Stands in for BitfinexAuthREST.get_key_permissions."""
    perms: KeyPermissions | None = None
    error: Exception | None = None

    async def get_key_permissions(self, *, ctx) -> KeyPermissions:
        if self.error is not None:
            raise self.error
        assert self.perms is not None
        return self.perms


def _perms(funding_w=True, withdraw_w=False) -> KeyPermissions:
    return KeyPermissions(scopes={
        "funding": (True, funding_w),
        "withdraw": (False, withdraw_w),
    })


async def _make_key(factory, user_id="u_v") -> UUID:
    async with session_scope(factory) as s:
        row = await _create_key(s, user_id=user_id, label="a", api_key="PUB", api_secret="sec")
        return row.id


@pytest.mark.asyncio
async def test_verify_success_marks_verified(pg_session_factory):
    rid = await _make_key(pg_session_factory, "u_ok")
    async with session_scope(pg_session_factory) as s:
        row = await verify_api_key(
            s, _FakeClient(perms=_perms()), user_id="u_ok", key_id=rid, kek=_KEK
        )
    assert row is not None
    assert row.exchange_status == "verified"
    assert row.verified_at is not None
    assert row.last_verify_error is None


@pytest.mark.asyncio
async def test_verify_withdraw_enabled_fails_closed(pg_session_factory):
    rid = await _make_key(pg_session_factory, "u_wd")
    async with session_scope(pg_session_factory) as s:
        row = await verify_api_key(
            s, _FakeClient(perms=_perms(withdraw_w=True)), user_id="u_wd", key_id=rid, kek=_KEK
        )
    assert row.exchange_status == "failed"
    assert row.last_verify_error == "withdraw_must_be_disabled"


@pytest.mark.asyncio
async def test_verify_no_funding_write_fails(pg_session_factory):
    rid = await _make_key(pg_session_factory, "u_nf")
    async with session_scope(pg_session_factory) as s:
        row = await verify_api_key(
            s, _FakeClient(perms=_perms(funding_w=False)), user_id="u_nf", key_id=rid, kek=_KEK
        )
    assert row.exchange_status == "failed"
    assert row.last_verify_error == "funding_write_required"


@pytest.mark.asyncio
async def test_verify_500_is_transient_does_not_demote(pg_session_factory):
    # #1: a 5xx (server/maintenance) is TRANSIENT -> re-raise (router 502),
    # status unchanged. A previously-verified key must NOT be demoted.
    rid = await _make_key(pg_session_factory, "u_5xx")
    async with session_scope(pg_session_factory) as s:
        await verify_api_key(s, _FakeClient(perms=_perms()), user_id="u_5xx", key_id=rid, kek=_KEK)
    err = BitfinexAPIError(status_code=500, message="server error", raw=None)
    async with session_scope(pg_session_factory) as s:
        with pytest.raises(BitfinexAPIError):
            await verify_api_key(s, _FakeClient(error=err), user_id="u_5xx", key_id=rid, kek=_KEK)
    async with session_scope(pg_session_factory) as s:
        got = await s.scalar(select(APIKey).where(APIKey.id == rid))
        assert got is not None
        assert got.exchange_status == "verified"


@pytest.mark.asyncio
@pytest.mark.parametrize("status_code", [429, 503])
async def test_verify_transient_status_does_not_demote(pg_session_factory, status_code):
    # #1: 429 (rate limit) and 503 (maintenance) are transient -> no demote.
    rid = await _make_key(pg_session_factory, f"u_t{status_code}")
    async with session_scope(pg_session_factory) as s:
        await verify_api_key(
            s, _FakeClient(perms=_perms()), user_id=f"u_t{status_code}", key_id=rid, kek=_KEK
        )
    err = BitfinexAPIError(status_code=status_code, message="transient", raw=None)
    async with session_scope(pg_session_factory) as s:
        with pytest.raises(BitfinexAPIError):
            await verify_api_key(
                s, _FakeClient(error=err), user_id=f"u_t{status_code}", key_id=rid, kek=_KEK
            )
    async with session_scope(pg_session_factory) as s:
        got = await s.scalar(select(APIKey).where(APIKey.id == rid))
        assert got is not None
        assert got.exchange_status == "verified"


@pytest.mark.asyncio
async def test_verify_401_demotes(pg_session_factory):
    # #1: a genuine 4xx credential error (401) DOES demote.
    rid = await _make_key(pg_session_factory, "u_401")
    async with session_scope(pg_session_factory) as s:
        await verify_api_key(s, _FakeClient(perms=_perms()), user_id="u_401", key_id=rid, kek=_KEK)
    err = BitfinexAPIError(status_code=401, message="unauthorized", raw=None)
    async with session_scope(pg_session_factory) as s:
        row = await verify_api_key(s, _FakeClient(error=err), user_id="u_401", key_id=rid, kek=_KEK)
    assert row.exchange_status == "failed"
    assert row.last_verify_error == "invalid_credentials"


@pytest.mark.asyncio
async def test_verify_funding_only_verified(pg_session_factory):
    # #3 fail-closed: only funding read+write -> verified.
    rid = await _make_key(pg_session_factory, "u_fonly")
    perms = KeyPermissions(scopes={"funding": (True, True)})
    async with session_scope(pg_session_factory) as s:
        row = await verify_api_key(
            s, _FakeClient(perms=perms), user_id="u_fonly", key_id=rid, kek=_KEK
        )
    assert row.exchange_status == "verified"
    assert row.last_verify_error is None


@pytest.mark.asyncio
async def test_verify_other_write_scope_fails_closed(pg_session_factory):
    # #3 fail-closed: funding-write ON + an OTHER (renamed) write scope -> failed.
    rid = await _make_key(pg_session_factory, "u_other")
    perms = KeyPermissions(scopes={
        "funding": (True, True),
        "orders": (True, True),
        "withdrawals": (False, True),
    })
    async with session_scope(pg_session_factory) as s:
        row = await verify_api_key(
            s, _FakeClient(perms=perms), user_id="u_other", key_id=rid, kek=_KEK
        )
    assert row.exchange_status == "failed"
    assert row.last_verify_error is not None
    assert row.last_verify_error.startswith("unexpected_write_scope")


@pytest.mark.asyncio
async def test_verify_wrong_kek_raises_key_mismatch(pg_session_factory):
    # #2: decrypt under a different KEK -> VaultKeyMismatchError, not a raw 500.
    from bfx_funding_bot.modules.accounts.vault import VaultKeyMismatchError
    rid = await _make_key(pg_session_factory, "u_kek")
    wrong_kek = bytes(range(31, -1, -1))
    async with session_scope(pg_session_factory) as s:
        with pytest.raises(VaultKeyMismatchError):
            await verify_api_key(
                s, _FakeClient(perms=_perms()), user_id="u_kek", key_id=rid, kek=wrong_kek
            )


@pytest.mark.asyncio
async def test_verify_transport_error_reraises(pg_session_factory):
    rid = await _make_key(pg_session_factory, "u_net")
    err = BitfinexAPIError(status_code=0, message="transport error", raw=None)
    async with session_scope(pg_session_factory) as s:
        with pytest.raises(BitfinexAPIError):
            await verify_api_key(s, _FakeClient(error=err), user_id="u_net", key_id=rid, kek=_KEK)


@pytest.mark.asyncio
async def test_verify_unknown_key_returns_none(pg_session_factory):
    from uuid import uuid4
    async with session_scope(pg_session_factory) as s:
        assert await verify_api_key(
            s, _FakeClient(perms=_perms()), user_id="nobody", key_id=uuid4(), kek=_KEK
        ) is None


@pytest.mark.asyncio
async def test_verify_transport_error_keeps_prior_verified(pg_session_factory):
    """A network blip during re-verify must NOT demote an already-verified key."""
    rid = await _make_key(pg_session_factory, "u_keep")
    async with session_scope(pg_session_factory) as s:
        await verify_api_key(
            s, _FakeClient(perms=_perms()), user_id="u_keep", key_id=rid, kek=_KEK
        )
    err = BitfinexAPIError(status_code=0, message="transport error", raw=None)
    async with session_scope(pg_session_factory) as s:
        with pytest.raises(BitfinexAPIError):
            await verify_api_key(
                s, _FakeClient(error=err), user_id="u_keep", key_id=rid, kek=_KEK
            )
    # re-fetch in a fresh session: the rollback reached the DB; still verified
    async with session_scope(pg_session_factory) as s:
        got = await s.scalar(select(APIKey).where(APIKey.id == rid))
        assert got is not None
        assert got.exchange_status == "verified"
        assert got.verified_at is not None
