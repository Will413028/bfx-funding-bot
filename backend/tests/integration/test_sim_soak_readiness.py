"""What a soak needs before it starts (ADR 2026-10-03 D3 and its 2026-10-04 amendment).

* A freshly bootstrapped database must actually trade: ``bootstrap_simulation_db --activate``
  then the production composition submits and the venue acknowledges. Without ``--activate``
  the same database stays HALTED and never submits, which is the vacuous soak this prevents.
* ``BFX_SIM_FAULTS`` reaches the in-process transport from the environment, and every injection
  is a durable ``fault_injected`` row; the Bitfinex composition refuses the knob.

Mutations (one at a time; revert after each): ``--activate`` writes nothing
(``test_bootstrap_then_the_daemon_really_submits``); the bitfinex branch of ``build_venue``
stops refusing ``BFX_SIM_FAULTS`` (``test_the_bitfinex_composition_refuses_the_fault_knob``); the
transport does not append the injection event (``test_the_env_fault_knob_injects_and_records``).
"""
from __future__ import annotations

import argparse
from decimal import Decimal
from uuid import UUID

import pytest
import pytest_asyncio
from sqlalchemy import text

from bfx_funding_bot.apps import bot
from bfx_funding_bot.core.errors import ConfigurationError
from scripts import bootstrap_simulation_db as bootstrap
from tests.modules.marketfeed.account_test_helpers import TEST_VAULT_KEK_B64

from .sim_daemon import HOUR, SimEnv, close_sim_env, make_sim_env
from .test_ledger_schema_roles import ledger_db  # noqa: F401 - fixture dependency

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

SIM_ACCOUNT = UUID("5a5a5a5a-1111-4222-8333-444455556666")


def _args(**changes) -> argparse.Namespace:  # type: ignore[no-untyped-def]
    base = {
        "exchange_account_id": SIM_ACCOUNT, "symbol": ["fUST"], "disabled_symbol": ["fUSD"],
        "max_offer_amount": Decimal("500"), "min_period_days": 2, "max_period_days": 2,
        "max_open_offers": 6, "rate_floor_ratio": Decimal("0.5"), "min_rate_apr": Decimal("0.01"),
        "activate": False,
    }
    return argparse.Namespace(**{**base, **changes})


@pytest_asyncio.fixture
async def fresh(ledger_db, monkeypatch, httpx_mock, tmp_path):  # noqa: F811
    """A stamped, migrated database with nothing on the ledger epoch: only the owner's bootstrap
    prepares it, and the daemon runs as the simulated account the script created."""
    sim, engine = await make_sim_env(
        ledger_db, monkeypatch, httpx_mock, tmp_path, epoch=None, policy=False)
    monkeypatch.setenv("BFX_EXCHANGE_ACCOUNT_ID", str(SIM_ACCOUNT))
    try:
        yield sim, engine
    finally:
        await close_sim_env(sim, engine)


async def _count(engine, sql: str) -> int:
    async with engine.connect() as conn:
        return int(await conn.scalar(text(sql)) or 0)


async def _first_cycle(sim: SimEnv, daemon) -> None:  # type: ignore[no-untyped-def]
    await sim.boot(daemon)
    sim.clock.advance(HOUR)
    sim.quote(daemon)
    await sim.tick(daemon)


async def test_bootstrap_then_the_daemon_really_submits(fresh) -> None:
    sim, engine = fresh
    report = await bootstrap.run(_args(activate=True), database_url=sim.url, allowed_realms=("ci",))
    assert report["trading_state"] == "activated"
    daemon = await sim.build()
    await _first_cycle(sim, daemon)
    resting = [o for o in sim.venue(daemon).state.offers.values() if o.resting]
    assert len(resting) == 1 and resting[0].symbol == "fUST"
    assert await _count(engine, "SELECT count(*) FROM transport_outcome_journal "
                                "WHERE kind = 'ack'") == 1
    assert await _count(engine, "SELECT count(*) FROM sim_venue_event "
                                "WHERE event_type = 'offer_placed'") == 1
    sim.assert_venue_clean(daemon)


async def test_without_activate_the_same_database_stays_halted_and_never_submits(fresh) -> None:
    sim, engine = fresh
    await bootstrap.run(_args(), database_url=sim.url, allowed_realms=("ci",))
    daemon = await sim.build()
    await _first_cycle(sim, daemon)
    assert not sim.venue(daemon).state.offers
    assert await _count(engine, "SELECT count(*) FROM submission_attempt_journal") == 0
    assert await _count(engine, "SELECT count(*) FROM trading_state") == 0


async def test_the_env_fault_knob_injects_and_records(fresh, monkeypatch) -> None:
    sim, engine = fresh
    monkeypatch.setenv("BFX_SIM_FAULTS", "unknown_5xx=1,seed=3")
    await bootstrap.run(_args(activate=True), database_url=sim.url, allowed_realms=("ci",))
    daemon = await sim.build()
    await _first_cycle(sim, daemon)
    assert await _count(engine, "SELECT count(*) FROM transport_outcome_journal "
                                "WHERE kind = 'unknown'") >= 1
    injected = await _count(engine, "SELECT count(*) FROM sim_venue_event "
                                    "WHERE event_type = 'fault_injected' "
                                    "AND payload->'data'->>'fault_kind' = 'unknown_5xx_error' "
                                    "AND payload->'data'->>'target' = 'submit'")
    assert injected >= 1
    assert not [o for o in sim.venue(daemon).state.offers.values() if o.resting]  # not placed
    # The record is in the database, so a restart (a new process) still has it.
    restarted = await sim.restart(daemon)
    assert restarted is not daemon
    assert await _count(engine, "SELECT count(*) FROM sim_venue_event "
                                "WHERE event_type = 'fault_injected'") >= injected


async def test_no_knob_means_no_injection_event(fresh, monkeypatch) -> None:
    sim, engine = fresh
    monkeypatch.delenv("BFX_SIM_FAULTS", raising=False)
    await bootstrap.run(_args(activate=True), database_url=sim.url, allowed_realms=("ci",))
    daemon = await sim.build()
    await _first_cycle(sim, daemon)
    assert await _count(engine, "SELECT count(*) FROM sim_venue_event "
                                "WHERE event_type = 'fault_injected'") == 0


async def test_the_bitfinex_composition_refuses_the_fault_knob(
        ledger_db, monkeypatch, httpx_mock, tmp_path) -> None:  # noqa: F811
    sim, engine = await make_sim_env(ledger_db, monkeypatch, httpx_mock, tmp_path)
    try:
        monkeypatch.delenv("BFX_SIM_INITIAL_WALLETS")
        monkeypatch.setenv("BFX_PHASE", "live")
        monkeypatch.setenv("BFX_WS_CLIENT_ENABLED", "true")
        monkeypatch.setenv("BFX_VAULT_KEK", TEST_VAULT_KEK_B64)
        monkeypatch.setenv("BFX_SIM_FAULTS", "unknown_5xx=0.01")

        async def ledger_epoch(_session: object, *, supported: object) -> str:
            return "ledger"  # as test_simulated_boot: production still refuses it for Bitfinex

        monkeypatch.setattr(bot, "read_authority", ledger_epoch)
        with pytest.raises(ConfigurationError, match="BFX_SIM_FAULTS is only valid"):
            await bot.build_daemon(cells_yaml_path=sim.cells_path, skip_ws=True)
    finally:
        await close_sim_env(sim, engine)
