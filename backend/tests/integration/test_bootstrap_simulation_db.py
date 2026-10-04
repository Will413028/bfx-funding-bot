"""``scripts/bootstrap_simulation_db.py`` on migrated PostgreSQL, run as the owner.

Mutations (one at a time; revert after each): skip the realm check (``test_it_refuses_*``
realm cases), skip the ``prod`` row scan (``test_it_refuses_a_database_holding_a_prod_row``),
append the epoch on every run or create the account again (``test_a_second_run_changes_nothing``),
leave the envelope out of the policy (``test_the_policy_carries_the_ledger_envelope``).
"""
from __future__ import annotations

import argparse
from decimal import Decimal
from uuid import UUID

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from bfx_funding_bot.core.database_realm import DatabaseRealmMismatch
from bfx_funding_bot.modules.ledger import Scope
from bfx_funding_bot.modules.ledger.wiring import build_policy_store
from scripts import bootstrap_simulation_db as script

from .test_ledger_schema_roles import ledger_db  # noqa: F401 - fixture dependency

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

ACCOUNT = UUID("0a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d")
REALMS = ("shadow", "ci")  # ``ci`` only in tests: the CLI allows ``shadow`` alone


def _args(**changes) -> argparse.Namespace:
    base = {
        "exchange_account_id": ACCOUNT, "symbol": ["fUST", "fUSD"],
        "max_offer_amount": Decimal("200"), "min_period_days": 2, "max_period_days": 2,
        "max_open_offers": 6, "rate_floor_ratio": Decimal("0.5"), "min_rate_apr": Decimal("0.01"),
    }
    return argparse.Namespace(**{**base, **changes})


@pytest.fixture
async def db(ledger_db):  # noqa: F811
    url = ledger_db.url.set(drivername="postgresql+asyncpg").render_as_string(hide_password=False)
    engine = create_async_engine(url)
    async with engine.begin() as conn:  # the owner's one-time stamp (the template is unstamped)
        await conn.execute(text("SET LOCAL session_replication_role = replica"))
        await conn.execute(text("DELETE FROM database_realm"))
        await conn.execute(text(
            "INSERT INTO database_realm (realm, stamped_at_ms, actor) VALUES ('ci', 1, 'test')"))
    yield url, engine
    await engine.dispose()


async def _restamp(engine, realm: str | None) -> None:
    async with engine.begin() as conn:
        await conn.execute(text("SET LOCAL session_replication_role = replica"))
        await conn.execute(text("DELETE FROM database_realm"))
        if realm is not None:
            await conn.execute(text(
                "INSERT INTO database_realm (realm, stamped_at_ms, actor) "
                "VALUES (:realm, 1, 'test')"), {"realm": realm})


async def _snapshot(engine) -> dict[str, object]:
    async with engine.connect() as conn:
        return {
            "epochs": (await conn.execute(text(
                "SELECT epoch_seq, authority FROM capital_authority_epoch ORDER BY epoch_seq"))).all(),
            "accounts": (await conn.execute(text(
                "SELECT id, label, lifecycle_status, venue FROM exchange_accounts"))).all(),
            "credentials": await conn.scalar(text(
                "SELECT count(*) FROM exchange_account_credentials")),
            "revisions": (await conn.execute(text(
                "SELECT symbol, revision FROM capital_policy_revisions ORDER BY symbol, revision"))).all(),
        }


async def test_it_prepares_a_simulation_database(db) -> None:
    url, engine = db
    report = await script.run(_args(), database_url=url, allowed_realms=REALMS)
    assert report["status"] == "ready" and report["realm"] == "ci"
    assert report["authority_epoch_appended"] is True and report["account_created"] is True
    assert report["policies"] == {"fUSD": "applied_disabled", "fUST": "applied"}
    state = await _snapshot(engine)
    assert state["epochs"] == [(1, "legacy"), (2, "ledger")]
    assert state["accounts"] == [(ACCOUNT, "simulation", "active", "bitfinex")]
    assert state["credentials"] == 0  # a simulated boot generates its own throwaway keys


async def test_the_policy_carries_the_ledger_envelope(db) -> None:
    url, engine = db
    await script.run(_args(), database_url=url, allowed_realms=REALMS)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    store = build_policy_store(Scope(ACCOUNT, "ci"))
    async with factory() as session:
        policy = (await store.read_applied(session, symbol="fUST")).policy
        assert policy.enabled is True and policy.max_offer_amount == Decimal("200")
        assert policy.envelope is not None
        assert (policy.envelope.min_period_days, policy.envelope.max_period_days,
                policy.envelope.max_open_offers, policy.envelope.rate_floor_ratio,
                policy.envelope.min_rate_apr) == (2, 2, 6, Decimal("0.5"), Decimal("0.01"))
        usd = (await store.read_applied(session, symbol="fUSD")).policy
        assert usd.enabled is False


async def test_a_second_run_changes_nothing(db) -> None:
    url, engine = db
    await script.run(_args(), database_url=url, allowed_realms=REALMS)
    before = await _snapshot(engine)
    report = await script.run(_args(), database_url=url, allowed_realms=REALMS)
    assert report["authority_epoch_appended"] is False and report["account_created"] is False
    assert report["policies"] == {"fUSD": "unchanged", "fUST": "unchanged"}
    assert await _snapshot(engine) == before


async def test_it_refuses_a_database_stamped_for_another_realm(db) -> None:
    url, engine = db
    for realm in ("prod", "ci"):  # ``ci`` is refused too unless the caller widens the set
        await _restamp(engine, realm)
        with pytest.raises(script.BootstrapRefused, match=f"database_realm_not_simulation stamp={realm}"):
            await script.run(_args(), database_url=url)  # the CLI's default: shadow only
    state = await _snapshot(engine)
    assert state["epochs"] == [(1, "legacy")] and state["accounts"] == []


async def test_it_refuses_an_unstamped_database(db) -> None:
    url, engine = db
    await _restamp(engine, None)
    with pytest.raises(DatabaseRealmMismatch, match="unstamped"):
        await script.run(_args(), database_url=url, allowed_realms=REALMS)
    assert (await _snapshot(engine))["epochs"] == [(1, "legacy")]


async def test_it_refuses_a_database_holding_a_prod_row(db) -> None:
    url, engine = db
    await _restamp(engine, "shadow")
    async with engine.begin() as conn:
        await conn.execute(text("SET LOCAL session_replication_role = replica"))
        await conn.execute(text(
            "INSERT INTO exchange_accounts(id,venue,label) VALUES (:id,'bitfinex','real')"),
            {"id": UUID("550e8400-e29b-41d4-a716-446655440000")})
        await conn.execute(text(
            "INSERT INTO capital_policy_revisions(id, exchange_account_id, deployment_environment, "
            "symbol, revision, schema_version, policy, digest, source) VALUES "
            "(gen_random_uuid(), '550e8400-e29b-41d4-a716-446655440000', 'prod', 'fUST', 1, 1, "
            "'{}', 'p', '{}')"))
    with pytest.raises(script.BootstrapRefused, match="prod_rows_present table=capital_policy_revisions"):
        await script.run(_args(), database_url=url)
    state = await _snapshot(engine)
    assert state["epochs"] == [(1, "legacy")]
    assert [a[1] for a in state["accounts"]] == ["real"]  # nothing was created beside it


async def test_it_refuses_an_account_that_holds_credentials(db) -> None:
    url, engine = db
    async with engine.begin() as conn:
        await conn.execute(text(
            "INSERT INTO exchange_accounts(id,venue,label) VALUES (:id,'bitfinex','simulation')"),
            {"id": ACCOUNT})
        await conn.execute(text(
            "INSERT INTO exchange_account_credentials(exchange_account_id, venue, label, api_key, "
            "secret_ciphertext, secret_nonce, wrapped_dek, dek_nonce, key_version, lifecycle_status) "
            "VALUES (:id, 'bitfinex', 'k', 'k', 'x'::bytea, 'x'::bytea, 'x'::bytea, 'x'::bytea, 1, 'pending')"),
            {"id": ACCOUNT})
    with pytest.raises(script.BootstrapRefused, match="simulation_account_has_credentials"):
        await script.run(_args(), database_url=url, allowed_realms=REALMS)
    assert (await _snapshot(engine))["epochs"] == [(1, "legacy")]  # rolled back whole
