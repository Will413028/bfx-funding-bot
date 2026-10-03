"""Phase 4.1 paper/shadow daemon coordinator.

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
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
)

from bfx_funding_bot.core.crypto import VaultNotConfiguredError, load_kek
from bfx_funding_bot.core.errors import (
    BootInvariantError,
    ConfigurationError,
)
from bfx_funding_bot.core.health import HealthProbe, assess_auth_ws_health
from bfx_funding_bot.core.telemetry import EventType, HealthStatus, HealthTarget, Level, Phase
from bfx_funding_bot.core.writer_lock import WriterLock
from bfx_funding_bot.external.bitfinex.auth_ws import BitfinexAuthWSClient
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
from bfx_funding_bot.modules.accounts.vault import (
    AccountCredentialNotConfiguredError,
    VaultKeyMismatchError,
    load_account_credentials,
)
from bfx_funding_bot.modules.admin.trading_status import TradingStatusService
from bfx_funding_bot.modules.candles.gap_fill import fill_gap_from_rest
from bfx_funding_bot.modules.candles.tables import FundingCandleRow
from bfx_funding_bot.modules.execution.audit import AuditContext
from bfx_funding_bot.modules.execution.bus import DomainEventBus
from bfx_funding_bot.modules.execution.capital_policy_control import CapitalPolicyRequestWorker
from bfx_funding_bot.modules.execution.command_gate import AccountCommandGate
from bfx_funding_bot.modules.execution.diagnostics.sink import DiagnosticsSink
from bfx_funding_bot.modules.execution.fill_tracker import (
    RestPollingFillTracker,
)
from bfx_funding_bot.modules.execution.periodic_reconcile import PeriodicReconcile
from bfx_funding_bot.modules.execution.protocols import (
    AccountContext,
    Credentials,
    ExecutorPort,
)
from bfx_funding_bot.modules.execution.safety.boot_stop import report_refused_boot
from bfx_funding_bot.modules.execution.safety.chain import SafetyGuardChain
from bfx_funding_bot.modules.execution.safety.config import (
    SafetyConfig,
    _AllocationCapCfg,
)
from bfx_funding_bot.modules.execution.safety.protection import (
    AutomaticProtection,
    WriterLockWatch,
)
from bfx_funding_bot.modules.execution.trading_control import TradingControlWorker
from bfx_funding_bot.modules.execution.uncertainty_resolution import (
    UncertaintyResolutionWorker,
)
from bfx_funding_bot.modules.execution.ws_dispatcher import BitfinexLiveWSDispatcher
from bfx_funding_bot.modules.ledger import ObservationSink, Scope
from bfx_funding_bot.modules.live_validation.credit_history import CreditHistorySync
from bfx_funding_bot.modules.live_validation.interest_ledger import (
    InterestLedgerSync,
)
from bfx_funding_bot.modules.marketfeed.book_snapshot import BookSnapshotWriter
from bfx_funding_bot.modules.marketfeed.candle_writer import CandleWriter
from bfx_funding_bot.modules.marketfeed.config import MarketfeedConfig
from bfx_funding_bot.modules.marketfeed.funding_book import (
    FundingBookService,
)
from bfx_funding_bot.modules.marketfeed.health_monitor import HealthMonitor
from bfx_funding_bot.modules.marketfeed.healthz import run_healthz_server
from bfx_funding_bot.modules.marketfeed.readiness import TradingReadiness
from bfx_funding_bot.modules.marketfeed.scheduler import (
    Scheduler,
    now_ms_utc,
)
from bfx_funding_bot.modules.marketfeed.signal_engine import SignalEngine
from bfx_funding_bot.modules.marketfeed.strategy_registry import StrategyRegistry
from bfx_funding_bot.modules.observability.metrics import (
    DaemonMetrics,
)
from bfx_funding_bot.modules.observability.stdout_sink import StdoutEventSink
from bfx_funding_bot.modules.observability.tracing import (
    DaemonTracing,
)
from bfx_funding_bot.modules.strategy import CellConfig, DecisionPayload, configured_symbols

if TYPE_CHECKING:
    from bfx_funding_bot.modules.admin.smoke_runner import SmokeRunner

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
    credentials: Credentials
    deployment_environment: str
    allocation_cap_usdt: Decimal
    config_draft: dict[str, object] | None
    config_revision: int | None

    @property
    def account_id(self) -> str:
        """Canonical string used by the pre-contract event APIs."""
        return account_id_canonical(self.exchange_account_id)

    def to_context(self) -> AccountContext:
        return AccountContext(
            account_id=self.account_id,
            credentials=self.credentials,
            allocation_cap_usdt=self.allocation_cap_usdt,
        )

    @staticmethod
    def reject_legacy_realm(*, phase: Phase) -> None:
        """Reject the old process-global realm in live boot modes."""
        legacy = os.environ.get("BFX_ACCOUNT_ID", "").strip()
        executor = os.environ.get("BFX_EXECUTOR", "paper").strip().lower()
        if legacy and (phase is Phase.LIVE or executor == "bitfinex_live"):
            raise ConfigurationError(
                "BFX_ACCOUNT_ID is no longer supported; use "
                "BFX_EXCHANGE_ACCOUNT_ID"
            )


async def load_account_bootstrap(
    session: AsyncSession,
    *,
    deployment_environment: str,
    allocation_cap_usdt: Decimal,
    phase: Phase | None = None,
) -> AccountBootstrap:
    """Resolve one explicit account and its vault/config material at boot.

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
    try:
        kek = load_kek()
        credentials = await load_account_credentials(
            session, exchange_account_id=exchange_account_id, kek=kek
        )
    except AccountCredentialNotConfiguredError as exc:
        raise ConfigurationError(str(exc)) from exc
    except VaultKeyMismatchError as exc:
        raise ConfigurationError(
            f"exchange account {canonical_id} credential vault cannot be opened"
        ) from exc
    except VaultNotConfiguredError as exc:
        raise ConfigurationError("BFX_VAULT_KEK is required for daemon boot") from exc
    except Exception as exc:
        # VaultNotConfiguredError and other crypto/config errors must not leak
        # an implementation-specific traceback through the boot contract.
        if isinstance(exc, (ValueError,)):
            raise ConfigurationError(str(exc)) from exc
        raise

    draft = await load_account_config_draft(
        session, exchange_account_id=exchange_account_id
    )
    return AccountBootstrap(
        exchange_account_id=exchange_account_id,
        credentials=credentials,
        deployment_environment=deployment_environment,
        allocation_cap_usdt=allocation_cap_usdt,
        config_draft=dict(draft.config) if draft is not None else None,
        config_revision=draft.revision if draft is not None else None,
    )


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
    executor: ExecutorPort
    safety_chain: SafetyGuardChain
    account_ctx: AccountContext
    bus: DomainEventBus
    smoke_runner: SmokeRunner | None = None
    fill_tracker: RestPollingFillTracker | None = None
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
    writer_lock_watch: WriterLockWatch | None = None
    # Applies operator resume/kill requests.
    trading_control: TradingControlWorker | None = None
    # Applies operator enable/disable of one currency's policy (its own queue:
    # a kill never waits behind it).
    capital_policy_control: CapitalPolicyRequestWorker | None = None
    _stop_event: asyncio.Event = field(default_factory=asyncio.Event)

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

    async def run(self) -> None:
        """Main entry — sub-task supervision via TaskGroup.

        Phase 4.1 used asyncio.gather + create_task with no propagation,
        producing 4hr zombie when candle_writer silently died. Per spec
        D4: any sub-task raise → TaskGroup cancel all → ExceptionGroup
        propagates → _run handles cleanup + exit non-zero.
        """
        # Initial cell registration (was in startup)
        for cell in self.config.cells:
            self.scheduler.register_from_now(cell)

        # 3a-recovery: reconcile against venue + resolve crash-mid-flight PENDING
        # BEFORE any sub-task starts (live only; paper leaves this None). A venue
        # fetch failure raises here -> daemon fails to start (fail-safe).
        await self._run_boot_recovery()
        self.booted = True

        async with asyncio.TaskGroup() as tg:
            if self.protection is not None:
                tg.create_task(self.protection.run(self._stop_event), name="automatic_protection")
            if self.trading_control is not None:
                tg.create_task(self.trading_control.run(self._stop_event), name="trading_control")
            if self.capital_policy_control is not None:
                tg.create_task(self.capital_policy_control.run(self._stop_event),
                               name="capital_policy_control")
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
            # Phase 4.2 Task 20: fill_tracker sub-task only runs when
            # build_executor enabled it (live executor + flag). Paper +
            # tracker is rejected at startup by registry CC4 invariant.
            if self.fill_tracker is not None:
                tg.create_task(
                    self.fill_tracker.poll_loop(self._stop_event),
                    name="fill_tracker",
                )
            # Phase 4.4a Task 19: WS dispatcher sub-task only runs when
            # build_executor enabled it (live executor + BFX_WS_CLIENT_ENABLED).
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
            # liveness task → never drives /healthz 503 / autoheal restart.
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
        from bfx_funding_bot.core.keepalive import keepalive_loop
        await keepalive_loop(
            self.db_engine,
            stop=self._stop_event,
            on_tick=lambda _ts: self.probe.record_heartbeat("db_keepalive"),
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
        """Container-level liveness HTTP endpoint for Koyeb / k8s probes.

        Independent of in-process scan_staleness (which can't catch
        daemon-wide event-loop deadlock — if asyncio is blocked,
        scan_staleness itself doesn't run). External HTTP probe sees no
        response → platform restarts container.
        """
        await run_healthz_server(
            probe=self.probe,
            host=self.healthz_host,
            port=self.healthz_port,
            stop_event=self._stop_event,
            smoke_runner=self.smoke_runner,
            admin_token=self.admin_token,
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
        auth fault won't heal on restart, so wiring it to /healthz 503 / autoheal
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

    async def _ws_heartbeat_poll_loop(self) -> None:
        """Poll ws_client.last_msg_age_ms() and record heartbeat when fresh.

        Bitfinex public WS sends `hb` frames every ~15s on subscribed channels
        when idle (quiet markets). `_handle_raw` in ws.py updates
        state.last_msg_ts on any frame (candle or hb), but candles() yields
        only candle data — so the daemon's main WS loop sees long gaps in
        quiet 1h funding markets even though the connection is alive.

        Solution: poll every 15s and check ws_client.last_msg_age_ms(). If
        < 60s, the connection is alive → record heartbeat for "ws" sub-task,
        and restore BITFINEX_WS state to HEALTHY (Bug B fix 5/20: nothing
        else flips ws→healthy after a disconnect set DEGRADED, so the state
        was sticky and health_monitor kept emitting warn every 5min).
        """
        assert self.ws_client is not None
        while not self._stop_event.is_set():
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=15.0)
                log.info("sub_task_exit name=ws_heartbeat")
                return  # stop requested
            except TimeoutError:
                pass
            # Only record if WS is connected and recently saw any frame
            if self.ws_client is not None and self.ws_client.last_msg_age_ms() < 60_000:
                self.probe.record_heartbeat("ws")
                # Bug B fix: only emit transition (degraded/down → healthy
                # or first-ever set) — don't spam every 15s with new
                # last_msg_age_ms values.
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
                        # ws heartbeat is recorded by _ws_heartbeat_poll_loop based on
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


# 3c: `AxiomReplayQueryAdapter` + `replay_from_axiom()` deleted — boot now uses
# PG from_snapshot (ledger reads position_state; registry reads offer_claims).

# _LedgerWrappedExecutor deleted in Phase 4.3 Task 10.
# Replaced by: HeartbeatMiddleware(ReservationEmittingMiddleware(executor), probe)
# (no retry wrapper — submit is a once-only financial write; see wrapped_executor).
# Ledger + OfferRegistry subscribe to DomainEventBus in build_daemon.


_LIVE_REQUIRED_HARD = ("manual_kill", "auth_health", "heartbeat")


def assert_live_guard_invariant(phase: Phase, safety_cfg: SafetyConfig) -> None:
    """Real money cannot silently disable ownership-independent safety guards.

    Live replaces the allocation/buying-power flags with the mandatory applied
    capital policy and its offer envelope, and still requires the
    trading-state/auth/heartbeat guards. No-op for paper/shadow. NAV drops only
    alert (lending envelope D3): they are not a guard.
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


def assert_caps_invariant(cells: list[CellConfig], alloc_cfg: _AllocationCapCfg) -> None:
    """Simulation: every configured-cell symbol needs an explicit caps entry.

    Config-fatal at boot (raises ValueError) — a configured currency with no
    explicit cap is an operator mistake that must abort startup, not silently
    fall through to default_cap. (Live sizes from the applied CapitalPolicy.)
    """
    for symbol in configured_symbols(cells):
        if symbol not in alloc_cfg.caps:
            raise ValueError(
                f"caps invariant: configured symbol {symbol!r} has no explicit caps entry"
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
