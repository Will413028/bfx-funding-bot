"""A bot whose key the venue refuses at boot, end to end on PostgreSQL (simulated venue).

The composition is production's (``bot.build_daemon`` and ``bot._run_daemon``); the only seam
is ``VenueSeam.client_credentials``: the bot signs with a key the simulated venue does not
know, so every signed read is answered exactly as Bitfinex answers a revoked key (HTTP 500,
``["error", 10100, "apikey: invalid"]``). The simulated venue has no vault, so no rotation
worker runs here; the rotation inside recovery mode is ``test_credential_recovery.py`` and
``test_credential_rotation.py``.

Mutation checks (one at a time; revert after each):

* recovery mode runs trading control: the kill is applied with the refused key (its cancel-all
  recorded as failed) and ``test_a_refused_key_at_boot_*`` fails on ``requested``.
* ``_boot_until_observed`` re-raises a credential rejection: ``_run_daemon`` raises (exit 1,
  ``boot_refused``) and the test fails.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import insert, select, text

from bfx_funding_bot.apps import bot
from bfx_funding_bot.apps.venue import VenueSeam
from bfx_funding_bot.modules.execution.protocols import Credentials
from bfx_funding_bot.modules.execution.safety.tables import TradingControlRequestRow
from bfx_funding_bot.modules.marketfeed.healthz import make_app
from tests.async_wait import until
from tests.modules.marketfeed.account_test_helpers import TEST_EXCHANGE_ACCOUNT_ID

from . import test_ledger_schema_roles as _schema_roles
from .sim_daemon import close_sim_env, make_sim_env

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]
# The migrated database fixture, shared with the other simulated-venue tests.
ledger_db = _schema_roles.ledger_db

REVOKED = Credentials("FAKE-REVOKED-KEY-000000", "FAKE-REVOKED-SECRET-not-real-000000")
# Liveness beats recovery mode may record; any trading task's beat would show up here.
RECOVERY_BEATS = {"health_check", "db_keepalive", "writer_lock", "db"}


async def _queue_kill(factory: Any) -> Any:
    request_id = uuid4()
    async with factory.begin() as session:
        await session.execute(insert(TradingControlRequestRow).values(
            request_id=request_id, exchange_account_id=TEST_EXCHANGE_ACCOUNT_ID,
            deployment_environment="ci", action="kill", reason="kill during recovery",
            requested_by="operator", created_at_ms=1))
    return request_id


async def _kill_state(factory: Any, request_id: Any) -> str:
    async with factory() as session:
        row = await session.get(TradingControlRequestRow, request_id)
        assert row is not None
        return row.state


async def _runs(factory: Any) -> list[tuple[Any, str | None]]:
    async with factory() as session:
        return [tuple(r) for r in (await session.execute(text(
            "SELECT run_id, end_reason FROM bot_runs ORDER BY started_at_ms"))).all()]


async def test_a_refused_key_at_boot_holds_in_recovery_mode_and_a_kill_waits_for_a_working_key(
        ledger_db, monkeypatch, httpx_mock, tmp_path) -> None:
    sim, engine = await make_sim_env(ledger_db, monkeypatch, httpx_mock, tmp_path)
    try:
        stop = asyncio.Event()
        daemon = await bot.build_daemon(
            cells_yaml_path=sim.cells_path, skip_ws=True, stop_event=stop,
            venue_seam=VenueSeam(feed=sim.feed, client_credentials=REVOKED))
        sim.daemons.append(daemon)
        sim.give_the_bot_a_book(daemon)

        compose = bot.build_daemon

        async def built(**_kw: Any) -> Any:
            return daemon

        monkeypatch.setattr(bot, "build_daemon", built)
        kill_id = await _queue_kill(sim.factory)
        run = asyncio.create_task(bot._run_daemon(stop, SimpleNamespace(on_lag=None)))
        await until(lambda: "credential_recovery_mode" in sim.alerts, what="recovery mode")
        await until(lambda: "db_keepalive" in daemon.probe.last_active_ts, what="liveness")

        # Recovery mode: the run is recorded and open, nothing booted, nothing trades.
        assert not run.done()
        assert daemon.booted is False
        assert [reason for _, reason in await _runs(sim.factory)] == [None]
        assert set(daemon.probe.last_active_ts) <= RECOVERY_BEATS
        assert sim.alerts.count("credential_recovery_mode") == 1
        assert "boot_refused" not in sim.alerts
        # The deploy gate sees a live process; readiness says it is not trading.
        client = TestClient(make_app(daemon.probe, readiness=daemon.trading_readiness))
        assert client.get("/healthz").status_code == 200
        assert client.get("/readyz").status_code == 503
        # The kill stays queued: a cancel-all on the refused key would be recorded as failed.
        assert await _kill_state(sim.factory, kill_id) == "requested"
        async with sim.factory() as session:
            assert await session.scalar(text("SELECT count(*) FROM funding_cancel_all_audit")) == 0
        # The venue answered every signed request with its refusal, nothing else.
        assert sim.venue(daemon).internal_failures == []

        stop.set()
        await run  # a clean end, not a refused boot
        assert [reason for _, reason in await _runs(sim.factory)] == ["clean_stop"]

        # The next boot on a working key applies the kill and sends its cancel-all.
        daemon.writer_lock = None  # released by _run_daemon's exit
        monkeypatch.setattr(bot, "build_daemon", compose)
        restarted = await sim.build()
        await sim.boot(restarted)

        async def allow(session: Any, *, account_id: Any, user: str) -> bool:
            return user == "operator"

        restarted.trading_control.authority = allow
        assert await restarted.trading_control.process(kill_id) == "applied"
        async with sim.factory() as session:
            audited = (await session.execute(select(text("operator_request_id, phase")).select_from(
                text("funding_cancel_all_audit")))).all()
        assert audited and {row.operator_request_id for row in audited} == {kill_id}
        assert "failed" not in {row.phase for row in audited}
    finally:
        await close_sim_env(sim, engine)
