"""Selection of the bot process's capital ports by capital authority (``apps/bot_ports.py``).

Mutation checks (one at a time; revert after each):

* the ledger selection returns any ``Legacy*`` adapter or event-sourced state holder:
  ``test_ledger_selection_builds_no_legacy_object``.
* ``PaperPositionLedger`` / ``OfferRegistry`` built under the ledger:
  ``test_ledger_selection_builds_no_legacy_object`` (``legacy`` must be ``None``).
* the boot sink's grace is 120 000, or the runtime's is not: ``test_ledger_observation_sinks_*``.
* the effects wrap the legacy sink, or the ledger sink is left unwrapped:
  ``test_ledger_observation_sinks_*``, ``test_legacy_observation_sinks_*``.
* the ledger hint sink is not the channel's, or the legacy projection is subscribed in another
  order: ``test_ledger_venue_hint_sink_*``, ``test_legacy_selection_is_the_object_graph_*``.
* the boot foreign-offer grace is 120 000 (or the runtime's is not): ``test_ledger_observation_sinks_*``.
* the script / bot select the legacy policy store under the ledger: ``test_policy_ports_*``.
* the reconciler cache hook is wired under the ledger / missing under legacy:
  ``test_the_uncertainty_cache_hook_*``.
* the legacy branch builds a different object graph than before:
  ``test_legacy_selection_is_the_object_graph_it_always_was``.
"""
from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace
from uuid import UUID

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker

from bfx_funding_bot.apps.bot_ports import (
    BotPorts,
    ObservationVenue,
    select_bot_ports,
    select_policy_ports,
)
from bfx_funding_bot.core.db import Base, make_async_engine_from_url
from bfx_funding_bot.modules.execution.bus import DomainEventBus
from bfx_funding_bot.modules.execution.command_boundary import (
    CommandBoundary,
    LedgerCommandEffects,
)
from bfx_funding_bot.modules.execution.deployment_input import (
    LedgerDeploymentInput,
    LegacyDeploymentInput,
)
from bfx_funding_bot.modules.execution.events import (
    OrderFilled,
    PositionReconciled,
    ReservationClaimed,
    ReservationReleased,
)
from bfx_funding_bot.modules.execution.ledger_cycle_effects import LedgerCycleEffects
from bfx_funding_bot.modules.execution.observation_sink import LegacyObservationSink
from bfx_funding_bot.modules.execution.protocols import AccountContext, Credentials
from bfx_funding_bot.modules.execution.resync_channel import ResyncChannel
from bfx_funding_bot.modules.execution.safety.protection import AutomaticProtection
from bfx_funding_bot.modules.execution.uncertainty_resolution import LegacyOperatorResolution
from bfx_funding_bot.modules.ledger import BOOT_GRACE_MS, RUNTIME_GRACE_MS, Scope
from tests.apps.walk import legacy_state

ACCOUNT = UUID("550e8400-e29b-41d4-a716-446655440000")
SCOPE = Scope(ACCOUNT, "ci")


@pytest_asyncio.fixture
async def factory(tmp_path):
    engine = make_async_engine_from_url(f"sqlite+aiosqlite:///{tmp_path / 'ports.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


async def _select(factory, authority: str, *, bus=None, resync=None) -> BotPorts:
    return await select_bot_ports(
        authority, session_factory=factory, scope=SCOPE, account_id=str(ACCOUNT),
        bus=bus or DomainEventBus(), resync=resync or ResyncChannel(),
        clock=lambda: 1_000, max_snapshot_age_ms=10_000,
    )


def _venue() -> ObservationVenue:
    return ObservationVenue(
        auth_rest=SimpleNamespace(),  # type: ignore[arg-type]
        account_ctx=AccountContext(str(ACCOUNT), Credentials("key", "secret"), Decimal("0")),
        protection=AutomaticProtection(clock=lambda: 1_000),
        symbols=["fUST"], cells=[("fUST", "fUST_a30")],
    )


@pytest.mark.asyncio
async def test_ledger_selection_builds_no_legacy_object(factory) -> None:
    ports = await _select(factory, "ledger")
    sinks = ports.capital.observation(_venue())  # type: ignore[union-attr]
    hint_sink = ports.venue_hint_sink

    assert ports.authority == "ledger"
    assert ports.legacy is None
    assert legacy_state(ports, "ports") == []
    assert legacy_state(sinks, "sinks") == []
    assert legacy_state(hint_sink, "hint_sink") == []


@pytest.mark.asyncio
async def test_ledger_selection_names_the_ledger_adapters(factory) -> None:
    ports = await _select(factory, "ledger")
    capital = ports.capital
    assert capital is not None
    names = {
        "uncertainty_reader": type(ports.uncertainty_reader).__name__,
        "managed_offers": type(ports.managed_offers).__name__,
        "capital_authority": type(capital.capital_authority).__name__,
        "scope_lock": type(capital.scope_lock).__name__,
        "policy_store": type(capital.policy_store).__name__,
        "journal": type(capital.command_boundary.journal).__name__,
        "operator_resolution": type(capital.operator_resolution).__name__,
        "hint_sink": type(ports.venue_hint_sink).__name__,
    }
    assert names == {
        "uncertainty_reader": "LedgerUncertaintyReader",
        "managed_offers": "LedgerManagedOfferReader",
        "capital_authority": "LedgerCapitalAuthority",
        "scope_lock": "LedgerScopeLock",
        "policy_store": "LedgerPolicyStore",
        "journal": "_SqlCommandJournal",
        "operator_resolution": "LedgerOperatorResolution",
        "hint_sink": "LedgerVenueHintSink",
    }
    assert isinstance(capital.command_boundary.effects, LedgerCommandEffects)
    assert isinstance(capital.deployment_input, LedgerDeploymentInput)
    assert capital.policy_store.scope == SCOPE


@pytest.mark.asyncio
async def test_ledger_observation_sinks_are_effects_around_the_ledger_cycle(factory) -> None:
    sinks = (await _select(factory, "ledger")).capital.observation(_venue())  # type: ignore[union-attr]

    assert isinstance(sinks.boot, LedgerCycleEffects)
    assert isinstance(sinks.runtime, LedgerCycleEffects)
    # The boot holds the writer lock, so it closes every outcome-less attempt; the runtime
    # leaves the younger ones to their own submit.
    assert (BOOT_GRACE_MS, RUNTIME_GRACE_MS) == (0, 120_000)
    assert sinks.boot._inner._grace_ms == BOOT_GRACE_MS  # type: ignore[attr-defined]
    assert sinks.runtime._inner._grace_ms == RUNTIME_GRACE_MS  # type: ignore[attr-defined]
    assert type(sinks.boot._inner).__name__ == "_LedgerObservationCycle"  # type: ignore[attr-defined]
    # One alert per foreign offer and per aged UNKNOWN across both.
    assert sinks.boot._foreign_exposure is sinks.runtime._foreign_exposure  # type: ignore[attr-defined]
    assert sinks.boot._quarantine_age is sinks.runtime._quarantine_age  # type: ignore[attr-defined]
    assert sinks.boot._cells == (("fUST", "fUST_a30"),)  # type: ignore[attr-defined]
    # A submit's transport may be sent before its outcome is journaled, but not at boot: no
    # transport is in flight then. The same constants time the attempts and the foreign offers.
    assert sinks.boot._foreign_grace_ms == BOOT_GRACE_MS == 0  # type: ignore[attr-defined]
    assert sinks.runtime._foreign_grace_ms == RUNTIME_GRACE_MS == 120_000  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_ledger_venue_hint_sink_requests_resync_through_the_channel(factory) -> None:
    resync = ResyncChannel()
    ports = await _select(factory, "ledger", resync=resync)
    sink = ports.venue_hint_sink
    assert sink._scope == SCOPE  # type: ignore[attr-defined]
    assert sink._request_resync == resync.request  # type: ignore[attr-defined]
    await sink.offer_gone("42", occurred_at_ms=1)
    assert resync.take() == "venue_hint:offer_gone:42"


@pytest.mark.asyncio
@pytest.mark.parametrize("authority", ["legacy", "ledger"])
async def test_every_selection_has_capital_ports_and_the_signature_has_no_live_switch(
    factory, authority: str,
) -> None:
    """The venue is chosen elsewhere (``apps/venue.py``): capital ports always exist, for
    either authority, and ``select_bot_ports`` takes no ``live`` argument."""
    import inspect

    assert "live" not in inspect.signature(select_bot_ports).parameters
    ports = await _select(factory, authority)
    assert ports.capital is not None


def _shape(obj: object) -> str:
    return type(obj).__name__


@pytest.mark.asyncio
async def test_legacy_selection_is_the_object_graph_it_always_was(factory) -> None:
    ports = await _select(factory, "legacy")
    capital, legacy = ports.capital, ports.legacy
    assert capital is not None and legacy is not None

    assert ports.authority == "legacy"
    assert {
        "uncertainty_reader": _shape(ports.uncertainty_reader),
        "managed_offers": _shape(ports.managed_offers),
        "capital_authority": _shape(capital.capital_authority),
        "scope_lock": _shape(capital.scope_lock),
        "policy_store": _shape(capital.policy_store),
        "journal": _shape(capital.command_boundary.journal),
        "effects": _shape(capital.command_boundary.effects),
        "operator_resolution": _shape(capital.operator_resolution),
        "deployment_input": _shape(capital.deployment_input),
        "persister": _shape(legacy.persister),
        "paper_ledger": _shape(legacy.paper_ledger),
        "offer_registry": _shape(legacy.offer_registry),
    } == {
        "uncertainty_reader": "LegacyUncertaintyReader",
        "managed_offers": "LegacyManagedOffers",
        "capital_authority": "LegacyCapitalAuthority",
        "scope_lock": "LegacyScopeLock",
        "policy_store": "LegacyPolicyStore",
        "journal": "LegacyCommandJournal",
        "effects": "LegacyCommandEffects",
        "operator_resolution": "LegacyOperatorResolution",
        "deployment_input": "LegacyDeploymentInput",
        "persister": "EventStorePersister",
        "paper_ledger": "PaperPositionLedger",
        "offer_registry": "OfferRegistry",
    }
    assert isinstance(capital.command_boundary, CommandBoundary)
    assert isinstance(capital.operator_resolution, LegacyOperatorResolution)
    assert isinstance(capital.deployment_input, LegacyDeploymentInput)
    assert (capital.command_boundary.scope, capital.policy_store.scope) == (SCOPE, SCOPE)

    # One repository/runtime behind the authority, the lock, the policy store and the journal.
    runtime = capital.capital_authority._runtime  # type: ignore[attr-defined]
    repository = runtime.repository
    assert capital.scope_lock._repository is repository  # type: ignore[attr-defined]
    assert capital.policy_store._repository is repository  # type: ignore[attr-defined]
    assert capital.command_boundary.journal._runtime is runtime  # type: ignore[attr-defined]
    assert (repository.account_id, repository.environment, repository.max_snapshot_age_ms) == (
        ACCOUNT, "ci", 10_000)
    effects = capital.command_boundary.effects
    assert effects._persister is legacy.persister  # type: ignore[attr-defined]
    assert effects._uncertainty_handler == legacy.paper_ledger.on_reservation_unknown  # type: ignore[attr-defined]

    # Hint sink: the registry, the persister and the bus; the resync request is ignored.
    sink = ports.venue_hint_sink
    assert _shape(sink) == "LegacyVenueHintSink"
    assert (sink._registry is legacy.offer_registry  # type: ignore[attr-defined]
            and sink._persister is legacy.persister  # type: ignore[attr-defined]
            and sink._account_id == str(ACCOUNT))  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_legacy_observation_sinks_are_the_boot_recovery_pair(factory) -> None:
    ports = await _select(factory, "legacy")
    legacy = ports.legacy
    assert legacy is not None
    sinks = ports.capital.observation(_venue())  # type: ignore[union-attr]
    for sink in (sinks.boot, sinks.runtime):
        assert isinstance(sink, LegacyObservationSink)
        assert not isinstance(sink, LedgerCycleEffects)
    boot, runtime = sinks.boot._recovery, sinks.runtime._recovery  # type: ignore[attr-defined]
    assert (boot._action_grace_ms, runtime._action_grace_ms) == (0, 120_000)
    for recovery in (boot, runtime):
        assert recovery._offer_registry is legacy.offer_registry
        assert recovery._capital_repository is not None
        assert recovery._symbols == ["fUST"]
        assert recovery._env == "ci"
    assert boot._foreign_exposure is runtime._foreign_exposure
    assert boot._store is runtime._store


@pytest.mark.asyncio
async def test_legacy_selection_subscribes_the_projection_then_the_registry_per_event_type(
    factory,
) -> None:
    bus = DomainEventBus()
    ports = await _select(factory, "legacy", bus=bus)
    legacy = ports.legacy
    assert legacy is not None
    ledger, registry = legacy.paper_ledger, legacy.offer_registry
    assert bus._handlers == {
        ReservationClaimed: [ledger.on_reservation_claimed, registry.handle],
        OrderFilled: [ledger.on_order_filled, registry.handle],
        ReservationReleased: [ledger.on_reservation_released, registry.handle],
        PositionReconciled: [ledger.on_position_reconciled],
    }


@pytest.mark.asyncio
async def test_a_ledger_selection_subscribes_nothing(factory) -> None:
    bus = DomainEventBus()
    await _select(factory, "ledger", bus=bus)
    assert bus._handlers == {}


@pytest.mark.asyncio
async def test_the_uncertainty_cache_hook_converges_the_legacy_projection_only(factory) -> None:
    legacy = await _select(factory, "legacy")
    ledger = await _select(factory, "ledger")
    assert ledger.capital.uncertainty_synced is None  # type: ignore[union-attr]
    synced = legacy.capital.uncertainty_synced  # type: ignore[union-attr]
    assert synced is not None
    projection = legacy.legacy.paper_ledger  # type: ignore[union-attr]
    projection._uncertain["fUST"] = Decimal("5")
    synced("fUST")
    assert projection.uncertain_exposure("fUST") == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("authority", ["legacy", "ledger"])
async def test_policy_ports_are_the_authoritys_and_the_ledger_never_replays(
    factory, monkeypatch, authority,
) -> None:
    from bfx_funding_bot.modules.execution.event_store.writer import AccountEventWriter
    from bfx_funding_bot.modules.trading import CapitalPolicy

    replays: list[object] = []

    async def prepare(self, session, *, account_id):
        replays.append(account_id)

    monkeypatch.setattr(AccountEventWriter, "prepare_locked", prepare)
    policy = select_policy_ports(authority, SCOPE, max_snapshot_age_ms=10_000)
    assert type(policy.store).__name__ == (
        "LegacyPolicyStore" if authority == "legacy" else "LedgerPolicyStore")
    assert type(policy.scope_lock).__name__ == (
        "LegacyScopeLock" if authority == "legacy" else "LedgerScopeLock")
    async with factory.begin() as session:
        await policy.scope_lock.lock(session, SCOPE)
        written = await policy.store.apply_policy(
            session, symbol="fUST", policy=CapitalPolicy(enabled=True), expected_revision=0,
            source={})
        assert (await policy.store.read_applied(session, symbol="fUST")).revision == written.revision
    assert bool(replays) == (authority == "legacy")


@pytest.mark.asyncio
@pytest.mark.parametrize("authority", ["legacy", "ledger"])
async def test_both_policy_stores_write_through_the_one_writer(
    factory, monkeypatch, authority,
) -> None:
    from bfx_funding_bot.modules.execution import capital_repository
    from bfx_funding_bot.modules.execution.event_store.writer import AccountEventWriter
    from bfx_funding_bot.modules.ledger import policy_write
    from bfx_funding_bot.modules.ledger._internal import policy_store
    from bfx_funding_bot.modules.trading import CapitalPolicy

    async def no_replay(self, session, *, account_id):
        return None

    monkeypatch.setattr(AccountEventWriter, "prepare_locked", no_replay)
    calls: list[str] = []

    def spy(module):
        async def write(*args, **kwargs):
            calls.append(module.__name__)
            return await policy_write.write_policy_revision(*args, **kwargs)
        monkeypatch.setattr(module, "write_policy_revision", write)

    spy(capital_repository)
    spy(policy_store)
    policy = select_policy_ports(authority, SCOPE, max_snapshot_age_ms=10_000)
    async with factory.begin() as session:
        await policy.store.apply_policy(
            session, symbol="fUST", policy=CapitalPolicy(enabled=True), expected_revision=0,
            source={})
    assert calls == [
        capital_repository.__name__ if authority == "legacy" else policy_store.__name__]
