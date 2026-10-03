"""build_daemon composes the bot process by capital authority (sqlite construction only).

A ledger-authority process is reachable only through the monkeypatched epoch read below:
``SUPPORTED_AUTHORITIES`` still refuses it at a real boot.

Mutation checks (one at a time; revert after each):

* a ledger-authority daemon holds any ``Legacy*`` adapter, the paper-position projection, the
  offer registry, the event store or a persister: ``test_a_ledger_daemon_holds_no_legacy_state``.
* ``PaperPositionLedger`` / ``OfferRegistry`` subscribed to the bus under the ledger: same test
  (the walk follows the bus's handler lists to their bound methods).
* the WS dispatcher or the fill tracker is left on the legacy hint sink, or each gets its own
  ledger sink: ``test_a_ledger_daemon_shares_one_ledger_hint_sink``.
* the policy worker builds its own repository / reaches the event stream under the ledger:
  ``test_a_ledger_daemon_applies_policy_and_resolutions_through_the_ledger``.
* the uncertainty worker is built without the ledger resolution: same test.
* the effects wrap the legacy sink: ``test_a_ledger_daemon_observes_through_cycle_effects``;
  a legacy daemon wrapped in effects: ``test_a_legacy_daemon_is_wired_as_it_always_was``.
* the legacy branch constructs a different object graph than before:
  ``test_a_legacy_daemon_is_wired_as_it_always_was``.
* a real ledger epoch row stops refusing a live boot: ``test_a_ledger_epoch_still_refuses_a_live_boot``.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from bfx_funding_bot.core.authority import SUPPORTED_AUTHORITIES, AuthorityMismatch
from bfx_funding_bot.modules.execution.deployment_input import (
    LedgerDeploymentInput,
    LegacyDeploymentInput,
)
from bfx_funding_bot.modules.execution.ledger_cycle_effects import LedgerCycleEffects
from bfx_funding_bot.modules.ledger import Scope
from bfx_funding_bot.modules.trading import CapitalPolicy
from tests.apps.walk import legacy_state
from tests.modules.marketfeed.account_test_helpers import (
    TEST_EXCHANGE_ACCOUNT_ID,
    configure_account_env,
    paper_ledger_of,
    seed_exchange_account,
)
from tests.modules.marketfeed.test_daemon_wiring import _write_cells_yaml


async def _env_and_db(monkeypatch, tmp_path, httpx_mock, *, authority: str):
    from bfx_funding_bot.core.db import Base, make_async_engine_from_url

    configure_account_env(monkeypatch)
    for name in list(os.environ):
        if name.startswith("BFX_CANARY_") or name in (
            "BFX_ALLOCATION_CAP_USDT", "BFX_BALANCE_BUFFER_USDT", "BFX_CONCENTRATION_PCT",
        ):
            monkeypatch.delenv(name)
    values = {
        "BFX_PHASE": "live", "BFX_DEPLOYMENT_ENV": "ci", "BFX_EXECUTOR": "bitfinex_live",
        "BFX_WS_CLIENT_ENABLED": "true", "BFX_FILL_TRACKER_ENABLED": "true",
        "BFX_EXECUTION_POLICY": "book_guarded", "BFX_BOOK_MAX_AGE_SECONDS": "30",
        "BFX_BOOK_RECONCILE_INTERVAL_SECONDS": "15", "BFX_BOOK_MAX_DOWN_PCT": "0.15",
        "BFX_SERVICE_VERSION": "test", "BFX_HEALTHZ_PORT": "0",
        "BFX_SAFETY_CONFIG": str(Path(__file__).parents[3] / "configs/safety.live.yaml"),
        "DATABASE_URL": f"sqlite+aiosqlite:///{tmp_path / authority}.db",
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


async def _apply_policies(factory, authority: str) -> None:
    from bfx_funding_bot.modules.execution.capital_repository import CapitalRepository
    from bfx_funding_bot.modules.execution.legacy_ports import LegacyPolicyStore
    from bfx_funding_bot.modules.ledger.wiring import build_policy_store

    scope = Scope(TEST_EXCHANGE_ACCOUNT_ID, "ci")
    store = (build_policy_store(scope) if authority == "ledger" else LegacyPolicyStore(
        CapitalRepository(account_id=TEST_EXCHANGE_ACCOUNT_ID, environment="ci",
                          max_snapshot_age_ms=10_000)))
    async with factory.begin() as session:
        await store.apply_policy(session, symbol="fUST", policy=CapitalPolicy(enabled=True),
                                 expected_revision=0, source={"fixture": True})
        await store.apply_policy(session, symbol="fUSD", policy=CapitalPolicy(enabled=False),
                                 expected_revision=0, source={"fixture": True})


async def _build(monkeypatch, tmp_path, httpx_mock, authority: str):
    from bfx_funding_bot.apps import bot
    from bfx_funding_bot.apps.bot import build_daemon

    engine, factory, path = await _env_and_db(monkeypatch, tmp_path, httpx_mock, authority=authority)
    await _apply_policies(factory, authority)
    if authority == "ledger":
        async def ledger_epoch(_session: object) -> str:
            return "ledger"

        monkeypatch.setattr(bot, "read_authority", ledger_epoch)
    return engine, factory, await build_daemon(cells_yaml_path=path, skip_ws=True)


@pytest.mark.asyncio
async def test_a_ledger_daemon_holds_no_legacy_state(monkeypatch, tmp_path, httpx_mock) -> None:
    engine, _, daemon = await _build(monkeypatch, tmp_path, httpx_mock, "ledger")
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
async def test_a_ledger_daemon_hints_through_the_reconcile_channel_and_composes_no_rest_tracker(
    monkeypatch, tmp_path, httpx_mock,
) -> None:
    # BFX_FILL_TRACKER_ENABLED=true is set: the ledger authority still composes no REST tracker.
    engine, _, daemon = await _build(monkeypatch, tmp_path, httpx_mock, "ledger")
    try:
        assert daemon.fill_tracker is None
        sink = daemon.ws_dispatcher._venue_hints
        assert type(sink).__name__ == "LedgerVenueHintSink"
        assert sink._request_resync == daemon.periodic_reconcile.resync.request
        assert daemon.auth_ws._on_resync_needed == daemon.periodic_reconcile.resync.request
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_a_ledger_daemon_applies_policy_and_resolutions_through_the_ledger(
    monkeypatch, tmp_path, httpx_mock,
) -> None:
    engine, _, daemon = await _build(monkeypatch, tmp_path, httpx_mock, "ledger")
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
async def test_a_ledger_daemon_observes_through_cycle_effects(monkeypatch, tmp_path, httpx_mock) -> None:
    engine, _, daemon = await _build(monkeypatch, tmp_path, httpx_mock, "ledger")
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
        assert daemon.periodic_reconcile._deployment._uncertainty_synced is None
        status = await daemon.trading_status.snapshot()
        assert "capital_policy" in {g["name"] for g in status["guards"]}
        assert not ({"allocation_cap", "buying_power"} & {g["name"] for g in status["guards"]})
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_a_legacy_daemon_is_wired_as_it_always_was(monkeypatch, tmp_path, httpx_mock) -> None:
    engine, _, daemon = await _build(monkeypatch, tmp_path, httpx_mock, "legacy")
    try:
        assert type(paper_ledger_of(daemon)).__name__ == "PaperPositionLedger"
        assert type(daemon.boot_recovery).__name__ == "LegacyObservationSink"
        recovery = daemon.periodic_reconcile._recovery
        assert type(recovery._inner).__name__ == "LegacyObservationSink"
        assert isinstance(daemon.periodic_reconcile._deployment_input, LegacyDeploymentInput)
        assert type(daemon.capital_policy_control.policy_store).__name__ == "LegacyPolicyStore"
        assert type(daemon.capital_policy_control.scope_lock).__name__ == "LegacyScopeLock"
        assert type(daemon.uncertainty_worker.requests.resolution).__name__ == (
            "LegacyOperatorResolution")
        assert type(daemon.fill_tracker._venue_hints).__name__ == "LegacyVenueHintSink"
        assert type(daemon.ws_dispatcher._venue_hints).__name__ == "LegacyVenueHintSink"
        assert type(daemon.command_gate._boundary.journal).__name__ == "LegacyCommandJournal"
        assert type(daemon.command_gate._boundary.effects).__name__ == "LegacyCommandEffects"
        assert daemon.periodic_reconcile._deployment._uncertainty_synced is not None
        assert type(daemon.trading_status._exposure).__name__ == "CapitalStatusReads"
        assert type(daemon.fill_tracker).__name__ == "RestPollingFillTracker"
        assert daemon.fill_tracker._venue_hints is daemon.ws_dispatcher._venue_hints
        assert daemon.auth_ws._on_resync_needed == daemon.periodic_reconcile.resync.request
        # The projection and the registry listen on the bus, ledger first (as before).
        from bfx_funding_bot.modules.execution.events import ReservationClaimed
        owners = [type(getattr(h, "__self__", None)).__name__
                  for h in daemon.bus._handlers[ReservationClaimed]]
        assert owners[:2] == ["PaperPositionLedger", "OfferRegistry"]
        assert any(name.startswith("Legacy") for name in
                   (entry.split(": ")[1] for entry in legacy_state(daemon, "daemon")))
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_a_ledger_epoch_still_refuses_a_live_boot(monkeypatch, tmp_path, httpx_mock) -> None:
    from bfx_funding_bot.apps.bot import build_daemon

    assert frozenset({"legacy"}) == SUPPORTED_AUTHORITIES
    engine, factory, path = await _env_and_db(monkeypatch, tmp_path, httpx_mock, authority="epoch")
    try:
        async with factory.begin() as session:
            await session.execute(text(
                "INSERT INTO capital_authority_epoch (epoch_seq, authority, set_at_ms, actor, reason) "
                "VALUES (2, 'ledger', 2, 'test', 'switch')"))
        with pytest.raises(AuthorityMismatch, match="authority_unsupported"):
            await build_daemon(cells_yaml_path=path, skip_ws=True)
    finally:
        await engine.dispose()
