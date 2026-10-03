"""Selection of the bot process's capital ports by capital authority (``apps/bot_ports.py``).

Mutation checks (one at a time; revert after each):

* the ledger selection returns any ``Legacy*`` adapter or event-sourced state holder:
  ``test_ledger_selection_builds_no_legacy_object``.
* ``PaperPositionLedger`` / ``OfferRegistry`` built under the ledger:
  ``test_ledger_selection_builds_no_legacy_object`` (``legacy`` must be ``None``).
* the boot sink's grace is 120 000, or the runtime's is not: ``test_ledger_observation_sinks_*``.
* the effects wrap the legacy sink, or the ledger sink is left unwrapped:
  ``test_ledger_observation_sinks_*``, ``test_legacy_observation_sinks_*``.
* the ledger hint sink is built per consumer: ``test_ledger_venue_hint_sink_*``.
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
    BOOT_GRACE_MS,
    RUNTIME_GRACE_MS,
    BotPorts,
    ObservationVenue,
    select_bot_ports,
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
from bfx_funding_bot.modules.execution.ledger_cycle_effects import LedgerCycleEffects
from bfx_funding_bot.modules.execution.observation_sink import LegacyObservationSink
from bfx_funding_bot.modules.execution.protocols import AccountContext, Credentials
from bfx_funding_bot.modules.execution.safety.protection import AutomaticProtection
from bfx_funding_bot.modules.execution.uncertainty_resolution import LegacyOperatorResolution
from bfx_funding_bot.modules.ledger import Scope
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


async def _select(factory, authority: str, *, live: bool = True) -> BotPorts:
    return await select_bot_ports(
        authority, session_factory=factory, scope=SCOPE, account_id=str(ACCOUNT),
        bus=DomainEventBus(), live=live, clock=lambda: 1_000, max_snapshot_age_ms=10_000,
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
    hint_sink = ports.venue_hint_sink(lambda reason: None)

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
        "hint_sink": type(ports.venue_hint_sink(lambda reason: None)).__name__,
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


@pytest.mark.asyncio
async def test_ledger_venue_hint_sink_needs_the_reconcile_request(factory) -> None:
    ports = await _select(factory, "ledger")
    with pytest.raises(ValueError, match="resync request"):
        ports.venue_hint_sink(None)
    requested: list[str] = []
    sink = ports.venue_hint_sink(requested.append)
    assert sink._scope == SCOPE  # type: ignore[attr-defined]
    assert sink._request_resync == requested.append  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_the_ledger_has_no_simulated_composition(factory) -> None:
    with pytest.raises(ValueError, match="no simulated composition"):
        await _select(factory, "ledger", live=False)


@pytest.mark.asyncio
async def test_legacy_paper_selection_has_no_capital_ports(factory) -> None:
    ports = await _select(factory, "legacy", live=False)
    assert ports.capital is None
    assert type(ports.uncertainty_reader).__name__ == "LegacyUncertaintyReader"
    assert type(ports.managed_offers).__name__ == "LegacyManagedOffers"
    assert ports.legacy is not None


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
    sink = ports.venue_hint_sink(None)
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
