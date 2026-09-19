"""HaltStateStore — persisted halt, append-only.

Motivation (2026-07-27): the canary halt lived only in BFX_KILL_SWITCH inside
canary.env. Any deploy that reverted that file would have resumed real-money
trading silently — nothing would have to go wrong, a plain `git revert` was
enough. Persisting the halt makes stopping trading a durable fact rather than a
property of whichever env file happens to be on disk.

Append-only rather than one mutable row: the history IS the audit trail. "Who
resumed trading, when, and on what grounds" is exactly the question that had no
answer today, and a row that gets overwritten on resume answers it even less.
"""
from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker

import bfx_funding_bot.modules.execution.safety.tables  # noqa: F401
from bfx_funding_bot.core.db import Base
from bfx_funding_bot.modules.execution.safety.halt_state import HaltStateStore


@pytest_asyncio.fixture
async def sf(sqlite_engine) -> async_sessionmaker:
    async with sqlite_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return async_sessionmaker(sqlite_engine, expire_on_commit=False)


def _store(sf, env: str = "ci") -> HaltStateStore:
    return HaltStateStore(sf, account_id="acct", deployment_environment=env)


@pytest.mark.asyncio
async def test_no_record_yet_reads_as_none_not_as_running(sf) -> None:
    """None means "never configured", which the guard must not confuse with
    "explicitly resumed"."""
    assert await _store(sf).current() is None


@pytest.mark.asyncio
async def test_halt_then_read_back(sf) -> None:
    store = _store(sf)
    await store.set_halted(
        True, reason="candle distortion", actor="admin", now_ms=1000,
    )
    state = await store.current()
    assert state is not None
    assert state.halted is True
    assert state.reason == "candle distortion"
    assert state.actor == "admin"
    assert state.created_at_ms == 1000


@pytest.mark.asyncio
async def test_resume_supersedes_halt_without_erasing_it(sf) -> None:
    store = _store(sf)
    await store.set_halted(True, reason="distortion", actor="admin", now_ms=1000)
    await store.set_halted(False, reason="L4 v2 passed", actor="admin", now_ms=2000)

    state = await store.current()
    assert state is not None
    assert state.halted is False
    assert state.reason == "L4 v2 passed"

    # Both transitions survive — newest first.
    hist = await store.history(limit=10)
    assert [(h.halted, h.reason) for h in hist] == [
        (False, "L4 v2 passed"), (True, "distortion"),
    ]


@pytest.mark.asyncio
async def test_current_is_the_latest_row_not_the_highest_timestamp(sf) -> None:
    """Ordering is by insertion, not by caller-supplied clock: a wrong now_ms
    (clock skew, a backfilled row) must not resurrect a superseded state on a
    safety control."""
    store = _store(sf)
    await store.set_halted(True, reason="halt", actor="admin", now_ms=9999)
    await store.set_halted(False, reason="resume", actor="admin", now_ms=1)
    state = await store.current()
    assert state is not None
    assert state.halted is False


@pytest.mark.asyncio
async def test_state_is_scoped_per_account_and_environment(sf) -> None:
    prod = _store(sf, env="prod")
    ci = _store(sf, env="ci")
    await prod.set_halted(True, reason="prod halt", actor="admin", now_ms=1000)
    assert await ci.current() is None
    prod_state = await prod.current()
    assert prod_state is not None
    assert prod_state.halted is True


@pytest.mark.asyncio
async def test_history_is_scoped_and_bounded(sf) -> None:
    store = _store(sf)
    for i in range(5):
        await store.set_halted(i % 2 == 0, reason=f"r{i}", actor="admin", now_ms=i)
    hist = await store.history(limit=3)
    assert [h.reason for h in hist] == ["r4", "r3", "r2"]


@pytest.mark.asyncio
async def test_default_clock_is_used_when_now_ms_is_omitted(sf) -> None:
    store = _store(sf)
    await store.set_halted(True, reason="halt", actor="admin")
    state = await store.current()
    assert state is not None
    assert state.created_at_ms > 0


@pytest.mark.asyncio
async def test_reassertion_preserves_logical_halt_epoch(sf) -> None:
    store = _store(sf)
    first = await store.set_halted(True, reason="operator", actor="operator", now_ms=1)
    again = await store.set_halted(True, reason="terminal", actor="worker", now_ms=2)
    assert again.id == first.id
    assert again.actor == "operator"
    await store.set_halted(False, reason="promotion", actor="operator", now_ms=3)
    next_epoch = await store.set_halted(True, reason="emergency", actor="operator", now_ms=4)
    assert next_epoch.id > first.id
