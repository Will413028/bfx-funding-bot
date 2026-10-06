"""build_daemon composes the bot process on the ledger, the only capital authority (sqlite
construction only).

The databases here hold the epochs a fresh database holds at head (``legacy``, then the
``ledger`` genesis); the boot-rule tests read the real epoch row and the H-1 seed rule.

Mutation checks (one at a time; revert after each):

* the daemon holds any ``Legacy*`` adapter, the paper-position projection, the offer registry,
  the event store or a persister: ``test_the_daemon_holds_no_legacy_state``.
* the WS dispatcher gets its own ledger hint sink: ``test_the_daemon_hints_through_the_reconcile_channel``.
* the policy worker builds its own repository / reaches the event stream:
  ``test_the_daemon_applies_policy_and_resolutions_through_the_ledger``.
* the uncertainty worker is built without the ledger resolution: same test.
* the cycle effects are left off: ``test_the_daemon_observes_through_cycle_effects``.
* the bot accepts a database whose latest epoch is ``legacy``:
  ``test_a_legacy_epoch_refuses_either_venue``.
* ``require_ledger_seed`` is not called in ``build_daemon``, or its history check is dropped:
  ``test_a_scope_with_legacy_history_boots_only_over_its_seed``.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from bfx_funding_bot.apps.authority_support import SUPPORTED
from bfx_funding_bot.core.authority import AuthorityMismatch
from bfx_funding_bot.modules.execution.deployment_input import LedgerDeploymentInput
from bfx_funding_bot.modules.execution.ledger_cycle_effects import LedgerCycleEffects
from bfx_funding_bot.modules.ledger import Scope
from bfx_funding_bot.modules.trading import CapitalPolicy
from tests.apps.walk import legacy_state
from tests.modules.marketfeed.account_test_helpers import (
    TEST_EXCHANGE_ACCOUNT_ID,
    configure_account_env,
    seed_exchange_account,
)
from tests.modules.marketfeed.test_daemon_wiring import _write_cells_yaml


async def _env_and_db(monkeypatch, tmp_path, httpx_mock, *, name: str, realm: str = "ci"):
    from bfx_funding_bot.core.db import Base, make_async_engine_from_url

    configure_account_env(monkeypatch)
    for name in list(os.environ):
        if variable_is_legacy(name):
            monkeypatch.delenv(name)
    values = {
        "BFX_PHASE": "live", "BFX_DEPLOYMENT_ENV": realm,
        "BFX_EXECUTION_POLICY": "book_guarded", "BFX_BOOK_MAX_AGE_SECONDS": "30",
        "BFX_BOOK_RECONCILE_INTERVAL_SECONDS": "15", "BFX_BOOK_MAX_DOWN_PCT": "0.15",
        "BFX_SERVICE_VERSION": "test", "BFX_HEALTHZ_PORT": "0",
        "BFX_SAFETY_CONFIG": str(Path(__file__).parents[3] / "configs/safety.live.yaml"),
        "DATABASE_URL": f"sqlite+aiosqlite:///{tmp_path / name}.db",
    }
    for name, value in values.items():
        monkeypatch.setenv(name, value)
    engine = make_async_engine_from_url(values["DATABASE_URL"])
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await seed_exchange_account(engine, capital_policies=False)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    httpx_mock.add_response(url=re.compile(r"https://api-pub\.bitfinex\.com/.*"),
                            method="GET", json=[], is_reusable=True, is_optional=True)
    return engine, factory, _write_cells_yaml(tmp_path)


def variable_is_legacy(name: str) -> bool:
    return name.startswith("BFX_CANARY_") or name in (
        "BFX_ALLOCATION_CAP_USDT", "BFX_BALANCE_BUFFER_USDT", "BFX_CONCENTRATION_PCT",
        "BFX_WS_CLIENT_ENABLED", "BFX_FILL_TRACKER_ENABLED",
    )


async def _apply_policies(factory, realm: str = "ci") -> None:
    from bfx_funding_bot.modules.ledger.wiring import build_policy_store

    store = build_policy_store(Scope(TEST_EXCHANGE_ACCOUNT_ID, realm))
    async with factory.begin() as session:
        await store.apply_policy(session, symbol="fUST", policy=CapitalPolicy(enabled=True),
                                 expected_revision=0, source={"fixture": True})
        await store.apply_policy(session, symbol="fUSD", policy=CapitalPolicy(enabled=False),
                                 expected_revision=0, source={"fixture": True})


async def _build(monkeypatch, tmp_path, httpx_mock):
    from bfx_funding_bot.apps.bot import build_daemon

    engine, factory, path = await _env_and_db(monkeypatch, tmp_path, httpx_mock, name="ledger")
    await _apply_policies(factory)
    return engine, factory, await build_daemon(cells_yaml_path=path, skip_ws=True)


@pytest.mark.asyncio
async def test_the_daemon_holds_no_legacy_state(monkeypatch, tmp_path, httpx_mock) -> None:
    engine, _, daemon = await _build(monkeypatch, tmp_path, httpx_mock)
    try:
        assert not hasattr(daemon, "ledger") and not hasattr(daemon, "offer_registry")
        assert legacy_state(daemon, "daemon") == []
        # Nothing of the event-sourced projection listens on the bus.
        handlers = {
            type(getattr(handler, "__self__", None)).__name__
            for handlers in daemon.bus._handlers.values() for handler in handlers
        }
        assert not handlers & {"PaperPositionLedger", "OfferRegistry"}
        # What the ledger publishes is counted, and the NAV signal still reaches its monitor.
        from bfx_funding_bot.modules.execution.command_boundary import CommandOutcomeNotice
        from bfx_funding_bot.modules.execution.events import PositionReconciled
        from bfx_funding_bot.modules.ledger import UnknownResolutionNotice, VenueHintNotification
        for notice in (CommandOutcomeNotice, UnknownResolutionNotice, VenueHintNotification):
            assert len(daemon.bus._handlers[notice]) == 1
        assert [h.__self__.__class__.__name__ for h in daemon.bus._handlers[PositionReconciled]
                if hasattr(h, "__self__")] == ["NavDropMonitor"]
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_the_daemon_hints_through_the_reconcile_channel(
    monkeypatch, tmp_path, httpx_mock,
) -> None:
    engine, _, daemon = await _build(monkeypatch, tmp_path, httpx_mock)
    try:
        assert not hasattr(daemon, "fill_tracker")
        sink = daemon.ws_dispatcher._venue_hints
        assert type(sink).__name__ == "LedgerVenueHintSink"
        assert sink._request_resync == daemon.periodic_reconcile.resync.request
        assert daemon.auth_ws._on_resync_needed == daemon.periodic_reconcile.resync.request
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_the_daemon_applies_policy_and_resolutions_through_the_ledger(
    monkeypatch, tmp_path, httpx_mock,
) -> None:
    engine, _, daemon = await _build(monkeypatch, tmp_path, httpx_mock)
    try:
        worker = daemon.capital_policy_control
        assert type(worker.policy_store).__name__ == "LedgerPolicyStore"
        assert type(worker.scope_lock).__name__ == "LedgerScopeLock"
        assert type(daemon.uncertainty_worker.requests.resolution).__name__ == (
            "LedgerOperatorResolution")
        assert isinstance(daemon.periodic_reconcile._deployment_input, LedgerDeploymentInput)
        boundary_journal = daemon.command_gate._boundary.journal
        assert type(boundary_journal).__name__ == "_SqlCommandJournal"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_the_daemon_observes_through_cycle_effects(monkeypatch, tmp_path, httpx_mock) -> None:
    engine, _, daemon = await _build(monkeypatch, tmp_path, httpx_mock)
    try:
        assert isinstance(daemon.boot_recovery, LedgerCycleEffects)
        runtime = daemon.periodic_reconcile._recovery  # the timing shim around the sink
        assert isinstance(runtime._inner, LedgerCycleEffects)
        assert daemon.boot_recovery._inner._grace_ms == 0
        assert runtime._inner._inner._grace_ms == 120_000
        assert daemon.boot_recovery._foreign_grace_ms == 0
        assert runtime._inner._foreign_grace_ms == 120_000
        assert daemon.observation_scope == Scope(TEST_EXCHANGE_ACCOUNT_ID, "ci")
        # The consumers that only cached the projection take none.
        assert type(daemon.trading_status._exposure).__name__ == "CapitalStatusReads"
        status = await daemon.trading_status.snapshot()
        assert "capital_policy" in {g["name"] for g in status["guards"]}
        assert not ({"allocation_cap", "buying_power"} & {g["name"] for g in status["guards"]})
    finally:
        await engine.dispose()


async def _db(monkeypatch, tmp_path, httpx_mock, *, realm: str):
    """A database as it is at head (latest epoch ``ledger``), with the policies applied."""
    engine, factory, path = await _env_and_db(
        monkeypatch, tmp_path, httpx_mock, name=f"db-{realm}", realm=realm)
    await _apply_policies(factory, realm)
    return engine, factory, path


async def _legacy_history(factory, *, account=TEST_EXCHANGE_ACCOUNT_ID, realm: str = "prod") -> None:
    async with factory.begin() as session:
        await session.execute(text(
            "INSERT INTO event_log (account_id, exchange_account_id, deployment_environment, "
            "event_type, payload, occurred_at_ms) VALUES (:a, :e, :r, 'x', '{}', 1)"),
            {"a": str(account), "e": str(account).replace("-", ""), "r": realm})


async def _observation(factory, *, origin: str, account=TEST_EXCHANGE_ACCOUNT_ID,
                       realm: str = "prod") -> None:
    from uuid import uuid4

    from bfx_funding_bot.modules.ledger.tables import LedgerObservationRow

    async with factory.begin() as session:
        session.add(LedgerObservationRow(
            id=uuid4(), query_id=uuid4(), exchange_account_id=account,
            deployment_environment=realm, schema_version=1, query_finished_at_ms=1,
            confirmation_finished_at_ms=1, accept_revision=0, origin=origin,
            wallets_complete=False, offers_complete=True, credits_complete=True,
            loans_complete=True, offer_history_complete=False, credit_history_complete=False,
            trades_complete=False, first_digest="d", confirmation_digest="d",
            accepted=origin == "legacy_seed",  # a runtime one is accepted only when complete
            evidence={},
        ))


@pytest.mark.asyncio
async def test_the_real_epoch_boots_bitfinex_on_the_ledger(monkeypatch, tmp_path, httpx_mock) -> None:
    """The real epoch read (no monkeypatch); ``ci`` has no legacy history, so no seed."""
    from bfx_funding_bot.apps.bot import build_daemon

    assert frozenset({"ledger"}) == SUPPORTED
    engine, _, path = await _db(monkeypatch, tmp_path, httpx_mock, realm="ci")
    try:
        daemon = await build_daemon(cells_yaml_path=path, skip_ws=True)
        assert isinstance(daemon.boot_recovery, LedgerCycleEffects)
        assert legacy_state(daemon, "daemon") == []
    finally:
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["live", "shadow"])
async def test_a_legacy_epoch_refuses_either_venue(monkeypatch, tmp_path, httpx_mock, phase) -> None:
    """A database whose latest epoch is ``legacy`` (never switched, or restored from before the
    switch) is refused by the bot on the real and on the simulated venue."""
    from bfx_funding_bot.apps import bot
    from bfx_funding_bot.apps.bot import build_daemon

    refused: list[str] = []
    monkeypatch.setattr(bot, "_refuse_live_boot", _recording(refused))
    engine, factory, path = await _db(monkeypatch, tmp_path, httpx_mock, realm="ci")
    monkeypatch.setenv("BFX_PHASE", phase)
    try:
        async with factory.begin() as session:
            await session.execute(text(
                "INSERT INTO capital_authority_epoch (epoch_seq, authority, set_at_ms, actor, reason) "
                "VALUES (3, 'legacy', 3, 'test', 'restored')"))
        with pytest.raises(AuthorityMismatch, match="authority_unsupported value=legacy"):
            await build_daemon(cells_yaml_path=path, skip_ws=True)
        assert refused == ["authority_unsupported value=legacy build=ledger"]
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_a_scope_with_legacy_history_boots_only_over_its_seed(
    monkeypatch, tmp_path, httpx_mock,
) -> None:
    """H-1: a scope with legacy history boots the ledger only over its seed observation; a
    runtime (``venue``) observation or another scope's seed is not it."""
    from uuid import UUID

    from bfx_funding_bot.apps import bot
    from bfx_funding_bot.apps.bot import build_daemon

    refused: list[str] = []
    monkeypatch.setattr(bot, "_refuse_live_boot", _recording(refused))
    engine, factory, path = await _db(monkeypatch, tmp_path, httpx_mock, realm="prod")
    try:
        # Without legacy history the boot needs no seed.
        await build_daemon(cells_yaml_path=path, skip_ws=True)
        await _legacy_history(factory)
        await _observation(factory, origin="venue")
        await _observation(factory, origin="legacy_seed",
                           account=UUID("00000000-0000-0000-0000-0000000000ff"))
        with pytest.raises(AuthorityMismatch, match="ledger_seed_missing"):
            await build_daemon(cells_yaml_path=path, skip_ws=True)
        assert refused == [f"ledger_seed_missing scope={TEST_EXCHANGE_ACCOUNT_ID}:prod"]

        await _observation(factory, origin="legacy_seed")
        daemon = await build_daemon(cells_yaml_path=path, skip_ws=True)
        assert isinstance(daemon.boot_recovery, LedgerCycleEffects)
        assert len(refused) == 1
    finally:
        await engine.dispose()


def _recording(refused: list[str]):
    async def refuse(exc: BaseException, **_: object) -> None:
        refused.append(str(exc))

    return refuse
