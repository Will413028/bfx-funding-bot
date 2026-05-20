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
import signal
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from bfx_funding_bot.external.axiom import (
    AxiomAuthError,
    AxiomClient,
    AxiomConfig,
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
from bfx_funding_bot.modules.candles.repository import get_up_to
from bfx_funding_bot.modules.candles.schemas import FundingCandle
from bfx_funding_bot.modules.candles.service import reindex_and_ffill
from bfx_funding_bot.modules.candles.tables import FundingCandleRow
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
from bfx_funding_bot.modules.marketfeed.scheduler import (
    Scheduler,
    now_ms_utc,
)
from bfx_funding_bot.modules.marketfeed.schemas import (
    EventType,
    HealthStatus,
    HealthTarget,
    Level,
)
from bfx_funding_bot.modules.marketfeed.signal_engine import SignalEngine
from bfx_funding_bot.modules.marketfeed.strategy_registry import StrategyRegistry
from bfx_funding_bot.modules.marketfeed.warmup import warmup_cell

log = logging.getLogger(__name__)

_BITFINEX_REST_BASE_URL = "https://api-pub.bitfinex.com"


@dataclass
class Daemon:
    config: MarketfeedConfig
    registry: StrategyRegistry
    candle_q: asyncio.Queue[CandleMessage | None]
    axiom: AxiomClient
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

        async with asyncio.TaskGroup() as tg:
            tg.create_task(self._candle_writer_loop(), name="candle_writer")
            tg.create_task(self._scheduler_loop(),     name="scheduler")
            tg.create_task(self._axiom_loop(),         name="axiom")
            tg.create_task(self._monitor_loop(),       name="monitor")
            tg.create_task(self._heartbeat_scan_loop(), name="health_check")
            tg.create_task(self._db_keepalive_loop(),  name="db_keepalive")
            if self.ws_client is not None:
                tg.create_task(self._ws_consume_with_reconnect(), name="ws")
                tg.create_task(self._ws_heartbeat_poll_loop(), name="ws_heartbeat")
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

    async def _axiom_loop(self) -> None:
        """Run axiom's background emit loop.

        Bug B fix (5/20): AxiomConfig.on_flush is wired in build_daemon to
        probe.record_heartbeat("axiom"), so scan_staleness can detect axiom
        task hung (threshold 60s in SUB_TASK_THRESHOLDS).
        """
        await self.axiom.start()
        await self._stop_event.wait()
        await self.axiom.stop()

    async def _monitor_loop(self) -> None:
        """State-change emission loop (was HealthMonitor.start() in Phase 4.1).
        Polls probe.drain_dirty() every 50ms + periodic full heartbeat emit."""
        await self.monitor.start()
        await self._stop_event.wait()
        await self.monitor.stop()

    async def _heartbeat_scan_loop(self) -> None:
        """Periodic staleness scan. Tick every 30s. FatalError → TaskGroup cancel."""
        while not self._stop_event.is_set():
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=30.0)
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
                async for msg in self.ws_client.candles():
                    await self.candle_q.put(msg)
                    # successful message → connection is alive; reset failure counter
                    consecutive_failures = 0
                    self.ws_client.maybe_reset_backoff()
                    # ws heartbeat is recorded by _ws_heartbeat_poll_loop based on
                    # ws_client.last_msg_age_ms() (which counts both candle frames
                    # and Bitfinex `hb` frames), not here — yielded candles are
                    # sparse on 1h cells.
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("ws_recv_error_will_reconnect")

            if self._stop_event.is_set():
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


async def build_daemon(
    *,
    cells_yaml_path: Path | None = None,
    skip_ws: bool = False,
) -> Daemon:
    config = load_config(cells_yaml_path=cells_yaml_path)
    db_engine = create_async_engine(config.database_url)
    session_factory = async_sessionmaker(db_engine, expire_on_commit=False)

    probe = HealthProbe()
    axiom = AxiomClient(
        AxiomConfig(
            api_key=config.axiom_api_key,
            dataset=config.axiom_dataset,
            # Bug B fix (5/20): wire axiom flush → heartbeat so scan_staleness
            # can detect axiom task hung (4.2.0 D4 DONE_WITH_CONCERNS).
            on_flush=lambda: probe.record_heartbeat("axiom"),
        ),
    )
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

    signal_engine_obj = SignalEngine(
        phase=config.phase, axiom=axiom, candles_repo=_CandlesRepoBridge(),
    )

    # Per-cell signal-pipeline health state for sticky transition logic.
    # Phase 4.3 LOCF: prevents duplicate DEGRADED emits on consecutive stale ticks.
    _cell_pipeline_status: dict[str, HealthStatus] = {}

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
        lookback = budget_hours + 1  # enough candles to cover one budget window
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

        if not filled:
            # No candles at all in lookback window — treat as hard-tier DEGRADED.
            budget_seconds = budget_hours * 3600
            prev_status = _cell_pipeline_status.get(cell.pair_id)
            if prev_status != HealthStatus.DEGRADED:
                _cell_pipeline_status[cell.pair_id] = HealthStatus.DEGRADED
                await axiom.emit({
                    "timestamp": datetime.now(UTC).isoformat(),
                    "level": Level.WARN.value,
                    "phase": config.phase.value,
                    "strategy": None,
                    "cell": cell.cell_id,
                    "event_type": EventType.HEALTH_CHECK.value,
                    "correlation_id": str(uuid4()),
                    "payload": {
                        "check_target": HealthTarget.SIGNAL_PIPELINE.value,
                        "status": HealthStatus.DEGRADED.value,
                        "error_message": "stale_exceeded: no candles in lookback window",
                        "reason": "stale_exceeded",
                        "stale_seconds": budget_seconds + 3600,
                        "budget_seconds": budget_seconds,
                    },
                })
            return

        latest = filled[-1]

        if latest.candle is None:
            # Hard tier: stale_seconds > budget — skip signal, emit DEGRADED once.
            budget_seconds = budget_hours * 3600
            prev_status = _cell_pipeline_status.get(cell.pair_id)
            if prev_status != HealthStatus.DEGRADED:
                _cell_pipeline_status[cell.pair_id] = HealthStatus.DEGRADED
                await axiom.emit({
                    "timestamp": datetime.now(UTC).isoformat(),
                    "level": Level.WARN.value,
                    "phase": config.phase.value,
                    "strategy": None,
                    "cell": cell.cell_id,
                    "event_type": EventType.HEALTH_CHECK.value,
                    "correlation_id": str(uuid4()),
                    "payload": {
                        "check_target": HealthTarget.SIGNAL_PIPELINE.value,
                        "status": HealthStatus.DEGRADED.value,
                        "error_message": "stale_exceeded",
                        "reason": "stale_exceeded",
                        "stale_seconds": latest.stale_seconds,
                        "budget_seconds": budget_seconds,
                    },
                })
            return

        # Soft tier or fresh — emit signal with staleness metadata.
        prev_status = _cell_pipeline_status.get(cell.pair_id)
        if prev_status == HealthStatus.DEGRADED:
            # HEALTHY restore transition
            _cell_pipeline_status[cell.pair_id] = HealthStatus.HEALTHY
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
            _cell_pipeline_status[cell.pair_id] = HealthStatus.HEALTHY

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

    return Daemon(
        config=config,
        registry=registry,
        candle_q=candle_q,
        axiom=axiom,
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


if __name__ == "__main__":
    main()
