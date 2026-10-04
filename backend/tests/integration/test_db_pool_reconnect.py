"""D3 — DB pool reconnect under chaos."""
import asyncio
import contextlib

import pytest
from sqlalchemy import text

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_pool_pre_ping_recovers_killed_connection(pg_engine):
    """pg_terminate_backend kills connection → pool_pre_ping detects on next
    acquire → engine rebuilds connection silently → query succeeds."""
    # Get a connection and its PID
    async with pg_engine.connect() as conn:
        pid = (await conn.execute(text("SELECT pg_backend_pid()"))).scalar()

    # Kill the connection from a separate connection
    # (terminating a connection closes it, so we can't use the pool conn that was just terminated)
    async with pg_engine.connect() as killer:
        # pg_terminate_backend may fail if the connection is already gone, or
        # the connection doing the terminating may get caught up. Either way,
        # the target pid should be dead.
        with contextlib.suppress(Exception):
            await killer.execute(text(f"SELECT pg_terminate_backend({pid})"))

    # Next acquire should rebuild connection with pool_pre_ping detecting the dead one
    async with pg_engine.connect() as conn2:
        new_pid = (await conn2.execute(text("SELECT pg_backend_pid()"))).scalar()

    assert new_pid != pid, "pool did not rebuild a fresh connection"


@pytest.mark.asyncio
async def test_keepalive_loop_holds_connection_warm(pg_engine):
    """keepalive_loop SELECT 1 every interval keeps connection from idle."""
    from bfx_funding_bot.core.keepalive import keepalive_loop

    stop = asyncio.Event()
    ticks = []

    async def runner():
        await keepalive_loop(
            pg_engine,
            interval_s=0.1,  # fast for test
            stop=stop,
            on_tick=lambda ts: ticks.append(ts),
        )

    task = asyncio.create_task(runner())
    # Wait for the ticks instead of sleeping a fixed window: the first one includes opening
    # the pool's connection, which takes arbitrarily long on a loaded machine (xdist workers).
    try:
        async with asyncio.timeout(60):
            while len(ticks) < 3:
                await asyncio.sleep(0.01)
    finally:
        stop.set()
        await task

    assert len(ticks) >= 3, f"keepalive stopped ticking after {len(ticks)} pings"
    assert ticks == sorted(ticks)
