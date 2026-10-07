"""WS candle queue -> funding_candles DB upsert.

Design basis: phase 4.1 paper/shadow infra design Section "Data Flow" Flow 2
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from decimal import Decimal

import asyncpg
from sqlalchemy.ext.asyncio import AsyncSession
from tenacity import (
    AsyncRetrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from bfx_funding_bot.core.errors import FatalError
from bfx_funding_bot.core.health import HealthProbe
from bfx_funding_bot.external.bitfinex.ws import CandleMessage
from bfx_funding_bot.modules.candles.repository import (
    mark_candles_final,
    upsert_candles,
)
from bfx_funding_bot.modules.candles.schemas import FundingCandle

log = logging.getLogger(__name__)

_DB_TRANSIENT = (
    asyncpg.InterfaceError,
    asyncpg.ConnectionDoesNotExistError,
)
_DB_FATAL = (
    asyncpg.InvalidPasswordError,
    asyncpg.InvalidCatalogNameError,
)
# With nothing queued, the consumer still beats once a minute (loop progress).
IDLE_BEAT_S = 60.0


class CandleWriter:
    def __init__(
        self,
        *,
        queue: asyncio.Queue[CandleMessage | None],
        session_factory: Callable[[], AsyncSession] | Callable[[], Awaitable[AsyncSession]],
        probe: HealthProbe,
        clock: Callable[[], int] | None = None,
        idle_beat_s: float = IDLE_BEAT_S,
    ) -> None:
        self._queue = queue
        self._idle_beat_s = idle_beat_s
        self._session_factory = session_factory
        self._probe = probe
        # One clock for both the write and the seal, so a candle can never be
        # judged "still forming" by one and "closed" by the other in the same tick.
        self._clock = clock or (lambda: int(time.time() * 1000))

    async def run(self) -> None:
        """Consume the queue until the None sentinel.

        The ``candle_writer`` liveness beat marks loop progress only: one per
        dequeued candle whatever the upsert outcome, and one per idle
        ``idle_beat_s`` with nothing queued. A quiet market or a WS outage leaves
        the queue empty and a database outage fails the upsert; neither is this
        loop being stuck, so neither may age the beat into a restart.
        """
        while True:
            try:
                msg = await asyncio.wait_for(self._queue.get(), timeout=self._idle_beat_s)
            except TimeoutError:
                self._probe.record_heartbeat("candle_writer")
                continue
            if msg is None:
                return
            try:
                await self._upsert(msg)
            except FatalError:
                raise  # propagate to TaskGroup → daemon exit → container restart policy
            except Exception:
                log.exception("candle_writer_upsert_failed mts=%d", msg.mts)
                # Tenacity stop_after_attempt(5) re-raised → log + continue
                # (next message gets fresh retry budget). A database outage shows
                # as the db freshness heartbeat going stale, not as this loop.
            self._probe.record_heartbeat("candle_writer")

    async def _upsert(self, msg: CandleMessage) -> None:
        candle = FundingCandle(
            symbol=msg.symbol,
            timeframe=msg.timeframe,
            period_agg=msg.period_agg,
            mts=msg.mts,
            open=Decimal(str(msg.open)),
            close=Decimal(str(msg.close)),
            high=Decimal(str(msg.high)),
            low=Decimal(str(msg.low)),
            volume=Decimal(str(msg.volume)),
        )

        async for attempt in AsyncRetrying(
            retry=retry_if_exception_type(_DB_TRANSIENT),
            wait=wait_exponential(multiplier=1, min=1, max=30),
            stop=stop_after_attempt(5),
            reraise=True,
        ):
            with attempt:
                try:
                    await self._do_upsert(candle)
                except _DB_FATAL as e:
                    raise FatalError(f"candle_writer db_fatal: {e!r}") from e

    async def _do_upsert(self, candle: FundingCandle) -> None:
        """Inner write op — invoked by tenacity wrapper."""
        maybe = self._session_factory()
        session: AsyncSession
        if asyncio.iscoroutine(maybe):
            session = await maybe
        else:
            session = maybe  # type: ignore[assignment]
        try:
            now_ms = self._clock()
            await upsert_candles(session, [candle], now_ms=now_ms)
            # The venue moving on to a later period is the ONLY reliable proof the
            # previous one closed — waiting a fixed settle delay is a guess (5s then
            # 30s were both wrong, and the strategy observed in-flight values as a
            # result). Sealing here is what makes `is_final` mean "safe to observe".
            await mark_candles_final(
                session,
                symbol=candle.symbol,
                timeframe=candle.timeframe,
                period_agg=candle.period_agg,
                through_mts=candle.mts - 1,
                now_ms=now_ms,
            )
            await session.commit()
        finally:
            await session.close()
