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

pytestmark = pytest.mark.integration

_KEK = base64.b64decode(base64.b64encode(bytes(range(32))))


async def _create_key(
    session, *, user_id: str, label: str, api_key: str, api_secret: str,
):
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


# ---------------------------------------------------------------------------
# rotate_active_credential (PostgreSQL: the one-active partial unique index)
# ---------------------------------------------------------------------------

_ROTATION_ACCOUNT = UUID("aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee")


async def _rotation_fixture(factory) -> tuple[UUID, UUID]:
    """An account with one active credential and a waiting rotation request naming it."""
    from datetime import UTC, datetime
    from uuid import uuid4

    from bfx_funding_bot.core.crypto import encrypt_secret_with_aad
    from bfx_funding_bot.modules.accounts.rotation_tables import (
        CredentialRotationRecipientRow,
        CredentialRotationRequestRow,
    )
    from bfx_funding_bot.modules.accounts.tables import ExchangeAccount, ExchangeAccountCredential
    from bfx_funding_bot.modules.observability.tables import BotRunRow

    envelope = encrypt_secret_with_aad("FAKE-OLD-SECRET", aad=str(_ROTATION_ACCOUNT), kek=_KEK)
    run_id, recipient_id, request_id = uuid4(), uuid4(), uuid4()
    async with session_scope(factory) as s:
        s.add(ExchangeAccount(id=_ROTATION_ACCOUNT, venue="bitfinex", label="rotation"))
        await s.flush()
        old = ExchangeAccountCredential(
            exchange_account_id=_ROTATION_ACCOUNT, venue="bitfinex", label="old",
            api_key="FAKE-OLD-KEY", secret_ciphertext=envelope.secret_ciphertext,
            secret_nonce=envelope.secret_nonce, wrapped_dek=envelope.wrapped_dek,
            dek_nonce=envelope.dek_nonce, key_version=envelope.key_version,
            lifecycle_status="active", verified_at=datetime.now(UTC), bitfinex_user_id=4242)
        s.add(old)
        s.add(BotRunRow(run_id=run_id, exchange_account_id=_ROTATION_ACCOUNT,
                        deployment_environment="ci", started_at_ms=1, host_name="t", pid=1))
        await s.flush()
        s.add(CredentialRotationRecipientRow(
            recipient_id=recipient_id, exchange_account_id=_ROTATION_ACCOUNT,
            deployment_environment="ci", run_id=run_id, public_key=b"\x01" * 32, created_at_ms=1))
        await s.flush()
        s.add(CredentialRotationRequestRow(
            request_id=request_id, exchange_account_id=_ROTATION_ACCOUNT,
            deployment_environment="ci", recipient_id=recipient_id,
            replaces_credential_id=old.id, sealed_credential=b"\x00" * 80, label="new",
            reason="rotate", requested_by="operator", created_at_ms=1))
        return old.id, request_id


async def _rotate(s, *, replaces: UUID, request_id: UUID, kek: bytes = _KEK):
    from datetime import UTC, datetime

    from bfx_funding_bot.modules.accounts.vault import rotate_active_credential

    return await rotate_active_credential(
        s, exchange_account_id=_ROTATION_ACCOUNT, replaces_credential_id=replaces,
        operator_request_id=request_id, label="new", api_key="FAKE-NEW-KEY",
        api_secret="FAKE-NEW-SECRET", verified_at=datetime.now(UTC), kek=kek,
        bitfinex_user_id=4242)


async def _states(factory) -> list[tuple[str, str, UUID | None]]:
    from bfx_funding_bot.modules.accounts.tables import ExchangeAccountCredential

    async with session_scope(factory) as s:
        rows = (await s.execute(select(
            ExchangeAccountCredential.api_key, ExchangeAccountCredential.lifecycle_status,
            ExchangeAccountCredential.operator_request_id))).all()
    return sorted(tuple(r) for r in rows)


@pytest.mark.asyncio
async def test_rotate_retires_the_replaced_credential_then_inserts_the_new_active(
        pg_session_factory):
    from bfx_funding_bot.modules.accounts.vault import load_account_credentials

    old_id, request_id = await _rotation_fixture(pg_session_factory)
    async with session_scope(pg_session_factory) as s:
        new = await _rotate(s, replaces=old_id, request_id=request_id)
        assert (new.lifecycle_status, new.operator_request_id) == ("active", request_id)
        # The new row names the Bitfinex user the verified key belongs to.
        assert new.bitfinex_user_id == 4242
    assert await _states(pg_session_factory) == [
        ("FAKE-NEW-KEY", "active", request_id), ("FAKE-OLD-KEY", "retired", None)]
    async with session_scope(pg_session_factory) as s:
        loaded = await load_account_credentials(
            s, exchange_account_id=_ROTATION_ACCOUNT, kek=_KEK)
    assert (loaded.api_key, loaded.api_secret) == ("FAKE-NEW-KEY", "FAKE-NEW-SECRET")


@pytest.mark.asyncio
async def test_rotate_refuses_when_the_active_credential_is_another(pg_session_factory):
    from uuid import uuid4

    from bfx_funding_bot.modules.accounts.vault import ActiveCredentialChangedError

    _, request_id = await _rotation_fixture(pg_session_factory)
    async with session_scope(pg_session_factory) as s:
        with pytest.raises(ActiveCredentialChangedError):
            await _rotate(s, replaces=uuid4(), request_id=request_id)
    assert await _states(pg_session_factory) == [("FAKE-OLD-KEY", "active", None)]


@pytest.mark.asyncio
async def test_a_rotation_that_fails_midway_leaves_the_old_credential_active(pg_session_factory):
    """The retire is flushed before the insert; a failure after it rolls both back."""
    old_id, request_id = await _rotation_fixture(pg_session_factory)
    with pytest.raises(ValueError):
        async with session_scope(pg_session_factory) as s:
            await _rotate(s, replaces=old_id, request_id=request_id, kek=b"too short")
    assert await _states(pg_session_factory) == [("FAKE-OLD-KEY", "active", None)]


@pytest.mark.asyncio
async def test_verify_never_writes_over_a_row_retired_while_bitfinex_answered(pg_session_factory):
    """The webapi verify reads the row unlocked and calls Bitfinex; a rotation that retires
    the row meanwhile wins: verify re-reads under the account lock and writes nothing."""
    from datetime import UTC, datetime

    from sqlalchemy import update

    from bfx_funding_bot.core.crypto import encrypt_secret_with_aad
    from bfx_funding_bot.modules.accounts.tables import (
        ExchangeAccount,
        ExchangeAccountCredential,
        ExchangeAccountMembership,
    )
    from bfx_funding_bot.modules.accounts.vault import verify_account_credential

    envelope = encrypt_secret_with_aad("FAKE-SECRET", aad=str(_ROTATION_ACCOUNT), kek=_KEK)
    async with session_scope(pg_session_factory) as s:
        s.add(ExchangeAccount(id=_ROTATION_ACCOUNT, venue="bitfinex", label="race"))
        await s.flush()
        s.add(ExchangeAccountMembership(exchange_account_id=_ROTATION_ACCOUNT, user_id="u_race",
                                        role="owner"))
        row = ExchangeAccountCredential(
            exchange_account_id=_ROTATION_ACCOUNT, venue="bitfinex", label="k",
            api_key="FAKE-KEY", secret_ciphertext=envelope.secret_ciphertext,
            secret_nonce=envelope.secret_nonce, wrapped_dek=envelope.wrapped_dek,
            dek_nonce=envelope.dek_nonce, key_version=envelope.key_version,
            lifecycle_status="active", verified_at=datetime.now(UTC), bitfinex_user_id=4242)
        s.add(row)
        await s.flush()
        key_id = row.id

    class RetiringClient:
        """Bitfinex answers a refusal while a rotation commits the retirement elsewhere."""

        async def get_key_permissions(self, *, ctx) -> KeyPermissions:
            async with session_scope(pg_session_factory) as other:
                await other.execute(update(ExchangeAccountCredential).where(
                    ExchangeAccountCredential.id == key_id).values(lifecycle_status="retired"))
            return _perms(funding_w=False)

    async with session_scope(pg_session_factory) as s:
        assert await verify_account_credential(
            s, RetiringClient(), exchange_account_id=_ROTATION_ACCOUNT, user_id="u_race",
            key_id=key_id, kek=_KEK) is None
    async with session_scope(pg_session_factory) as s:
        got = await s.get(ExchangeAccountCredential, key_id)
        assert got is not None
        assert (got.lifecycle_status, got.last_verify_error) == ("retired", None)
