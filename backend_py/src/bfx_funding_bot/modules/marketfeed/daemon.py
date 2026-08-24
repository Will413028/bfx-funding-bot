"""Phase 4.1 paper/shadow daemon coordinator.

設計依據: phase4.1-paper-shadow-infra-design.md Section "Data Flow"
- Startup: load config → warmup all cells → start ws + writer + scheduler + health_monitor
- Steady state: scheduler 觸發 signal_engine.process_candle
- Shutdown: SIGTERM → stop scheduler → drain in-flight → close ws → exit 0
"""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import os
import signal
import sys
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import uuid4

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
)

from bfx_funding_bot.core.db import make_async_engine_from_url
from bfx_funding_bot.core.errors import (
    EXIT_CODE_AUTH_FAILED,
    EXIT_CODE_WRITER_LOCKED,
    ExecutorAuthError,
    WriterLockUnacquired,
)
from bfx_funding_bot.core.writer_lock import WriterLock, derive_lock_key
from bfx_funding_bot.external.bitfinex.auth_rest import BitfinexAuthREST
from bfx_funding_bot.external.bitfinex.auth_ws import BitfinexAuthWSClient
from bfx_funding_bot.external.bitfinex.fill_tracker import (
    RestPollingFillTracker,
)
from bfx_funding_bot.external.bitfinex.funding_book_ws import FundingBookWSClient
from bfx_funding_bot.external.bitfinex.gap_fill import fill_gap_from_rest
from bfx_funding_bot.external.bitfinex.nonce import make_monotonic_us_nonce
from bfx_funding_bot.external.bitfinex.rate_limit import FundingRateLimiter
from bfx_funding_bot.external.bitfinex.rest import BitfinexREST
from bfx_funding_bot.external.bitfinex.ws import (
    BitfinexWSClient,
    CandleMessage,
    ChannelSpec,
    compute_backoff_secs,
)
from bfx_funding_bot.external.bitfinex.ws_dispatcher import BitfinexLiveWSDispatcher
from bfx_funding_bot.modules.admin.trading_status import TradingStatusService
from bfx_funding_bot.modules.candles.repository import get_up_to, seal_closed_periods
from bfx_funding_bot.modules.candles.schemas import FundingCandle
from bfx_funding_bot.modules.candles.service import reindex_and_ffill
from bfx_funding_bot.modules.candles.tables import FundingCandleRow
from bfx_funding_bot.modules.execution.audit import AuditContext, ExecutionDecisionRecorder
from bfx_funding_bot.modules.execution.boot_recovery import BootRecovery
from bfx_funding_bot.modules.execution.bus import DomainEventBus
from bfx_funding_bot.modules.execution.contracts import ExecutionPolicy
from bfx_funding_bot.modules.execution.deployment.eligibility import ExecutionGate
from bfx_funding_bot.modules.execution.deployment.ladder import ladder_policy_from_env
from bfx_funding_bot.modules.execution.deployment.period_pricing import PeriodPricer
from bfx_funding_bot.modules.execution.deployment.reconciler import DeploymentReconciler
from bfx_funding_bot.modules.execution.deployment.reprice import policy_from_env
from bfx_funding_bot.modules.execution.deployment.standing_quote import StandingQuoteStore
from bfx_funding_bot.modules.execution.deployment.submit_attempt import (
    SubmitAttemptRecorder,
)
from bfx_funding_bot.modules.execution.deployment.tracker import CellDeploymentTracker
from bfx_funding_bot.modules.execution.diagnostics.sink import DiagnosticsSink
from bfx_funding_bot.modules.execution.event_store.persister import EventStorePersister
from bfx_funding_bot.modules.execution.event_store.store import PostgresEventStore
from bfx_funding_bot.modules.execution.events import (
    CancelAcknowledged,
    CancelRequested,
    CreditClosed,
    OrderFilled,
    PositionReconciled,
    ReservationClaimed,
    ReservationReleased,
)
from bfx_funding_bot.modules.execution.ledger import PaperPositionLedger
from bfx_funding_bot.modules.execution.middleware import (
    HeartbeatMiddleware,
    ReservationEmittingMiddleware,
)
from bfx_funding_bot.modules.execution.periodic_reconcile import PeriodicReconcile
from bfx_funding_bot.modules.execution.protocols import (
    AccountContext,
    CancelPort,
    Credentials,
    ExecutorPort,
    GuardRule,
)
from bfx_funding_bot.modules.execution.registry import build_executor
from bfx_funding_bot.modules.execution.registry_offers import OfferRegistry
from bfx_funding_bot.modules.execution.safety.calibrated_guards import (
    DivergenceRateGuard,
    DrawdownGuard,
    RealizedLossGuard,
)
from bfx_funding_bot.modules.execution.safety.chain import SafetyGuardChain
from bfx_funding_bot.modules.execution.safety.config import (
    SafetyConfig,
    _AllocationCapCfg,
    load_safety_config,
)
from bfx_funding_bot.modules.execution.safety.halt_state import HaltStateStore
from bfx_funding_bot.modules.execution.safety.hard_guards import (
    AllocationCapGuard,
    AuthHealthGuard,
    BuyingPowerGuard,
    HeartbeatGuard,
    ManualKillGuard,
    WriterLockGuard,
)
from bfx_funding_bot.modules.execution.safety.nav_peak_store import NavPeakStore
from bfx_funding_bot.modules.execution.safety.nav_pnl_source import ReconcileNavTracker
from bfx_funding_bot.modules.live_validation.regime import record_config_regime
from bfx_funding_bot.modules.marketfeed.book_snapshot import BookSnapshotWriter
from bfx_funding_bot.modules.marketfeed.candle_writer import CandleWriter
from bfx_funding_bot.modules.marketfeed.config import (
    CellConfig,
    MarketfeedConfig,
    configured_symbols,
    load_config,
)
from bfx_funding_bot.modules.marketfeed.funding_book import (
    FundingBookService,
    FundingBookStore,
)
from bfx_funding_bot.modules.marketfeed.health_monitor import (
    HealthMonitor,
    HealthProbe,
    assess_auth_ws_health,
)
from bfx_funding_bot.modules.marketfeed.healthz import run_healthz_server
from bfx_funding_bot.modules.marketfeed.readiness import TradingReadiness
from bfx_funding_bot.modules.marketfeed.scheduler import (
    Scheduler,
    now_ms_utc,
)
from bfx_funding_bot.modules.marketfeed.schemas import (
    DecisionPayload,
    EventType,
    HealthStatus,
    HealthTarget,
    Level,
    Phase,
)
from bfx_funding_bot.modules.marketfeed.signal_engine import SignalEngine
from bfx_funding_bot.modules.marketfeed.strategy_registry import StrategyRegistry
from bfx_funding_bot.modules.marketfeed.warmup import warmup_cell
from bfx_funding_bot.modules.observability.metrics import (
    DaemonMetrics,
    MetricsSubmitMiddleware,
    TimedReconcileRecovery,
    attach_httpx_metrics,
    install_log_metrics_handler,
)
from bfx_funding_bot.modules.observability.resource import EventResource
from bfx_funding_bot.modules.observability.stdout_sink import StdoutEventSink
from bfx_funding_bot.modules.observability.tracing import (
    DaemonTracing,
    TracedReconcileRecovery,
    TracingSubmitMiddleware,
    instrument_ws_dispatcher,
    tracing_from_env,
)

if TYPE_CHECKING:
    from bfx_funding_bot.modules.admin.smoke_runner import SmokeRunner

log = logging.getLogger(__name__)

_BITFINEX_REST_BASE_URL = "https://api-pub.bitfinex.com"


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
    """Return non-empty env var or raise ValueError with the var name.

    Matches the pattern in `marketfeed/config.py:load_config` so missing env
    surfaces as `config_fatal <name> env var required — exit 1` via main()'s
    ValueError handler, instead of a bare KeyError traceback.
    """
    val = os.environ.get(name)
    if not val:
        raise ValueError(f"{name} env var required")
    return val


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
    executor: ExecutorPort
    safety_chain: SafetyGuardChain
    account_ctx: AccountContext
    ledger: PaperPositionLedger
    bus: DomainEventBus
    smoke_runner: SmokeRunner | None = None
    fill_tracker: RestPollingFillTracker | None = None
    offer_registry: OfferRegistry | None = None
    auth_ws: BitfinexAuthWSClient | None = None
    ws_dispatcher: BitfinexLiveWSDispatcher | None = None
    boot_recovery: BootRecovery | None = None
    periodic_reconcile: PeriodicReconcile | None = None
    book_snapshot_writer: BookSnapshotWriter | None = None
    funding_book_service: FundingBookService | None = None
    healthz_host: str = "0.0.0.0"
    healthz_port: int = 8080
    admin_token: str | None = None
    # Behaviour-reporting service behind /admin/trading-status + /admin/dry-evaluate.
    trading_status: TradingStatusService | None = None
    trading_readiness: TradingReadiness | None = None
    # Single-writer advisory lock — live+Postgres only; None on sim/sqlite.
    writer_lock: WriterLock | None = None
    # Four Golden Signals registry — served at /metrics on the healthz server.
    metrics: DaemonMetrics | None = None
    # OTel traces (wiki pending #4) — default-off (BFX_OTEL_ENABLED), fail-open.
    tracing: DaemonTracing | None = None
    _stop_event: asyncio.Event = field(default_factory=asyncio.Event)

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
        if self.boot_recovery is not None:
            await self.boot_recovery.run()

        async with asyncio.TaskGroup() as tg:
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
        submit is blocked — safety is preserved without restarting. Tying this
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
            if await self.writer_lock.refresh():
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


class _StubDivergenceSource:
    """4.2 stub — disabled DivergenceRateGuard never reaches this.
    4.4 wires a real source backed by `signal_divergence_warn` event counts."""

    def divergence_rate_pct(self, window_minutes: int) -> float:
        return 0.0


_CANARY_REQUIRED_HARD = ("manual_kill", "auth_health", "heartbeat", "allocation_cap")
_CANARY_REQUIRED_CALIBRATED = ("realized_loss_24h", "drawdown_from_peak")


def assert_canary_guard_invariant(phase: Phase, safety_cfg: SafetyConfig) -> None:
    """Canary (real money) must not run with a safety guard silently off.

    Requires the 4 hard guards + the 2 loss-limiters enabled; raises a
    config-fatal ValueError (propagates to non-zero startup exit) otherwise.
    No-op for paper/shadow. divergence_rate stays optional.
    """
    if phase != Phase.CANARY:
        return
    missing = [
        name for name in _CANARY_REQUIRED_HARD
        if not getattr(safety_cfg.hard_guards, name).enabled
    ]
    missing += [
        name for name in _CANARY_REQUIRED_CALIBRATED
        if not getattr(safety_cfg.calibrated_guards, name).enabled
    ]
    if missing:
        raise ValueError(
            f"BFX_PHASE=canary requires all safety guards enabled; disabled: {missing}"
        )


def assert_caps_invariant(
    phase: Phase, cells: list[CellConfig], alloc_cfg: _AllocationCapCfg
) -> None:
    """Every configured-cell symbol needs an explicit caps entry; >0 under canary.

    Config-fatal at boot (raises ValueError) — a configured currency with no
    explicit cap (or a zero cap under real money) is an operator mistake that
    must abort startup, not silently fall through to default_cap.
    """
    for symbol in configured_symbols(cells):
        if symbol not in alloc_cfg.caps:
            raise ValueError(
                f"caps invariant: configured symbol {symbol!r} has no explicit caps entry"
            )
        if phase == Phase.CANARY and alloc_cfg.caps[symbol] <= 0:
            raise ValueError(
                f"caps invariant: canary symbol {symbol!r} cap must be > 0, "
                f"got {alloc_cfg.caps[symbol]}"
            )


async def build_daemon(
    *,
    cells_yaml_path: Path | None = None,
    skip_ws: bool = False,
) -> Daemon:
    config = load_config(cells_yaml_path=cells_yaml_path)
    db_engine = make_async_engine_from_url(config.database_url)
    session_factory = async_sessionmaker(db_engine, expire_on_commit=False)

    probe = HealthProbe()
    # ── Four Golden Signals metrics (observe-only; /metrics on healthz srv) ──
    # Wired FIRST so every sink / client / wrapper below can carry the hook.
    # All recording is fail-open — a metrics fault never touches trading paths.
    metrics = DaemonMetrics()
    metrics.register_probe(probe)  # heartbeat age/threshold + health_status
    install_log_metrics_handler(metrics)  # WARNING+ error-rate, idempotent
    trading_readiness = TradingReadiness(on_change=metrics.set_trading_ready)
    # deployment_environment comes from config (BFX_DEPLOYMENT_ENV via load_config).
    event_resource = EventResource(
        deployment_environment=config.deployment_environment,
    )
    metrics.set_daemon_info(
        service_version=event_resource.service_version,
        deployment_environment=config.deployment_environment.value,
        phase=config.phase.value,
    )
    # OTel traces (wiki pending #4) — DEFAULT OFF via BFX_OTEL_ENABLED. When
    # disabled this is a no-op object (zero SDK init) and NOTHING below gets
    # wrapped, so the money-path call chain is byte-identical to the
    # metrics-era stack. All span recording is fail-open.
    tracing = tracing_from_env(os.environ, event_resource=event_resource)
    if tracing.enabled:
        log.info("otel_tracing_enabled endpoint=%s", tracing.endpoint)
    stdout_sink = StdoutEventSink(resource=event_resource, metrics=metrics)
    bitfinex_http = httpx.AsyncClient()
    # Venue REST traffic/latency/error metrics — additive event hooks on the
    # ONE shared client (public REST + auth REST + live executor + tracker).
    attach_httpx_metrics(bitfinex_http, metrics)
    bitfinex = BitfinexREST(
        http=bitfinex_http,
        base_url=_BITFINEX_REST_BASE_URL,
        limiter=FundingRateLimiter(),
    )
    funding_book_service: FundingBookService | None = None
    if config.execution_policy is not ExecutionPolicy.PAPER:
        if (
            config.book_max_age_seconds is None
            or config.book_reconcile_interval_seconds is None
            or config.book_max_down_pct is None
        ):
            raise ValueError(
                "book-capable execution policy requires FundingBookService configuration",
            )
        funding_book_service = FundingBookService(
            store=FundingBookStore(max_age_seconds=config.book_max_age_seconds),
            rest=bitfinex,
            ws=FundingBookWSClient(symbols=sorted(configured_symbols(config.cells))),
            symbols=sorted(configured_symbols(config.cells)),
            reconcile_interval_seconds=config.book_reconcile_interval_seconds,
        )
    registry = StrategyRegistry()
    monitor = HealthMonitor(phase=config.phase, event_sink=stdout_sink, probe=probe)
    candle_q: asyncio.Queue[CandleMessage | None] = asyncio.Queue()

    now_mts = now_ms_utc()
    async with session_factory() as session:
        for cell in config.cells:
            try:
                await warmup_cell(
                    cell=cell,
                    registry=registry,
                    bitfinex=bitfinex,
                    session=session,
                    now_mts=now_mts,
                )
            except Exception:
                log.exception(
                    "warmup_cell_failed cell=%s — skipping", cell.pair_id,
                )
        probe.update(HealthTarget.BITFINEX_REST, HealthStatus.HEALTHY, latency_ms=0)
        probe.update(HealthTarget.DB, HealthStatus.HEALTHY, latency_ms=0)

    class _CandlesRepoBridge:
        async def get_up_to(
            self,
            *,
            symbol: str,
            timeframe: str,
            period_agg: str,
            mts_inclusive: int,
            lookback: int,
        ) -> list[FundingCandle]:
            async with session_factory() as s:
                return await get_up_to(
                    s,
                    symbol=symbol,
                    timeframe=timeframe,
                    period_agg=period_agg,
                    mts_inclusive=mts_inclusive,
                    lookback=lookback,
                )

    # ── Phase 4.2 Task 20 wiring ─────────────────────────────────────────
    # AccountContext: 4.2 single hardcoded account from env. Phase 5+ SaaS
    # extends to per-tenant context loaded from vault.
    account_id = os.environ.get("BFX_ACCOUNT_ID", "default")
    credentials = Credentials(
        api_key=_require_env("BFX_API_KEY"),
        api_secret=_require_env("BFX_API_SECRET"),
    )
    allocation_cap = Decimal(
        os.environ.get("BFX_ALLOCATION_CAP_USDT", "500"),
    )
    account_ctx = AccountContext(
        account_id=account_id,
        credentials=credentials,
        allocation_cap_usdt=allocation_cap,
    )
    # ONE monotonic µs nonce shared by every auth client on this single API key.
    # Bitfinex nonces are per-key across REST *and* WS, so mixed scales /
    # independent time-based providers get "nonce: small" rejections — that is
    # what left the auth WS flapping. See external/bitfinex/nonce.py.
    bfx_nonce = make_monotonic_us_nonce()

    # Phase 4.4c / 3a: PG event-store replaces Axiom replay at boot.
    # from_snapshot reads position_state + offer_claims from Postgres (written
    # synchronously by ReservationEmittingMiddleware in the command txn — A2).
    env_str = config.deployment_environment.value
    event_store = PostgresEventStore(deployment_environment=env_str)
    persister = EventStorePersister(store=event_store, session_factory=session_factory)
    diagnostics = DiagnosticsSink(
        session_factory=session_factory, account_id=account_id,
        deployment_environment=env_str,
        metrics=metrics,  # bfx_diagnostic_events_total (safety trips / decisions)
    )
    async with session_factory() as snap_session:
        ledger = await PaperPositionLedger.from_snapshot(
            snap_session, account_id=account_id, deployment_environment=env_str
        )
        offer_registry = await OfferRegistry.from_snapshot(
            snap_session,
            account_id=account_id,
            deployment_environment=env_str,
            clock=lambda: int(time.time() * 1000),
        )

    # Safety config — immutable for daemon lifetime. Config change = redeploy.
    safety_cfg_path = Path(
        os.environ.get("BFX_SAFETY_CONFIG", "configs/safety.yaml"),
    )
    safety_cfg = load_safety_config(safety_cfg_path)

    # L2 loss-limiter source: account NAV (available + reserved + realized)
    # sampled from each reconcile snapshot — replaces the 0/0 stub so the canary
    # RealizedLossGuard / DrawdownGuard can actually trip. Subscribed to
    # PositionReconciled below (alongside the ledger).
    pnl_source = ReconcileNavTracker(
        account_id=account_id,
        peak_store=NavPeakStore(
            session_factory, account_id=account_id, deployment_environment=env_str,
        ),
    )
    # Seed the all-time peak from nav_peak so drawdown_pct survives restarts
    # (fail-permissive: load errors leave the in-memory-only behavior).
    await pnl_source.load_persisted_peaks()
    div_source = _StubDivergenceSource()

    # Persisted kill switch. Built unconditionally (like NavPeakStore) so every
    # phase can be halted durably. Note the OPPOSITE failure posture to the peak
    # store above: an unreadable halt state blocks submits rather than degrading
    # gracefully — see ManualKillGuard.
    halt_store = HaltStateStore(
        session_factory, account_id=account_id, deployment_environment=env_str,
    )

    # cells[0] used for safety_chain emit envelope (phase/strategy/cell) —
    # 4.2 is single-cell paper / shadow; multi-cell uniform-policy refinement
    # tracked in Phase 4.4. AllocationCap is account-scoped (not per-cell),
    # so the envelope labels are informational only.
    first_cell = config.cells[0]

    # M2: SafetyConfig.<guard>.enabled is honoured at build time — disabled
    # guards are not constructed (cleaner than relying on internal no-op).
    # `BFX_PHASE=canary` is rejected upstream in load_config so allowing
    # operators to disable hard guards in paper/shadow is bounded; 4.4 canary
    # spec will need an additional invariant requiring all hard guards on.
    # Canary (real money) must not boot with a safety guard silently off.
    assert_canary_guard_invariant(config.phase, safety_cfg)
    hg = safety_cfg.hard_guards
    cg = safety_cfg.calibrated_guards
    # Phase 2: every configured currency must have an explicit cap (and >0 under
    # canary) — config-fatal otherwise. Then log the effective cap per symbol so
    # the boot log is the authoritative record of how much real money each
    # currency may deploy.
    assert_caps_invariant(config.phase, config.cells, hg.allocation_cap)
    log.info(
        "effective_cap_per_symbol %s",
        # assert_caps_invariant (above) already proved every configured symbol has
        # an explicit caps entry in ALL phases, so direct indexing can't KeyError.
        {s: hg.allocation_cap.caps[s] for s in configured_symbols(config.cells)},
    )
    # Single available-buffer bound shared by the per-offer BuyingPowerGuard and
    # the cumulative DeploymentReconciler clamp — read once so both consume the
    # same value (no double subtraction).
    balance_buffer_usdt = Decimal(os.environ.get("BFX_BALANCE_BUFFER_USDT", "3"))

    # Executor is built before the guard chain so guard composition can branch on
    # spec.is_simulated (BuyingPowerGuard is live-only — see allocation_cap block).
    # bus is the live executor's construction dependency, so it is created here.
    # Env-driven via registry (CC4 invariant — paper + fill_tracker rejected;
    # bitfinex_live rejected in 4.2; 4.4 enables live path).
    bus = DomainEventBus()
    all_symbols = frozenset(configured_symbols(config.cells))
    spec = build_executor(
        event_sink=stdout_sink,
        phase=config.phase,
        strategy=first_cell.strategy,
        configured_symbols=all_symbols,
        cell=first_cell.cell_id,
        http=bitfinex_http,
        bus=bus,
        nonce_provider=bfx_nonce,
    )

    # Single-writer advisory lock (A1). Construct LIVE-ONLY (not spec.is_simulated)
    # so paper/shadow leave it None and the guard/liveness/release are all inert.
    # ACQUIRE only on Postgres: sqlite wiring tests construct the object but must
    # never touch a real lock; the boot acquire raises WriterLockUnacquired on
    # contention → propagates to main() → sys.exit(EXIT_CODE_WRITER_LOCKED). It is
    # built here (before the guards block + the Daemon return) so the same variable
    # is in scope at both the guard-append and the return.
    writer_lock: WriterLock | None = None
    if not spec.is_simulated:
        writer_lock = WriterLock(
            database_url=config.database_url,
            key=derive_lock_key(account_id, env_str),
        )
        if config.database_url.startswith(("postgres", "postgresql")):
            await writer_lock.acquire()  # raises WriterLockUnacquired on contention

    guards: list[GuardRule] = []
    if hg.manual_kill.enabled:
        guards.append(ManualKillGuard(halt_store=halt_store))
    if hg.auth_health.enabled:
        guards.append(AuthHealthGuard(probe=probe))
    if hg.heartbeat.enabled:
        guards.append(HeartbeatGuard(
            probe=probe,
            threshold_seconds=hg.heartbeat.sub_task_stale_threshold_seconds,
            # Readiness gate: block POST only when our MARKET VIEW is stale.
            # Watch market-data own-loop liveness ("ws"), not the reactive
            # executor/safety_chain — those are bumped only by trading itself,
            # so watching them self-suppresses trades in quiet markets and was
            # part of the 2026-05-26 canary restart loop. ws stays fresh in
            # quiet markets via _ws_heartbeat_poll_loop (Bitfinex hb ~15s).
            watched_sub_tasks=["ws"],
        ))
    if hg.allocation_cap.enabled:
        guards.append(AllocationCapGuard(
            ledger=ledger,
            caps=hg.allocation_cap.caps,
            default_cap=hg.allocation_cap.default_cap,
            # Reuse the already-read scalar (read-once, like BuyingPowerGuard
            # reuses balance_buffer_usdt) rather than re-reading the env var with
            # a different default. assert_caps_invariant guarantees every
            # configured symbol has an explicit caps entry, so this fallback is
            # dead code for real currencies — but keeping it consistent with the
            # reconciler's env-fallback (account_ctx.allocation_cap_usdt, also
            # = allocation_cap) avoids a latent divergence.
            env_fallback_cap=allocation_cap,
        ))
        # BuyingPowerGuard is the physical-funds backstop and is LIVE-ONLY: it
        # reads funding-wallet available (0 until the first live reconcile), so in
        # the simulated path it would block every POST. The chain is inert in sim
        # today only because its sole evaluator (DeploymentReconciler.deploy) is
        # live-only; gate here so that contract is local to the guard rather than
        # an emergent invariant a future sim-path chain evaluation could violate.
        if not spec.is_simulated:
            guards.append(BuyingPowerGuard(
                ledger=ledger,
                buffers=hg.buying_power.buffers,
                default_buffer=hg.buying_power.default_buffer,
                # Legacy global scalar as fallback for any symbol absent from the
                # buffers map. Reuses the same env value the DeploymentReconciler
                # clamp consumes (balance_buffer_usdt), so for the live
                # single-symbol (fUST) case the resolved buffer equals the scalar
                # and no double subtraction occurs.
                env_fallback_buffer=balance_buffer_usdt,
            ))
    if cg.realized_loss_24h.enabled:
        guards.append(RealizedLossGuard(
            enabled=True,
            threshold_pct=cg.realized_loss_24h.threshold_pct,
            source=pnl_source,
        ))
    if cg.drawdown_from_peak.enabled:
        guards.append(DrawdownGuard(
            enabled=True,
            threshold_pct=cg.drawdown_from_peak.threshold_pct,
            source=pnl_source,
        ))
    if cg.divergence_rate.enabled:
        guards.append(DivergenceRateGuard(
            enabled=True,
            threshold_pct=cg.divergence_rate.threshold_pct,
            window_minutes=cg.divergence_rate.window_minutes,
            source=div_source,
        ))
    # Fail-closed single-writer guard — live-only (writer_lock is None on
    # paper/shadow). Authoritative per-submit liveness via verify_held().
    if writer_lock is not None:
        guards.append(WriterLockGuard(lock=writer_lock))

    safety_chain = SafetyGuardChain(
        guards=guards,
        probe=probe,
        diagnostics=diagnostics,
        phase=config.phase,
        strategy=first_cell.strategy,
        cell=first_cell.cell_id,
        account_id=account_id,
    )

    # ---- Phase 4.3/4.4a executor middleware chain wiring ----
    # bus + spec (executor) are built above the guard chain (guard composition
    # branches on spec.is_simulated).
    quote_store = StandingQuoteStore(
        ttl_ms=int(os.environ.get("BFX_QUOTE_TTL_MS", "3900000")),
    )

    executor: ExecutorPort = spec.executor

    # 3a-recovery: live-only venue reconciliation. Paper/shadow have no real
    # venue offers (BFX_FILL_TRACKER/WS gated off) -> boot_recovery stays None
    # and Daemon.run() skips it.
    boot_recovery: BootRecovery | None = None
    periodic_reconcile: PeriodicReconcile | None = None
    book_snapshot_writer: BookSnapshotWriter | None = None
    if not spec.is_simulated:
        auth_rest = BitfinexAuthREST(http=bitfinex_http, nonce_provider=bfx_nonce)
        boot_recovery = BootRecovery(
            store=event_store,
            session_factory=session_factory,
            auth_rest=auth_rest,
            account_ctx=account_ctx,
            deployment_environment=env_str,
            bus=bus,
            offer_registry=offer_registry,
            is_simulated=spec.is_simulated,
            symbols=configured_symbols(config.cells),
        )
        reconcile_interval_s = float(os.environ.get("BFX_RECONCILE_INTERVAL_S", "90"))
        if reconcile_interval_s <= 0:
            raise ValueError(
                f"BFX_RECONCILE_INTERVAL_S must be > 0, got {reconcile_interval_s}"
            )
        resync_min_interval_s = float(os.environ.get("BFX_RESYNC_MIN_INTERVAL_S", "10"))
        if resync_min_interval_s < 0:
            raise ValueError(
                f"BFX_RESYNC_MIN_INTERVAL_S must be >= 0, got {resync_min_interval_s}"
            )
        runtime_recovery = BootRecovery(
            store=event_store,
            session_factory=session_factory,
            auth_rest=auth_rest,
            account_ctx=account_ctx,
            deployment_environment=env_str,
            bus=bus,
            offer_registry=offer_registry,
            is_simulated=spec.is_simulated,
            symbols=configured_symbols(config.cells),
            action_grace_ms=120_000,
        )

    fill_tracker: RestPollingFillTracker | None = None
    if spec.fill_tracker_enabled:
        fill_tracker = RestPollingFillTracker(
            http=bitfinex_http,
            event_sink=stdout_sink,
            probe=probe,
            bus=bus,
            phase=config.phase,
            strategy=first_cell.strategy,
            cell=first_cell.cell_id,
            account_id=account_id,
            registry=offer_registry,
            persister=persister,
        )
    bus.subscribe(ReservationClaimed,  ledger.on_reservation_claimed)
    bus.subscribe(OrderFilled,         ledger.on_order_filled)
    bus.subscribe(ReservationReleased, ledger.on_reservation_released)
    # Phase 4.4a: OfferRegistry projection — stays in sync with event log.
    bus.subscribe(ReservationClaimed,  offer_registry.handle)
    bus.subscribe(OrderFilled,         offer_registry.handle)
    bus.subscribe(ReservationReleased, offer_registry.handle)
    # 3b: cancel lifecycle → diagnostics (CANCEL_AUDIT). Forensic, best-effort.
    bus.subscribe(CancelRequested,     diagnostics.handle_cancel_requested)
    bus.subscribe(CancelAcknowledged,  diagnostics.handle_cancel_acknowledged)
    # Credit-aware reconcile: PositionReconciled is the ledger's sole exposure
    # authority at reconcile time (recovery FSM events are routed to the registry,
    # not the bus — see BootRecovery._route_fsm). Wiring both together is required:
    # subscribing here without the registry routing would double-count orphans.
    bus.subscribe(PositionReconciled, ledger.on_position_reconciled)
    # Same snapshot feeds the L2 loss-limiter source: NAV peak + 24h window drive
    # RealizedLossGuard / DrawdownGuard (no-op stub before this — see #4).
    bus.subscribe(PositionReconciled, pnl_source.on_position_reconciled)
    # Traffic signal: bfx_domain_events_total{event_type} — one fail-open
    # counting handler across all execution domain events (observe-only; a
    # handler failure is already isolated by the bus's per-handler gather).
    domain_event_counter = metrics.domain_event_handler()
    for _domain_event_type in (
        ReservationClaimed, OrderFilled, ReservationReleased,
        CancelRequested, CancelAcknowledged, PositionReconciled, CreditClosed,
    ):
        bus.subscribe(_domain_event_type, domain_event_counter)

    # NO retry wrapper around submit: a funding-offer submit is a financial write
    # that must be attempted exactly once. Bitfinex funding offers have no client
    # cid dedup (only trading orders do), so retrying a submit would risk a real
    # duplicate live offer. Transient failures are recovered by the periodic
    # reconcile, not by re-submitting. (cancel retries internally — it's idempotent.)
    # MetricsSubmitMiddleware is OUTERMOST and observe-only: times the full
    # submit chain (intent persist + venue POST + ack) into
    # bfx_executor_submit_duration_seconds and counts outcomes. It re-raises /
    # returns unchanged, so the HeartbeatMiddleware I1 invariant and the
    # no-retry submit contract below are untouched.
    wrapped_executor: ExecutorPort = MetricsSubmitMiddleware(
        HeartbeatMiddleware(
            ReservationEmittingMiddleware(
                executor,
                bus=bus,
                persister=persister,
                is_simulated=spec.is_simulated,
            ),
            probe=probe,
        ),
        metrics=metrics,
    )
    # OTel span "executor.submit" — enabled-only, stacked OUTSIDE metrics so
    # one span covers the full chain. Transparent: result/exception unchanged.
    if tracing.enabled:
        wrapped_executor = TracingSubmitMiddleware(wrapped_executor, tracing=tracing)

    # Last-submit-attempt slot read by GET /admin/trading-status. Built
    # unconditionally so the endpoint always has a `started_at` to report;
    # only the live reconciler ever writes to it, so on paper/shadow it stays
    # empty — which correctly reads as "this process has submitted nothing".
    attempt_recorder = SubmitAttemptRecorder()

    deployment_reconciler = None
    if not spec.is_simulated:
        if funding_book_service is None:
            raise ValueError(
                "live execution requires a FundingBookService for the configured policy",
            )
        reprice_policy = policy_from_env(os.environ)
        ladder_policy = ladder_policy_from_env(os.environ)
        execution_gate = ExecutionGate(
            policy=config.execution_policy,
            audit=ExecutionDecisionRecorder(session_factory),
            readiness=trading_readiness,
            events=stdout_sink,
            metrics=metrics,
        )
        config_hash = hashlib.sha256(
            json.dumps(config.model_dump(mode="json"), sort_keys=True).encode(),
        ).hexdigest()
        deployment_reconciler = DeploymentReconciler(
            store=quote_store,
            tracker=CellDeploymentTracker(),
            ledger=ledger,
            safety_chain=safety_chain,
            executor=wrapped_executor,
            account_ctx=account_ctx,
            cells=config.cells,
            venue_floor_usd=Decimal(os.environ.get("BFX_VENUE_FLOOR_USD", "150")),
            min_offer_buffer_pct=Decimal(
                os.environ.get("BFX_MIN_OFFER_BUFFER_PCT", "0.02"),
            ),
            concentration_pct=Decimal(os.environ.get("BFX_CONCENTRATION_PCT", "0.70")),
            balance_buffer_usdt=balance_buffer_usdt,
            caps=hg.allocation_cap.caps,
            default_cap=hg.allocation_cap.default_cap,
            buffers=hg.buying_power.buffers,
            default_buffer=hg.buying_power.default_buffer,
            clock=now_ms_utc,
            event_sink=stdout_sink,
            phase=config.phase,
            canceller=executor if isinstance(executor, CancelPort) else None,
            reprice=reprice_policy,
            ladder=ladder_policy,
            attempt_recorder=attempt_recorder,
            book_provider=funding_book_service,
            execution_gate=execution_gate,
            execution_policy=config.execution_policy,
            optimizer_fee_rate=config.optimizer_fee_rate,
            period_pricer=PeriodPricer(
                max_down_pct=Decimal(str(config.book_max_down_pct)),
                tick=Decimal("0.00000001"),
            ),
            audit_context_factory=_DaemonAuditContextFactory(
                account_id=account_id,
                deployment_environment=env_str,
                service_version=event_resource.service_version,
                config_hash=config_hash,
            ),
        )
        # Execution-policy regime telemetry: one row per boot (flags are
        # boot-immutable, so boots are the regime boundaries). Best-effort —
        # record_config_regime never raises.
        await record_config_regime(
            session_factory,
            account_id=account_id,
            deployment_environment=env_str,
            clamp_enabled=False,
            reprice_enabled=reprice_policy.enabled,
            git_sha=os.environ.get("GIT_SHA") or os.environ.get("BFX_SERVICE_VERSION"),
            now_ms=now_ms_utc(),
        )
        # Transparent timing shim (bfx_reconcile_tick_duration_seconds /
        # bfx_reconcile_ticks_total) — PeriodicReconcile's failure handling
        # sees exactly what the raw recovery would produce. When tracing is
        # enabled, the "reconcile.tick" span wrapper stacks OUTSIDE the timer
        # (same window as the histogram); both are observe-only pass-throughs.
        recovery_runner: TimedReconcileRecovery | TracedReconcileRecovery = (
            TimedReconcileRecovery(runtime_recovery, metrics=metrics)
        )
        if tracing.enabled:
            recovery_runner = TracedReconcileRecovery(recovery_runner, tracing=tracing)
        periodic_reconcile = PeriodicReconcile(
            recovery=recovery_runner,
            probe=probe,
            interval_s=reconcile_interval_s,
            min_resync_interval_s=resync_min_interval_s,
            deployment=deployment_reconciler,
        )

    # ---- Phase 4.4 prework: SmokeRunner ----
    from bfx_funding_bot.modules.admin.pg_event_log_query import PostgresEventLogQueryAdapter
    from bfx_funding_bot.modules.admin.smoke_runner import SmokeRunner

    # Simulated-only: the L2/L3 smoke submits a probe order through wrapped_executor
    # with placeholder creds, expecting a simulated "filled". Against a live executor
    # that is a real venue POST with bogus creds (→ 10100 "apikey: digest invalid"),
    # or with real creds a real boot-time order. So wire smoke only when simulated;
    # live (canary) leaves it None → boot smoke skips + HTTP /admin/smoke-test unmounted.
    smoke_runner: SmokeRunner | None = None
    if spec.is_simulated:
        smoke_pg_query = PostgresEventLogQueryAdapter(
            session_factory=session_factory,
            deployment_environment=env_str,
        )
        smoke_runner = SmokeRunner(
            executor=wrapped_executor,
            bus=bus,
            pg_query=smoke_pg_query,
            phase=config.phase,
            strategy=first_cell.strategy,
            cell=first_cell.cell_id,
        )

    # ---- Phase 4.3 replay invariant report ----
    if ledger.replay_floor_hit_count > 0:
        probe.update(
            HealthTarget.LEDGER, HealthStatus.DEGRADED,
            error_message=f"{ledger.replay_floor_hit_count} floor hits during replay",
        )
        await stdout_sink.emit({
            "timestamp": datetime.now(UTC).isoformat(),
            "level": Level.WARN.value,
            "phase": config.phase.value,
            "strategy": None, "cell": None,
            "event_type": EventType.HEALTH_CHECK.value,
            "correlation_id": str(uuid4()),
            "payload": {
                "check_target": HealthTarget.LEDGER.value,
                "status": HealthStatus.DEGRADED.value,
                "error_message": f"replay_floor_hit_count={ledger.replay_floor_hit_count}",
            },
        })

    signal_engine_obj = SignalEngine(
        phase=config.phase,
        event_sink=stdout_sink,
        diagnostics=diagnostics,
        candles_repo=_CandlesRepoBridge(),
        quote_store=quote_store,
        clock=lambda: int(time.time() * 1000),
    )

    async def on_scheduler_tick(cell: CellConfig, mts: int) -> None:
        # Scheduler fires AT the period boundary T (e.g. 11:00 UTC), but
        # Bitfinex's candle at mts=T is the OPEN of period [T, T+timeframe) —
        # only the FIRST tick of the new period creates it, which may not
        # arrive within the 5s buffer. The candle we actually want is the
        # one that JUST CLOSED — by Bitfinex start-of-period convention,
        # that has mts = T - timeframe.
        # Discovered Phase 4.2.0 d90363fa shadow run: 56 degraded events,
        # 0 signal events because query used mts=T (always empty).
        from bfx_funding_bot.modules.marketfeed.scheduler import _TIMEFRAME_MS
        candle_mts = mts - _TIMEFRAME_MS[cell.timeframe]

        # Phase 4.3 LOCF: fetch a lookback window and LOCF-fill gaps.
        # staleness_budget_hours is guaranteed non-None after load_config().
        budget_hours: int = cell.staleness_budget_hours  # type: ignore[assignment]
        # lookback here is for staleness-tier determination only:
        # reindex_and_ffill needs candles within `budget_hours` to decide hard vs soft
        # tier. Strategy state itself lives in the pre-warmed StrategyRegistry which
        # was fed during daemon startup — NOT re-fed from this small window.
        lookback = budget_hours + 1  # +1 to ensure boundary candle is included
        async with session_factory() as s:
            # Seal on elapsed time before reading. CandleWriter only seals when a
            # LATER mts lands, so a slow venue push leaves mts=candle_mts unsealed
            # at exactly the moment we need it — a final-only read then skips it,
            # LOCF fills the slot from the previous candle, and the live observe
            # sequence gains a hole that replay does not have.
            await seal_closed_periods(
                s,
                symbol=cell.symbol,
                timeframe=cell.timeframe,
                period_agg=cell.period_agg,
            )
            await s.commit()
            raw_candles = await get_up_to(
                s,
                symbol=cell.symbol,
                timeframe=cell.timeframe,
                period_agg=cell.period_agg,
                mts_inclusive=candle_mts,
                lookback=lookback,
            )

        filled = reindex_and_ffill(
            raw_candles, ref_mts=candle_mts, max_gap_hours=budget_hours,
        )

        log.info(
            "scheduler_tick cell=%s boundary_mts=%d candle_mts=%d raw=%d filled=%d",
            cell.pair_id, mts, candle_mts, len(raw_candles), len(filled),
        )

        # pair_id = strategy + cell_id; used as state key so two strategies on
        # the same cell are independent SIGNAL_PIPELINE state machines
        # (e.g. RP can be DEGRADED while MR is HEALTHY).
        if not filled:
            # No candles at all in lookback window — treat as hard-tier DEGRADED.
            budget_seconds = budget_hours * 3600
            if probe.get_cell_pipeline_status(cell.pair_id) != HealthStatus.DEGRADED:
                probe.set_cell_pipeline_status(cell.pair_id, HealthStatus.DEGRADED)
                await _emit_locf_degraded(
                    stdout_sink, config, cell,
                    stale_seconds=None,  # no candles → no meaningful age
                    budget_seconds=budget_seconds,
                )
            return

        latest = filled[-1]

        if latest.candle is None:
            # Hard tier: stale_seconds > budget — skip signal, emit DEGRADED once.
            budget_seconds = budget_hours * 3600
            if probe.get_cell_pipeline_status(cell.pair_id) != HealthStatus.DEGRADED:
                probe.set_cell_pipeline_status(cell.pair_id, HealthStatus.DEGRADED)
                await _emit_locf_degraded(
                    stdout_sink, config, cell,
                    stale_seconds=latest.stale_seconds,
                    budget_seconds=budget_seconds,
                )
            return

        # Soft tier or fresh — emit signal with staleness metadata.
        if probe.get_cell_pipeline_status(cell.pair_id) == HealthStatus.DEGRADED:
            # HEALTHY restore transition
            probe.set_cell_pipeline_status(cell.pair_id, HealthStatus.HEALTHY)
            await stdout_sink.emit({
                "timestamp": datetime.now(UTC).isoformat(),
                "level": Level.INFO.value,
                "phase": config.phase.value,
                "strategy": None,
                "cell": cell.cell_id,
                "event_type": EventType.HEALTH_CHECK.value,
                "correlation_id": str(uuid4()),
                "payload": {
                    "check_target": HealthTarget.SIGNAL_PIPELINE.value,
                    "status": HealthStatus.HEALTHY.value,
                },
            })
        else:
            probe.set_cell_pipeline_status(cell.pair_id, HealthStatus.HEALTHY)

        # Unwrap LOCF-filled candles for strategy compute — strategies receive
        # FundingCandle objects with forward-filled rates (LOCF semantic).
        await signal_engine_obj.process_candle(
            cell=cell,
            candle=latest.candle,
            registry=registry,
            is_stale=latest.is_stale,
            stale_seconds=latest.stale_seconds,
        )

    scheduler = Scheduler(
        callback=on_scheduler_tick,
        probe=probe,
        buffer_s=config.scheduler_buffer_s,
    )
    writer = CandleWriter(queue=candle_q, session_factory=session_factory, probe=probe)

    ws_client: BitfinexWSClient | None = None
    if not skip_ws:
        ws_client = BitfinexWSClient(
            channels=[
                ChannelSpec(
                    symbol=c.symbol,
                    timeframe=c.timeframe,
                    period_agg=c.period_agg,
                )
                for c in config.cells
            ],
            on_disconnect=lambda reason: probe.update(
                HealthTarget.BITFINEX_WS,
                HealthStatus.DEGRADED,
                last_msg_age_ms=999_999,
                reconnect_count_last_hour=0,
                error_message=f"ws_disconnect: {reason}",
            ),
        )

    # ---- GET /admin/trading-status + POST /admin/dry-evaluate ----
    # Reports what the guards actually do, not what the config says. The env
    # fallbacks passed here are the SAME scalars the guards and the reconciler
    # resolve against (allocation_cap / balance_buffer_usdt), so the report's
    # "which tier bound this" answer describes the live resolution rather than
    # a second reading of the same files.
    trading_status = TradingStatusService(
        chain=safety_chain,
        ledger=ledger,
        account_ctx=account_ctx,
        cells=config.cells,
        caps=hg.allocation_cap.caps,
        default_cap=hg.allocation_cap.default_cap,
        env_fallback_cap=allocation_cap,
        buffers=hg.buying_power.buffers,
        default_buffer=hg.buying_power.default_buffer,
        env_fallback_buffer=balance_buffer_usdt,
        phase=config.phase,
        attempts=attempt_recorder,
        halt_store=halt_store,
        readiness=trading_readiness,
    )

    healthz_port_env = os.environ.get("BFX_HEALTHZ_PORT", "").strip()
    healthz_port = int(healthz_port_env) if healthz_port_env else 8080
    healthz_host = os.environ.get("BFX_HEALTHZ_HOST", "0.0.0.0").strip() or "0.0.0.0"
    admin_token = os.environ.get("BFX_ADMIN_TOKEN", "").strip() or None

    # Phase 4.4a Task 19: WS dispatcher — only wired when live executor +
    # BFX_WS_CLIENT_ENABLED=true. Paper path: spec.ws_client_enabled=False
    # → these remain None → run() TaskGroup skips the ws_dispatcher task.
    auth_ws: BitfinexAuthWSClient | None = None
    ws_dispatcher: BitfinexLiveWSDispatcher | None = None
    if spec.ws_client_enabled:
        creds = Credentials(
            api_key=_require_env("BFX_API_KEY"),
            api_secret=_require_env("BFX_API_SECRET"),
        )
        auth_ws = BitfinexAuthWSClient(
            creds=creds,
            nonce_provider=bfx_nonce,
            on_resync_needed=(
                periodic_reconcile.request_resync
                if periodic_reconcile is not None
                else None
            ),
        )
        ws_dispatcher = BitfinexLiveWSDispatcher(
            ws_client=auth_ws,
            registry=offer_registry,
            bus=bus,
            event_sink=stdout_sink,
            persister=persister,
            account_id=account_id,
        )
        bus.subscribe(CancelRequested, ws_dispatcher.handle_cancel_requested)
        # Saturation signal: queue depth read live at scrape time (replaces the
        # 30s ws_dispatcher_queue_depth log line as the primary surface).
        _dispatcher_for_gauge = ws_dispatcher
        metrics.bind_ws_dispatcher_queue(
            depth_fn=lambda: _dispatcher_for_gauge.queue_depth,
            capacity=ws_dispatcher.queue_capacity,
        )
        # OTel span "ws_dispatcher.process" — enabled-only in-place wrap of the
        # per-event translate+persist+publish step (fail-open, transparent).
        if tracing.enabled:
            instrument_ws_dispatcher(ws_dispatcher, tracing=tracing)

    # Funding-book depth self-recording (2026-07-19): Bitfinex serves no
    # historical book — book-aware backtests can only use data we record.
    # Additive/fail-open; default off, flag lives in deploy/vm/canary.env.
    if os.environ.get("BFX_BOOK_SNAPSHOT_ENABLED", "").lower() == "true":
        book_snapshot_writer = BookSnapshotWriter(
            rest=bitfinex,
            session_factory=session_factory,
            symbols=sorted(configured_symbols(config.cells)),
            interval_s=int(os.environ.get("BFX_BOOK_SNAPSHOT_INTERVAL_S", "3600")),
        )

    return Daemon(
        config=config,
        registry=registry,
        candle_q=candle_q,
        diagnostics=diagnostics,
        probe=probe,
        monitor=monitor,
        scheduler=scheduler,
        signal_engine=signal_engine_obj,
        db_engine=db_engine,
        ws_client=ws_client,
        writer=writer,
        bitfinex_http=bitfinex_http,
        bitfinex=bitfinex,
        session_factory=session_factory,
        executor=executor,
        safety_chain=safety_chain,
        account_ctx=account_ctx,
        ledger=ledger,
        bus=bus,
        smoke_runner=smoke_runner,
        fill_tracker=fill_tracker,
        offer_registry=offer_registry,
        auth_ws=auth_ws,
        ws_dispatcher=ws_dispatcher,
        boot_recovery=boot_recovery,
        book_snapshot_writer=book_snapshot_writer,
        funding_book_service=funding_book_service,
        periodic_reconcile=periodic_reconcile,
        healthz_host=healthz_host,
        healthz_port=healthz_port,
        admin_token=admin_token,
        trading_status=trading_status,
        trading_readiness=trading_readiness,
        writer_lock=writer_lock,
        metrics=metrics,
        tracing=tracing,
    )


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    try:
        asyncio.run(_run())
    except WriterLockUnacquired as exc:
        # Another live writer holds the Postgres advisory lock (boot acquire in
        # build_daemon). Fail fast with EX_TEMPFAIL so the platform staggers a
        # retry instead of two processes contending on real-money execution.
        log.critical(
            "writer_lock_unacquired %s — sys.exit(EXIT_CODE_WRITER_LOCKED=75)",
            exc,
        )
        sys.exit(EXIT_CODE_WRITER_LOCKED)
    except ValueError as exc:
        log.error("config_fatal %s — exit 1", exc)
        sys.exit(1)


async def _run() -> None:
    daemon = await build_daemon()

    # Phase 4.4 prework: boot smoke (pre-TaskGroup) — see daemon_smoke_boot.py
    from bfx_funding_bot.modules.marketfeed.daemon_smoke_boot import run_boot_smoke
    await run_boot_smoke(daemon)

    stop = daemon._stop_event  # share with signal handler
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)

    log.info(
        "daemon_started phase=%s cells=%d",
        daemon.config.phase, len(daemon.config.cells),
    )

    # Optional duration cap (BFX_RUN_DURATION_HOURS); shadow has no duration.
    duration = daemon.config.run_duration_hours
    if duration is not None:
        async def _duration_timer() -> None:
            try:
                await asyncio.wait_for(stop.wait(), timeout=duration * 3600)
            except TimeoutError:
                log.info("daemon_run_duration_reached hours=%d", duration)
                stop.set()
        _timer_task = asyncio.create_task(_duration_timer())  # noqa: RUF006

    try:
        await daemon.run()
        log.info("daemon_run_clean_exit")
    except* asyncio.CancelledError:
        log.info("daemon_cancelled_via_signal")
    except* ExecutorAuthError:
        # Auth failure means credentials are wrong / revoked — operator must
        # intervene. Avoid auto-retry loop (Google SRE Book ch. 22 — auth
        # crash-loop-backoff via sysexits EX_CONFIG 78 lets Koyeb stagger
        # restarts instead of tight crash-on-boot retries.)
        log.critical(
            "executor_auth_failed — sys.exit(EXIT_CODE_AUTH_FAILED=78)",
        )
        sys.exit(EXIT_CODE_AUTH_FAILED)
    except* Exception as eg:
        log.error(
            "daemon_taskgroup_fatal exceptions=%s",
            [type(e).__name__ for e in eg.exceptions],
        )
        raise
    finally:
        # Cleanup after TaskGroup completes (close http client)
        log.info("daemon_shutdown_complete")
        # Release the single-writer advisory lock so the next process can acquire
        # it without waiting for the server-side session to expire (live+PG only).
        if daemon.writer_lock is not None:
            with contextlib.suppress(Exception):
                await daemon.writer_lock.release()
        await daemon.bitfinex_http.aclose()
        # SmokeRunner.aclose() is a no-op for PostgresEventLogQueryAdapter (no owned client);
        # kept for forward-compatibility with adapters that may hold resources.
        if daemon.smoke_runner is not None:
            with contextlib.suppress(Exception):
                await daemon.smoke_runner.aclose()
        # Flush any batched spans before exit (no-op when tracing disabled).
        if daemon.tracing is not None:
            with contextlib.suppress(Exception):
                daemon.tracing.shutdown()



if __name__ == "__main__":
    main()
