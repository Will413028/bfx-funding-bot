from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest

from bfx_funding_bot.modules.marketfeed.self_smoke import maybe_run_self_smoke


@pytest.mark.asyncio
async def test_self_smoke_runs_when_gated():
    """phase=paper + duration set → smoke_runner called, returns its exit code."""
    smoke_runner = AsyncMock(return_value=0)
    sleep_fn = AsyncMock(return_value=None)
    cells: list[dict[str, Any]] = [{"strategy": "x", "symbol": "y", "period_agg": "z", "timeframe": "1h"}]

    result = await maybe_run_self_smoke(
        phase="paper",
        duration=1,
        cells=cells,
        smoke_runner=smoke_runner,
        sleep_fn=sleep_fn,
    )

    assert result == 0
    smoke_runner.assert_awaited_once_with(phase="paper", hours=1, cells=cells)


@pytest.mark.asyncio
async def test_self_smoke_skipped_shadow():
    """phase=shadow → smoke_runner NOT called, returns 0."""
    smoke_runner = AsyncMock(return_value=999)
    sleep_fn = AsyncMock(return_value=None)

    result = await maybe_run_self_smoke(
        phase="shadow",
        duration=1,
        cells=[],
        smoke_runner=smoke_runner,
        sleep_fn=sleep_fn,
    )

    assert result == 0
    smoke_runner.assert_not_awaited()
    sleep_fn.assert_not_awaited()


@pytest.mark.asyncio
async def test_self_smoke_skipped_no_duration():
    """duration=None → smoke_runner NOT called even if phase=paper."""
    smoke_runner = AsyncMock(return_value=999)
    sleep_fn = AsyncMock(return_value=None)

    result = await maybe_run_self_smoke(
        phase="paper",
        duration=None,
        cells=[],
        smoke_runner=smoke_runner,
        sleep_fn=sleep_fn,
    )

    assert result == 0
    smoke_runner.assert_not_awaited()
    sleep_fn.assert_not_awaited()


@pytest.mark.asyncio
async def test_self_smoke_propagates_exit_code():
    """smoke_runner returns 1 → helper returns 1."""
    smoke_runner = AsyncMock(return_value=1)
    sleep_fn = AsyncMock(return_value=None)

    result = await maybe_run_self_smoke(
        phase="paper",
        duration=1,
        cells=[],
        smoke_runner=smoke_runner,
        sleep_fn=sleep_fn,
    )

    assert result == 1


@pytest.mark.asyncio
async def test_self_smoke_sleeps_before_runner():
    """sleep_fn called with 30 BEFORE smoke_runner is awaited."""
    call_order: list[str] = []

    async def fake_smoke(**kw: Any) -> int:
        call_order.append("smoke")
        return 0

    async def fake_sleep(seconds: float) -> None:
        call_order.append(f"sleep:{seconds}")

    result = await maybe_run_self_smoke(
        phase="paper",
        duration=1,
        cells=[],
        smoke_runner=fake_smoke,
        sleep_fn=fake_sleep,
    )

    assert result == 0
    assert call_order == ["sleep:30", "smoke"]
