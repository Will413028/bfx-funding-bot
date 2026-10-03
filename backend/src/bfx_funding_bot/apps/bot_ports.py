"""Pick the bot process's capital ports from the capital authority it booted under.

The one place a consumer's port is bound to an authority (``apps/read_models.py`` does the
same for the web API). The legacy branch builds today's objects, in today's wiring; the
ledger branch builds none of them: no event store, persister, paper-position projection,
offer registry or capital runtime exists in a ledger-authority process, because the
ledger's own tables are its single record and the bus only carries notifications after a
transaction committed.

Anything that needs the venue connection or the safety state (``auth_rest``, the
protection) is built after the ports are chosen, so those come in as factories:
``CapitalPorts.observation`` and ``BotPorts.venue_hint_sink``.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bfx_funding_bot.core.authority import Authority
from bfx_funding_bot.external.bitfinex.auth_rest import BitfinexAuthREST
from bfx_funding_bot.modules.execution.boot_recovery import (
    BootRecovery,
    ForeignExposureMonitor,
    QuarantineAgeMonitor,
)
from bfx_funding_bot.modules.execution.bus import DomainEventBus
from bfx_funding_bot.modules.execution.capital_repository import CapitalRepository
from bfx_funding_bot.modules.execution.capital_runtime import CapitalRuntime
from bfx_funding_bot.modules.execution.command_boundary import (
    CommandBoundary,
    LedgerCommandEffects,
)
from bfx_funding_bot.modules.execution.deployment_input import (
    DeploymentInput,
    LedgerDeploymentInput,
    LegacyDeploymentInput,
)
from bfx_funding_bot.modules.execution.event_store.persister import EventStorePersister
from bfx_funding_bot.modules.execution.event_store.store import PostgresEventStore
from bfx_funding_bot.modules.execution.ledger import PaperPositionLedger
from bfx_funding_bot.modules.execution.ledger_cycle_effects import LedgerCycleEffects
from bfx_funding_bot.modules.execution.legacy_command_effects import LegacyCommandEffects
from bfx_funding_bot.modules.execution.legacy_command_journal import LegacyCommandJournal
from bfx_funding_bot.modules.execution.legacy_ports import (
    LegacyCapitalAuthority,
    LegacyManagedOffers,
    LegacyPolicyStore,
    LegacyScopeLock,
    LegacyUncertaintyReader,
)
from bfx_funding_bot.modules.execution.legacy_venue_hints import LegacyVenueHintSink
from bfx_funding_bot.modules.execution.observation_sink import LegacyObservationSink
from bfx_funding_bot.modules.execution.protocols import AccountContext
from bfx_funding_bot.modules.execution.registry_offers import OfferRegistry
from bfx_funding_bot.modules.execution.safety.protection import AutomaticProtection
from bfx_funding_bot.modules.execution.uncertainty_resolution import LegacyOperatorResolution
from bfx_funding_bot.modules.execution.venue_observation import BitfinexVenueObservation
from bfx_funding_bot.modules.ledger import (
    CapitalAuthority,
    ManagedOfferReader,
    ObservationSink,
    OperatorResolution,
    PolicyStore,
    Scope,
    ScopeLock,
    UncertaintyReader,
    VenueHintSink,
)
from bfx_funding_bot.modules.ledger.wiring import (
    build_capital_authority,
    build_command_journal,
    build_ledger_conservation_reader,
    build_ledger_cycle_reads,
    build_ledger_managed_offers,
    build_managed_offer_reader,
    build_observation_sink,
    build_operator_reads,
    build_operator_resolution,
    build_policy_store,
    build_scope_lock,
    build_uncertainty_reader,
    build_venue_hint_sink,
)

# The boot closes every outcome-less attempt it finds (it holds the writer lock, so none can
# be in flight); the runtime cycle leaves the ones younger than this to their own submit.
BOOT_GRACE_MS = 0
RUNTIME_GRACE_MS = 120_000
# How long a live venue offer without provenance may sit before it is called foreign.
FOREIGN_GRACE_MS = 120_000

type ResyncRequest = Callable[[str], None]


@dataclass(frozen=True, slots=True)
class ObservationVenue:
    """What the observation sinks need that exists only once the venue client does."""

    auth_rest: BitfinexAuthREST
    account_ctx: AccountContext
    protection: AutomaticProtection
    symbols: list[str]
    cells: Sequence[tuple[str, str]]  # (symbol, cell id) of every configured cell


@dataclass(frozen=True, slots=True)
class ObservationSinks:
    boot: ObservationSink
    runtime: ObservationSink


@dataclass(frozen=True, slots=True)
class CapitalPorts:
    """What a process that lends real capital binds to its authority, all or none."""

    capital_authority: CapitalAuthority
    scope_lock: ScopeLock
    policy_store: PolicyStore
    command_boundary: CommandBoundary
    operator_resolution: OperatorResolution
    deployment_input: DeploymentInput
    observation: Callable[[ObservationVenue], ObservationSinks]


@dataclass(frozen=True, slots=True)
class LegacyExtras:
    """The event-sourced authority's own state; a ledger-authority process has none of it."""

    persister: EventStorePersister
    paper_ledger: PaperPositionLedger
    offer_registry: OfferRegistry


@dataclass(frozen=True, slots=True)
class BotPorts:
    authority: Authority
    uncertainty_reader: UncertaintyReader
    managed_offers: ManagedOfferReader
    # The sink for venue hints (WS and REST polling). Built once the resync request exists;
    # a ledger sink debounces per instance, so the caller builds one and shares it.
    venue_hint_sink: Callable[[ResyncRequest | None], VenueHintSink]
    # None where nothing lends: paper and shadow.
    capital: CapitalPorts | None
    legacy: LegacyExtras | None


async def select_bot_ports(
    authority: Authority,
    *,
    session_factory: async_sessionmaker[AsyncSession],
    scope: Scope,
    account_id: str,
    bus: DomainEventBus,
    live: bool,
    clock: Callable[[], int],
    max_snapshot_age_ms: int,
) -> BotPorts:
    """Both authorities are covered; ``SUPPORTED_AUTHORITIES`` decides which may boot."""
    if authority == "ledger":
        if not live:
            raise ValueError("the ledger authority has no simulated composition")
        return _ledger_ports(
            session_factory, scope, account_id, bus, clock, max_snapshot_age_ms,
        )
    return await _legacy_ports(
        session_factory, scope, account_id, bus, live, clock, max_snapshot_age_ms,
    )


def _ledger_ports(
    session_factory: async_sessionmaker[AsyncSession], scope: Scope, account_id: str,
    bus: DomainEventBus, clock: Callable[[], int], max_snapshot_age_ms: int,
) -> BotPorts:
    capital_authority = build_capital_authority(
        session_factory, max_snapshot_age_ms=max_snapshot_age_ms,
    )

    def observation(venue: ObservationVenue) -> ObservationSinks:
        observed = BitfinexVenueObservation(
            rest=venue.auth_rest, ctx=venue.account_ctx, scope=scope,
        )
        # One alert per foreign offer and per aged UNKNOWN across the boot and the runtime.
        foreign_exposure = ForeignExposureMonitor()
        quarantine_age = QuarantineAgeMonitor()

        def sink(grace_ms: int) -> ObservationSink:
            return LedgerCycleEffects(
                build_observation_sink(session_factory, observed, now_ms=clock, grace_ms=grace_ms),
                scope=scope, account_id=account_id, session_factory=session_factory,
                capital=capital_authority, cells=venue.cells, protection=venue.protection,
                bus=bus, reads=build_ledger_cycle_reads(),
                conservation=build_ledger_conservation_reader(),
                operator_reads=build_operator_reads(), foreign_exposure=foreign_exposure,
                quarantine_age=quarantine_age, foreign_grace_ms=FOREIGN_GRACE_MS, clock=clock,
            )

        return ObservationSinks(boot=sink(BOOT_GRACE_MS), runtime=sink(RUNTIME_GRACE_MS))

    def venue_hint_sink(request_resync: ResyncRequest | None) -> VenueHintSink:
        if request_resync is None:
            raise ValueError("the ledger venue hint sink needs the reconcile's resync request")
        return build_venue_hint_sink(scope=scope, request_resync=request_resync, bus=bus)

    return BotPorts(
        authority="ledger",
        uncertainty_reader=build_uncertainty_reader(session_factory),
        managed_offers=build_managed_offer_reader(),
        venue_hint_sink=venue_hint_sink,
        capital=CapitalPorts(
            capital_authority=capital_authority,
            scope_lock=build_scope_lock(),
            policy_store=build_policy_store(scope),
            command_boundary=CommandBoundary(
                scope, session_factory,
                build_command_journal(session_factory, max_snapshot_age_ms=max_snapshot_age_ms),
                LedgerCommandEffects(bus),
            ),
            operator_resolution=build_operator_resolution(),
            deployment_input=LedgerDeploymentInput(
                session_factory=session_factory, account_id=scope.exchange_account_id,
                environment=scope.deployment_environment, offers=build_ledger_managed_offers(),
            ),
            observation=observation,
        ),
        legacy=None,
    )


async def _legacy_ports(
    session_factory: async_sessionmaker[AsyncSession], scope: Scope, account_id: str,
    bus: DomainEventBus, live: bool, clock: Callable[[], int], max_snapshot_age_ms: int,
) -> BotPorts:
    env = scope.deployment_environment
    uncertainty_reader = LegacyUncertaintyReader(session_factory)
    # Phase 4.4c / 3a: the PG event store replaces Axiom replay at boot. from_snapshot reads
    # position_state + offer_claims (written synchronously in the command txn, A2).
    event_store = PostgresEventStore(deployment_environment=env)
    persister = EventStorePersister(store=event_store, session_factory=session_factory)
    async with session_factory() as snap_session:
        paper_ledger = await PaperPositionLedger.from_snapshot(
            snap_session, account_id=account_id, deployment_environment=env,
        )
        offer_registry = await OfferRegistry.from_snapshot(
            snap_session, account_id=account_id, deployment_environment=env,
            clock=lambda: int(time.time() * 1000),
        )

    def venue_hint_sink(request_resync: ResyncRequest | None) -> VenueHintSink:
        # The event-log authority reconciles through the registry, not through a resync.
        return LegacyVenueHintSink(
            registry=offer_registry, bus=bus, persister=persister, account_id=account_id,
        )

    capital: CapitalPorts | None = None
    if live:
        runtime = CapitalRuntime(
            repository=CapitalRepository(
                account_id=scope.exchange_account_id, environment=env,
                max_snapshot_age_ms=max_snapshot_age_ms,
            ),
            session_factory=session_factory, clock=clock,
        )

        def observation(venue: ObservationVenue) -> ObservationSinks:
            # One alert per foreign offer across the boot and the runtime reconcile.
            foreign_exposure = ForeignExposureMonitor()

            def recovery(action_grace_ms: int) -> ObservationSink:
                return LegacyObservationSink(BootRecovery(
                    store=event_store,
                    session_factory=session_factory,
                    auth_rest=venue.auth_rest,
                    account_ctx=venue.account_ctx,
                    deployment_environment=env,
                    bus=bus,
                    offer_registry=offer_registry,
                    is_simulated=False,
                    symbols=venue.symbols,
                    uncertainty_handler=paper_ledger.on_reservation_unknown,
                    capital_repository=runtime.repository,
                    protection=venue.protection,
                    foreign_exposure=foreign_exposure,
                    action_grace_ms=action_grace_ms,
                ), scope)

            return ObservationSinks(
                boot=recovery(BOOT_GRACE_MS), runtime=recovery(RUNTIME_GRACE_MS),
            )

        capital = CapitalPorts(
            capital_authority=LegacyCapitalAuthority(runtime),
            scope_lock=LegacyScopeLock(runtime.repository),
            policy_store=LegacyPolicyStore(runtime.repository),
            command_boundary=CommandBoundary(
                scope, session_factory,
                LegacyCommandJournal(
                    runtime, date_provider=lambda: datetime.now(UTC).date(),
                    clock=lambda: int(time.time() * 1000), uncertainty_reader=uncertainty_reader,
                ),
                LegacyCommandEffects(persister, bus, paper_ledger.on_reservation_unknown),
            ),
            operator_resolution=LegacyOperatorResolution(),
            deployment_input=LegacyDeploymentInput(),
            observation=observation,
        )
    return BotPorts(
        authority="legacy",
        uncertainty_reader=uncertainty_reader,
        managed_offers=LegacyManagedOffers(),
        venue_hint_sink=venue_hint_sink,
        capital=capital,
        legacy=LegacyExtras(persister, paper_ledger, offer_registry),
    )
