"""``scripts/bootstrap_simulation_db.py`` on migrated PostgreSQL, run as the owner.

Mutations (one at a time; revert after each): skip the realm check (``test_it_refuses_*``
realm cases), append the epoch on every run create the account again or write a revision on an unchanged second run
(``test_a_second_run_changes_nothing``),
leave the envelope out of the policy (``test_the_policy_carries_the_ledger_envelope``),
let ``--activate`` append over an existing HALT (``test_activate_never_resumes_a_halt``) or on a
second run (``test_activate_is_idempotent``).
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
        "exchange_account_id": ACCOUNT, "symbol": ["fUST"], "disabled_symbol": ["fUSD"],
        "max_offer_amount": Decimal("200"), "min_period_days": 2, "max_period_days": 2,
        "max_open_offers": 6, "rate_floor_ratio": Decimal("0.5"), "min_rate_apr": Decimal("0.01"),
        "activate": False,
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
    assert report["policies"] == {"fUSD": "applied", "fUST": "applied"}
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


async def test_a_changed_target_is_written_as_the_next_revision_and_only_that(db) -> None:
    url, engine = db
    await script.run(_args(), database_url=url, allowed_realms=REALMS)
    report = await script.run(
        _args(max_open_offers=3), database_url=url, allowed_realms=REALMS)
    assert report["policies"] == {"fUSD": "unchanged", "fUST": "applied"}
    assert (await _snapshot(engine))["revisions"] == [("fUSD", 1), ("fUST", 1), ("fUST", 2)]


async def test_what_may_be_enabled_is_the_ledgers_rule_not_the_scripts(db) -> None:
    from bfx_funding_bot.modules.ledger import PolicyRefused

    url, engine = db
    with pytest.raises(PolicyRefused, match="unsupported_enabled_symbol"):
        await script.run(_args(symbol=["fUST", "fUSD"], disabled_symbol=[]),
                         database_url=url, allowed_realms=REALMS)
    state = await _snapshot(engine)
    assert state["epochs"] == [(1, "legacy")] and state["revisions"] == []  # rolled back whole


async def test_the_realm_stamp_is_the_only_content_check(db) -> None:
    """A stamped-shadow database is prepared whatever else it holds; the stamp and the realm
    trigger are the single authority (no scan for rows of another realm)."""
    url, engine = db
    await _restamp(engine, "shadow")
    report = await script.run(_args(), database_url=url)  # the CLI's default realm set
    assert report["status"] == "ready" and report["realm"] == "shadow"


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


async def _states(engine) -> list[tuple[str, str, str]]:
    async with engine.connect() as conn:
        return [tuple(r) for r in (await conn.execute(text(
            "SELECT state, cause, actor FROM trading_state ORDER BY id"))).all()]


async def test_without_activate_the_scope_stays_without_a_state_row(db) -> None:
    url, engine = db
    report = await script.run(_args(), database_url=url, allowed_realms=REALMS)
    assert report["trading_state"] == "not_requested"
    assert await _states(engine) == []


async def test_activate_appends_active_once_on_a_fresh_scope(db) -> None:
    url, engine = db
    report = await script.run(_args(activate=True), database_url=url, allowed_realms=REALMS)
    assert report["trading_state"] == "activated"
    assert await _states(engine) == [("ACTIVE", "operator", "bootstrap_simulation_db")]


async def test_activate_is_idempotent(db) -> None:
    url, engine = db
    await script.run(_args(activate=True), database_url=url, allowed_realms=REALMS)
    report = await script.run(_args(activate=True), database_url=url, allowed_realms=REALMS)
    assert report["trading_state"] == "kept_active"
    assert len(await _states(engine)) == 1


async def test_activate_never_resumes_a_halt(db) -> None:
    url, engine = db
    await script.run(_args(), database_url=url, allowed_realms=REALMS)
    async with engine.begin() as conn:
        await conn.execute(text(
            "INSERT INTO trading_state (exchange_account_id, deployment_environment, state, "
            "cause, actor, reason, created_at_ms) VALUES (:a, 'ci', 'HALTED', 'operator', "
            "'soak-kill', 'kill test', 1)"), {"a": ACCOUNT})
    report = await script.run(_args(activate=True), database_url=url, allowed_realms=REALMS)
    assert report["trading_state"] == "kept_halted"
    assert await _states(engine) == [("HALTED", "operator", "soak-kill")]
