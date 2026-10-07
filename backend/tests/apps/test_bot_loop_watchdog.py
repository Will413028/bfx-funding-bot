"""The bot's run path arms the loop watchdog before the build and hands it the stop event.

A fake watchdog stands in for ``LoopWatchdog`` (the real one arms a process-wide
``faulthandler`` timer that would end the pytest worker); its behaviour is
covered in ``tests/core/test_loop_watchdog.py``. This checks the wiring only:
running before ``build_daemon`` (so the build and the boot observation are
covered), following the very event the daemon stops on (so a stop disarms it
before the drain), ending with the process, and taking ``BFX_LOOP_WATCHDOG_S``.

Mutation checks (one at a time; revert after each):

* start the watchdog task after ``build_daemon``: ``test_the_watchdog_*``.
* pass the watchdog a different event than the daemon's: ``test_the_watchdog_*``.
"""
from __future__ import annotations

import asyncio
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any, ClassVar

import pytest

from bfx_funding_bot.apps import bot
from bfx_funding_bot.modules.observability import alerts


class _Watchdog:
    instances: ClassVar[list[_Watchdog]] = []

    def __init__(self, *, timeout_s: float) -> None:
        self.timeout_s = timeout_s
        self.on_lag: Any = None
        self.running = False
        self.stop: asyncio.Event | None = None
        self.saw_stop = False
        _Watchdog.instances.append(self)

    async def run(self, stop: asyncio.Event) -> None:
        self.running = True
        self.stop = stop
        try:
            await stop.wait()
            self.saw_stop = True
        finally:
            self.running = False


class _RunRecord:
    """Stands in for ``BotRunRecord``: records the order of lifecycle calls."""

    events: ClassVar[list[str]] = []

    def __init__(self, session_factory: Any, *, exchange_account_id: Any,
                 deployment_environment: str) -> None:
        self.scope = (exchange_account_id, deployment_environment)

    async def start(self) -> list[Any]:
        _RunRecord.events.append("start")
        return []

    async def finish(self, reason: str) -> None:
        _RunRecord.events.append(f"finish:{reason}")


@pytest.fixture
def watchdogs(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[_Watchdog]]:
    _Watchdog.instances = []
    _RunRecord.events = []
    monkeypatch.setattr(bot, "LoopWatchdog", _Watchdog)
    monkeypatch.setattr(bot, "BotRunRecord", _RunRecord)
    previous = alerts.current()
    yield _Watchdog.instances
    alerts.install(previous)


def _fake_daemon(stop: asyncio.Event, watchdog_during_run: list[bool], *,
                 fail: BaseException | None = None, booted: bool = True) -> Any:
    async def run() -> None:
        watchdog_during_run.append(_Watchdog.instances[0].running)
        _RunRecord.events.append("run")
        stop.set()  # a SIGTERM: the drain begins
        await asyncio.sleep(0)
        _RunRecord.events.append("drained")
        if fail is not None:
            raise fail

    async def aclose() -> None:
        return None

    return SimpleNamespace(
        run=run, metrics=None, booted=booted, writer_lock=None, venue_aclose=None,
        tracing=None, bitfinex_http=SimpleNamespace(aclose=aclose),
        config=SimpleNamespace(phase="live", cells=[], run_duration_hours=None),
        session_factory=None,
        account_bootstrap=SimpleNamespace(exchange_account_id="acct", deployment_environment="ci"),
    )


async def test_the_watchdog_covers_the_build_and_follows_the_daemon_stop(
    monkeypatch: pytest.MonkeyPatch, watchdogs: list[_Watchdog],
) -> None:
    monkeypatch.setenv("BFX_LOOP_WATCHDOG_S", "45")
    during_build: list[bool] = []
    during_run: list[bool] = []

    async def build_daemon(*, stop_event: asyncio.Event) -> Any:
        await asyncio.sleep(0)  # the real build awaits long before it returns
        during_build.append(watchdogs[0].running)
        assert watchdogs[0].stop is stop_event
        return _fake_daemon(stop_event, during_run)

    monkeypatch.setattr(bot, "build_daemon", build_daemon)
    await bot._run()

    [watchdog] = watchdogs
    assert watchdog.timeout_s == 45.0
    assert during_build == [True]
    assert during_run == [True]
    assert watchdog.saw_stop is True
    assert watchdog.running is False


async def test_the_watchdog_ends_with_a_refused_build(
    monkeypatch: pytest.MonkeyPatch, watchdogs: list[_Watchdog],
) -> None:
    async def refuse(**_: object) -> None:
        await asyncio.sleep(0)
        raise ValueError("config_fatal")

    monkeypatch.setattr(bot, "build_daemon", refuse)
    with pytest.raises(ValueError, match="config_fatal"):
        await bot._run()
    assert watchdogs[0].running is False


async def test_an_unusable_watchdog_timeout_refuses_the_boot(
    monkeypatch: pytest.MonkeyPatch, watchdogs: list[_Watchdog],
) -> None:
    monkeypatch.setenv("BFX_LOOP_WATCHDOG_S", "0")
    with pytest.raises(ValueError, match="BFX_LOOP_WATCHDOG_S"):
        await bot._run()
    assert watchdogs == []  # refused before anything was armed


@pytest.mark.parametrize(
    ("fail", "booted", "reason"),
    [(None, True, "clean_stop"), (RuntimeError("task died"), True, "fatal"),
     (RuntimeError("refused"), False, "boot_refused")],
)
async def test_the_run_is_recorded_after_the_build_and_its_end_after_the_drain(
    monkeypatch: pytest.MonkeyPatch, watchdogs: list[_Watchdog],
    fail: BaseException | None, booted: bool, reason: str,
) -> None:
    """``bot_runs`` lifecycle: the row once the build (and its writer lock) is done, the
    end only once the drain is over -- a process killed before that leaves the row open
    for the next boot to report."""
    async def build_daemon(*, stop_event: asyncio.Event) -> Any:
        return _fake_daemon(stop_event, [], fail=fail, booted=booted)

    monkeypatch.setattr(bot, "build_daemon", build_daemon)
    if fail is None:
        await bot._run()
    else:
        with pytest.raises(ExceptionGroup):
            await bot._run()
    assert _RunRecord.events == ["start", "run", "drained", f"finish:{reason}"]
