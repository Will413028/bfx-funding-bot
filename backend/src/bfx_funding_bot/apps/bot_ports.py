"""Build the bot process's capital ports: the ledger's, the only capital authority.

The one place a consumer's port is bound (``apps/read_models.py`` does the same for the
web API). No event store, persister, paper-position projection, offer registry or capital
runtime exists in the bot: the ledger's own tables are its single record and the bus only
carries notifications after a transaction committed.

Anything that needs the venue connection or the safety state (``auth_rest``, the
protection) is built after the ports are chosen, so those come in as factories:
``CapitalPorts.observation``.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bfx_funding_bot.external.bitfinex.auth_rest import BitfinexAuthREST
from bfx_funding_bot.modules.execution.bus import DomainEventBus
from bfx_funding_bot.modules.execution.command_boundary import (
    CommandBoundary,
    LedgerCommandEffects,
)
from bfx_funding_bot.modules.execution.deployment_input import (
    DeploymentInput,
    LedgerDeploymentInput,
)
from bfx_funding_bot.modules.execution.ledger_cycle_effects import LedgerCycleEffects
from bfx_funding_bot.modules.execution.protocols import AccountContext
from bfx_funding_bot.modules.execution.reconcile_monitors import (
    ForeignExposureMonitor,
    QuarantineAgeMonitor,
)
from bfx_funding_bot.modules.execution.resync_channel import ResyncChannel
from bfx_funding_bot.modules.execution.safety.protection import AutomaticProtection
from bfx_funding_bot.modules.execution.venue_observation import BitfinexVenueObservation
from bfx_funding_bot.modules.ledger import (
    BOOT_GRACE_MS,
    RUNTIME_GRACE_MS,
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
class PolicyPorts:
    """The applied-policy store and the lock its callers hold across a read and the write."""

    store: PolicyStore
    scope_lock: ScopeLock


def select_policy_ports(scope: Scope) -> PolicyPorts:
    """The policy ports of ``scope``; the bot and the owner's scripts share them."""
    return PolicyPorts(build_policy_store(scope), build_scope_lock())


@dataclass(frozen=True, slots=True)
class CapitalPorts:
    """What a process that lends real capital binds to the ledger."""

    capital_authority: CapitalAuthority
    scope_lock: ScopeLock
    policy_store: PolicyStore
    command_boundary: CommandBoundary
    operator_resolution: OperatorResolution
    deployment_input: DeploymentInput
    observation: Callable[[ObservationVenue], ObservationSinks]


@dataclass(frozen=True, slots=True)
class BotPorts:
    uncertainty_reader: UncertaintyReader
    managed_offers: ManagedOfferReader
    # The sink for venue hints (WS and REST polling). A ledger sink debounces per instance,
    # so there is one, shared by every producer.
    venue_hint_sink: VenueHintSink
    capital: CapitalPorts


def select_bot_ports(
    *,
    session_factory: async_sessionmaker[AsyncSession],
    scope: Scope,
    account_id: str,
    bus: DomainEventBus,
    resync: ResyncChannel,
    clock: Callable[[], int],
    max_snapshot_age_ms: int,
) -> BotPorts:
    """The bot boots only on the ``ledger`` authority (``apps/authority_support.py``)."""
    capital_authority = build_capital_authority(
        session_factory, max_snapshot_age_ms=max_snapshot_age_ms,
    )
    policy = select_policy_ports(scope)

    def observation(venue: ObservationVenue) -> ObservationSinks:
        observed = BitfinexVenueObservation(
            rest=venue.auth_rest, ctx=venue.account_ctx, scope=scope, clock_ms=clock,
        )
        # One alert per foreign offer and per aged UNKNOWN across the boot and the runtime.
        foreign_exposure = ForeignExposureMonitor()
        quarantine_age = QuarantineAgeMonitor()

        def sink(grace_ms: int) -> ObservationSink:
            # One time constant per cycle: how long an attempt or an unexplained offer may
            # still be in flight.
            return LedgerCycleEffects(
                build_observation_sink(session_factory, observed, now_ms=clock, grace_ms=grace_ms),
                scope=scope, account_id=account_id, session_factory=session_factory,
                capital=capital_authority, cells=venue.cells, protection=venue.protection,
                bus=bus, reads=build_ledger_cycle_reads(),
                conservation=build_ledger_conservation_reader(),
                operator_reads=build_operator_reads(), foreign_exposure=foreign_exposure,
                quarantine_age=quarantine_age, foreign_grace_ms=grace_ms, clock=clock,
            )

        return ObservationSinks(boot=sink(BOOT_GRACE_MS), runtime=sink(RUNTIME_GRACE_MS))

    return BotPorts(
        uncertainty_reader=build_uncertainty_reader(session_factory),
        managed_offers=build_managed_offer_reader(),
        venue_hint_sink=build_venue_hint_sink(scope=scope, request_resync=resync.request, bus=bus),
        capital=CapitalPorts(
            capital_authority=capital_authority,
            scope_lock=policy.scope_lock,
            policy_store=policy.store,
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
    )

