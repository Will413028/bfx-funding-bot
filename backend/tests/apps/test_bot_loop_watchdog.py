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
import logging
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
    # Whether ``start`` wrote the row (``BotRunRecord.start`` swallows its own failure).
    start_succeeds: ClassVar[bool] = True

    def __init__(self, session_factory: Any, *, exchange_account_id: Any,
                 deployment_environment: str) -> None:
        self.scope = (exchange_account_id, deployment_environment)
        self.run_id = "run-1"
        self.started = False
        self.started_at_ms: int | None = None

    async def start(self) -> list[Any]:
        _RunRecord.events.append("start")
        self.started = _RunRecord.start_succeeds
        self.started_at_ms = 123 if self.started else None
        return []

    async def finish(self, reason: str) -> None:
        _RunRecord.events.append(f"finish:{reason}")


@pytest.fixture
def watchdogs(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[_Watchdog]]:
    _Watchdog.instances = []
    _RunRecord.events = []
    _RunRecord.start_succeeds = True
    monkeypatch.setattr(bot, "LoopWatchdog", _Watchdog)
    monkeypatch.setattr(bot, "BotRunRecord", _RunRecord)
    previous = alerts.current()
    yield _Watchdog.instances
    alerts.install(previous)


def _fake_daemon(stop: asyncio.Event, watchdog_during_run: list[bool], *,
                 fail: BaseException | None = None, booted: bool = True,
                 credential_rotation: Any = None) -> Any:
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

    bound_runs: list[int] = []
    return SimpleNamespace(
        run=run, metrics=None, booted=booted, writer_lock=None, venue_aclose=None,
        tracing=None, bitfinex_http=SimpleNamespace(aclose=aclose),
        config=SimpleNamespace(phase="live", cells=[], run_duration_hours=None),
        session_factory=None, credential_rotation=credential_rotation,
        account_bootstrap=SimpleNamespace(exchange_account_id="acct", deployment_environment="ci"),
        bound_runs=bound_runs, bind_run=bound_runs.append,
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


async def test_a_writer_lock_release_that_never_returns_does_not_hold_the_exit(
    monkeypatch: pytest.MonkeyPatch, watchdogs: list[_Watchdog],
) -> None:
    """The release talks to the database; one that stopped answering must not keep the
    process past Docker's stop grace period (the server drops the lock with the
    connection anyway)."""
    released: list[str] = []

    class _HungLock:
        async def release(self) -> None:
            released.append("called")
            await asyncio.Event().wait()  # never answers

    async def build_daemon(*, stop_event: asyncio.Event) -> Any:
        daemon = _fake_daemon(stop_event, [])
        daemon.writer_lock = _HungLock()
        return daemon

    monkeypatch.setattr(bot, "build_daemon", build_daemon)
    monkeypatch.setattr(bot, "_RELEASE_TIMEOUT_S", 0.05)
    await asyncio.wait_for(bot._run(), timeout=10)
    assert released == ["called"]
    assert _RunRecord.events[-1] == "finish:clean_stop"


class _Rotation:
    def __init__(self) -> None:
        self.bound: list[tuple[str, int]] = []

    def bind_run(self, run_id: str, started_at_ms: int) -> None:
        _RunRecord.events.append("bind_run")
        self.bound.append((run_id, started_at_ms))


@pytest.mark.parametrize("start_succeeds", [True, False])
async def test_the_rotation_worker_is_bound_to_the_run_once_it_is_recorded(
    monkeypatch: pytest.MonkeyPatch, watchdogs: list[_Watchdog], start_succeeds: bool,
) -> None:
    """A recipient key is published only under a run the CLI can see as running."""
    _RunRecord.start_succeeds = start_succeeds
    rotation = _Rotation()

    async def build_daemon(*, stop_event: asyncio.Event) -> Any:
        return _fake_daemon(stop_event, [], credential_rotation=rotation)

    monkeypatch.setattr(bot, "build_daemon", build_daemon)
    await bot._run()
    if start_succeeds:
        assert rotation.bound == [("run-1", 123)]
        assert _RunRecord.events[:3] == ["start", "bind_run", "run"]
    else:
        assert rotation.bound == []


async def test_the_daemon_learns_when_its_run_started(
    monkeypatch: pytest.MonkeyPatch, watchdogs: list[_Watchdog],
) -> None:
    """Credential recovery mode counts BOT_SETTLE_WINDOW from the recorded run's start."""
    daemons: list[Any] = []

    async def build_daemon(*, stop_event: asyncio.Event) -> Any:
        daemons.append(_fake_daemon(stop_event, []))
        return daemons[0]

    monkeypatch.setattr(bot, "build_daemon", build_daemon)
    await bot._run()
    assert daemons[0].bound_runs == [123]


async def test_a_key_refused_while_running_exits_78_as_a_fatal_error(
    monkeypatch: pytest.MonkeyPatch, watchdogs: list[_Watchdog],
) -> None:
    """The periodic reconcile raises the rejection out of the TaskGroup; ``_run_daemon``
    must take it before ``except* Exception`` (which would exit 1). The restarted process
    then boots into credential recovery mode.

    Mutation: move the ``except* VenueCredentialRejectedError`` branch after
    ``except* Exception``: this test fails (ExceptionGroup, not SystemExit 78)."""
    from bfx_funding_bot.core.errors import EXIT_CODE_AUTH_FAILED
    from bfx_funding_bot.external.bitfinex.errors import VenueCredentialRejectedError

    sent: list[tuple[str, dict[str, object]]] = []
    monkeypatch.setattr(alerts, "emit", lambda event, **fields: sent.append((event, fields)))
    refused = ExceptionGroup("taskgroup", [VenueCredentialRejectedError(
        500, "Internal Server Error", '["error",10100,"apikey: digest invalid"]')])

    async def build_daemon(*, stop_event: asyncio.Event) -> Any:
        return _fake_daemon(stop_event, [], fail=refused, booted=True)

    monkeypatch.setattr(bot, "build_daemon", build_daemon)
    with pytest.raises(SystemExit) as exited:
        await bot._run()
    assert exited.value.code == EXIT_CODE_AUTH_FAILED == 78
    assert [event for event, _ in sent] == ["daemon_fatal"]
    assert _RunRecord.events[-1] == "finish:fatal"


async def test_a_key_found_noncompliant_while_running_exits_78_as_a_fatal_error(
    monkeypatch: pytest.MonkeyPatch, watchdogs: list[_Watchdog],
) -> None:
    """9g: the 6-hour recheck raises ``CredentialNoncompliantError`` out of the TaskGroup; the
    exit is the refused key's (78), so the restarted process boots into credential recovery
    mode, never exit 1.

    Mutation: drop (or move after ``except* Exception``) the
    ``except* CredentialNoncompliantError`` branch: this test fails (ExceptionGroup)."""
    from bfx_funding_bot.core.errors import EXIT_CODE_AUTH_FAILED
    from bfx_funding_bot.modules.execution.credential_compliance import (
        CredentialNoncompliantError,
    )

    sent: list[tuple[str, dict[str, object]]] = []
    monkeypatch.setattr(alerts, "emit", lambda event, **fields: sent.append((event, fields)))
    noncompliant = ExceptionGroup("taskgroup", [
        CredentialNoncompliantError("withdraw_must_be_disabled")])

    async def build_daemon(*, stop_event: asyncio.Event) -> Any:
        return _fake_daemon(stop_event, [], fail=noncompliant, booted=True)

    monkeypatch.setattr(bot, "build_daemon", build_daemon)
    with pytest.raises(SystemExit) as exited:
        await bot._run()
    assert exited.value.code == EXIT_CODE_AUTH_FAILED == 78
    assert sent == [("daemon_fatal", {
        "error": "CredentialNoncompliantError: credential_noncompliant:withdraw_must_be_disabled"})]
    assert _RunRecord.events[-1] == "finish:fatal"


def _boot_refusals(caplog: pytest.LogCaptureFixture) -> list[tuple[str, str, list[str]]]:
    return [(r.event, r.reason, r.error_types) for r in caplog.records
            if r.name == "bfx.security" and r.levelno == logging.CRITICAL]


async def test_a_refused_build_is_a_boot_refused_security_event(
    monkeypatch: pytest.MonkeyPatch, watchdogs: list[_Watchdog], caplog: pytest.LogCaptureFixture,
) -> None:
    """9j: exception types only; the message (which may name a secret) stays in the alert,
    which scrubs it."""
    async def refuse(**_: object) -> None:
        raise ValueError("config_fatal password=S3cret")

    monkeypatch.setattr(bot, "build_daemon", refuse)
    with caplog.at_level(logging.WARNING), pytest.raises(ValueError):
        await bot._run()

    assert _boot_refusals(caplog) == [("boot_refused", "boot_failed", ["ValueError"])]
    assert all("S3cret" not in r.getMessage() for r in caplog.records if r.name == "bfx.security")


@pytest.mark.parametrize("booted", [False, True])
async def test_a_task_group_failure_is_a_boot_refusal_only_before_the_boot(
    monkeypatch: pytest.MonkeyPatch, watchdogs: list[_Watchdog], caplog: pytest.LogCaptureFixture,
    booted: bool,
) -> None:
    async def build_daemon(*, stop_event: asyncio.Event) -> Any:
        return _fake_daemon(stop_event, [], fail=RuntimeError("refused"), booted=booted)

    monkeypatch.setattr(bot, "build_daemon", build_daemon)
    with caplog.at_level(logging.WARNING), pytest.raises(ExceptionGroup):
        await bot._run()

    expected = [] if booted else [("boot_refused", "boot_failed", ["RuntimeError"])]
    assert _boot_refusals(caplog) == expected
