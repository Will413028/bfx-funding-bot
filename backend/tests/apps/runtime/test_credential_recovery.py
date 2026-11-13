"""Credential recovery mode: the venue refused the key at boot, so nothing trades and only
the credential rotation worker and liveness run until the key works again or is replaced.

A ``SimpleNamespace`` stands in for the daemon with the real ``Daemon`` methods bound to it:
anything recovery mode reaches that a test did not wire is an AttributeError. The waits
(backoff, settle window) are injected: each advances a fake clock, so no test waits on a
wall clock; the rotation-worker test blocks on the stop event instead.

Mutation checks (one at a time; revert after each):

1. recovery mode also starts the scheduler (or any trading task):
   ``test_recovery_mode_runs_the_rotation_worker_and_liveness_only`` fails.
4. recovery mode runs neither ``health_check`` nor ``db_keepalive`` (/healthz 503
   ``starting``): ``test_healthz_answers_200_and_readyz_503_in_recovery_mode`` fails.
5. a successful retry stops the run without waiting out the settle window:
   ``test_a_successful_retry_restarts_only_after_the_settle_window`` fails.
8. the retry does not compare the active credential:
   ``test_a_replaced_active_credential_restarts_without_asking_the_venue`` fails.
9. recovery mode runs trading control: ``test_recovery_mode_runs_the_rotation_worker_*``
   fails.
"""
from __future__ import annotations

import asyncio
import importlib
import logging
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from bfx_funding_bot.apps.runtime import daemon as daemon_module
from bfx_funding_bot.apps.runtime.daemon import (
    Daemon,
    _wait_or_stop,
    credential_recovery_retry_delay_s,
)
from bfx_funding_bot.apps.runtime.healthz import make_app
from bfx_funding_bot.apps.runtime.market_data import MarketDataRuntime
from bfx_funding_bot.core.crypto import encrypt_secret_with_aad
from bfx_funding_bot.core.db import Base
from bfx_funding_bot.core.errors import BootInvariantError, FatalError
from bfx_funding_bot.core.health import DB_FRESHNESS, MARKET_DATA_FRESHNESS, HealthProbe
from bfx_funding_bot.external.bitfinex.auth_rest import KeyPermissions
from bfx_funding_bot.external.bitfinex.errors import (
    BitfinexAPIError,
    VenueCredentialRejectedError,
)
from bfx_funding_bot.external.bitfinex.funding_book_level import FundingBookLevel
from bfx_funding_bot.modules.accounts.tables import ExchangeAccount, ExchangeAccountCredential
from bfx_funding_bot.modules.execution import operator_commands as commands
from bfx_funding_bot.modules.execution.credential_rotation import (
    BOT_SETTLE_WINDOW,
    CredentialRotationWorker,
)
from bfx_funding_bot.modules.execution.operator_commands import OperatorContext
from bfx_funding_bot.modules.execution.protocols import AccountContext
from bfx_funding_bot.modules.execution.readiness import TradingReadiness
from bfx_funding_bot.modules.ledger import CycleResult, Scope
from bfx_funding_bot.modules.ledger.wiring import build_scope_lock
from bfx_funding_bot.modules.market._internal.recorders.book_snapshot import BookSnapshotWriter
from bfx_funding_bot.modules.marketfeed.candle_writer import CandleWriter
from bfx_funding_bot.modules.observability.tables import BotRunRow
from tests.async_wait import until

# Every table on Base.metadata, foreign-key targets included.
importlib.import_module("bfx_funding_bot.apps.schema")

ACCOUNT = UUID("aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee")
BOOT_KEY, BOOT_SECRET = "FAKE-BOOT-KEY-000000", "FAKE-BOOT-SECRET-not-real-000000"
NEW_KEY, NEW_SECRET = "FAKE-NEW-KEY-111111", "FAKE-NEW-SECRET-not-real-111111"
KEK = bytes(range(32))
T0 = 1_800_000_000_000
SETTLE_MS = int(BOT_SETTLE_WINDOW.total_seconds() * 1000)
DIGEST_INVALID = '["error",10100,"apikey: digest invalid"]'

_METHODS = (
    "run", "_run_account", "_run_account_until_stopped", "_market_data_loop",
    "_boot_and_run", "_account_feed_loop", "_boot_until_observed", "_boot_attempt", "_run_boot_recovery",
    "_run_credential_recovery",
    "_credential_recovery_tasks", "_credential_recovery_retry_loop",
    "_active_credential_changed", "_end_credential_recovery", "_recovery_started_at_ms",
    "_heartbeat_scan_loop", "_db_keepalive_loop", "_dependency_answered",
    "_writer_lock_liveness_loop", "bind_run",
)


def _refused() -> BaseException:
    return VenueCredentialRejectedError(500, "Internal Server Error", DIGEST_INVALID)


class Venue:
    """The boot observation: each call takes the next outcome (an exception to raise, or
    ``"accepted"``); the last one repeats. ``on_call`` sees the call number first."""

    def __init__(self, *outcomes: Any, on_call: Callable[[int], None] | None = None) -> None:
        self.outcomes = outcomes
        self.calls = 0
        self.on_call = on_call

    async def run(self, scope: Scope) -> CycleResult:
        self.calls += 1
        if self.on_call is not None:
            self.on_call(self.calls)
        outcome = self.outcomes[min(self.calls, len(self.outcomes)) - 1]
        if isinstance(outcome, BaseException):
            raise outcome
        return CycleResult("accepted", uuid4())


class Clock:
    def __init__(self, now: int = T0) -> None:
        self.now = now

    def __call__(self) -> int:
        return self.now


@dataclass
class Recorder:
    """A task recovery mode must never start."""

    started: list[str] = field(default_factory=list)

    async def run(self, stop: asyncio.Event) -> None:
        self.started.append("run")

    async def start(self) -> None:
        self.started.append("start")


@pytest.fixture
async def engine(tmp_path: Any) -> AsyncIterator[AsyncEngine]:
    # A file, not one shared in-memory connection: recovery mode's tasks (keepalive, the
    # rotation worker, the retry) and the test open sessions concurrently.
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/recovery.db",
                                 connect_args={"timeout": 30})
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    envelope = encrypt_secret_with_aad(BOOT_SECRET, aad=str(ACCOUNT), kek=KEK)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory.begin() as session:
        session.add(ExchangeAccount(id=ACCOUNT, venue="bitfinex", label="test",
                                    lifecycle_status="active"))
        await session.flush()
        session.add(ExchangeAccountCredential(
            exchange_account_id=ACCOUNT, venue="bitfinex", label="boot", api_key=BOOT_KEY,
            secret_ciphertext=envelope.secret_ciphertext, secret_nonce=envelope.secret_nonce,
            wrapped_dek=envelope.wrapped_dek, dek_nonce=envelope.dek_nonce,
            key_version=envelope.key_version, lifecycle_status="active",
            verified_at=datetime.fromtimestamp(T0 / 1000, UTC) - timedelta(days=30),
            bitfinex_user_id=4242))
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest.fixture
def sent(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, dict[str, Any]]]:
    recorded: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(daemon_module.alerts, "emit",
                        lambda event, **fields: recorded.append((event, fields)))
    return recorded


def _daemon(engine: AsyncEngine, venue: Venue, *, clock: Clock | None = None,
            run_started_at_ms: int | None = T0, boot_key: str | None = BOOT_KEY,
            rotation: Any = None, writer_lock: Any = None,
            wait: Callable[..., Any] | None = None,
            delay: Callable[[int], float] = credential_recovery_retry_delay_s,
            compliance: Any = None) -> Any:
    clock = clock or Clock()
    waits: list[float] = []

    async def advance(stop: asyncio.Event, seconds: float) -> None:
        waits.append(seconds)
        clock.now += int(seconds * 1000)
        await asyncio.sleep(0)

    async def healthz() -> None:  # the real server binds a port; make_app is read directly
        await fake._stop_event.wait()

    fake = SimpleNamespace(
        config=SimpleNamespace(cells=[]), scheduler=MagicMock(), boot_mts=T0,
        boot_recovery=venue, observation_scope=Scope(ACCOUNT),
        protection=SimpleNamespace(run_pending=AsyncMock()), periodic_reconcile=None,
        writer_lock_watch=SimpleNamespace(check=AsyncMock(return_value=True)),
        writer_lock=writer_lock, boot_retry_delay_s=lambda _n: 0.0,
        _stop_event=asyncio.Event(), booted=False,
        probe=HealthProbe(), monitor=SimpleNamespace(scan_staleness=AsyncMock(return_value=[])),
        db_engine=engine, session_factory=async_sessionmaker(engine, expire_on_commit=False),
        trading_readiness=TradingReadiness(dependencies=(MARKET_DATA_FRESHNESS, DB_FRESHNESS)),
        account_bootstrap=SimpleNamespace(account_id=str(ACCOUNT), exchange_account_id=ACCOUNT),
        boot_credential_api_key=boot_key, run_started_at_ms=run_started_at_ms, clock=clock,
        credential_recovery_retry_delay_s=delay,
        credential_recovery_wait=wait or advance, waits=waits,
        credential_rotation=rotation, credential_compliance=compliance,
        credential_recovery_reason=None, market_data=None, public_data_failed=False,
        funding_book_service=None,
        account_failed=False,
        # Trading wiring recovery mode must never touch.
        trading_control=Recorder(), capital_policy_control=Recorder(),
        uncertainty_worker=Recorder(), periodic_reconcile_recorder=Recorder(),
        executor=SimpleNamespace(submit=AsyncMock()),
        _healthz_server_loop=healthz,
    )
    for name in _METHODS:
        setattr(fake, name, getattr(Daemon, name).__get__(fake))
    fake._scheduler_loop = fake.scheduler.start  # a MagicMock: any call is recorded
    return fake


def _trading_untouched(fake: Any) -> None:
    assert fake.booted is False
    assert fake.trading_control.started == []
    assert fake.capital_policy_control.started == []
    assert fake.uncertainty_worker.started == []
    fake.scheduler.start.assert_not_called()
    fake.executor.submit.assert_not_awaited()


# --- what runs --------------------------------------------------------------------------------

async def test_recovery_mode_runs_the_rotation_worker_and_liveness_only(engine, sent) -> None:
    rotation = SimpleNamespace(run=AsyncMock())
    venue = Venue(_refused(), on_call=lambda n: n == 3 and fake._stop_event.set())
    fake = _daemon(engine, venue, rotation=rotation, writer_lock=object())

    assert [name for name, _ in fake._credential_recovery_tasks()] == [
        "credential_rotation", "health_check", "db_keepalive", "healthz", "writer_lock",
        "credential_recovery_retry"]
    fake._writer_lock_liveness_loop = AsyncMock()  # its real loop waits 30s between refreshes

    await Daemon.run(fake)

    _trading_untouched(fake)
    rotation.run.assert_awaited_once_with(fake._stop_event)
    assert sent == [("credential_recovery_mode", {
        "level": "critical", "account": str(ACCOUNT), "reason": "venue_refused_key"})]


async def test_healthz_answers_200_and_readyz_503_in_recovery_mode(engine, sent) -> None:
    """The deploy health gate reads /healthz: a deploy into recovery mode succeeds (it changes
    no trading state); /readyz and the alert say it is not trading."""
    fake = _daemon(engine, Venue(_refused()), wait=_wait_or_stop, delay=lambda _n: 3600.0)
    task = asyncio.create_task(Daemon.run(fake))
    await until(lambda: "db_keepalive" in fake.probe.last_active_ts, what="liveness beat")
    client = TestClient(make_app(fake.probe, readiness=fake.trading_readiness))
    assert client.get("/healthz").status_code == 200
    assert client.get("/readyz").status_code == 503
    fake._stop_event.set()
    await task
    _trading_untouched(fake)


# --- how it ends ------------------------------------------------------------------------------

async def test_a_key_still_refused_keeps_waiting_with_a_slow_capped_backoff(engine, sent) -> None:
    venue = Venue(_refused(), on_call=lambda n: n == 6 and fake._stop_event.set())
    fake = _daemon(engine, venue)

    await Daemon.run(fake)

    assert venue.calls == 6  # the boot, then five retries
    assert fake.waits == [60.0, 120.0, 240.0, 300.0, 300.0]
    assert fake.writer_lock_watch.check.await_count == 5  # only ever as the single writer
    _trading_untouched(fake)


async def test_a_successful_retry_restarts_only_after_the_settle_window(
        engine, sent, caplog) -> None:
    clock = Clock(T0)
    fake = _daemon(engine, Venue(_refused(), "accepted"), clock=clock, run_started_at_ms=T0)

    with caplog.at_level(logging.INFO):
        await Daemon.run(fake)

    assert fake._stop_event.is_set()
    # The retry succeeded 60s into the run; the stop waited for the rest of the window.
    assert fake.waits == [60.0, (SETTLE_MS - 60_000) / 1000]
    assert clock.now - T0 >= SETTLE_MS
    assert "credential_recovery_resolved reason=boot_observation_succeeded" in caplog.text
    _trading_untouched(fake)


async def test_a_retry_after_the_settle_window_restarts_at_once(engine, sent) -> None:
    fake = _daemon(engine, Venue(_refused(), "accepted"), run_started_at_ms=T0 - SETTLE_MS)

    await Daemon.run(fake)

    assert fake._stop_event.is_set()
    assert fake.waits == [60.0]


async def test_without_a_recorded_run_the_window_counts_from_recovery_mode(engine, sent) -> None:
    clock = Clock(T0)
    fake = _daemon(engine, Venue(_refused(), "accepted"), clock=clock, run_started_at_ms=None)

    await Daemon.run(fake)

    assert fake.run_started_at_ms == T0
    assert clock.now - T0 >= SETTLE_MS


async def test_a_replaced_active_credential_restarts_without_asking_the_venue(
        engine, sent, caplog) -> None:
    """A key the web API put in place while the bot waits: the next boot loads it."""
    async with engine.begin() as conn:
        await conn.execute(text("UPDATE exchange_account_credentials SET api_key = :k"),
                           {"k": "FAKE-WEBAPI-KEY"})
    # Fail-safe: a loop that ignored the change would keep asking; stop it after a while.
    venue = Venue(_refused(), on_call=lambda n: n == 4 and fake._stop_event.set())
    fake = _daemon(engine, venue, run_started_at_ms=T0 - SETTLE_MS)

    with caplog.at_level(logging.INFO):
        await Daemon.run(fake)

    assert venue.calls == 1  # the boot only
    assert "credential_recovery_resolved reason=active_credential_changed" in caplog.text


@pytest.mark.parametrize("boot_key", [BOOT_KEY, None], ids=["same_key", "no_vault"])
async def test_the_same_key_or_no_vault_is_no_change(engine, sent, boot_key) -> None:
    venue = Venue(_refused(), on_call=lambda n: n == 3 and fake._stop_event.set())
    fake = _daemon(engine, venue, boot_key=boot_key)

    await Daemon.run(fake)

    assert venue.calls == 3


async def test_an_unreachable_venue_during_recovery_keeps_waiting(engine, sent) -> None:
    fake = _daemon(engine, Venue(_refused(), BitfinexAPIError(0, "transport error"), "accepted"),
                   run_started_at_ms=T0 - SETTLE_MS)

    await Daemon.run(fake)

    assert fake.boot_recovery.calls == 3
    assert fake._stop_event.is_set()


async def test_any_other_failure_still_refuses_the_boot(engine, sent) -> None:
    fake = _daemon(engine, Venue(_refused(), BootInvariantError("query admission refused")))

    with pytest.raises(ExceptionGroup) as refused:
        await Daemon.run(fake)

    assert refused.group_contains(BootInvariantError)
    _trading_untouched(fake)


async def test_a_stop_while_waiting_ends_recovery_cleanly(engine, sent) -> None:
    fake = _daemon(engine, Venue(_refused()), wait=_wait_or_stop, delay=lambda _n: 3600.0)
    task = asyncio.create_task(Daemon.run(fake))
    await until(lambda: "db_keepalive" in fake.probe.last_active_ts, what="recovery mode")
    fake._stop_event.set()
    await task
    assert fake.boot_recovery.calls == 1
    _trading_untouched(fake)


def test_the_retry_backoff_runs_from_one_to_five_minutes() -> None:
    assert [credential_recovery_retry_delay_s(n) for n in (1, 2, 3, 4, 5, 50)] == [
        60.0, 120.0, 240.0, 300.0, 300.0, 300.0]


# --- with the real rotation worker ------------------------------------------------------------

class _NewKeyClient:
    """The rotation's own client: only the new key is ever asked, never the refused one."""

    async def get_key_permissions(self, *, ctx: AccountContext) -> KeyPermissions:
        assert ctx.credentials.api_key == NEW_KEY
        return KeyPermissions(scopes={"funding": (True, True), "withdraw": (False, False)})

    async def get_user_id(self, *, ctx: AccountContext) -> int:
        assert ctx.credentials.api_key == NEW_KEY
        return 4242


async def test_an_applied_rotation_in_recovery_mode_ends_the_run(engine, sent) -> None:
    """The deadlock this mode removes: with the key refused at boot, the worker still runs,
    publishes its recipient key, applies the rotation (compared with the stored user id; the
    worker holds no client of the refused key) and stops the run for a boot on the new key."""
    factory = async_sessionmaker(engine, expire_on_commit=False)
    run_id = uuid4()
    started = T0 - 2 * SETTLE_MS
    async with factory.begin() as session:
        session.add(BotRunRow(run_id=run_id, exchange_account_id=ACCOUNT,
                              started_at_ms=started,
                              host_name="test", pid=1))
    holder: dict[str, Any] = {}
    restarts: list[int] = []

    def restart() -> None:
        restarts.append(1)
        holder["daemon"]._stop_event.set()

    async def authority(session: Any, *, account_id: UUID, user: str) -> bool:
        return True

    worker = CredentialRotationWorker(
        session_factory=factory, account_id=ACCOUNT, authority=authority,
        client_factory=_NewKeyClient,
        kek_loader=lambda: KEK, restart=restart,
        clock=lambda: T0, poll_interval_s=0.01, scope_lock=build_scope_lock())
    worker.bind_run(run_id, started)
    fake = _daemon(engine, Venue(_refused()), rotation=worker, run_started_at_ms=started,
                   wait=_wait_or_stop, delay=lambda _n: 3600.0)
    holder["daemon"] = fake
    task = asyncio.create_task(Daemon.run(fake))
    await until(lambda: worker.recipient_id is not None, what="recipient key published")
    async with factory.begin() as session:
        request_id = await commands.request_credential_rotation(
            session, OperatorContext(exchange_account_id=ACCOUNT, user_id="operator-1",
                                     role="owner", lifecycle_status="active"),
            api_key=NEW_KEY, api_secret=NEW_SECRET, label="rotated", reason="key revoked",
            now_ms=T0)
    async def settled() -> str | None:
        async with factory() as session:
            state = await session.scalar(text(
                "SELECT state FROM credential_rotation_requests WHERE request_id = :r"),
                {"r": request_id.hex})
        return None if state == "requested" else state

    await until(settled, what="the rotation settled")
    await until(lambda: restarts or task.done(), what="the restart")
    if not task.done():  # a refused rotation never restarts: end the run to report it
        holder["daemon"]._stop_event.set()
    await task
    async with factory() as session:
        row = (await session.execute(text(
            "SELECT state, outcome_reason FROM credential_rotation_requests "
            "WHERE request_id = :r"), {"r": request_id.hex})).one()
    assert row.state == "applied", row.outcome_reason
    assert restarts == [1]  # the worker's own restart ended the run
    _trading_untouched(fake)


# --- a key that is not compliant (9g) ---------------------------------------------------------
# Mutation: drop the compliance check from ``Daemon._boot_attempt``: the boot test fails (the
# daemon boots and trades). Mutation: have ``credential_refusal`` ignore
# ``CredentialNoncompliantError``: the boot raises instead of holding in recovery mode.
# Mutation: let ``_credential_recheck_loop`` swallow ``CredentialNoncompliantError``: the
# recheck test fails.

class Compliance:
    """The key's compliance check: each call takes the next outcome (an exception to raise, or
    None for compliant); the last one repeats."""

    def __init__(self, *outcomes: BaseException | None,
                 on_call: Callable[[int], None] | None = None) -> None:
        self.outcomes = outcomes or (None,)
        self.calls = 0
        self.on_call = on_call

    async def check(self) -> None:
        self.calls += 1
        if self.on_call is not None:
            self.on_call(self.calls)
        outcome = self.outcomes[min(self.calls, len(self.outcomes)) - 1]
        if outcome is not None:
            raise outcome


def _noncompliant(reason: str = "withdraw_must_be_disabled") -> BaseException:
    from bfx_funding_bot.modules.execution.credential_compliance import (
        CredentialNoncompliantError,
    )
    return CredentialNoncompliantError(reason)


async def test_a_noncompliant_key_at_boot_holds_in_recovery_mode_without_trading(
        engine, sent) -> None:
    compliance = Compliance(_noncompliant(),
                            on_call=lambda n: n == 3 and fake._stop_event.set())
    venue = Venue("accepted")
    fake = _daemon(engine, venue, compliance=compliance)

    await Daemon.run(fake)

    _trading_untouched(fake)
    assert venue.calls == 0  # the boot observation never ran on the non-compliant key
    assert compliance.calls == 3  # the boot, then the retries
    assert sent == [("credential_recovery_mode", {
        "level": "critical", "account": str(ACCOUNT),
        "reason": "credential_noncompliant:withdraw_must_be_disabled"})]


async def test_a_key_made_compliant_again_ends_recovery_mode(engine, sent, caplog) -> None:
    """D11: no separate stop state; once the whole boot passes, the run restarts and trades."""
    compliance = Compliance(_noncompliant(), None)
    fake = _daemon(engine, Venue("accepted"), compliance=compliance,
                   run_started_at_ms=T0 - SETTLE_MS)

    with caplog.at_level(logging.INFO):
        await Daemon.run(fake)

    assert fake._stop_event.is_set()
    assert "credential_recovery_resolved reason=boot_observation_succeeded" in caplog.text
    _trading_untouched(fake)


def _recheck(fake: Any, compliance: Compliance) -> Any:
    waits: list[float] = []

    async def wait(stop: asyncio.Event, seconds: float) -> None:
        waits.append(seconds)
        await asyncio.sleep(0)

    fake.credential_compliance = compliance
    fake.credential_recheck_interval_s = 21_600.0
    fake.credential_recheck_wait = wait
    fake.waits = waits
    return Daemon._credential_recheck_loop.__get__(fake)


async def test_the_running_recheck_raises_on_a_noncompliant_key(engine, sent) -> None:
    from bfx_funding_bot.modules.execution.credential_compliance import (
        CredentialNoncompliantError,
    )
    fake = _daemon(engine, Venue("accepted"))
    # The stop at the third call only ends a loop that wrongly kept going (no hang).
    loop = _recheck(fake, Compliance(None, _noncompliant("credential_account_mismatch"),
                                     on_call=lambda n: n >= 3 and fake._stop_event.set()))

    with pytest.raises(CredentialNoncompliantError) as raised:
        await loop()

    assert raised.value.reason == "credential_account_mismatch"
    assert fake.waits == [21_600.0, 21_600.0]  # every 6 hours


async def test_the_running_recheck_raises_on_a_key_the_venue_refused(engine, sent) -> None:
    wrapped = RuntimeError("permissions read failed")
    wrapped.__cause__ = BitfinexAPIError(500, "Internal Server Error", DIGEST_INVALID)
    fake = _daemon(engine, Venue("accepted"))
    loop = _recheck(fake, Compliance(wrapped))

    with pytest.raises(VenueCredentialRejectedError) as raised:
        await loop()

    assert raised.value.venue_coded and raised.value.__cause__ is wrapped


async def test_a_recheck_the_venue_did_not_answer_is_retried_sooner_not_fatal(
        engine, sent) -> None:
    from bfx_funding_bot.modules.execution.credential_compliance import (
        CREDENTIAL_RECHECK_RETRY_S,
    )
    fake = _daemon(engine, Venue("accepted"))
    compliance = Compliance(
        BitfinexAPIError(0, "transport error"),
        VenueCredentialRejectedError(403, "Forbidden", "<html>waf</html>"),
        None,
        on_call=lambda n: n == 4 and fake._stop_event.set())
    loop = _recheck(fake, compliance)

    await loop()

    assert compliance.calls == 4
    assert fake.waits == [21_600.0, CREDENTIAL_RECHECK_RETRY_S, CREDENTIAL_RECHECK_RETRY_S,
                          21_600.0]


# --- a recheck that reaches no verdict, a key a rotation replaced (9g review) ----------------
# Mutation: never emit ``credential_recheck_failing`` (or emit it on every failure): the streak
# test fails. Mutation: drop the ``CredentialSupersededError`` branch of the recovery retry
# loop: the superseded-in-recovery test raises out of ``Daemon.run``.

NONCE_SMALL = '["error",10114,"nonce: small"]'


async def test_a_recheck_that_keeps_failing_alerts_once_and_says_when_it_passes_again(
        engine, sent, caplog) -> None:
    """A refusal that is not the key's (10114) never passes: trading continues, the loop keeps
    retrying, and the operator hears it once the streak reaches the threshold."""
    from bfx_funding_bot.modules.execution.credential_compliance import (
        CREDENTIAL_RECHECK_ALERT_AFTER,
        CREDENTIAL_RECHECK_RETRY_S,
    )
    n = CREDENTIAL_RECHECK_ALERT_AFTER
    fake = _daemon(engine, Venue("accepted"))
    failing = BitfinexAPIError(500, "Internal Server Error", NONCE_SMALL)
    # n + 2 failures (the alert is sent once), a pass, then one more failure (a new streak,
    # far from the threshold); the stop at the next call ends the test.
    outcomes: list[BaseException | None] = [failing] * (n + 2) + [None, failing, None]
    compliance = Compliance(*outcomes, on_call=lambda c: c == len(outcomes) and
                            fake._stop_event.set())
    loop = _recheck(fake, compliance)

    with caplog.at_level(logging.INFO):
        await loop()  # never raises: nothing here is a verdict on the key

    assert sent == [
        ("credential_recheck_failing", {
            "level": "warning", "account": str(ACCOUNT), "recovered": False,
            "consecutive": n, "error": "BitfinexAPIError"}),
        ("credential_recheck_failing", {
            "level": "info", "account": str(ACCOUNT), "recovered": True, "after": n + 2}),
    ]
    assert fake.waits == ([21_600.0] + [CREDENTIAL_RECHECK_RETRY_S] * (n + 2)
                          + [21_600.0, CREDENTIAL_RECHECK_RETRY_S])
    assert f"credential_recheck_passed_again after={n + 2}" in caplog.text


async def test_a_recheck_on_a_key_a_rotation_replaced_waits_for_the_restart(
        engine, sent, caplog) -> None:
    from bfx_funding_bot.modules.execution.credential_compliance import (
        CREDENTIAL_RECHECK_RETRY_S,
        CredentialSupersededError,
    )
    fake = _daemon(engine, Venue("accepted"))
    loop = _recheck(fake, Compliance(CredentialSupersededError("replaced_by_rotation"),
                                     on_call=lambda c: c == 2 and fake._stop_event.set()))

    with caplog.at_level(logging.INFO):
        await loop()  # no exit 78: the rotation's restart ends the run

    assert fake.waits == [21_600.0, CREDENTIAL_RECHECK_RETRY_S]
    assert "credential_recheck_key_superseded reason=replaced_by_rotation" in caplog.text
    assert sent == []


async def test_a_key_superseded_during_recovery_ends_it_as_a_changed_credential(
        engine, sent, caplog) -> None:
    """The rotation lands between the retry's credential comparison and the check: the next
    attempt's comparison ends recovery mode, as for any replaced key."""
    from bfx_funding_bot.modules.execution.credential_compliance import (
        CredentialSupersededError,
    )

    class Rotated(Compliance):
        async def check(self) -> None:
            if self.calls == 1:  # the first retry: the rotation commits now
                async with engine.begin() as conn:
                    await conn.execute(text(
                        "UPDATE exchange_account_credentials SET api_key = :k"), {"k": NEW_KEY})
            await super().check()

    compliance = Rotated(_noncompliant(), CredentialSupersededError("replaced_by_rotation"),
                         on_call=lambda c: c == 5 and fake._stop_event.set())
    fake = _daemon(engine, Venue("accepted"), compliance=compliance,
                   run_started_at_ms=T0 - SETTLE_MS)

    with caplog.at_level(logging.INFO):
        await Daemon.run(fake)

    assert compliance.calls == 2  # the boot, then the retry that met the rotation
    assert "credential_recovery_key_superseded attempt=1 reason=replaced_by_rotation" in caplog.text
    assert "credential_recovery_resolved reason=active_credential_changed" in caplog.text
    _trading_untouched(fake)


# --- market data runs beside the account (R9b) -----------------------------------------------

class _BookRest:
    async def get_funding_book(self, *, symbol: str, length: int = 25) -> list[FundingBookLevel]:
        return [FundingBookLevel(rate=0.0002, period=2, count=1, amount=100.0)]


class _CrashingCandleWriter:
    async def run(self) -> None:
        raise RuntimeError("candle store gone")


def _market_data(fake: Any, *, writer: Any = None,
                 book: BookSnapshotWriter | None = None) -> MarketDataRuntime:
    queue: asyncio.Queue[Any] = asyncio.Queue()
    return MarketDataRuntime(
        cells=[], probe=fake.probe, candle_q=queue,
        writer=writer or CandleWriter(queue=queue, session_factory=fake.session_factory,
                                      probe=fake.probe),
        ws_client=None, bitfinex=MagicMock(), session_factory=fake.session_factory,
        book_snapshot_writer=book,
    )


async def _snapshot_count(engine: AsyncEngine) -> int:
    async with engine.connect() as conn:
        return int((await conn.execute(text(
            "SELECT count(*) FROM funding_book_snapshots"))).scalar_one())


async def test_book_snapshots_keep_writing_in_credential_recovery_mode(engine, sent) -> None:
    """Research recording does not depend on the account's key: while the boot holds in
    credential recovery mode (nothing trades), the book snapshot still writes."""
    fake = _daemon(engine, Venue(_refused()), wait=_wait_or_stop, delay=lambda _n: 3600.0)
    book = BookSnapshotWriter(rest=_BookRest(), session_factory=fake.session_factory,
                              symbols=["fUSD"], interval_s=3600)
    fake.market_data = _market_data(fake, book=book)
    task = asyncio.create_task(Daemon.run(fake))
    await until(lambda: fake.credential_recovery_reason is not None, what="recovery mode")
    for _ in range(200):
        if await _snapshot_count(engine) >= 1:
            break
        await asyncio.sleep(0.01)
    assert await _snapshot_count(engine) == 1
    fake._stop_event.set()
    await asyncio.wait_for(task, timeout=5)
    _trading_untouched(fake)
    assert [event for event, _ in sent] == ["credential_recovery_mode"]


async def test_a_recorder_failure_ends_the_run_with_a_market_data_error(engine, sent) -> None:
    """A recorder that dies is never silent: the run raises (``apps.bot`` exits non-zero)
    even while the account half is still waiting for its boot, and the failure is marked as
    market data's, not as a refused boot."""
    unreachable = BitfinexAPIError(status_code=0, message="transport error: connect failed")
    fake = _daemon(engine, Venue(unreachable), delay=lambda _n: 3600.0)
    fake.boot_retry_delay_s = lambda _n: 3600.0
    fake.market_data = _market_data(fake, writer=_CrashingCandleWriter())

    with pytest.raises(BaseExceptionGroup) as raised:
        await asyncio.wait_for(Daemon.run(fake), timeout=5)

    assert raised.group_contains(FatalError)
    assert fake.public_data_failed is True
    assert fake.booted is False
    assert Daemon.refused_boot.fget(fake) is False  # apps.bot reports it as fatal
    _trading_untouched(fake)


class _MarketDataFailingOnTeardown:
    async def run(self, stop: asyncio.Event) -> None:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            raise RuntimeError("ws close failed") from None


async def test_a_refused_boot_stays_refused_when_market_data_fails_while_torn_down(
        engine, sent) -> None:
    """The account refused the boot first; a market task raising while it is cancelled does
    not turn the refused boot into a market data failure."""
    fake = _daemon(engine, Venue(BootInvariantError("boot invariant broken")))
    fake.market_data = _MarketDataFailingOnTeardown()

    with pytest.raises(BaseExceptionGroup) as raised:
        await asyncio.wait_for(Daemon.run(fake), timeout=5)

    assert raised.group_contains(BootInvariantError)
    assert fake.public_data_failed is False
    assert Daemon.refused_boot.fget(fake) is True


class _BookFeed:
    """The account's book feed: delivers frames ``frame_age_ms`` old until stopped, or
    raises ``fail`` (at once, or only when cancelled with ``on_teardown``)."""

    def __init__(self, frame_age_ms: int | None = 500, fail: Exception | None = None,
                 on_teardown: bool = False) -> None:
        self.frame_age_ms = frame_age_ms
        self.fail = fail
        self.on_teardown = on_teardown

    def last_frame_age_ms(self) -> int | None:
        return self.frame_age_ms

    async def run(self, stop: asyncio.Event) -> None:
        if self.fail is not None and not self.on_teardown:
            raise self.fail
        try:
            await stop.wait()
        except asyncio.CancelledError:
            if self.fail is not None:
                raise self.fail from None
            raise


async def test_the_book_feed_clears_ws_data_while_the_boot_still_waits(engine, sent) -> None:
    """ws_data comes from the account's own book feed, which starts before the boot: by the
    time the boot completes the market view is already known fresh (R9c)."""
    unreachable = BitfinexAPIError(status_code=0, message="transport error: connect failed")
    fake = _daemon(engine, Venue(unreachable))
    fake.boot_retry_delay_s = lambda _n: 3600.0
    fake.funding_book_service = _BookFeed(frame_age_ms=500)
    task = asyncio.create_task(Daemon.run(fake))

    await until(lambda: MARKET_DATA_FRESHNESS in fake.probe.last_active_ts, what="ws_data beat")
    assert fake.booted is False
    assert fake.trading_readiness.snapshot().dependency != MARKET_DATA_FRESHNESS
    fake._stop_event.set()
    await asyncio.wait_for(task, timeout=5)
    _trading_untouched(fake)


async def test_a_book_feed_failure_before_the_boot_is_fatal_not_a_refused_boot(engine, sent) -> None:
    unreachable = BitfinexAPIError(status_code=0, message="transport error: connect failed")
    fake = _daemon(engine, Venue(unreachable))
    fake.boot_retry_delay_s = lambda _n: 3600.0
    fake.funding_book_service = _BookFeed(fail=RuntimeError("book ws refused"))

    with pytest.raises(BaseExceptionGroup) as raised:
        await asyncio.wait_for(Daemon.run(fake), timeout=5)

    assert raised.group_contains(RuntimeError)
    assert fake.public_data_failed is True
    assert Daemon.refused_boot.fget(fake) is False
    _trading_untouched(fake)


async def test_a_refused_boot_stays_refused_when_the_book_feed_fails_while_torn_down(
        engine, sent) -> None:
    fake = _daemon(engine, Venue(BootInvariantError("boot invariant broken")))
    fake.funding_book_service = _BookFeed(fail=RuntimeError("ws close failed"), on_teardown=True)

    with pytest.raises(BaseExceptionGroup) as raised:
        await asyncio.wait_for(Daemon.run(fake), timeout=5)

    assert raised.group_contains(BootInvariantError)
    assert fake.public_data_failed is False
    assert Daemon.refused_boot.fget(fake) is True
