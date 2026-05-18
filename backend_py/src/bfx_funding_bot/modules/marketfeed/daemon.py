"""Phase 4.1 paper/shadow daemon coordinator.

設計依據: phase4.1-paper-shadow-infra-design.md Section "Data Flow"
- Startup: load config → warmup all cells → start ws + writer + scheduler + health_monitor
- Steady state: scheduler 觸發 signal_engine.process_candle
- Shutdown: SIGTERM → stop scheduler → drain in-flight → flush axiom → close ws → exit 0
"""
from __future__ import annotations

import asyncio
import logging
import signal
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from bfx_funding_bot.external.axiom import (
    AxiomAuthError,
    AxiomClient,
    AxiomConfig,
)
from bfx_funding_bot.external.bitfinex.rate_limit import FundingRateLimiter
from bfx_funding_bot.external.bitfinex.rest import BitfinexREST
from bfx_funding_bot.external.bitfinex.ws import (
    BitfinexWSClient,
    CandleMessage,
    ChannelSpec,
)
from bfx_funding_bot.modules.candles.repository import (
    get_candles_in_range,
    get_up_to,
)
from bfx_funding_bot.modules.candles.schemas import FundingCandle
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
    HealthStatus,
    HealthTarget,
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
    engine: SignalEngine
    ws_client: BitfinexWSClient | None
    writer: CandleWriter
    bitfinex_http: httpx.AsyncClient
    _tasks: list[asyncio.Task[Any]] = field(default_factory=list)

    async def startup(self) -> None:
        await self.axiom.start()
        await self.monitor.start()
        await self.scheduler.start()
        self._tasks.append(asyncio.create_task(self.writer.run()))
        if self.ws_client is not None:
            self._tasks.append(asyncio.create_task(self._ws_consume()))
        for cell in self.config.cells:
            self.scheduler.register_from_now(cell)

    async def _ws_consume(self) -> None:
        assert self.ws_client is not None
        async for msg in self.ws_client.candles():
            await self.candle_q.put(msg)

    async def shutdown(self) -> None:
        if self.ws_client is not None:
            await self.ws_client.close()
        await self.scheduler.stop()
        await self.candle_q.put(None)
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        await self.monitor.stop()
        await self.axiom.stop()
        await self.bitfinex_http.aclose()


async def build_daemon(
    *,
    cells_yaml_path: Path | None = None,
    skip_ws: bool = False,
) -> Daemon:
    config = load_config(cells_yaml_path=cells_yaml_path)
    db_engine = create_async_engine(config.database_url)
    session_factory = async_sessionmaker(db_engine, expire_on_commit=False)

    axiom = AxiomClient(
        AxiomConfig(api_key=config.axiom_api_key, dataset=config.axiom_dataset),
    )
    bitfinex_http = httpx.AsyncClient()
    bitfinex = BitfinexREST(
        http=bitfinex_http,
        base_url=_BITFINEX_REST_BASE_URL,
        limiter=FundingRateLimiter(),
    )
    registry = StrategyRegistry()
    probe = HealthProbe()
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

    async def on_scheduler_tick(cell: CellConfig, mts: int) -> None:
        async with session_factory() as s:
            rows = await get_candles_in_range(
                s,
                symbol=cell.symbol,
                timeframe=cell.timeframe,
                period_agg=cell.period_agg,
                start_mts=mts,
                end_mts=mts,
            )
        if not rows:
            probe.update(
                HealthTarget.BITFINEX_WS,
                HealthStatus.DEGRADED,
                last_msg_age_ms=999_999,
                reconnect_count_last_hour=0,
                error_message="candle_missing_at_scheduled_observe",
            )
            return
        await signal_engine_obj.process_candle(
            cell=cell, candle=rows[0], registry=registry,
        )

    scheduler = Scheduler(callback=on_scheduler_tick)
    writer = CandleWriter(queue=candle_q, session_factory=session_factory)

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
        engine=signal_engine_obj,
        ws_client=ws_client,
        writer=writer,
        bitfinex_http=bitfinex_http,
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
    stop = asyncio.Event()
    loop = asyncio.get_event_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    await daemon.startup()
    log.info(
        "daemon_started phase=%s cells=%d",
        daemon.config.phase, len(daemon.config.cells),
    )

    duration = daemon.config.run_duration_hours
    if duration is not None:
        try:
            await asyncio.wait_for(stop.wait(), timeout=duration * 3600)
        except TimeoutError:
            log.info("daemon_run_duration_reached hours=%d", duration)
    else:
        await stop.wait()

    log.info("daemon_shutdown_begin")
    await daemon.shutdown()
    log.info("daemon_shutdown_complete")


if __name__ == "__main__":
    main()
