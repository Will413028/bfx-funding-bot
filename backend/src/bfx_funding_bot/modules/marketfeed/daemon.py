"""Daemon coordinator.

設計依據: phase 4.1 paper/shadow infra design Section "Data Flow"
- Startup: load config → warmup all cells → start ws + writer + scheduler + health_monitor
- Steady state: scheduler 觸發 signal_engine.process_candle
- Shutdown: SIGTERM → stop scheduler → drain in-flight → close ws → exit 0
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from collections.abc import Awaitable, Callable, Coroutine, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from typing import Any, Protocol
from uuid import UUID, uuid4

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
)

from bfx_funding_bot.core.errors import (
    BootInvariantError,
    ConfigurationError,
)
from bfx_funding_bot.core.health import (
    DB_FRESHNESS,
    MARKET_DATA_FRESHNESS,
    HealthProbe,
    assess_auth_ws_health,
)
from bfx_funding_bot.core.telemetry import EventType, HealthStatus, HealthTarget, Level, Phase
from bfx_funding_bot.core.writer_lock import WriterLock
from bfx_funding_bot.external.bitfinex.auth_ws import BitfinexAuthWSClient
from bfx_funding_bot.external.bitfinex.errors import credential_rejection
from bfx_funding_bot.external.bitfinex.rest import BitfinexREST
from bfx_funding_bot.external.bitfinex.ws import (
    BitfinexWSClient,
    CandleMessage,
    ChannelSpec,
    compute_backoff_secs,
)
from bfx_funding_bot.modules.accounts.config_service import load_account_config_draft
from bfx_funding_bot.modules.accounts.exchange_accounts import (
    AccountNotFound,
    AccountRetired,
    account_id_canonical,
    get_exchange_account,
)
from bfx_funding_bot.modules.accounts.tables import ExchangeAccountCredential
from bfx_funding_bot.modules.admin.trading_status import TradingStatusService
from bfx_funding_bot.modules.candles.gap_fill import fill_gap_from_rest
from bfx_funding_bot.modules.candles.tables import FundingCandleRow
from bfx_funding_bot.modules.execution.audit import AuditContext
from bfx_funding_bot.modules.execution.bus import DomainEventBus
from bfx_funding_bot.modules.execution.capital_policy_control import CapitalPolicyRequestWorker
from bfx_funding_bot.modules.execution.command_gate import AccountCommandGate
from bfx_funding_bot.modules.execution.credential_compliance import (
    CREDENTIAL_RECHECK_ALERT_AFTER,
    CREDENTIAL_RECHECK_INTERVAL_S,
    CREDENTIAL_RECHECK_RETRY_S,
    CredentialCompliance,
    CredentialNoncompliantError,
    CredentialSupersededError,
)
from bfx_funding_bot.modules.execution.credential_rotation import (
    BOT_SETTLE_WINDOW,
    CredentialRotationWorker,
)
from bfx_funding_bot.modules.execution.diagnostics.sink import DiagnosticsSink
from bfx_funding_bot.modules.execution.periodic_reconcile import PeriodicReconcile
from bfx_funding_bot.modules.execution.protocols import (
    AccountContext,
    Credentials,
    VenueExecutorPort,
)
from bfx_funding_bot.modules.execution.safety.boot_stop import report_refused_boot
from bfx_funding_bot.modules.execution.safety.chain import SafetyGuardChain
from bfx_funding_bot.modules.execution.safety.config import SafetyConfig
from bfx_funding_bot.modules.execution.safety.protection import (
    AutomaticProtection,
    WriterLockWatch,
)
from bfx_funding_bot.modules.execution.trading_control import TradingControlWorker
from bfx_funding_bot.modules.execution.uncertainty_requests import UncertaintyResolutionWorker
from bfx_funding_bot.modules.execution.ws_dispatcher import BitfinexLiveWSDispatcher
from bfx_funding_bot.modules.ledger import ObservationSink, Scope
from bfx_funding_bot.modules.live_validation.credit_history import CreditHistorySync
from bfx_funding_bot.modules.live_validation.interest_ledger import (
    InterestLedgerSync,
)
from bfx_funding_bot.modules.marketfeed.book_snapshot import BookSnapshotWriter
from bfx_funding_bot.modules.marketfeed.boot_wait import (
    BootWait,
    boot_retry_delay_s,
    is_transient_dependency_error,
)
from bfx_funding_bot.modules.marketfeed.candle_writer import CandleWriter
from bfx_funding_bot.modules.marketfeed.config import MarketfeedConfig
from bfx_funding_bot.modules.marketfeed.funding_book import FundingBookService
from bfx_funding_bot.modules.marketfeed.health_monitor import HealthMonitor
from bfx_funding_bot.modules.marketfeed.healthz import run_healthz_server
from bfx_funding_bot.modules.marketfeed.readiness import TradingReadiness
from bfx_funding_bot.modules.marketfeed.scheduler import (
    Scheduler,
    now_ms_utc,
)
from bfx_funding_bot.modules.marketfeed.signal_engine import SignalEngine
from bfx_funding_bot.modules.marketfeed.strategy_registry import StrategyRegistry
from bfx_funding_bot.modules.observability import alerts
from bfx_funding_bot.modules.observability.metrics import (
    DaemonMetrics,
)
from bfx_funding_bot.modules.observability.stdout_sink import StdoutEventSink
from bfx_funding_bot.modules.observability.tracing import (
    DaemonTracing,
)
from bfx_funding_bot.modules.strategy import CellConfig, DecisionPayload

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class _DaemonAuditContextFactory:
    account_id: str
    deployment_environment: str
    service_version: str
    config_hash: str

    def build(
        self, *, candidate: DecisionPayload, cell_id: str, reconcile_id: str,
    ) -> AuditContext:
        return AuditContext(
            account_id=self.account_id,
            deployment_environment=self.deployment_environment,
            reconcile_id=reconcile_id,
            cell_id=cell_id,
            symbol=candidate.symbol,
            signal_correlation_id=str(candidate.signal_correlation_id),
            service_version=self.service_version,
            config_hash=self.config_hash,
        )


def _require_env(name: str) -> str:
    """Return a required env value, canonicalizing the account UUID.

    Matches the pattern in `marketfeed/config.py:load_config` so missing env
    surfaces as a stable ``ConfigurationError`` via ``main()`` instead of a
    bare ``KeyError`` traceback.  The account identity is the one exception to
    the string contract: it is parsed and returned in canonical UUID form.
    """
    val = os.environ.get(name)
    if not val or not val.strip():
        raise ConfigurationError(f"{name} env var required")
    value = val.strip()
    if name == "BFX_EXCHANGE_ACCOUNT_ID":
        try:
            return account_id_canonical(value)
        except ValueError as exc:
            raise ConfigurationError(
                f"{name} must be a valid UUID"
            ) from exc
    return value


@dataclass(frozen=True, slots=True)
class AccountBootstrap:
    """Immutable daemon account identity and boot-time runtime material."""

    exchange_account_id: UUID
    deployment_environment: str
    config_draft: dict[str, object] | None
    config_revision: int | None

    @property
    def account_id(self) -> str:
        """Canonical string used by the pre-contract event APIs."""
        return account_id_canonical(self.exchange_account_id)

    def to_context(self, credentials: Credentials) -> AccountContext:
        """The account context; the credentials are the venue wiring's, not the account's."""
        return AccountContext(
            account_id=self.account_id,
            credentials=credentials,
        )

    @staticmethod
    def reject_legacy_realm(*, phase: Phase) -> None:
        """Reject the old process-global realm in live boot mode."""
        legacy = os.environ.get("BFX_ACCOUNT_ID", "").strip()
        if legacy and phase is Phase.LIVE:
            raise ConfigurationError(
                "BFX_ACCOUNT_ID is no longer supported; use "
                "BFX_EXCHANGE_ACCOUNT_ID"
            )


async def load_account_bootstrap(
    session: AsyncSession,
    *,
    deployment_environment: str,
    phase: Phase | None = None,
) -> AccountBootstrap:
    """Resolve one explicit account row and its config draft at boot.

    Credentials are not loaded here: the venue wiring owns where they come from
    (``apps/venue.py``: the vault for Bitfinex, throwaway keys for the simulated venue).

    This function is deliberately the sole UUID/env parsing seam.  All daemon
    services receive the resulting canonical account string from the returned
    object; no component can silently fall back to a process-global realm.
    """
    canonical_id = _require_env("BFX_EXCHANGE_ACCOUNT_ID")
    exchange_account_id = UUID(canonical_id)
    if phase is not None:
        AccountBootstrap.reject_legacy_realm(phase=phase)

    try:
        account = await get_exchange_account(
            session,
            exchange_account_id=exchange_account_id,
            for_command=True,
        )
    except AccountNotFound as exc:
        raise ConfigurationError(
            f"exchange account {canonical_id} is not provisioned"
        ) from exc
    except AccountRetired as exc:
        raise ConfigurationError(
            f"exchange account {canonical_id} must be active for daemon boot"
        ) from exc
    if account.lifecycle_status != "active":
        raise ConfigurationError(
            f"exchange account {canonical_id} must be active for daemon boot"
        )
    draft = await load_account_config_draft(
        session, exchange_account_id=exchange_account_id
    )
    return AccountBootstrap(
        exchange_account_id=exchange_account_id,
        deployment_environment=deployment_environment,
        config_draft=dict(draft.config) if draft is not None else None,
        config_revision=draft.revision if draft is not None else None,
    )


class VenueTask(Protocol):
    """A sub-task of the venue wiring (``apps/venue.py``), supervised like any other.

    The daemon starts the venue's tasks first and waits (bounded) for each to be ready
    before anything that talks to the venue runs."""

    name: str

    async def run(self, stop: asyncio.Event) -> None: ...

    async def wait_ready(self, stop: asyncio.Event, *, timeout_s: float) -> bool: ...


class VenueDiagnostics(Protocol):
    """What the soak report and CI read from the simulated venue: failures of the simulator
    itself (never venue answers), requests it did not route, and its state."""

    @property
    def internal_failures(self) -> Sequence[Any]: ...

    @property
    def unexpected(self) -> Sequence[Any]: ...

    @property
    def state(self) -> Any: ...


_VENUE_TASK_READY_TIMEOUT_S = 30.0

# Credential recovery mode retries the boot observation 1, 2, 4 minutes apart, then every 5
# (plan decision D11, sub-question 2): slow, because the venue's answer only changes once
# someone fixes or replaces the key at Bitfinex.
CREDENTIAL_RECOVERY_RETRY_BASE_S = 60.0
CREDENTIAL_RECOVERY_RETRY_MAX_S = 300.0


def credential_recovery_retry_delay_s(attempt: int) -> float:
    """60, 120, 240, 300, 300, ... seconds before the 1st, 2nd, 3rd, ... retry."""
    return float(min(CREDENTIAL_RECOVERY_RETRY_MAX_S,
                     CREDENTIAL_RECOVERY_RETRY_BASE_S * 2 ** max(attempt - 1, 0)))


class BootOutcome(Enum):
    """How :meth:`Daemon._boot_until_observed` ended (every other failure raises)."""

    OBSERVED = "observed"
    # A stop was requested while waiting for an unreachable venue.
    STOPPED = "stopped"
    # The venue refused the account's key, or the key is not one the bot may trade with
    # (``CredentialNoncompliantError``): the boot holds in credential recovery mode.
    CREDENTIAL_REJECTED = "credential_rejected"


def credential_refusal(exc: BaseException) -> str | None:
    """Why the account's key cannot be used, or None: the venue refused it (10100 or
    401/403 anywhere in the chain) or it fails the compliance rule. Recovery mode's reason."""
    if credential_rejection(exc) is not None:
        return "venue_refused_key"
    current: BaseException | None = exc
    while current is not None:
        if isinstance(current, CredentialNoncompliantError):
            return str(current)
        current = current.__cause__ or current.__context__
    return None


async def _wait_or_stop(stop: asyncio.Event, seconds: float) -> None:
    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(stop.wait(), timeout=seconds)


@dataclass
class Daemon:
    config: MarketfeedConfig
    registry: StrategyRegistry
    candle_q: asyncio.Queue[CandleMessage | None]
    diagnostics: DiagnosticsSink
    probe: HealthProbe
    monitor: HealthMonitor
    scheduler: Scheduler
    signal_engine: SignalEngine
    db_engine: AsyncEngine
    ws_client: BitfinexWSClient | None
    writer: CandleWriter
    bitfinex_http: httpx.AsyncClient
    bitfinex: BitfinexREST
    session_factory: async_sessionmaker[AsyncSession]
    # Phase 4.2 Task 20: execution + safety wiring.
    account_bootstrap: AccountBootstrap
    executor: VenueExecutorPort
    safety_chain: SafetyGuardChain
    account_ctx: AccountContext
    bus: DomainEventBus
    auth_ws: BitfinexAuthWSClient | None = None
    ws_dispatcher: BitfinexLiveWSDispatcher | None = None
    boot_recovery: ObservationSink | None = None
    observation_scope: Scope | None = None
    periodic_reconcile: PeriodicReconcile | None = None
    book_snapshot_writer: BookSnapshotWriter | None = None
    interest_ledger_sync: InterestLedgerSync | None = None
    credit_history_sync: CreditHistorySync | None = None
    funding_book_service: FundingBookService | None = None
    healthz_host: str = "0.0.0.0"
    healthz_port: int = 8080
    admin_token: str | None = field(default=None, repr=False)
    admin_read_token: str | None = field(default=None, repr=False)
    # Behaviour-reporting service behind /admin/trading-status + /admin/dry-evaluate.
    trading_status: TradingStatusService | None = None
    trading_readiness: TradingReadiness | None = None
    # Single-writer advisory lock — live+Postgres only; None on sim/sqlite.
    writer_lock: WriterLock | None = None
    command_gate: AccountCommandGate | None = None
    # Applies operator adjudications the web API queued (ADR D4'); live only.
    uncertainty_worker: UncertaintyResolutionWorker | None = None
    # Four Golden Signals registry — served at /metrics on the healthz server.
    metrics: DaemonMetrics | None = None
    # OTel traces (wiki pending #4) — default-off (BFX_OTEL_ENABLED), fail-open.
    tracing: DaemonTracing | None = None
    # Automatic protections: supervised consumer turning trips into the kill.
    protection: AutomaticProtection | None = None
    # True once boot recovery passed; an exit before that is a refused boot (T8 alert).
    booted: bool = False
    # Clock the warmup and boot rehydrate tick used; the scheduler arms from it.
    boot_mts: int | None = None
    writer_lock_watch: WriterLockWatch | None = None
    # Applies operator resume/kill requests.
    trading_control: TradingControlWorker | None = None
    # Applies operator enable/disable of one currency's policy (its own queue:
    # a kill never waits behind it).
    capital_policy_control: CapitalPolicyRequestWorker | None = None
    # Applies operator rotations of the vault credential (Bitfinex only; an applied one
    # sets the stop event so the process restarts on the new key).
    credential_rotation: CredentialRotationWorker | None = None
    # What the venue wiring contributes: its tasks, and how to close what it opened
    # (called by ``apps.bot._run``). Diagnostics exist for the simulated venue only.
    venue_tasks: tuple[VenueTask, ...] = ()
    venue_aclose: Callable[[], Awaitable[None]] | None = None
    venue_diagnostics: VenueDiagnostics | None = None
    # Backoff between boot observation attempts while the venue is unreachable.
    boot_retry_delay_s: Callable[[int], float] = boot_retry_delay_s
    # Credential recovery mode (``_run_credential_recovery``). The api key the boot loaded
    # from the vault (None for the simulated venue's throwaway keys): a different active
    # credential in the database ends recovery mode. The run's start
    # (``bind_run``, from ``bot_runs``) bounds when recovery mode may restart the process.
    boot_credential_api_key: str | None = field(default=None, repr=False)
    run_started_at_ms: int | None = None
    clock: Callable[[], int] = now_ms_utc
    credential_recovery_retry_delay_s: Callable[[int], float] = credential_recovery_retry_delay_s
    # How recovery mode waits (a backoff, the settle window): returns early on the stop event.
    credential_recovery_wait: Callable[[asyncio.Event, float], Awaitable[None]] = _wait_or_stop
    # The key's compliance (``execution.credential_compliance``; Bitfinex vault keys only, None
    # for the simulated venue): part of every boot attempt, and rechecked while running.
    credential_compliance: CredentialCompliance | None = None
    credential_recheck_interval_s: float = CREDENTIAL_RECHECK_INTERVAL_S
    credential_recheck_wait: Callable[[asyncio.Event, float], Awaitable[None]] = _wait_or_stop
    # Why the boot holds in credential recovery mode (set when it enters it).
    credential_recovery_reason: str | None = None
    _stop_event: asyncio.Event = field(default_factory=asyncio.Event)

    def bind_run(self, started_at_ms: int) -> None:
        """This process's ``bot_runs`` start: recovery mode never restarts the process before
        the run is :data:`BOT_SETTLE_WINDOW` old (a deploy may be settling it)."""
        self.run_started_at_ms = started_at_ms

    async def _run_boot_recovery(self) -> None:
        if self.boot_recovery is None:
            return
        try:
            if self.observation_scope is None:
                raise ValueError("boot observation scope is required")
            # One rule for either authority. The legacy sink raises on a refusal and
            # otherwise answers ``accepted``. The ledger cycle answers by decision:
            # an admission refusal at grace 0 under the writer lock is impossible
            # (every outcome-less attempt was closed first), so it is an invariant
            # failure; a fenced or incomplete observation boots with trading blocked
            # (no accepted basis yet) and seeds the periodic loop's streak.
            cycle = await self.boot_recovery.run(self.observation_scope)
            if cycle.decision == "query_admission_refused":
                raise BootInvariantError("boot observation refused query admission at grace 0")
            if cycle.decision != "accepted":
                log.warning("boot_observation_not_accepted decision=%s", cycle.decision)
                if self.periodic_reconcile is not None:
                    self.periodic_reconcile.note_boot(cycle.decision)
        except BaseException:
            # A protection tripped by the refused boot observation must be
            # durable before the daemon exits, or the next boot starts without
            # the stop it found.
            if self.protection is not None:
                await self.protection.run_pending()
            raise

    async def _boot_attempt(self) -> None:
        """One boot: the key's compliance (when the venue has a vault key), then the boot
        observation. Recovery mode retries this same attempt, so a key that is refused or
        non-compliant ends recovery mode only once the whole boot would pass."""
        if self.credential_compliance is not None:
            await self.credential_compliance.check()
        await self._run_boot_recovery()

    async def _boot_until_observed(self) -> BootOutcome:
        """Run the boot observation until it completes, a stop is requested while waiting, or
        the venue refuses the account's key.

        A transient reachability failure (venue or database did not answer, see
        ``boot_wait.is_transient_dependency_error``) is retried in-process with a
        capped backoff instead of exiting: a restart would meet the same outage
        and crash-loop. Nothing trades meanwhile — no sub-task exists yet. The venue
        refusing the key (``external.bitfinex.errors.credential_rejection``: 10100 or
        HTTP 401/403, anywhere in the chain) or the key failing its compliance check
        (``CredentialNoncompliantError``, see :meth:`_boot_attempt`) returns
        ``CREDENTIAL_REJECTED`` for credential recovery mode. Every other failure (a refusal such as nonce 10114,
        an invariant) propagates and refuses the boot. Before each retry the writer lock is re-checked
        (``WriterLockWatch.check``: re-acquire if its connection dropped, exit if
        another writer holds it), so the observation only ever runs as the
        single writer, as it does on the first attempt right after the acquire.
        """
        wait = BootWait("venue observation", delay_s=self.boot_retry_delay_s)
        while True:
            if wait.failures and self.writer_lock_watch is not None:
                await self.writer_lock_watch.check()
            try:
                await self._boot_attempt()
            except Exception as exc:
                refusal = credential_refusal(exc)
                if refusal is not None:
                    log.critical("boot_venue_credential_rejected reason=%s error=%r",
                                 refusal, exc)
                    self.credential_recovery_reason = refusal
                    return BootOutcome.CREDENTIAL_REJECTED
                if not is_transient_dependency_error(exc):
                    raise
                delay = wait.failed(exc)
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._stop_event.wait(), timeout=delay)
                if self._stop_event.is_set():
                    log.info("boot_stopped_while_waiting_for_dependency")
                    return BootOutcome.STOPPED
                continue
            wait.succeeded()
            return BootOutcome.OBSERVED

    # ------------------------------------------------------ credential recovery mode

    def _credential_recovery_tasks(
        self,
    ) -> list[tuple[str, Callable[[], Coroutine[Any, Any, None]]]]:
        """What runs while the venue refuses the key: the credential rotation worker (the CLI
        seals a new key to the recipient it publishes) and the liveness that keeps /healthz
        200 and the writer lock held. No trading task, no venue reader, and no trading
        control: a kill's cancel-all would go out on the refused key and be recorded as
        failed, so a queued kill waits for the next boot on a working key."""
        tasks: list[tuple[str, Callable[[], Coroutine[Any, Any, None]]]] = []
        rotation = self.credential_rotation
        if rotation is not None:
            tasks.append(("credential_rotation", lambda: rotation.run(self._stop_event)))
        tasks += [
            ("health_check", self._heartbeat_scan_loop),
            ("db_keepalive", self._db_keepalive_loop),
            ("healthz", self._healthz_server_loop),
        ]
        if self.writer_lock is not None:
            tasks.append(("writer_lock", self._writer_lock_liveness_loop))
        tasks.append(("credential_recovery_retry", self._credential_recovery_retry_loop))
        return tasks

    async def _run_credential_recovery(self) -> None:
        """Hold the boot while the venue refuses the account's key, then end the run cleanly.

        Nothing trades: the boot observation never completed, so no trading sub-task exists.
        The run ends (the stop event, then ``clean_stop`` and exit 0; the container restart
        policy boots a fresh process) once the key works again or was replaced -- an applied
        rotation (the worker restarts as it always does), a retried boot observation that
        succeeds, or a different active credential in the database (since 9c-2b only the owner)
        -- and the run is at least :data:`BOT_SETTLE_WINDOW` old. A SIGTERM ends it too.
        """
        account = self.account_bootstrap.account_id
        reason = self.credential_recovery_reason or "venue_refused_key"
        log.critical("credential_recovery_mode_entered account=%s reason=%s", account, reason)
        alerts.emit(alerts.CREDENTIAL_RECOVERY_MODE, level=alerts.CRITICAL, account=account,
                    reason=reason)
        self._recovery_started_at_ms()  # pinned now when bot_runs gave no start
        async with asyncio.TaskGroup() as tg:
            for name, task in self._credential_recovery_tasks():
                tg.create_task(task(), name=name)

    async def _credential_recovery_retry_loop(self) -> None:
        """Retry the boot itself, slowly, until it succeeds or the active credential changed.

        Each attempt first re-checks the writer lock (as the boot wait does), then compares
        the active credential's api key with the one this process booted on: a different key
        ends recovery mode without asking the venue (the next boot loads it). Otherwise the
        boot observation runs again: still refused or unreachable, the loop waits; any other
        failure raises out of the TaskGroup and refuses the boot as before; success ends
        recovery mode. There is no separate probe, so a probe that passes while the boot
        still fails cannot loop the process through restarts.
        """
        attempt = 0
        while not self._stop_event.is_set():
            attempt += 1
            delay = self.credential_recovery_retry_delay_s(attempt)
            await self.credential_recovery_wait(self._stop_event, delay)
            if self._stop_event.is_set():
                break
            if self.writer_lock_watch is not None:
                await self.writer_lock_watch.check()
            try:
                if await self._active_credential_changed():
                    await self._end_credential_recovery("active_credential_changed")
                    return
                await self._boot_attempt()
            except CredentialSupersededError as exc:
                # A rotation landed between the comparison above and the check: the next
                # attempt's comparison ends recovery mode (or the worker's restart does).
                log.info("credential_recovery_key_superseded attempt=%d reason=%s",
                         attempt, exc.reason)
                continue
            except Exception as exc:
                refusal = credential_refusal(exc)
                if refusal is not None:
                    log.warning("credential_recovery_still_rejected attempt=%d reason=%s",
                                attempt, refusal)
                    continue
                if is_transient_dependency_error(exc):
                    log.warning("credential_recovery_venue_unreachable attempt=%d error=%r",
                                attempt, exc)
                    continue
                raise
            await self._end_credential_recovery("boot_observation_succeeded")
            return
        log.info("sub_task_exit name=credential_recovery_retry")

    async def _credential_recheck_loop(self) -> None:
        """Recheck the key every :data:`CREDENTIAL_RECHECK_INTERVAL_S` while trading.

        Non-compliant, or refused by the venue itself (its body says 10100): raise, so the
        daemon exits 78 and the next boot holds in credential recovery mode (the same exit as a
        refused reconcile read; ADR 2026-10-18 D11, no separate stop state). The key replaced
        by a rotation (``CredentialSupersededError``): the rotation's own restart ends the run,
        so wait for it. Anything else (the venue did not answer, an answer that does not
        parse, a bare 401/403 a gateway can send, another refusal such as nonce 10114) is
        retried after :data:`CREDENTIAL_RECHECK_RETRY_S`: a recheck never stops trading on a
        guess. :data:`CREDENTIAL_RECHECK_ALERT_AFTER` such rechecks in a row send one
        ``credential_recheck_failing`` alert, so a recheck that can never pass is not silent;
        the next recheck that passes says so and starts a new streak."""
        compliance = self.credential_compliance
        assert compliance is not None
        delay = self.credential_recheck_interval_s
        failures = 0
        account = self.account_bootstrap.account_id
        while not self._stop_event.is_set():
            await self.credential_recheck_wait(self._stop_event, delay)
            if self._stop_event.is_set():
                break
            try:
                await compliance.check()
            except CredentialNoncompliantError as exc:
                log.critical("credential_recheck_noncompliant reason=%s", exc.reason)
                raise
            except Exception as exc:
                rejected = credential_rejection(exc)
                if rejected is not None and rejected.venue_coded:
                    log.critical("credential_recheck_rejected error=%r", exc)
                    if rejected is exc:
                        raise
                    raise rejected from exc
                failures += 1
                if isinstance(exc, CredentialSupersededError):
                    log.warning("credential_recheck_key_superseded reason=%s failures=%d",
                                exc.reason, failures)
                else:
                    log.warning("credential_recheck_failed failures=%d error=%r", failures, exc)
                if failures == CREDENTIAL_RECHECK_ALERT_AFTER:
                    alerts.emit(alerts.CREDENTIAL_RECHECK_FAILING, level=alerts.WARNING,
                                account=account, recovered=False, consecutive=failures,
                                # The type only: a venue body can echo what it was sent.
                                error=(str(exc) if isinstance(exc, CredentialSupersededError)
                                       else type(exc).__name__))
                delay = CREDENTIAL_RECHECK_RETRY_S
                continue
            if failures >= CREDENTIAL_RECHECK_ALERT_AFTER:
                alerts.emit(alerts.CREDENTIAL_RECHECK_FAILING, level=alerts.INFO,
                            account=account, recovered=True, after=failures)
            if failures:
                log.info("credential_recheck_passed_again after=%d", failures)
            failures = 0
            delay = self.credential_recheck_interval_s
        log.info("sub_task_exit name=credential_recheck")

    async def _active_credential_changed(self) -> bool:
        """Whether the account's active Bitfinex credential is no longer the key this process
        booted on (only the key column is read). No active credential at all is no change:
        a restart would only refuse the boot at the vault."""
        if self.boot_credential_api_key is None:
            return False
        async with self.session_factory() as session:
            active = await session.scalar(select(ExchangeAccountCredential.api_key).where(
                ExchangeAccountCredential.exchange_account_id
                == self.account_bootstrap.exchange_account_id,
                ExchangeAccountCredential.venue == "bitfinex",
                ExchangeAccountCredential.lifecycle_status == "active",
            ))
        return active is not None and active != self.boot_credential_api_key

    async def _end_credential_recovery(self, reason: str) -> None:
        """Stop the run once it is :data:`BOT_SETTLE_WINDOW` old, so the next boot runs on a
        working key; a deploy that is still health-checking and settling this run must not
        see it restart (a stop request meanwhile ends the wait)."""
        settle_ms = int(BOT_SETTLE_WINDOW.total_seconds() * 1000)
        while not self._stop_event.is_set():
            remaining_ms = settle_ms - (self.clock() - self._recovery_started_at_ms())
            if remaining_ms <= 0:
                break
            log.info("credential_recovery_resolved_waiting_for_settle reason=%s remaining_s=%.0f",
                     reason, remaining_ms / 1000)
            await self.credential_recovery_wait(self._stop_event, remaining_ms / 1000)
        if self._stop_event.is_set():
            return  # stopped from outside while waiting: that stop ends the run
        log.info("credential_recovery_resolved reason=%s", reason)
        self._stop_event.set()

    def _recovery_started_at_ms(self) -> int:
        if self.run_started_at_ms is None:
            # No recorded run (its row could not be written): count from now, which is never
            # earlier than the real start, so the settle window is kept, never shortened.
            self.run_started_at_ms = self.clock()
        return self.run_started_at_ms

    async def run(self) -> None:
        """Main entry — sub-task supervision via TaskGroup.

        Phase 4.1 used asyncio.gather + create_task with no propagation,
        producing 4hr zombie when candle_writer silently died. Per spec
        D4: any sub-task raise → TaskGroup cancel all → ExceptionGroup
        propagates → _run handles cleanup + exit non-zero.
        """
        # Initial cell registration (was in startup)
        for cell in self.config.cells:
            self.scheduler.register_from_now(cell, now_ms=self.boot_mts)

        # 3a-recovery: reconcile against venue + resolve crash-mid-flight PENDING
        # BEFORE any sub-task starts (None only in unit compositions). An
        # unreachable venue is waited out in-process; a refused key holds the boot in
        # credential recovery mode (nothing trades); any other refusal raises here and
        # the daemon fails to start (fail-safe).
        outcome = await self._boot_until_observed()
        if outcome is BootOutcome.STOPPED:
            return
        if outcome is BootOutcome.CREDENTIAL_REJECTED:
            await self._run_credential_recovery()
            return
        self.booted = True

        async with asyncio.TaskGroup() as tg:
            # First, and awaited: a submit that reaches the simulated venue before its feed
            # holds a book would be a simulator-internal failure, not a venue answer.
            for venue_task in self.venue_tasks:
                tg.create_task(venue_task.run(self._stop_event), name=venue_task.name)
                if not await venue_task.wait_ready(
                    self._stop_event, timeout_s=_VENUE_TASK_READY_TIMEOUT_S,
                ):
                    log.warning("venue_task_not_ready_at_start name=%s", venue_task.name)
            if self.protection is not None:
                tg.create_task(self.protection.run(self._stop_event), name="automatic_protection")
            if self.trading_control is not None:
                tg.create_task(self.trading_control.run(self._stop_event), name="trading_control")
            if self.capital_policy_control is not None:
                tg.create_task(self.capital_policy_control.run(self._stop_event),
                               name="capital_policy_control")
            if self.credential_rotation is not None:
                tg.create_task(self.credential_rotation.run(self._stop_event),
                               name="credential_rotation")
            if self.credential_compliance is not None:
                tg.create_task(self._credential_recheck_loop(), name="credential_recheck")
            if self.uncertainty_worker is not None:
                tg.create_task(
                    self.uncertainty_worker.run(self._stop_event), name="uncertainty_resolution",
                )
            tg.create_task(self._candle_writer_loop(), name="candle_writer")
            tg.create_task(self._scheduler_loop(),     name="scheduler")
            tg.create_task(self._monitor_loop(),       name="monitor")
            tg.create_task(self._heartbeat_scan_loop(), name="health_check")
            tg.create_task(self._db_keepalive_loop(),  name="db_keepalive")
            tg.create_task(self._healthz_server_loop(), name="healthz")
            # Single-writer lock liveness (live+Postgres only; None otherwise).
            if self.writer_lock is not None:
                tg.create_task(
                    self._writer_lock_liveness_loop(), name="writer_lock",
                )
            if self.ws_client is not None:
                tg.create_task(self._ws_consume_with_reconnect(), name="ws")
                tg.create_task(self._ws_heartbeat_poll_loop(), name="ws_heartbeat")
            # Phase 4.4a Task 19: WS dispatcher sub-task only runs when
            # the venue's capabilities require the authenticated WebSocket.
            if self.ws_dispatcher is not None:
                tg.create_task(
                    self.ws_dispatcher.run(self._stop_event),
                    name="ws_dispatcher",
                )
            # Book depth self-recording (observe-only; fail-open inside).
            if self.book_snapshot_writer is not None:
                tg.create_task(
                    self.book_snapshot_writer.run(self._stop_event),
                    name="book_snapshot",
                )
            # Realized interest from the venue ledger (observe-only; fail-open inside).
            if self.interest_ledger_sync is not None:
                tg.create_task(
                    self.interest_ledger_sync.run(self._stop_event),
                    name="interest_ledger",
                )
            # Per-credit truth for attribution (observe-only; fail-open inside).
            if self.credit_history_sync is not None:
                tg.create_task(
                    self.credit_history_sync.run(self._stop_event),
                    name="credit_history",
                )
            # The live eligibility provider owns its own WS shutdown in run()'s
            # finally block. TaskGroup supervision ensures that path is used once.
            if self.funding_book_service is not None:
                tg.create_task(
                    self.funding_book_service.run(self._stop_event),
                    name="funding_book",
                )
            # Observe-only auth-WS health poll (2026-07 nonce-flap fix): surfaces
            # "enabled but never authenticates" in the HEALTH_CHECK stream. NOT a
            # liveness task → never drives /healthz 503 or a restart.
            if self.auth_ws is not None:
                tg.create_task(
                    self._auth_ws_health_poll_loop(), name="auth_ws_health",
                )
            # Spec 2026-05-27: periodic venue reconcile is the correctness
            # backbone — runs live-only, converges the ledger every interval.
            if self.periodic_reconcile is not None:
                tg.create_task(
                    self.periodic_reconcile.run_loop(self._stop_event),
                    name="periodic_reconcile",
                )
            # When _stop_event is set externally (SIGTERM), each sub-task's
            # internal loop exits cleanly; TaskGroup waits for all to drain.

    async def _candle_writer_loop(self) -> None:
        """Plumbing wrapper for self.writer.run() with FatalError pass-through.
        Heartbeat is recorded inside CandleWriter.run() after each successful upsert.
        Sends None sentinel to unblock queue.get() on graceful stop."""
        try:
            # Run writer and a stop-sentinel task concurrently; cancel the
            # sentinel if writer exits naturally (e.g. on CancelledError).
            async def _drain_sentinel() -> None:
                await self._stop_event.wait()
                await self.candle_q.put(None)

            writer_task = asyncio.create_task(self.writer.run(), name="candle_writer_inner")
            sentinel_task = asyncio.create_task(_drain_sentinel(), name="candle_writer_sentinel")
            try:
                done, pending = await asyncio.wait(
                    {writer_task, sentinel_task},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                for t in pending:
                    t.cancel()
                    with contextlib.suppress(asyncio.CancelledError, Exception):
                        await t
                # Re-raise any exception from writer_task
                for t in done:
                    if t is writer_task and not t.cancelled():
                        exc = t.exception()
                        if exc is not None:
                            raise exc
            except asyncio.CancelledError:
                writer_task.cancel()
                sentinel_task.cancel()
                raise
            log.info("sub_task_exit name=candle_writer")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.exception("candle_writer_fatal")
            from bfx_funding_bot.core.errors import FatalError
            raise FatalError(f"candle_writer crashed: {e!r}") from e

    async def _scheduler_loop(self) -> None:
        await self.scheduler.start()
        await self._stop_event.wait()
        await self.scheduler.stop()
        log.info("sub_task_exit name=scheduler")

    async def _monitor_loop(self) -> None:
        """State-change emission loop (was HealthMonitor.start() in Phase 4.1).
        Polls probe.drain_dirty() every 50ms + periodic full heartbeat emit."""
        await self.monitor.start()
        await self._stop_event.wait()
        await self.monitor.stop()
        log.info("sub_task_exit name=monitor")

    async def _heartbeat_scan_loop(self) -> None:
        """Periodic staleness scan. Tick every 30s. FatalError → TaskGroup cancel."""
        while not self._stop_event.is_set():
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=30.0)
                log.info("sub_task_exit name=health_check")
                return  # stop requested
            except TimeoutError:
                pass
            self.probe.record_heartbeat("health_check")
            await self.monitor.scan_staleness()  # may raise FatalError

    async def _db_keepalive_loop(self) -> None:
        """Ping the database at start and then every 5 min. ``db_keepalive``
        (liveness) beats per attempt; ``db`` (dependency freshness, readiness
        only) per success, which also clears it in /readyz at once. A recovered
        database therefore shows ready again at the next ping, at most one
        keepalive interval later."""
        from bfx_funding_bot.core.keepalive import keepalive_loop
        await keepalive_loop(
            self.db_engine,
            stop=self._stop_event,
            on_tick=lambda _ts: self._dependency_answered(DB_FRESHNESS),
            on_attempt=lambda: self.probe.record_heartbeat("db_keepalive"),
        )
        log.info("sub_task_exit name=db_keepalive")

    async def _writer_lock_liveness_loop(self) -> None:
        """OBSERVABILITY + RECOVERY ONLY for the single-writer advisory lock.

        Every 30s: refresh() (re-acquires if a dropped connection lost the lock
        server-side, while nobody else holds it) and record a heartbeat on a
        SUCCESSFUL refresh. The heartbeat is NON-FATAL: a lost lock makes this
        beat go stale, but health_monitor classifies "writer_lock" as
        activity-class (ACTIVITY_THRESHOLDS) so scan_staleness emits a WARN/down
        observability event and NEVER escalates to FatalError / daemon restart.

        The authoritative fail-closed gate is the per-submit
        WriterLockGuard.verify_held(): if the lock isn't held, every real-money
        submit is blocked — safety is preserved without restarting. A lock still
        not held after refresh also trips the writer_lock_lost protection
        (HALTED/auto), which is durable where the guard is per-process. Tying this
        recovery loop to liveness would re-create the 2026-05-26 reactive
        restart-loop anti-pattern.

        Wait-first shape mirrors the sibling loops (_heartbeat_scan_loop,
        _ws_heartbeat_poll_loop): wait on the stop event with a 30s timeout, then
        do work. refresh and verify_held serialize on the same internal lock, so
        the 30s interval stays safely above submit cadence (no contention)."""
        assert self.writer_lock is not None
        while not self._stop_event.is_set():
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=30.0)
                log.info("sub_task_exit name=writer_lock")
                return  # stop requested
            except TimeoutError:
                pass
            held = (await self.writer_lock_watch.check() if self.writer_lock_watch is not None
                    else await self.writer_lock.refresh())
            if held:
                self.probe.record_heartbeat("writer_lock")
        log.info("sub_task_exit name=writer_lock")

    async def _healthz_server_loop(self) -> None:
        """Liveness/readiness HTTP endpoints (``GET /healthz``, ``GET /readyz``).

        Served from this event loop, so a blocked loop answers nothing; an
        external probe sees no response, and the process-level
        ``core.loop_watchdog`` (armed in ``apps.bot._run``) ends the process.
        The deploy health gate (deploy/vm/ops/bfx_deploy.py) reads /healthz;
        the compose file defines no Docker healthcheck on it.
        """
        await run_healthz_server(
            probe=self.probe,
            host=self.healthz_host,
            port=self.healthz_port,
            stop_event=self._stop_event,
            admin_token=self.admin_token,
            read_token=self.admin_read_token,
            metrics=self.metrics,
            trading_status=self.trading_status,
            readiness=self.trading_readiness,
        )
        log.info("sub_task_exit name=healthz")

    async def _auth_ws_health_poll_loop(self) -> None:
        """Observe-only: surface a dead/flapping authenticated WS in the
        HEALTH_CHECK stream. Polls every 60s and calls assess_auth_ws_health.

        Deliberately does NOT record a liveness heartbeat: a DOWN here (auth WS
        connected but never authenticated) must stay observable-only — a nonce/
        auth fault won't heal on restart, so wiring it to /healthz 503
        would just flap-restart the real-money bot. Transition-only update()
        (like BITFINEX_WS) avoids emitting every 60s.
        """
        assert self.auth_ws is not None
        while not self._stop_event.is_set():
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=60.0)
                log.info("sub_task_exit name=auth_ws_health")
                return
            except TimeoutError:
                pass
            status, msg = assess_auth_ws_health(
                connection_count=self.auth_ws.connection_count,
                auth_ok_count=self.auth_ws.auth_ok_count,
                reconnect_count_last_hour=self.auth_ws.reconnect_count_last_hour(),
            )
            if self.probe.current_status(HealthTarget.AUTH_WS) != status:
                self.probe.update(
                    HealthTarget.AUTH_WS, status,
                    reconnect_count_last_hour=self.auth_ws.reconnect_count_last_hour(),
                    error_message=msg,
                )

    def _dependency_answered(self, dependency: str) -> None:
        """A dependency answered: beat its freshness and clear it in /readyz now,
        not at the next staleness scan (which only ever marks it stale)."""
        self.probe.record_heartbeat(dependency)
        if self.trading_readiness is not None:
            self.trading_readiness.clear_dependency(dependency)

    async def _ws_heartbeat_poll_loop(self) -> None:
        """Run ``_ws_freshness_tick`` every 15s until stop."""
        assert self.ws_client is not None
        while not self._stop_event.is_set():
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=15.0)
                log.info("sub_task_exit name=ws_heartbeat")
                return  # stop requested
            except TimeoutError:
                pass
            self._ws_freshness_tick()

    def _ws_freshness_tick(self) -> None:
        """One poll of the public WS: liveness always, data freshness if fresh.

        ``ws`` (liveness) beats on every tick: it says this poller is iterating,
        nothing about Bitfinex, so a venue outage can never age it into a restart.
        ``ws_data`` (dependency freshness) beats only when this client actually
        received a frame within 60s (``last_frame_age_ms``; a client that has not
        received one yet proves nothing). Bitfinex sends `hb` frames every ~15s on subscribed channels when
        idle; `_handle_raw` in ws.py updates state.last_msg_ts on any frame
        (candle or hb), while candles() yields only candle data — so the frame
        age, not the candle stream, is the freshness signal. A stale ``ws_data``
        flips readiness and blocks submits through HeartbeatGuard; reconnecting
        is the WS client's own job (its hb watchdog and
        ``_ws_consume_with_reconnect``).

        A fresh tick also restores BITFINEX_WS to HEALTHY (Bug B fix 5/20:
        nothing else flips ws→healthy after a disconnect set DEGRADED, so the
        state was sticky and health_monitor kept emitting warn every 5min).
        """
        self.probe.record_heartbeat("ws")
        if self.ws_client is None:
            return
        frame_age_ms = self.ws_client.last_frame_age_ms()
        if frame_age_ms is None or frame_age_ms >= 60_000:
            return  # no frame yet from this client, or none for 60s: not fresh
        self._dependency_answered(MARKET_DATA_FRESHNESS)
        # Bug B fix: only emit transition (degraded/down → healthy or
        # first-ever set) — don't spam every 15s with new last_msg_age_ms values.
        if self.probe.current_status(HealthTarget.BITFINEX_WS) != HealthStatus.HEALTHY:
            self.probe.update(
                HealthTarget.BITFINEX_WS,
                HealthStatus.HEALTHY,
                last_msg_age_ms=self.ws_client.last_msg_age_ms(),
                reconnect_count_last_hour=self.ws_client.reconnect_count_last_hour(),
            )

    async def _ws_consume_with_reconnect(self) -> None:
        """WS recv + reconnect loop. Never exits unless daemon shuts down.

        Spec: Flow 3 (WS disconnect → reconnect). Exponential backoff via
        compute_backoff_secs; resets after 5min stable connection; emits
        health_check transitions; runs REST gap-fill on reconnect success.
        """
        assert self.ws_client is not None
        consecutive_failures = 0
        while not self._stop_event.is_set():
            try:
                # async for would block in ws_client.candles() recv() between
                # yields without checking stop_event — paper exit cycle 2026-05-21
                # hung daemon TaskGroup drain because ws never noticed stop.
                # Manual __anext__() race with stop_event each iteration ensures
                # cancellation arrives within the recv window, not the next yield.
                candles_iter = self.ws_client.candles().__aiter__()
                try:
                    while not self._stop_event.is_set():
                        # ensure_future accepts Awaitable (anext returns Awaitable[T]
                        # not Coroutine, so create_task fails mypy strict).
                        msg_task = asyncio.ensure_future(candles_iter.__anext__())
                        stop_task = asyncio.create_task(self._stop_event.wait())
                        done, _ = await asyncio.wait(
                            {msg_task, stop_task}, return_when=asyncio.FIRST_COMPLETED,
                        )
                        if stop_task in done:
                            msg_task.cancel()
                            with contextlib.suppress(asyncio.CancelledError, Exception):
                                await msg_task
                            log.info("sub_task_exit name=ws path=stop_during_recv")
                            return
                        stop_task.cancel()
                        with contextlib.suppress(asyncio.CancelledError):
                            await stop_task
                        try:
                            msg = msg_task.result()
                        except StopAsyncIteration:
                            break  # generator exhausted → reconnect path below
                        await self.candle_q.put(msg)
                        # successful message → connection is alive; reset failure counter
                        consecutive_failures = 0
                        self.ws_client.maybe_reset_backoff()
                        # ws freshness is recorded by _ws_freshness_tick based on
                        # ws_client.last_msg_age_ms() (which counts both candle frames
                        # and Bitfinex `hb` frames), not here — yielded candles are
                        # sparse on 1h cells.
                finally:
                    # aclose only on async generators (not plain AsyncIterator).
                    # AttributeError suppressed if candles() returns the latter.
                    with contextlib.suppress(Exception):
                        await candles_iter.aclose()  # type: ignore[attr-defined]
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("ws_recv_error_will_reconnect")

            if self._stop_event.is_set():
                log.info("sub_task_exit name=ws path=stop_after_recv_exit")
                return

            # WS exited unexpectedly → enter reconnect loop
            attempt = self.ws_client.reconnect_attempts
            backoff = compute_backoff_secs(max(1, attempt))
            log.info(
                "ws_reconnect_attempt=%d backoff_s=%d", attempt, backoff,
            )
            self.probe.update(
                HealthTarget.BITFINEX_WS,
                HealthStatus.DEGRADED,
                last_msg_age_ms=999_999,
                reconnect_count_last_hour=self.ws_client.reconnect_count_last_hour(),
                error_message="ws_reconnect_pending",
            )
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(), timeout=backoff,
                )
                log.info("sub_task_exit name=ws path=stop_during_backoff")
                return
            except TimeoutError:
                pass

            # Recreate WS client (existing client may have _ws=None + closed state)
            prev_attempts = attempt
            try:
                await self.ws_client.close()
            except Exception:
                log.exception("ws_reconnect_close_old_failed")
            self.ws_client = BitfinexWSClient(
                channels=[
                    ChannelSpec(
                        symbol=c.symbol,
                        timeframe=c.timeframe,
                        period_agg=c.period_agg,
                    )
                    for c in self.config.cells
                ],
                on_disconnect=lambda reason: self.probe.update(
                    HealthTarget.BITFINEX_WS,
                    HealthStatus.DEGRADED,
                    last_msg_age_ms=999_999,
                    reconnect_count_last_hour=0,
                    error_message=f"ws_disconnect: {reason}",
                ),
            )
            # carry over the attempt counter for backoff schedule continuity
            self.ws_client.reconnect_attempts = prev_attempts + 1
            consecutive_failures += 1

            # Gap-fill what we missed during the outage
            try:
                async with self.session_factory() as session:
                    for cell in self.config.cells:
                        row = (
                            await session.execute(
                                select(FundingCandleRow.mts)
                                .where(
                                    FundingCandleRow.symbol == cell.symbol,
                                    FundingCandleRow.timeframe == cell.timeframe,
                                    FundingCandleRow.period_agg
                                    == cell.period_agg,
                                )
                                .order_by(FundingCandleRow.mts.desc())
                                .limit(1)
                            )
                        ).scalar_one_or_none()
                        last_mts = int(row) if row is not None else 0
                        if last_mts > 0:
                            await fill_gap_from_rest(
                                bitfinex=self.bitfinex,
                                session=session,
                                symbol=cell.symbol,
                                timeframe=cell.timeframe,
                                period_agg=cell.period_agg,
                                last_known_mts=last_mts,
                                now_mts=now_ms_utc(),
                            )
            except Exception:
                log.exception("ws_reconnect_gap_fill_failed")

            if consecutive_failures >= 5:
                self.probe.update(
                    HealthTarget.BITFINEX_WS,
                    HealthStatus.DOWN,
                    last_msg_age_ms=999_999,
                    reconnect_count_last_hour=self.ws_client.reconnect_count_last_hour(),
                    error_message="reconnect_failed_5_consecutive",
                )

            # Loop continues → tries `candles()` again with new client


async def _emit_locf_degraded(
    stdout_sink: StdoutEventSink,
    config: MarketfeedConfig,
    cell: CellConfig,
    stale_seconds: int | None,
    budget_seconds: int,
) -> None:
    """Emit HealthCheckPayload SIGNAL_PIPELINE DEGRADED reason=stale_exceeded.

    Called from two paths: (a) no candles in lookback window (stale_seconds=None),
    (b) source candle older than budget (stale_seconds = computed wall-clock gap).

    # LOCF DEGRADED events carry cell_id in the "cell" envelope field (HealthMonitor._emit
    # always writes "cell": None — intentional divergence to enable per-cell dashboards).
    """
    error_message = (
        "stale_exceeded: no candles in lookback window"
        if stale_seconds is None
        else "stale_exceeded"
    )
    payload: dict[str, object] = {
        "check_target": HealthTarget.SIGNAL_PIPELINE.value,
        "status": HealthStatus.DEGRADED.value,
        "error_message": error_message,
        "reason": "stale_exceeded",
        "stale_seconds": stale_seconds,
        "budget_seconds": budget_seconds,
    }
    await stdout_sink.emit({
        "timestamp": datetime.now(UTC).isoformat(),
        "level": Level.WARN.value,
        "phase": config.phase.value,
        "strategy": None,
        "cell": cell.cell_id,
        "event_type": EventType.HEALTH_CHECK.value,
        "correlation_id": str(uuid4()),
        "payload": payload,
    })


_LIVE_REQUIRED_HARD = ("manual_kill", "auth_health", "heartbeat")


def assert_live_guard_invariant(phase: Phase, safety_cfg: SafetyConfig) -> None:
    """Real money cannot silently disable ownership-independent safety guards.

    Live takes its capital limits from the mandatory applied capital policy and
    its offer envelope, and requires the trading-state/auth/heartbeat guards.
    No-op for other phases. NAV drops only alert (lending envelope D3): they are
    not a guard.
    """
    if phase is not Phase.LIVE:
        return
    missing = [
        name for name in _LIVE_REQUIRED_HARD
        if not getattr(safety_cfg.hard_guards, name).enabled
    ]
    if missing:
        raise ValueError(
            f"BFX_PHASE={phase.value} requires all safety guards enabled; disabled: {missing}"
        )


async def _refuse_live_boot(exc: BaseException, *, config: MarketfeedConfig,
                            session_factory: async_sessionmaker[AsyncSession]) -> None:
    """A live boot that refuses to run alerts (``safety/boot_stop``) before the
    error is raised; nothing is written and nothing reaches the venue."""
    try:
        account_id = UUID(_require_env("BFX_EXCHANGE_ACCOUNT_ID"))
    except Exception:
        log.critical("boot_refused_without_account account unknown")
        return
    report_refused_boot(account_id=account_id, environment=config.deployment_environment.value,
                        reason=f"boot_blocked: {str(exc) or type(exc).__name__}")
