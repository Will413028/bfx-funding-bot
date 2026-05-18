"""Per-cell time-driven scheduler — fires `callback(cell, mts)` at
`mts + timeframe + buffer_s` for each cell.

設計依據: phase4.1-paper-shadow-infra-design.md Q11
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from bfx_funding_bot.modules.marketfeed.config import CellConfig

log = logging.getLogger(__name__)

_TIMEFRAME_MS = {"15m": 15 * 60_000, "30m": 30 * 60_000, "1h": 60 * 60_000}


def next_candle_close_mts(*, timeframe: str, now_ms: int) -> int:
    """Wall-clock-aligned next candle close in ms epoch."""
    step = _TIMEFRAME_MS[timeframe]
    return ((now_ms // step) + 1) * step


def now_ms_utc() -> int:
    """Current UTC time in ms (timezone-independent: based on epoch)."""
    return int(time.time() * 1000)


@dataclass
class _Entry:
    cell: CellConfig
    next_fire_mts: int


class Scheduler:
    def __init__(
        self,
        *,
        callback: Callable[[CellConfig, int], Awaitable[None]],
        buffer_s: float = 5.0,
    ) -> None:
        self._cb = callback
        self._buffer_s = buffer_s
        self._entries: dict[str, _Entry] = {}
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()

    def register(self, cell: CellConfig, *, fire_at_mts: int) -> None:
        self._entries[cell.pair_id] = _Entry(cell=cell, next_fire_mts=fire_at_mts)

    def register_from_now(self, cell: CellConfig) -> None:
        nxt = next_candle_close_mts(timeframe=cell.timeframe, now_ms=now_ms_utc())
        self.register(cell, fire_at_mts=nxt)

    async def start(self) -> None:
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            await self._task
            self._task = None

    async def _loop(self) -> None:
        while not self._stop.is_set():
            now = now_ms_utc()
            due: list[_Entry] = [
                e for e in self._entries.values()
                if now >= e.next_fire_mts + int(self._buffer_s * 1000)
            ]
            for e in due:
                try:
                    await self._cb(e.cell, e.next_fire_mts)
                except Exception:
                    log.exception(
                        "scheduler_cb_exception cell=%s mts=%d",
                        e.cell.pair_id, e.next_fire_mts,
                    )
                e.next_fire_mts = next_candle_close_mts(
                    timeframe=e.cell.timeframe, now_ms=e.next_fire_mts + 1,
                )
            await asyncio.sleep(0.05)
