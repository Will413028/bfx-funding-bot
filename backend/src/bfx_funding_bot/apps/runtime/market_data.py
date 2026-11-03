"""Market data recording: the public candle WS (with REST gap-fill on reconnect), the candle
writer, the WS freshness poll and the funding-book snapshot.

It runs in its own TaskGroup beside the account half of the daemon and starts before the
boot waits for the venue or holds in credential recovery mode, so research recording does not
depend on the account's health (ADR 2026-10-19 runtime roles; brief §5.3 R9b). Any of its
tasks failing fails this TaskGroup, and the daemon's outer TaskGroup ends the process
non-zero. R9d moves it into its own process (``apps.marketdata``).
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Sequence
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bfx_funding_bot.apps.runtime.scheduler import now_ms_utc
from bfx_funding_bot.core.errors import FatalError
from bfx_funding_bot.core.health import MARKET_DATA_FRESHNESS, HealthProbe
from bfx_funding_bot.core.telemetry import HealthStatus, HealthTarget
from bfx_funding_bot.external.bitfinex.rest import BitfinexREST
from bfx_funding_bot.external.bitfinex.ws import (
    BitfinexWSClient,
    CandleMessage,
    ChannelSpec,
    compute_backoff_secs,
)
from bfx_funding_bot.modules.candles.gap_fill import fill_gap_from_rest
from bfx_funding_bot.modules.candles.tables import FundingCandleRow
from bfx_funding_bot.modules.execution.readiness import TradingReadiness
from bfx_funding_bot.modules.market._internal.recorders.book_snapshot import BookSnapshotWriter
from bfx_funding_bot.modules.marketfeed.candle_writer import CandleWriter
from bfx_funding_bot.modules.strategy import CellConfig

log = logging.getLogger(__name__)


def dependency_answered(probe: HealthProbe, readiness: TradingReadiness | None,
                        dependency: str) -> None:
    """A dependency answered: beat its freshness and clear it in /readyz now,
    not at the next staleness scan (which only ever marks it stale)."""
    probe.record_heartbeat(dependency)
    if readiness is not None:
        readiness.clear_dependency(dependency)


@dataclass
class MarketDataRuntime:
    cells: Sequence[CellConfig]
    probe: HealthProbe
    candle_q: asyncio.Queue[CandleMessage | None]
    writer: CandleWriter
    ws_client: BitfinexWSClient | None
    bitfinex: BitfinexREST
    session_factory: async_sessionmaker[AsyncSession]
    # Book depth self-recording (observe-only; fail-open inside). None when disabled.
    book_snapshot_writer: BookSnapshotWriter | None = None
    # Where the ws_data freshness clears readiness (the account's; R9c moves the source).
    trading_readiness: TradingReadiness | None = None
    _stop: asyncio.Event = field(default_factory=asyncio.Event)

    async def run(self, stop_event: asyncio.Event) -> None:
        """Record until ``stop_event``; a task that raises fails the TaskGroup."""
        self._stop = stop_event
        async with asyncio.TaskGroup() as tg:
            tg.create_task(self._candle_writer_loop(), name="candle_writer")
            if self.ws_client is not None:
                tg.create_task(self._ws_consume_with_reconnect(), name="ws")
                tg.create_task(self._ws_heartbeat_poll_loop(), name="ws_heartbeat")
            if self.book_snapshot_writer is not None:
                tg.create_task(self.book_snapshot_writer.run(stop_event), name="book_snapshot")

    async def _candle_writer_loop(self) -> None:
        """Plumbing wrapper for self.writer.run() with FatalError pass-through.
        Heartbeat is recorded inside CandleWriter.run() after each successful upsert.
        Sends None sentinel to unblock queue.get() on graceful stop."""
        try:
            # Run writer and a stop-sentinel task concurrently; cancel the
            # sentinel if writer exits naturally (e.g. on CancelledError).
            async def _drain_sentinel() -> None:
                await self._stop.wait()
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
            raise FatalError(f"candle_writer crashed: {e!r}") from e

    async def _ws_heartbeat_poll_loop(self) -> None:
        """Run ``_ws_freshness_tick`` every 15s until stop."""
        assert self.ws_client is not None
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=15.0)
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
        dependency_answered(self.probe, self.trading_readiness, MARKET_DATA_FRESHNESS)
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
        while not self._stop.is_set():
            try:
                # async for would block in ws_client.candles() recv() between
                # yields without checking stop_event — paper exit cycle 2026-05-21
                # hung daemon TaskGroup drain because ws never noticed stop.
                # Manual __anext__() race with stop_event each iteration ensures
                # cancellation arrives within the recv window, not the next yield.
                candles_iter = self.ws_client.candles().__aiter__()
                try:
                    while not self._stop.is_set():
                        # ensure_future accepts Awaitable (anext returns Awaitable[T]
                        # not Coroutine, so create_task fails mypy strict).
                        msg_task = asyncio.ensure_future(candles_iter.__anext__())
                        stop_task = asyncio.create_task(self._stop.wait())
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

            if self._stop.is_set():
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
                    self._stop.wait(), timeout=backoff,
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
                    for c in self.cells
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
                    for cell in self.cells:
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
