"""A submit the venue answers by refusing the key, end to end on PostgreSQL (simulated venue).

The composition is production's (``bot.build_daemon``, ``bot._run_daemon``, the real command
gate, deployment reconciler, periodic reconcile and ledger). The seams:

* the simulated venue answers the first submit with ``FaultKind.KEY_REFUSED`` -- HTTP 500
  ``["error", 10100, "apikey: invalid"]``, nothing placed: the key was revoked between the
  cycle's reads and its submit;
* the second process signs with a key the venue does not know (``VenueSeam``), as the
  revoked key would be after the restart;
* ``Daemon.run`` of the first process is one periodic-reconcile tick in a TaskGroup (what the
  real run does every 90 s), so ``_run_daemon``'s exit handling sees what the real loop
  raises without waiting on a wall-clock interval.

The rotation that installs the new key cannot run here (the simulated venue has no vault;
``test_credential_recovery.py`` covers the rotation inside recovery mode): the third process
boots on a key the venue accepts, which is what an applied rotation's restart does.

Mutation checks (one at a time; revert after each):

* drop the raise in ``DeploymentReconciler`` after a key-refused UNKNOWN: the tick returns,
  ``_run_daemon`` does not exit 78, the test fails.
* drop ``_raise_if_key_refused`` in ``PeriodicReconcile._deploy``: same.
* classify a 10100 submit as REJECTED (not UNKNOWN): no UNKNOWN, the ``unknown`` count fails.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import func, select, text

from bfx_funding_bot.apps import bot
from bfx_funding_bot.apps.venue import VenueSeam
from bfx_funding_bot.core.errors import EXIT_CODE_AUTH_FAILED
from bfx_funding_bot.modules.execution.protocols import Credentials
from bfx_funding_bot.modules.execution.safety.trading_state import read_current
from bfx_funding_bot.modules.ledger.matching import UNKNOWN_SETTLE_MS
from bfx_funding_bot.modules.ledger.tables import (
    ExecutionResolutionJournalRow,
    TransportOutcomeJournalRow,
)
from bfx_funding_bot.modules.simulated_venue import FaultKind, FaultPlan, FaultRule, FaultTarget
from tests.async_wait import until
from tests.modules.marketfeed.account_test_helpers import TEST_EXCHANGE_ACCOUNT_ID

from . import test_ledger_schema_roles as _schema_roles
from .sim_daemon import HOUR, SCOPE, T0, close_sim_env, make_sim_env

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]
ledger_db = _schema_roles.ledger_db

REVOKED = Credentials("FAKE-REVOKED-KEY-000000", "FAKE-REVOKED-SECRET-not-real-000000")


async def _unknown_submits(factory: Any) -> int:
    async with factory() as session:
        return int(await session.scalar(select(func.count()).select_from(
            TransportOutcomeJournalRow).where(TransportOutcomeJournalRow.kind == "unknown")) or 0)


async def _states(factory: Any) -> list[str]:
    async with factory() as session:
        return [row.state for row in (await session.execute(text(
            "SELECT state FROM trading_state ORDER BY id"))).all()]


async def _runs(factory: Any) -> list[str | None]:
    async with factory() as session:
        return [row.end_reason for row in (await session.execute(text(
            "SELECT end_reason FROM bot_runs ORDER BY started_at_ms"))).all()]


async def test_a_submit_refused_for_the_key_exits_78_and_its_unknown_resolves_on_the_new_key(
        ledger_db, monkeypatch, httpx_mock, tmp_path) -> None:
    sim, engine = await make_sim_env(ledger_db, monkeypatch, httpx_mock, tmp_path)
    compose = bot.build_daemon
    try:
        # -- process 1: trading, then a submit answered with the key refusal ----------------
        plan = FaultPlan(rules=(
            FaultRule(FaultTarget.SUBMIT, FaultKind.KEY_REFUSED, ordinals=frozenset({1})),))
        first = await sim.build(faults=plan)
        await sim.boot(first, T0)
        await sim.activate()
        activated = await _states(sim.factory)
        assert activated[-1] == "ACTIVE"
        sim.clock.advance(HOUR)
        sim.quote(first)

        async def one_tick() -> None:  # the periodic reconcile's next tick, inside the run
            first.booted = True
            async with asyncio.TaskGroup() as tg:
                tg.create_task(sim.tick(first), name="periodic_reconcile")

        first.run = one_tick

        async def built(**_kw: Any) -> Any:
            return first

        monkeypatch.setattr(bot, "build_daemon", built)
        with pytest.raises(SystemExit) as exited:
            await bot._run_daemon(asyncio.Event(), SimpleNamespace(on_lag=None))
        assert exited.value.code == EXIT_CODE_AUTH_FAILED == 78
        first.writer_lock = None  # released by _run_daemon's exit
        assert "daemon_fatal" in sim.alerts
        assert await _runs(sim.factory) == ["fatal"]
        # The refused submit is UNKNOWN (classification unchanged); nothing was placed, and no
        # second submit went out on the refused key.
        assert await _unknown_submits(sim.factory) == 1
        assert sim.venue(first).state.offers == {}
        assert await first.command_gate._uncertainty_reader.has_open(None, SCOPE, "fUST")
        assert await _states(sim.factory) == activated  # no trading state written

        # -- process 2: the restart on the still-refused key holds in recovery mode ---------
        stop = asyncio.Event()
        second = await compose(
            cells_yaml_path=sim.cells_path, skip_ws=True, stop_event=stop,
            venue_seam=VenueSeam(feed=sim.feed, client_credentials=REVOKED))
        sim.daemons.append(second)
        sim.give_the_bot_a_book(second)

        async def built_second(**_kw: Any) -> Any:
            return second

        monkeypatch.setattr(bot, "build_daemon", built_second)
        run = asyncio.create_task(bot._run_daemon(stop, SimpleNamespace(on_lag=None)))
        await until(lambda: "credential_recovery_mode" in sim.alerts, what="recovery mode")
        assert second.booted is False
        stop.set()
        await run
        second.writer_lock = None
        assert await _runs(sim.factory) == ["fatal", "clean_stop"]

        # -- process 3: the new key works; the UNKNOWN resolves by itself -------------------
        monkeypatch.setattr(bot, "build_daemon", compose)
        third = await sim.build()
        await sim.boot(third, sim.clock.now + UNKNOWN_SETTLE_MS + 10_000)
        reader = third.command_gate._uncertainty_reader
        for _ in range(4):  # the boot observation, then periodic ticks if it needs them
            if not await reader.has_open(None, SCOPE, "fUST"):
                break
            sim.clock.advance(UNKNOWN_SETTLE_MS)
            await sim.tick(third)
        assert not await reader.has_open(None, SCOPE, "fUST"), "the UNKNOWN did not resolve"
        async with sim.factory() as session:
            resolutions = (await session.execute(select(
                ExecutionResolutionJournalRow.actor_kind, ExecutionResolutionJournalRow.actor_id,
                ExecutionResolutionJournalRow.action))).all()
            state = await read_current(session, account_id=TEST_EXCHANGE_ACCOUNT_ID,
                                       environment="ci")
        assert [tuple(r) for r in resolutions] == [("system", "system:reconcile", "not_accepted")]
        # No HALTED was written on the way: the account is still ACTIVE and trades again.
        assert await _states(sim.factory) == activated
        assert state is not None and state.state == "ACTIVE"
        sim.quote(third)
        await sim.tick(third)
        assert sim.venue(third).state.offers, "the new key did not trade"
        await sim.assert_sound(third)
    finally:
        monkeypatch.setattr(bot, "build_daemon", compose)
        await close_sim_env(sim, engine)
