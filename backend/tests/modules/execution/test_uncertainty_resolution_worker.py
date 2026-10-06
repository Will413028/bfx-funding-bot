"""Supervision contract of the daemon-side adjudication worker (ADR D4')."""
import asyncio
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID

import pytest

from bfx_funding_bot.modules.execution.uncertainty_requests import (
    ResolutionScope,
    UncertaintyResolutionWorker,
)
from bfx_funding_bot.modules.ledger.wiring import build_operator_resolution

SCOPE = ResolutionScope(UUID("550e8400-e29b-41d4-a716-446655440000"), "ci")


@pytest.mark.asyncio
async def test_a_failed_tick_does_not_stop_the_writer() -> None:
    """The worker runs inside the daemon's TaskGroup; raising would kill lending."""
    worker = UncertaintyResolutionWorker(
        session_factory=MagicMock(), scope=SCOPE, poll_interval_s=0.01,
        authority=AsyncMock(return_value=True), resolution=build_operator_resolution())
    stop = asyncio.Event()
    calls = 0

    async def flaky() -> bool:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("connection reset")
        stop.set()
        return False

    worker.tick = flaky  # type: ignore[method-assign]
    await asyncio.wait_for(worker.run(stop), timeout=2)
    assert calls == 2


@pytest.mark.asyncio
async def test_worker_drains_the_queue_without_waiting_between_requests() -> None:
    worker = UncertaintyResolutionWorker(
        session_factory=MagicMock(), scope=SCOPE, poll_interval_s=60,
        authority=AsyncMock(return_value=True), resolution=build_operator_resolution())
    stop = asyncio.Event()
    results = iter([True, True, False])

    async def tick() -> bool:
        processed = next(results)
        if not processed:
            stop.set()
        return processed

    worker.tick = tick  # type: ignore[method-assign]
    await asyncio.wait_for(worker.run(stop), timeout=2)


@pytest.mark.asyncio
async def test_worker_refuses_to_apply_without_writer_ownership() -> None:
    factory = MagicMock()
    worker = UncertaintyResolutionWorker(
        session_factory=factory, scope=SCOPE, ownership=AsyncMock(return_value=False),
        authority=AsyncMock(return_value=True), resolution=build_operator_resolution())
    with pytest.raises(RuntimeError, match="ownership_lost"):
        await worker.tick()
    factory.begin.assert_not_called()
