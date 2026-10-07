"""DB keepalive task — issues SELECT 1 every 5 min so idle pool
connections don't drop below Neon's 15min cut threshold.

Layered with engine pool_pre_ping + pool_recycle=600 (10min). This
task ensures *some* traffic per 5min window so connections don't sit
idle for the full recycle period.
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from sqlalchemy import text

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine

log = logging.getLogger(__name__)

KEEPALIVE_INTERVAL_S = 300.0  # 5 min (= pool_recycle/2, per AWS RDS Proxy convention)


async def keepalive_loop(
    engine: AsyncEngine,
    *,
    interval_s: float = KEEPALIVE_INTERVAL_S,
    stop: asyncio.Event,
    on_tick: Callable[[datetime], None] | None = None,
    on_attempt: Callable[[], None] | None = None,
) -> None:
    """Run SELECT 1 now and then every interval_s seconds until stop.set().

    Args:
        engine: AsyncEngine to ping.
        interval_s: Seconds between pings.
        stop: Set this Event to terminate the loop.
        on_tick: Optional callable invoked with current datetime after each
                 successful ping (the Daemon's database freshness heartbeat).
        on_attempt: Optional callable invoked after every ping attempt,
                 successful or not (the Daemon's own-loop liveness heartbeat).
    """
    while not stop.is_set():
        # Ping first, then wait: the first answer (the database freshness beat
        # that lifts the boot-time "never seen" state) comes at start, not after
        # a full interval.
        try:
            async with engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
            log.debug("db_keepalive_ok")
            if on_tick is not None:
                on_tick(datetime.now(UTC))
        except Exception:
            log.exception("db_keepalive_failed")
            # Don't raise: pool_pre_ping will retry on next acquire;
            # keepalive failures alone shouldn't kill daemon.
        if on_attempt is not None:
            on_attempt()
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval_s)
            return  # stop was set during the wait
        except TimeoutError:
            pass  # interval elapsed; ping again
