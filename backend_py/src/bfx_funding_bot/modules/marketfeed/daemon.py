"""Phase 4.1 paper/shadow daemon coordinator.

設計依據: phase4.1-paper-shadow-infra-design.md Section "Data Flow"
- Startup: load config → warmup all cells → start ws + writer + scheduler + health_monitor
- Steady state: scheduler 觸發 signal_engine.process_candle
- Shutdown: SIGTERM → stop scheduler → drain in-flight → flush axiom → close ws → exit 0
"""
from __future__ import annotations

import asyncio
import contextlib
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
from bfx_funding_bot.core.errors import EXIT_CODE_AUTH_FAILED, ExecutorAuthError
from bfx_funding_bot.external.axiom import (
    AxiomAuthError,
    AxiomClient,
    AxiomConfig,
)
from bfx_funding_bot.external.bitfinex.auth_rest import BitfinexAuthREST
from bfx_funding_bot.external.bitfinex.auth_ws import BitfinexAuthWSClient
from bfx_funding_bot.external.bitfinex.fill_tracker import (
    RestPollingFillTracker,
)
from bfx_funding_bot.external.bitfinex.gap_fill import fill_gap_from_rest
from bfx_funding_bot.external.bitfinex.rate_limit import FundingRateLimiter
from bfx_funding_bot.external.bitfinex.rest import BitfinexREST
from bfx_funding_bot.external.bitfinex.ws import (
    BitfinexWSClient,
    CandleMessage,
    ChannelSpec,
    compute_backoff_secs,
)
from bfx_funding_bot.external.bitfinex.ws_dispatcher import BitfinexLiveWSDispatcher
from bfx_funding_bot.modules.candles.repository import get_up_to
from bfx_funding_bot.modules.candles.schemas import FundingCandle
from bfx_funding_bot.modules.candles.service import reindex_and_ffill
from bfx_funding_bot.modules.candles.tables import FundingCandleRow
from bfx_funding_bot.modules.execution.axiom_sink import AxiomEventSink
from bfx_funding_bot.modules.execution.boot_recovery import BootRecovery
from bfx_funding_bot.modules.execution.bus import DomainEventBus
from bfx_funding_bot.modules.execution.diagnostics.sink import DiagnosticsSink
from bfx_funding_bot.modules.execution.event_store.persister import EventStorePersister
from bfx_funding_bot.modules.execution.event_store.store import PostgresEventStore
from bfx_funding_bot.modules.execution.events import (
    CancelAcknowledged,
    CancelRequested,
    OrderFilled,
    ReservationClaimed,
    ReservationReleased,
)
from bfx_funding_bot.modules.execution.ledger import PaperPositionLedger
from bfx_funding_bot.modules.execution.middleware import (
    HeartbeatMiddleware,
    ReservationEmittingMiddleware,
    TransientRetryMiddleware,
)
from bfx_funding_bot.modules.execution.protocols import (
    AccountContext,
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
from bfx_funding_bot.modules.execution.safety.config import load_safety_config
from bfx_funding_bot.modules.execution.safety.hard_guards import (
    AllocationCapGuard,
    AuthHealthGuard,
    HeartbeatGuard,
    ManualKillGuard,
)
from bfx_funding_bot.modules.marketfeed.candle_writer import CandleWriter
from bfx_funding_bot.modules.marketfeed.config import (
    CellConfig,
    MarketfeedConfig,
    load_config,
)
from bfx_funding_bot.modules.marketfeed.health_monitor import (
    HealthMonitor,
    HealthProbe,
)
from bfx_funding_bot.modules.marketfeed.healthz import run_healthz_server
from bfx_funding_bot.modules.marketfeed.scheduler import (
    Scheduler,
    now_ms_utc,
)
from bfx_funding_bot.modules.marketfeed.schemas import (
    EventType,
    HealthStatus,
    HealthTarget,
    Level,
    StrategyName,
)
from bfx_funding_bot.modules.marketfeed.self_smoke import maybe_run_self_smoke
from bfx_funding_bot.modules.marketfeed.signal_engine import SignalEngine
from bfx_funding_bot.modules.marketfeed.strategy_registry import StrategyRegistry
from bfx_funding_bot.modules.marketfeed.warmup import warmup_cell
from bfx_funding_bot.modules.observability.resource import EventResource
from bfx_funding_bot.smoke.g1 import run_smoke_async

if TYPE_CHECKING:
    from bfx_funding_bot.modules.admin.smoke_runner import SmokeRunner

log = logging.getLogger(__name__)

_BITFINEX_REST_BASE_URL = "https://api-pub.bitfinex.com"


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
    axiom: AxiomClient
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
    smoke_runner: SmokeRunner | None = None
    fill_tracker: RestPollingFillTracker | None = None
    offer_registry: OfferRegistry | None = None
    auth_ws: BitfinexAuthWSClient | None = None
    ws_dispatcher: BitfinexLiveWSDispatcher | None = None
    boot_recovery: BootRecovery | None = None
    healthz_host: str = "0.0.0.0"
    healthz_port: int = 8080
    admin_token: str | None = None
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
            tg.create_task(self._axiom_loop(),         name="axiom")
            tg.create_task(self._monitor_loop(),       name="monitor")
            tg.create_task(self._heartbeat_scan_loop(), name="health_check")
            tg.create_task(self._db_keepalive_loop(),  name="db_keepalive")
            tg.create_task(self._healthz_server_loop(), name="healthz")
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

    async def _axiom_loop(self) -> None:
        """Run axiom's background emit loop.

        Bug B fix (5/20): AxiomConfig.on_flush is wired in build_daemon to
        probe.record_heartbeat("axiom"), so scan_staleness can detect axiom
        task hung (threshold 60s in SUB_TASK_THRESHOLDS).
        """
        await self.axiom.start()
        await self._stop_event.wait()
        await self.axiom.stop()
        log.info("sub_task_exit name=axiom")

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
        )
        log.info("sub_task_exit name=healthz")

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


def _resolve_event_replay_days() -> int:
    """Resolve event replay window days with backward-compat fallback.

    Preferred: BFX_EVENT_REPLAY_DAYS (Phase 4.4b D4)
    Fallback: BFX_LEDGER_REPLAY_DAYS (pre-4.4b — transition for 1 deploy cycle)
    Default: 30
    """
    return int(
        os.environ.get("BFX_EVENT_REPLAY_DAYS")
        or os.environ.get("BFX_LEDGER_REPLAY_DAYS", "30"),
    )


async def _emit_locf_degraded(
    axiom: AxiomClient,
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
    await axiom.emit({
        "timestamp": datetime.now(UTC).isoformat(),
        "level": Level.WARN.value,
        "phase": config.phase.value,
        "strategy": None,
        "cell": cell.cell_id,
        "event_type": EventType.HEALTH_CHECK.value,
        "correlation_id": str(uuid4()),
        "payload": payload,
    })


# Phase 4.4b D1: `_AxiomQueryAdapter` (4.3 ledger stub returning []) and
# `_OfferRegistryQueryStub` (4.4a registry stub returning []) deleted. Both
# replay paths now go through `AxiomReplayQueryAdapter` (axiom_event_query.py),
# a single instance that satisfies both consumer protocols via duck typing.

# _LedgerWrappedExecutor deleted in Phase 4.3 Task 10.
# Replaced by: HeartbeatMiddleware(ReservationEmittingMiddleware(TransientRetryMiddleware(executor), bus), probe)
# Ledger + AxiomEventSink subscribe to DomainEventBus in build_daemon.


class _StubPnLSource:
    """4.2 stub — disabled L2 guards never reach this (enabled=False short-circuits).
    4.4 wires real PnLLedger that aggregates realized P&L from order_fill events.
    """

    def realized_loss_24h(self) -> Decimal:
        return Decimal("0")

    def drawdown_pct(self) -> float:
        return 0.0


class _StubDivergenceSource:
    """4.2 stub — disabled DivergenceRateGuard never reaches this.
    4.4 wires a real source backed by `signal_divergence_warn` event counts."""

    def divergence_rate_pct(self, window_minutes: int) -> float:
        return 0.0


async def build_daemon(
    *,
    cells_yaml_path: Path | None = None,
    skip_ws: bool = False,
) -> Daemon:
    config = load_config(cells_yaml_path=cells_yaml_path)
    db_engine = make_async_engine_from_url(config.database_url)
    session_factory = async_sessionmaker(db_engine, expire_on_commit=False)

    probe = HealthProbe()
    # from_env() reads the SAME AXIOM_API_KEY/AXIOM_DATASET that load_config
    # validated, plus BFX_DEPLOYMENT_ENV. A single axiom_cfg.deployment_env
    # feeds BOTH the emit client (resource envelope) and the replay query
    # adapter below — making emit/query env drift structurally impossible.
    axiom_cfg = AxiomConfig.from_env()
    # on_flush isn't an env-derived field; wire the heartbeat callback after.
    # Bug B fix (5/20): wire axiom flush → heartbeat so scan_staleness can
    # detect axiom task hung (4.2.0 D4 DONE_WITH_CONCERNS).
    axiom_cfg.on_flush = lambda: probe.record_heartbeat("axiom")
    event_resource = EventResource(
        deployment_environment=axiom_cfg.deployment_env,
    )
    axiom = AxiomClient(cfg=axiom_cfg, resource=event_resource)
    bitfinex_http = httpx.AsyncClient()
    bitfinex = BitfinexREST(
        http=bitfinex_http,
        base_url=_BITFINEX_REST_BASE_URL,
        limiter=FundingRateLimiter(),
    )
    registry = StrategyRegistry()
    monitor = HealthMonitor(phase=config.phase, axiom=axiom, probe=probe)
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

    # Phase 4.4c / 3a: PG event-store replaces Axiom replay at boot.
    # from_snapshot reads position_state + offer_claims from Postgres (written
    # synchronously by ReservationEmittingMiddleware in the command txn — A2).
    env_str = axiom_cfg.deployment_env.value
    event_store = PostgresEventStore(deployment_environment=env_str)
    persister = EventStorePersister(store=event_store, session_factory=session_factory)
    diagnostics = DiagnosticsSink(
        session_factory=session_factory, deployment_environment=env_str,
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

    pnl_source = _StubPnLSource()
    div_source = _StubDivergenceSource()

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
    hg = safety_cfg.hard_guards
    cg = safety_cfg.calibrated_guards
    guards: list[GuardRule] = []
    if hg.manual_kill.enabled:
        guards.append(ManualKillGuard())
    if hg.auth_health.enabled:
        guards.append(AuthHealthGuard(probe=probe))
    if hg.heartbeat.enabled:
        guards.append(HeartbeatGuard(
            probe=probe,
            threshold_seconds=hg.heartbeat.sub_task_stale_threshold_seconds,
            watched_sub_tasks=["safety_chain", "executor"],
        ))
    if hg.allocation_cap.enabled:
        guards.append(AllocationCapGuard(ledger=ledger))
    if cg.realized_loss_24h.enabled:
        guards.append(RealizedLossGuard(
            enabled=True,
            threshold_usdt=cg.realized_loss_24h.threshold_usdt,
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

    safety_chain = SafetyGuardChain(
        guards=guards,
        probe=probe,
        axiom=axiom,
        phase=config.phase,
        strategy=first_cell.strategy,
        cell=first_cell.cell_id,
        account_id=account_id,
    )

    # ---- Phase 4.3/4.4a executor middleware chain wiring ----
    # bus created before build_executor so live executor gets it at construction.
    bus = DomainEventBus()

    # Executor: env-driven via registry (CC4 invariant — paper + fill_tracker
    # rejected; bitfinex_live rejected in 4.2; 4.4 enables live path).
    spec = build_executor(
        axiom=axiom,
        phase=config.phase,
        strategy=first_cell.strategy,
        cell=first_cell.cell_id,
        http=bitfinex_http,
        bus=bus,
    )
    executor: ExecutorPort = spec.executor

    # 3a-recovery: live-only venue reconciliation. Paper/shadow have no real
    # venue offers (BFX_FILL_TRACKER/WS gated off) -> boot_recovery stays None
    # and Daemon.run() skips it.
    boot_recovery: BootRecovery | None = None
    if not spec.is_simulated:
        auth_rest = BitfinexAuthREST(http=bitfinex_http)
        boot_recovery = BootRecovery(
            store=event_store,
            session_factory=session_factory,
            auth_rest=auth_rest,
            account_ctx=account_ctx,
            deployment_environment=env_str,
            bus=bus,
            is_simulated=spec.is_simulated,
        )

    fill_tracker: RestPollingFillTracker | None = None
    if spec.fill_tracker_enabled:
        fill_tracker = RestPollingFillTracker(
            http=bitfinex_http,
            axiom=axiom,
            probe=probe,
            bus=bus,
            phase=config.phase,
            strategy=first_cell.strategy,
            cell=first_cell.cell_id,
            account_id=account_id,
            registry=offer_registry,
            persister=persister,
        )
    axiom_sink = AxiomEventSink(
        axiom_client=axiom,
        phase=config.phase,
        strategy=StrategyName.RATE_PERCENTILE,  # 4.3 single-strategy; multi-strategy = Phase 5+
        cell="bfx_USDT",                         # 4.3 single-cell wiring
    )
    bus.subscribe(ReservationClaimed,  ledger.on_reservation_claimed)
    bus.subscribe(OrderFilled,         ledger.on_order_filled)
    bus.subscribe(ReservationReleased, ledger.on_reservation_released)
    bus.subscribe(ReservationClaimed,  axiom_sink.on_reservation_claimed)
    bus.subscribe(OrderFilled,         axiom_sink.on_order_filled)
    bus.subscribe(ReservationReleased, axiom_sink.on_reservation_released)
    # Phase 4.4a: OfferRegistry projection — stays in sync with event log.
    bus.subscribe(ReservationClaimed,  offer_registry.handle)
    bus.subscribe(OrderFilled,         offer_registry.handle)
    bus.subscribe(ReservationReleased, offer_registry.handle)
    # 3b: cancel lifecycle → diagnostics (CANCEL_AUDIT). Forensic, best-effort.
    # (axiom_sink keeps its redundant SoT emits until 3c.)
    bus.subscribe(CancelRequested,     diagnostics.handle_cancel_requested)
    bus.subscribe(CancelAcknowledged,  diagnostics.handle_cancel_acknowledged)

    wrapped_executor = HeartbeatMiddleware(
        ReservationEmittingMiddleware(
            TransientRetryMiddleware(executor),
            bus=bus,
            persister=persister,
            is_simulated=spec.is_simulated,
        ),
        probe=probe,
    )

    # ---- Phase 4.4 prework: SmokeRunner ----
    from bfx_funding_bot.modules.admin.axiom_query import AxiomSmokeQueryAdapter
    from bfx_funding_bot.modules.admin.smoke_runner import SmokeRunner

    smoke_axiom_query = AxiomSmokeQueryAdapter(
        api_key=config.axiom_api_key,
        dataset=config.axiom_dataset,
    )
    smoke_runner = SmokeRunner(
        executor=wrapped_executor,
        bus=bus,
        axiom_client=axiom,
        axiom_query=smoke_axiom_query,
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
        await axiom.emit({
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
        axiom=axiom,
        diagnostics=diagnostics,
        candles_repo=_CandlesRepoBridge(),
        safety_chain=safety_chain,
        executor=wrapped_executor,
        account_ctx=account_ctx,
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
                    axiom, config, cell,
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
                    axiom, config, cell,
                    stale_seconds=latest.stale_seconds,
                    budget_seconds=budget_seconds,
                )
            return

        # Soft tier or fresh — emit signal with staleness metadata.
        if probe.get_cell_pipeline_status(cell.pair_id) == HealthStatus.DEGRADED:
            # HEALTHY restore transition
            probe.set_cell_pipeline_status(cell.pair_id, HealthStatus.HEALTHY)
            await axiom.emit({
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
        auth_ws = BitfinexAuthWSClient(creds=creds)
        ws_dispatcher = BitfinexLiveWSDispatcher(
            ws_client=auth_ws,
            registry=offer_registry,
            bus=bus,
            axiom=axiom,
            persister=persister,
        )
        bus.subscribe(CancelRequested, ws_dispatcher.handle_cancel_requested)

    return Daemon(
        config=config,
        registry=registry,
        candle_q=candle_q,
        axiom=axiom,
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
        smoke_runner=smoke_runner,
        fill_tracker=fill_tracker,
        offer_registry=offer_registry,
        auth_ws=auth_ws,
        ws_dispatcher=ws_dispatcher,
        boot_recovery=boot_recovery,
        healthz_host=healthz_host,
        healthz_port=healthz_port,
        admin_token=admin_token,
    )


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    try:
        asyncio.run(_run())
    except AxiomAuthError as exc:
        log.error("axiom_auth_fail %r — exit 1", exc)
        sys.exit(1)
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

    # Optional duration cap (used by paper smoke mode); shadow has no duration.
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
        # restarts instead of tight crash-on-boot retries.) Flush axiom so
        # the safety_trigger event survives the exit.
        log.critical(
            "executor_auth_failed — sys.exit(EXIT_CODE_AUTH_FAILED=78)",
        )
        with contextlib.suppress(Exception):
            await daemon.axiom.flush()
        sys.exit(EXIT_CODE_AUTH_FAILED)
    except* Exception as eg:
        log.error(
            "daemon_taskgroup_fatal exceptions=%s",
            [type(e).__name__ for e in eg.exceptions],
        )
        raise
    finally:
        # Cleanup after TaskGroup completes (flush axiom, close http client)
        log.info("daemon_shutdown_complete")
        await daemon.bitfinex_http.aclose()
        # SmokeRunner's AxiomSmokeQueryAdapter holds its own httpx.AsyncClient;
        # close it on shutdown to avoid leaking the connection pool.
        if daemon.smoke_runner is not None:
            with contextlib.suppress(Exception):
                await daemon.smoke_runner.aclose()

    # Self-smoke trigger (Phase 4.1.x — see specs/2026-05-21-g1-c1-continuity-redesign-design.md)
    # Gated by phase=paper + duration set; exception path is structurally unreachable here
    # (except* Exception: raise above propagates past finally, skipping this code).
    # CellConfig is Pydantic — convert to dict for the helper's expected list[dict[str, Any]] contract.
    log.info("g1_smoke_starting phase=%s duration=%s", daemon.config.phase.value, duration)
    smoke_exit_code = await maybe_run_self_smoke(
        phase=daemon.config.phase.value,
        duration=duration,
        cells=[c.model_dump() for c in daemon.config.cells],
        smoke_runner=run_smoke_async,
        sleep_fn=asyncio.sleep,
    )
    log.info("g1_smoke_done exit_code=%d", smoke_exit_code)
    if smoke_exit_code != 0:
        sys.exit(smoke_exit_code)


if __name__ == "__main__":
    main()
