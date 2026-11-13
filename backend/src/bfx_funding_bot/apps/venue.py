"""Venue composition: the one place that decides what differs between the venues.

``build_venue`` answers for both venues with the same shape (``VenueWiring``): where the
credentials come from and the credentials themselves, the HTTP client the authenticated REST
client and the executor use, the one per-process ``AuthRequestGate``, what the venue has
(``VenueCapabilities``: consumers check these, never a venue name), the venue's own tasks and
how to close it.

* ``bitfinex``: credentials from the account's vault, the process's real HTTP client, the
  authenticated WebSocket required, no extra task.
* ``simulated``: an event-sourced venue on this process's own database (its log is
  ``sim_venue_event``), reached through ``httpx.AsyncClient(transport=<venue>)``. No auth
  request can leave the process. Credentials are generated for this boot, the vault is never
  opened and a vault key in the environment refuses the boot. This module is the only
  importer of ``simulated_venue.wiring``.

The simulated venue exists for tests: its market feed (and any ``FaultPlan``) come only
through ``VenueSeam``, and a simulated boot without one is refused (ADR
2026-11-08-retire-the-simulation-soak).
"""
from __future__ import annotations

import contextlib
import logging
import secrets
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from uuid import UUID

import httpx
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from bfx_funding_bot.apps.runtime.config import MarketfeedConfig
from bfx_funding_bot.core.crypto import VaultNotConfiguredError, load_kek
from bfx_funding_bot.core.errors import ConfigurationError
from bfx_funding_bot.core.secret_files import secret_present
from bfx_funding_bot.core.venue import Venue, VenueCapabilities
from bfx_funding_bot.external.bitfinex.nonce import AuthRequestGate
from bfx_funding_bot.modules.credentials.vault import (
    AccountCredentialNotConfiguredError,
    VaultKeyMismatchError,
    load_account_credentials,
)
from bfx_funding_bot.modules.execution.protocols import Credentials
from bfx_funding_bot.modules.observability.metrics import DaemonMetrics, attach_httpx_metrics
from bfx_funding_bot.modules.simulated_venue import (
    FaultPlan,
    MarketFeed,
    SimAccount,
    SimulatedVenue,
    SimulatedVenueConfig,
    SqlVenueEventStore,
)
from bfx_funding_bot.modules.simulated_venue.wiring import build_simulated_venue
from bfx_funding_bot.modules.strategy import configured_symbols

log = logging.getLogger(__name__)

BITFINEX_CAPABILITIES = VenueCapabilities(auth_ws="required")
SIMULATED_CAPABILITIES = VenueCapabilities(auth_ws="forbidden")


@dataclass(frozen=True, slots=True)
class VenueSeam:
    """What a simulated boot runs on (tests only), and the only way to select that venue:
    the venue's market feed, optional venue faults, the wallets funded on an empty venue log
    (``initial_wallets``, currency -> amount), ``on_built`` to hand the built venue to the
    test (its state, unexpected requests and internal failures), and optionally
    ``client_credentials`` for the bot to sign with instead of the keys the simulated venue
    accepts (the venue then refuses every signed request, as Bitfinex refuses a revoked
    key)."""

    feed: MarketFeed
    faults: FaultPlan | None = None
    initial_wallets: Mapping[str, Decimal] = field(default_factory=dict)
    on_built: Callable[[SimulatedVenue], None] | None = None
    client_credentials: Credentials | None = field(default=None, repr=False)


@dataclass(slots=True)
class VenueWiring:
    venue: Venue
    # Venue credentials: from the vault (Bitfinex) or generated for this boot (simulated).
    credentials: Credentials
    # What BitfinexAuthREST and the executor (cancel-all included) are built on.
    auth_http: httpx.AsyncClient
    # One nonce gate per process: Bitfinex nonces are per key across REST and WS.
    auth_gate: AuthRequestGate
    capabilities: VenueCapabilities
    aclose: Callable[[], Awaitable[None]]
    # The credentials came from the account vault, so an operator can rotate them
    # (``execution.credential_rotation``); the simulated venue's are throwaway.
    credential_vault: bool = False


async def _noop() -> None:
    return None


async def _vault_credentials(
    session: AsyncSession, exchange_account_id: UUID,
) -> Credentials:
    try:
        return await load_account_credentials(
            session, exchange_account_id=exchange_account_id, kek=load_kek())
    except AccountCredentialNotConfiguredError as exc:
        raise ConfigurationError(str(exc)) from exc
    except VaultKeyMismatchError as exc:
        raise ConfigurationError(
            f"exchange account {exchange_account_id} credential vault cannot be opened"
        ) from exc
    except VaultNotConfiguredError as exc:
        raise ConfigurationError("BFX_VAULT_KEK is required for daemon boot") from exc
    except ValueError as exc:
        # Crypto and config errors must not leak an implementation-specific traceback
        # through the boot contract.
        raise ConfigurationError(str(exc)) from exc


def _throwaway_credentials() -> Credentials:
    if secret_present("BFX_VAULT_KEK"):
        # In the env (even empty) or mounted as a secret file: either way it is the wrong
        # env file or compose file for a simulation.
        raise ConfigurationError(
            "BFX_VAULT_KEK must not be set or mounted for a simulated venue boot: the "
            "simulation never opens the credential vault"
        )
    return Credentials(secrets.token_hex(16), secrets.token_hex(32))


async def build_venue(
    config: MarketfeedConfig,
    *,
    exchange_account_id: UUID,
    session_factory: async_sessionmaker[AsyncSession],
    db_engine: AsyncEngine,
    bitfinex_http: httpx.AsyncClient,
    clock: Callable[[], int],
    metrics: DaemonMetrics,
    seam: VenueSeam | None = None,
) -> VenueWiring:
    # The seam alone picks the venue; ``load_config`` already refused a phase that disagrees
    # with it (shadow without a seam, a seam without shadow), before any database work.
    gate = AuthRequestGate()
    if seam is None:
        async with session_factory() as session:
            credentials = await _vault_credentials(session, exchange_account_id)
        return VenueWiring(
            venue="bitfinex", credentials=credentials, auth_http=bitfinex_http,
            auth_gate=gate, capabilities=BITFINEX_CAPABILITIES, aclose=_noop,
            credential_vault=True)
    return await _build_simulated(
        config, exchange_account_id=exchange_account_id, db_engine=db_engine,
        clock=clock, metrics=metrics, gate=gate, seam=seam)


async def _build_simulated(
    config: MarketfeedConfig, *, exchange_account_id: UUID, db_engine: AsyncEngine,
    clock: Callable[[], int], metrics: DaemonMetrics, gate: AuthRequestGate, seam: VenueSeam,
) -> VenueWiring:
    credentials = _throwaway_credentials()
    symbols = tuple(sorted(configured_symbols(config.cells)))
    account = SimAccount(str(exchange_account_id))
    # The store refuses a database not on the ledger epoch, an unstamped or prod-stamped
    # one, or one without sim_venue_event (its own reads); load_config already refused
    # the simulated venue outside the ci realm (require_simulated_realm).
    store = await SqlVenueEventStore.open(db_engine)
    venue = await build_simulated_venue(
        account=account,
        config=SimulatedVenueConfig(
            api_key=credentials.api_key, api_secret=credentials.api_secret, symbols=symbols),
        store=store, feed=seam.feed, clock_ms=clock, faults=seam.faults,
    )
    funded = await venue.fund_wallets_if_empty(dict(seam.initial_wallets))
    if funded:
        log.info("simulated_venue_funded wallets=%s", sorted(seam.initial_wallets))
    if seam.on_built is not None:
        seam.on_built(venue)
    client = venue.client()
    attach_httpx_metrics(client, metrics)
    return VenueWiring(
        venue="simulated",
        credentials=seam.client_credentials if seam.client_credentials is not None
        else credentials,
        auth_http=client, auth_gate=gate,
        capabilities=SIMULATED_CAPABILITIES, aclose=_closer(client),
    )


def _closer(client: httpx.AsyncClient) -> Callable[[], Awaitable[None]]:
    async def aclose() -> None:
        with contextlib.suppress(Exception):
            await client.aclose()

    return aclose
