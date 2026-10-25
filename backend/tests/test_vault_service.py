import base64
from uuid import UUID

import pytest
from sqlalchemy import select

import bfx_funding_bot.modules.accounts.user_profile  # noqa: F401
from bfx_funding_bot.core.db import session_scope
from bfx_funding_bot.external.bitfinex.auth_rest import KeyPermissions

pytestmark = pytest.mark.integration

_KEK = base64.b64decode(base64.b64encode(bytes(range(32))))


def _perms(funding_w=True, withdraw_w=False) -> KeyPermissions:
    return KeyPermissions(scopes={
        "funding": (True, funding_w),
        "withdraw": (False, withdraw_w),
    })


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
