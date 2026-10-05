"""The owner's amendment script picks its policy store by the authority the database is under.

Mutations (one at a time; revert after each): the script builds the legacy store whatever the
epoch says (``test_the_ledger_authority_amends_without_the_event_stream``), or skips the epoch
read (``test_an_unsupported_authority_refuses``).
"""
from __future__ import annotations

import argparse
from uuid import UUID

import pytest

from bfx_funding_bot.core.authority import AuthorityMismatch
from bfx_funding_bot.core.database_realm import DatabaseRealmMismatch, DatabaseRealmRow
from bfx_funding_bot.core.db import Base, make_async_engine_from_url, make_session_factory
from bfx_funding_bot.modules.execution.event_store.writer import AccountEventWriter
from bfx_funding_bot.modules.ledger import Scope
from bfx_funding_bot.modules.ledger.policy_write import write_policy_revision
from bfx_funding_bot.modules.trading import CapitalPolicy
from scripts import amend_capital_policy as script

ACCOUNT = UUID("550e8400-e29b-41d4-a716-446655440000")


def _args(**changes) -> argparse.Namespace:
    base = {
        "exchange_account_id": ACCOUNT, "environment": "ci", "symbol": "fUST", "enabled": False,
        "max_offer_amount": None, "min_period_days": None, "max_period_days": None,
        "max_open_offers": None, "rate_floor_ratio": None, "min_rate_apr": None,
        "apply_digest": None,
    }
    return argparse.Namespace(**{**base, **changes})


async def _stamp(session, realm: str) -> None:
    session.add(DatabaseRealmRow(realm=realm, stamped_at_ms=1, actor="test"))


@pytest.fixture
async def database(tmp_path, monkeypatch):
    url = f"sqlite+aiosqlite:///{tmp_path / 'amend.db'}"
    monkeypatch.setenv("DATABASE_URL", url)
    engine = make_async_engine_from_url(url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = make_session_factory(engine)
    async with factory.begin() as session:
        await _stamp(session, "ci")
        await write_policy_revision(
            session, Scope(ACCOUNT, "ci"), symbol="fUST", policy=CapitalPolicy(enabled=True),
            expected_revision=0, source={"fixture": True})
    yield factory
    await engine.dispose()


def _authority(monkeypatch, name: str) -> None:
    async def read(_session: object, *, supported: object) -> str:
        assert name in supported  # type: ignore[operator]
        return name

    monkeypatch.setattr(script, "read_authority", read)


@pytest.mark.asyncio
@pytest.mark.parametrize("authority", ["legacy", "ledger"])
async def test_the_script_amends_through_the_store_of_the_authority(
    database, monkeypatch, authority,
) -> None:
    _authority(monkeypatch, authority)
    replays: list[object] = []

    async def prepare(self, session, *, account_id):
        if authority == "ledger":
            raise AssertionError("the ledger authority must not replay the event stream")
        replays.append(account_id)

    monkeypatch.setattr(AccountEventWriter, "prepare_locked", prepare)

    report = await script.run(_args())
    assert report["status"] == "dry_run" and report["new_policy"]["enabled"] is False
    applied = await script.run(_args(apply_digest=report["amendment_digest"]))

    assert applied["status"] == "applied" and applied["new_revision"] == 2
    assert bool(replays) == (authority == "legacy")
    async with database() as session:
        from bfx_funding_bot.modules.ledger.wiring import build_policy_store
        read = await build_policy_store(Scope(ACCOUNT, "ci")).read_applied(session, symbol="fUST")
    assert (read.revision, read.policy.enabled) == (2, False)


@pytest.mark.asyncio
async def test_an_unsupported_authority_refuses(database, monkeypatch) -> None:
    async def refuse(_session: object, *, supported: object) -> str:
        raise AuthorityMismatch("authority_unsupported value=ledger build=legacy")

    monkeypatch.setattr(script, "read_authority", refuse)
    with pytest.raises(AuthorityMismatch):
        await script.run(_args())


@pytest.mark.asyncio
@pytest.mark.parametrize(("realm", "expected"), [
    ("prod", {"legacy", "ledger"}), ("shadow", {"legacy", "ledger"}), ("ci", {"legacy", "ledger"}),
])
async def test_the_supported_authorities_follow_the_database_realm(
    database, monkeypatch, realm: str, expected: set[str],
) -> None:
    """The owner amends policy in prod after the switch. Mutation: drop ``ledger`` from prod's
    set fails the ``prod`` row; an unknown realm is refused (``supported_for_policy_script``)."""
    from sqlalchemy import text

    async with database.begin() as session:
        await session.execute(text("UPDATE database_realm SET realm = :realm"), {"realm": realm})
    seen: list[frozenset[str]] = []

    async def read(_session: object, *, supported: frozenset[str]) -> str:
        seen.append(supported)
        raise AuthorityMismatch("stop after the read")

    monkeypatch.setattr(script, "read_authority", read)
    with pytest.raises(AuthorityMismatch):
        await script.run(_args())
    assert seen == [frozenset(expected)]


@pytest.mark.asyncio
async def test_an_unstamped_database_refuses(database, monkeypatch) -> None:
    from sqlalchemy import text

    async with database.begin() as session:
        await session.execute(text("DELETE FROM database_realm"))
    _authority(monkeypatch, "legacy")
    with pytest.raises(DatabaseRealmMismatch, match="unstamped"):
        await script.run(_args())
