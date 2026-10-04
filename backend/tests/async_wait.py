"""Condition-driven waits for async tests.

Tests must not decide outcomes by sleeping a fixed wall-clock time and then counting
events: under CPU contention (pytest-xdist) the loop under test may tick far fewer
times than expected. Wait for the asserted condition instead; the timeout is only a
safety net that fails the test when the condition never happens.
"""
from __future__ import annotations

import asyncio
import contextlib
import inspect
from collections.abc import AsyncIterator, Awaitable, Callable

SAFETY_TIMEOUT_S = 10.0


async def until(
    predicate: Callable[[], object],
    *,
    timeout: float = SAFETY_TIMEOUT_S,
    what: str = "condition",
) -> None:
    """Yield to the event loop until ``predicate()`` is truthy.

    ``predicate`` may be a plain callable or return an awaitable (awaited each poll).
    """

    async def _check() -> object:
        result = predicate()
        if inspect.isawaitable(result):
            result = await result
        return result

    async def _poll() -> None:
        while not await _check():
            await asyncio.sleep(0.001)

    try:
        await asyncio.wait_for(_poll(), timeout=timeout)
    except TimeoutError:
        raise AssertionError(f"timed out after {timeout}s waiting for {what}") from None


async def yield_loop(times: int = 20) -> None:
    """Let every ready task run ``times`` rounds (no wall-clock dependence)."""
    for _ in range(times):
        await asyncio.sleep(0)


@contextlib.asynccontextmanager
async def running(
    loop_fn: Callable[[asyncio.Event], Awaitable[object]],
    *,
    timeout: float = SAFETY_TIMEOUT_S,
) -> AsyncIterator[tuple[asyncio.Event, asyncio.Task[object]]]:
    """Run ``loop_fn(stop)`` as a task; on exit set stop and await a clean exit.

    The body drives the loop with ``until`` (e.g. ``resync.request(...)`` between
    ticks). A loop that fails or does not exit within ``timeout`` fails the test.
    """
    stop = asyncio.Event()
    task: asyncio.Task[object] = asyncio.ensure_future(loop_fn(stop))
    try:
        yield stop, task
    finally:
        stop.set()
        try:
            await asyncio.wait_for(task, timeout=timeout)
        except BaseException:
            task.cancel()
            raise


async def run_until(
    loop_fn: Callable[[asyncio.Event], Awaitable[object]],
    predicate: Callable[[], object],
    *,
    timeout: float = SAFETY_TIMEOUT_S,
    what: str = "condition",
) -> None:
    """Run ``loop_fn(stop)`` until ``predicate()`` holds, then stop it and await exit.

    Fails if the predicate never holds within ``timeout`` (or the loop dies first).
    """
    async with running(loop_fn, timeout=timeout) as (_stop, task):
        await until(lambda: predicate() or task.done(), timeout=timeout, what=what)
        if task.done() and not predicate():
            task.result()  # surfaces the loop's own exception, if any
            raise AssertionError(f"loop exited before {what}")
