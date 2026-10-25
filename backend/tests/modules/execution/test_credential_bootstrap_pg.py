"""The bootstrap against PostgreSQL: Bitfinex is asked outside any transaction, and the row is
written in a short one that re-checks the account under its lock.

Row locks and a second connection committing while the first waits on Bitfinex are what is
tested; SQLite has neither.
"""
from __future__ import annotations

from uuid import UUID

import pytest
from sqlalchemy import func, select, text

from bfx_funding_bot.core.db import session_scope
from bfx_funding_bot.external.bitfinex.auth_rest import KeyPermissions
from bfx_funding_bot.modules.accounts.exchange_accounts import grant_membership
from bfx_funding_bot.modules.accounts.tables import ExchangeAccount, ExchangeAccountCredential
from bfx_funding_bot.modules.credentials import vault
from bfx_funding_bot.modules.execution import credential_bootstrap as bootstrap

pytestmark = pytest.mark.integration

ACCOUNT = UUID("aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee")
KEK = bytes(range(32))


async def _seed(factory) -> None:
    async with session_scope(factory) as s:
        s.add(ExchangeAccount(id=ACCOUNT, venue="bitfinex", label="Primary",
                              lifecycle_status="active"))
        await s.flush()
        await grant_membership(s, exchange_account_id=ACCOUNT, user_id="owner", role="owner")


async def _rows(factory) -> list[tuple[str, str]]:
    async with factory() as s:
        return [(r.api_key, r.lifecycle_status)
                for r in await s.scalars(select(ExchangeAccountCredential))]


class _Client:
    """A funding-only key; ``during`` runs while Bitfinex is answering the permissions call."""

    def __init__(self, during) -> None:
        self.during = during

    async def get_key_permissions(self, *, ctx) -> KeyPermissions:
        await self.during()
        return KeyPermissions(scopes={"funding": (True, True)})

    async def get_user_id(self, *, ctx) -> int:
        return 4242


async def _bootstrap(factory, client, *, apply: bool = True):
    return await bootstrap.bootstrap_credential(
        factory, exchange_account_id=ACCOUNT, owner_user_id="owner", api_key="FAKE-KEY",
        api_secret="FAKE-SECRET", kek=KEK, client=client, apply=apply)


@pytest.mark.asyncio
async def test_no_transaction_is_open_while_bitfinex_answers(pg_session_factory):
    """The account row stays lockable and no session of the bootstrap is in a transaction
    while the venue is asked: a slow Bitfinex must not hold the lock rotation and the
    daemon's writers take."""
    await _seed(pg_session_factory)
    seen: dict[str, int] = {}

    async def probe() -> None:
        async with session_scope(pg_session_factory) as other:
            await other.execute(text("SET LOCAL lock_timeout = '1s'"))
            await other.scalar(select(ExchangeAccount.id).where(
                ExchangeAccount.id == ACCOUNT).with_for_update(nowait=True))
            # Every other backend of this test database: an open transaction is the bootstrap's.
            seen["in_transaction"] = await other.scalar(text(
                "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() "
                "AND pid <> pg_backend_pid() AND state LIKE 'idle in transaction%'"))

    assert await _bootstrap(pg_session_factory, _Client(probe)) == {
        "status": "verified", "applied": True}
    assert seen == {"in_transaction": 0}
    assert await _rows(pg_session_factory) == [("FAKE-KEY", "active")]


@pytest.mark.asyncio
async def test_a_credential_written_while_bitfinex_answers_is_not_joined(pg_session_factory):
    """Nothing spans the venue call, so the write re-checks under the account lock: a
    credential that appeared meanwhile (another bootstrap, a rotation) is never joined by a
    second one."""
    await _seed(pg_session_factory)

    async def race() -> None:
        async with session_scope(pg_session_factory) as other:
            await other.execute(text("SET LOCAL lock_timeout = '1s'"))
            await vault.create_active_credential(
                other, exchange_account_id=ACCOUNT, label="other", api_key="OTHER-KEY",
                api_secret="OTHER-SECRET", kek=KEK, bitfinex_user_id=4242,
                verified_at=await other.scalar(select(func.now())))

    with pytest.raises(bootstrap.BootstrapError) as raised:
        await _bootstrap(pg_session_factory, _Client(race))
    assert str(raised.value) == "bootstrap_credential_exists"
    assert await _rows(pg_session_factory) == [("OTHER-KEY", "active")]
