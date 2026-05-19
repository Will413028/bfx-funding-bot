"""D3 — DB pool reconnect under chaos."""
import asyncio
from sqlalchemy import text

import pytest

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
        try:
            await killer.execute(text(f"SELECT pg_terminate_backend({pid})"))
        except Exception:
            # pg_terminate_backend may fail if the connection is already gone,
            # or the connection doing the terminating may get caught up.
            # Either way, the target pid should be dead.
            pass

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
    await asyncio.sleep(0.35)
    stop.set()
    await task

    # Expect ~3 ticks (at 0.1, 0.2, 0.3)
    assert 2 <= len(ticks) <= 4, f"expected ~3 ticks, got {len(ticks)}"
