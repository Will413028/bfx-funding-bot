"""The out-of-loop watchdog ends a process whose event loop is wedged, and only then.

The behaviour tests run a real interpreter: ``faulthandler`` keeps one process-wide
timer and its expiry calls ``_exit``, so it cannot run inside the pytest process.
Their only time bound is the watchdog's own timeout (T=1s) against a loop blocked
for 3s or a drain that outlasts T; nothing else depends on wall-clock thresholds.

Mutation checks (one at a time; revert after each):

* drop the re-arm in ``LoopWatchdog.run``: ``test_a_loop_that_keeps_iterating_is_left_alone``.
* drop the disarm in ``LoopWatchdog.run``: ``test_a_drain_after_stop_outlasting_the_timeout_exits_cleanly``.
"""
from __future__ import annotations

import asyncio
import subprocess
import sys
import textwrap

import pytest

from bfx_funding_bot.core.errors import ConfigurationError
from bfx_funding_bot.core.loop_watchdog import (
    DEFAULT_LOOP_WATCHDOG_S,
    LoopWatchdog,
    loop_watchdog_timeout_s,
)
from tests.async_wait import until

_PRELUDE = """
import asyncio
import time

from bfx_funding_bot.core.loop_watchdog import LoopWatchdog
"""


def _run_script(body: str) -> subprocess.CompletedProcess[str]:
    script = _PRELUDE + textwrap.dedent(body)
    return subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True, text=True, timeout=60, check=False,
    )


def test_a_blocked_loop_dumps_the_blocking_frame_and_exits_non_zero() -> None:
    result = _run_script("""
        def block_the_loop() -> None:
            time.sleep(3)

        async def main() -> None:
            stop = asyncio.Event()
            task = asyncio.create_task(LoopWatchdog(timeout_s=1.0).run(stop))
            await asyncio.sleep(0.05)  # the watchdog task has armed the timer
            block_the_loop()
            print("survived")
            stop.set()
            await task

        asyncio.run(main())
    """)
    assert result.returncode != 0
    assert "survived" not in result.stdout
    assert "Timeout" in result.stderr
    assert "block_the_loop" in result.stderr


def test_a_loop_that_keeps_iterating_is_left_alone() -> None:
    result = _run_script("""
        async def main() -> None:
            stop = asyncio.Event()
            task = asyncio.create_task(LoopWatchdog(timeout_s=1.0).run(stop))
            for _ in range(10):  # ~5s in all, never blocked for more than 0.3s
                time.sleep(0.3)
                await asyncio.sleep(0.2)
            stop.set()
            await task
            print("finished")

        asyncio.run(main())
    """)
    assert result.returncode == 0, result.stderr
    assert "finished" in result.stdout
    assert "Timeout" not in result.stderr


def test_a_drain_after_stop_outlasting_the_timeout_exits_cleanly() -> None:
    result = _run_script("""
        async def main() -> None:
            stop = asyncio.Event()
            task = asyncio.create_task(LoopWatchdog(timeout_s=1.0).run(stop))
            await asyncio.sleep(0.3)
            stop.set()
            await task           # the watchdog saw the stop request
            time.sleep(2.5)      # a drain that blocks longer than the timeout
            print("drained")

        asyncio.run(main())
    """)
    assert result.returncode == 0, result.stderr
    assert "drained" in result.stdout
    assert "Timeout" not in result.stderr


class _Recorder:
    def __init__(self) -> None:
        self.calls: list[tuple[str, float | None]] = []

    def arm(self, timeout: float, *, exit: bool) -> None:
        assert exit is True
        self.calls.append(("arm", timeout))

    def disarm(self) -> None:
        self.calls.append(("disarm", None))


async def test_cancellation_disarms_the_timer() -> None:
    rec = _Recorder()
    watchdog = LoopWatchdog(timeout_s=60.0, arm=rec.arm, disarm=rec.disarm)
    task = asyncio.create_task(watchdog.run(asyncio.Event()))
    await until(lambda: rec.calls, what="the first arm")
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert rec.calls == [("arm", 60.0), ("disarm", None)]


def test_timeout_comes_from_the_environment_with_a_default() -> None:
    assert loop_watchdog_timeout_s({}) == DEFAULT_LOOP_WATCHDOG_S == 120.0
    assert loop_watchdog_timeout_s({"BFX_LOOP_WATCHDOG_S": " "}) == 120.0
    assert loop_watchdog_timeout_s({"BFX_LOOP_WATCHDOG_S": "45"}) == 45.0


@pytest.mark.parametrize("raw", ["0", "-1", "abc", "nan", "inf"])
def test_an_unusable_timeout_refuses_to_start(raw: str) -> None:
    with pytest.raises(ConfigurationError, match="BFX_LOOP_WATCHDOG_S"):
        loop_watchdog_timeout_s({"BFX_LOOP_WATCHDOG_S": raw})


async def test_each_rearm_reports_the_loop_lag() -> None:
    rec = _Recorder()
    lags: list[float] = []
    watchdog = LoopWatchdog(timeout_s=0.04, arm=rec.arm, disarm=rec.disarm)
    watchdog.on_lag = lags.append
    stop = asyncio.Event()
    task = asyncio.create_task(watchdog.run(stop))
    await until(lambda: len(lags) >= 2, what="two re-arms")
    stop.set()
    await task
    assert all(lag >= 0.0 for lag in lags)
    assert [call[0] for call in rec.calls].count("arm") >= 3
    assert rec.calls[-1] == ("disarm", None)
