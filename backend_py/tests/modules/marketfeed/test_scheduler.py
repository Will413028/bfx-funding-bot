from __future__ import annotations

import asyncio

from bfx_funding_bot.modules.marketfeed.config import CellConfig
from bfx_funding_bot.modules.marketfeed.health_monitor import HealthProbe
from bfx_funding_bot.modules.marketfeed.scheduler import (
    Scheduler,
    next_candle_close_mts,
    now_ms_utc,
)


def _cell() -> CellConfig:
    return CellConfig.model_validate({
        "strategy": "rate_percentile", "symbol": "fUSD", "period_agg": "a30",
        "timeframe": "1h",
        "params": {"percentile": 75, "lookback_hours": 5},
        "reference_amount_usdt": 150.0,
    })


def test_next_candle_close_aligns_to_timeframe():
    now_ms = 1747584000000 + 1234  # just past hour mark
    nxt = next_candle_close_mts(timeframe="1h", now_ms=now_ms)
    assert nxt == 1747584000000 + 3600_000


def test_next_candle_close_30m():
    now_ms = 1747584000000 + 60_000  # 1 minute past hour
    nxt = next_candle_close_mts(timeframe="30m", now_ms=now_ms)
    assert nxt == 1747584000000 + 30 * 60_000


async def test_scheduler_fires_callback_after_timeframe_plus_buffer():
    cell = _cell()
    callbacks: list[int] = []

    async def cb(c: CellConfig, mts: int) -> None:
        callbacks.append(mts)

    sched = Scheduler(callback=cb, probe=HealthProbe(), buffer_s=0.05)
    # Schedule fire at "now" — buffer 0.05s means it fires almost immediately
    sched.register(cell, fire_at_mts=now_ms_utc() - 100)

    await sched.start()
    await asyncio.sleep(0.3)
    await sched.stop()

    assert len(callbacks) >= 1
