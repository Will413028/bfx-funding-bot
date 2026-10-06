"""The bot process's capital ports (``apps/bot_ports.py``): the ledger's, and nothing else.

Mutation checks (one at a time; revert after each):

* the selection returns any ``Legacy*`` adapter or event-sourced state holder:
  ``test_the_selection_builds_no_legacy_object``.
* the boot sink's grace is 120 000, or the runtime's is not: ``test_observation_sinks_*``.
* the ledger sink is left unwrapped by the cycle effects: ``test_observation_sinks_*``.
* the hint sink is not the channel's: ``test_venue_hint_sink_*``.
* the boot foreign-offer grace is 120 000 (or the runtime's is not): ``test_observation_sinks_*``.
* the policy ports replay the event stream or write around the one writer: ``test_policy_ports_*``.
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
from bfx_funding_bot.modules.execution.command_boundary import LedgerCommandEffects
from bfx_funding_bot.modules.execution.deployment_input import LedgerDeploymentInput
from bfx_funding_bot.modules.execution.ledger_cycle_effects import LedgerCycleEffects
from bfx_funding_bot.modules.execution.protocols import AccountContext, Credentials
from bfx_funding_bot.modules.execution.resync_channel import ResyncChannel
from bfx_funding_bot.modules.execution.safety.protection import AutomaticProtection
from bfx_funding_bot.modules.ledger import BOOT_GRACE_MS, RUNTIME_GRACE_MS, Scope
from tests.apps.walk import legacy_state

ACCOUNT = UUID("550e8400-e29b-41d4-a716-446655440000")
SCOPE = Scope(ACCOUNT, "ci")


@pytest_asyncio.fixture
async def factory(tmp_path):
    # The ledger journals' foreign keys name the shared decision audit and request tables.
    import bfx_funding_bot.modules.execution.audit.tables
    import bfx_funding_bot.modules.execution.uncertainty_tables  # noqa: F401
    engine = make_async_engine_from_url(f"sqlite+aiosqlite:///{tmp_path / 'ports.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


def _select(factory, *, bus=None, resync=None) -> BotPorts:
    return select_bot_ports(
        session_factory=factory, scope=SCOPE, account_id=str(ACCOUNT),
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
async def test_the_selection_builds_no_legacy_object(factory) -> None:
    ports = _select(factory)
    sinks = ports.capital.observation(_venue())
    hint_sink = ports.venue_hint_sink

    assert legacy_state(ports, "ports") == []
    assert legacy_state(sinks, "sinks") == []
    assert legacy_state(hint_sink, "hint_sink") == []


@pytest.mark.asyncio
async def test_the_selection_names_the_ledger_adapters(factory) -> None:
    ports = _select(factory)
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
async def test_observation_sinks_are_effects_around_the_ledger_cycle(factory) -> None:
    sinks = _select(factory).capital.observation(_venue())

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
async def test_venue_hint_sink_requests_resync_through_the_channel(factory) -> None:
    resync = ResyncChannel()
    ports = _select(factory, resync=resync)
    sink = ports.venue_hint_sink
    assert sink._scope == SCOPE  # type: ignore[attr-defined]
    assert sink._request_resync == resync.request  # type: ignore[attr-defined]
    await sink.offer_gone("42", occurred_at_ms=1)
    assert resync.take() == "venue_hint:offer_gone:42"


def test_the_signature_names_no_authority_and_no_live_switch() -> None:
    """The venue is chosen elsewhere (``apps/venue.py``) and the ledger is the only
    authority: ``select_bot_ports`` takes neither an ``authority`` nor a ``live`` argument."""
    import inspect

    parameters = inspect.signature(select_bot_ports).parameters
    assert "live" not in parameters and "authority" not in parameters


@pytest.mark.asyncio
async def test_the_selection_subscribes_nothing(factory) -> None:
    bus = DomainEventBus()
    _select(factory, bus=bus)
    assert bus._handlers == {}


@pytest.mark.asyncio
async def test_policy_ports_are_the_ledgers(factory) -> None:
    from bfx_funding_bot.modules.trading import CapitalPolicy

    policy = select_policy_ports(SCOPE)
    assert type(policy.store).__name__ == "LedgerPolicyStore"
    assert type(policy.scope_lock).__name__ == "LedgerScopeLock"
    async with factory.begin() as session:
        await policy.scope_lock.lock(session, SCOPE)
        written = await policy.store.apply_policy(
            session, symbol="fUST", policy=CapitalPolicy(enabled=True), expected_revision=0,
            source={})
        assert (await policy.store.read_applied(session, symbol="fUST")).revision == written.revision


@pytest.mark.asyncio
async def test_policy_ports_write_through_the_one_writer(factory, monkeypatch) -> None:
    from bfx_funding_bot.modules.ledger import policy_write
    from bfx_funding_bot.modules.ledger._internal import policy_store
    from bfx_funding_bot.modules.trading import CapitalPolicy

    calls: list[str] = []

    async def write(*args, **kwargs):
        calls.append(policy_store.__name__)
        return await policy_write.write_policy_revision(*args, **kwargs)

    monkeypatch.setattr(policy_store, "write_policy_revision", write)
    policy = select_policy_ports(SCOPE)
    async with factory.begin() as session:
        await policy.store.apply_policy(
            session, symbol="fUST", policy=CapitalPolicy(enabled=True), expected_revision=0,
            source={})
    assert calls == [policy_store.__name__]
