"""Telegram alert sink (T8): never in the way of trading, de-duplicated, rate-limited.

Hooks under test live in safety/trading_state.py, safety/protection.py and
safety/kill_switch.py; the daemon only installs the sink and reports a refused
boot. A hung or failing Telegram must not delay a single trading-path call.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import httpx
import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker

import bfx_funding_bot.modules.execution.event_store.tables
import bfx_funding_bot.modules.execution.safety.tables
import bfx_funding_bot.modules.execution.uncertainty_tables  # noqa: F401
from bfx_funding_bot.core.db import Base
from bfx_funding_bot.modules.accounts.tables import ExchangeAccount
from bfx_funding_bot.modules.execution.protocols import FundingCancelAllResult
from bfx_funding_bot.modules.execution.safety.kill_switch import KillSwitch
from bfx_funding_bot.modules.execution.safety.protection import AutomaticProtection
from bfx_funding_bot.modules.execution.safety.trading_state import TradingStateRepository
from bfx_funding_bot.modules.observability import alerts

# A fake bot token, assembled at run time so no token-shaped literal sits in the
# source for secret scanners; same shape as a real one (digits:35 url-safe chars).
TOKEN = "1234" + "56789:" + "fake-" + "TelegramToken" + "x" * 17


class Recorder:
    """A transport that records, and can hang or fail on demand."""

    def __init__(self, *, hang: bool = False, fail: bool = False) -> None:
        self.sent: list[str] = []
        self.hang, self.fail = hang, fail
        self.closed = False

    async def send(self, text: str) -> None:
        if self.hang:
            await asyncio.Event().wait()
        if self.fail:
            raise alerts.AlertDeliveryError("telegram_http_502")
        self.sent.append(text)

    async def aclose(self) -> None:
        self.closed = True


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def installed() -> Any:
    """Install a recording sink process-wide; restore the previous one afterwards."""
    sinks: list[alerts.AlertSink] = []

    def install(transport: Any = None, **options: Any) -> alerts.AlertSink:
        options.setdefault("context", "ci")
        sink = alerts.AlertSink(transport if transport is not None else Recorder(), **options)
        sinks.append(alerts.install(sink))
        return sink

    yield install
    if sinks:
        alerts.install(sinks[0])


async def _drain(sink: alerts.AlertSink) -> None:
    await sink.aclose(timeout_s=2.0)


# --------------------------------------------------------------------------- never blocks


@pytest.mark.asyncio
async def test_emit_returns_at_once_while_telegram_hangs(installed: Any) -> None:
    transport = Recorder(hang=True)
    sink = installed(transport, send_timeout_s=0.05)
    started = time.perf_counter()
    for index in range(50):
        alerts.emit("probe", index=index)
    assert time.perf_counter() - started < 0.05
    await asyncio.sleep(0.2)
    assert sink.counts.get("failed", 0) >= 1          # timed out, logged, counted
    await _drain(sink)


@pytest.mark.asyncio
async def test_full_queue_drops_and_counts_instead_of_blocking(installed: Any) -> None:
    sink = installed(Recorder(hang=True), queue_size=2, rate_capacity=100,
                     send_timeout_s=60)
    observed: list[tuple[str, str]] = []
    sink.observer = lambda event, outcome: observed.append((event, outcome))
    for index in range(6):
        alerts.emit("probe", index=index)
    await asyncio.sleep(0)
    assert sink.counts["queue_full"] >= 3
    assert ("probe", "queue_full") in observed
    await _drain(sink)


@pytest.mark.asyncio
async def test_failed_delivery_is_logged_and_the_next_alert_still_goes_out(
    installed: Any, caplog: pytest.LogCaptureFixture,
) -> None:
    transport = Recorder(fail=True)
    sink = installed(transport)
    alerts.emit("first")
    await asyncio.sleep(0.05)
    transport.fail = False
    alerts.emit("second")
    await _drain(sink)
    assert sink.counts == {"failed": 1, "sent": 1}
    assert [line.split("] ", 1)[1].split("\n")[0] for line in transport.sent] == ["second"]
    assert "alert_delivery_failed event=first error=telegram_http_502" in caplog.text


def test_emit_never_raises_even_on_unrenderable_fields(installed: Any) -> None:
    class Hostile:
        def __str__(self) -> str:
            raise RuntimeError("boom")

    installed()
    alerts.emit("probe", value=Hostile())  # must not raise


@pytest_asyncio.fixture
async def trading(sqlite_engine: Any) -> Any:
    async with sqlite_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(sqlite_engine, expire_on_commit=False)
    account = uuid4()
    async with factory.begin() as session:
        session.add(ExchangeAccount(id=account, venue="bitfinex", label="alerts"))
    return factory, TradingStateRepository(factory, account_id=account, deployment_environment="ci")


@pytest.mark.asyncio
async def test_trading_path_is_not_delayed_by_a_hung_telegram(installed: Any, trading: Any) -> None:
    sink = installed(Recorder(hang=True), send_timeout_s=30)
    _, repo = trading
    started = time.perf_counter()
    result = await repo.transition("HALTED", cause="operator", actor="will", reason="maintenance")
    elapsed = time.perf_counter() - started
    assert result.changed and elapsed < 1.0          # the write never waits for delivery
    assert (await repo.current()) is not None and (await repo.current()).state == "HALTED"
    await sink.aclose(timeout_s=0.05)                  # hung delivery is abandoned, not awaited


# --------------------------------------------------------------------------- dedup / rate limit


@pytest.mark.asyncio
async def test_same_trigger_is_deduplicated_then_reported_with_a_count(installed: Any) -> None:
    clock = Clock()
    transport = Recorder()
    sink = installed(transport, clock=clock, dedup_window_s=600)
    for detail in ("seq=1", "seq=2", "seq=3"):
        alerts.emit(alerts.PROTECTION_TRIPPED, trigger="submit_outcome_unknown", detail=detail)
    alerts.emit(alerts.PROTECTION_TRIPPED, trigger="orphan_quarantined", detail="555")
    clock.now += 601
    alerts.emit(alerts.PROTECTION_TRIPPED, trigger="submit_outcome_unknown", detail="seq=4")
    await _drain(sink)
    assert len(transport.sent) == 3
    assert sink.counts["deduplicated"] == 2
    assert "(2 repeat(s) suppressed)" in transport.sent[-1]


@pytest.mark.asyncio
async def test_rate_limit_drops_then_reports_the_drop_count(installed: Any) -> None:
    clock = Clock()
    transport = Recorder()
    sink = installed(transport, clock=clock, rate_capacity=2, rate_refill_per_s=0.1)
    for index in range(5):
        alerts.emit("probe", index=index)
    assert sink.counts["rate_limited"] == 3
    clock.now += 10                                    # one token back
    alerts.emit("probe", index=99)
    await _drain(sink)
    assert len(transport.sent) == 3
    assert "(3 earlier alert(s) dropped by the rate limit)" in transport.sent[-1]


# --------------------------------------------------------------------------- one per event type


@pytest.mark.asyncio
async def test_trading_state_transition_alerts_after_commit_with_cause_actor_reason(
    installed: Any, trading: Any,
) -> None:
    transport = Recorder()
    sink = installed(transport)
    _, repo = trading
    await repo.transition("HALTED", cause="operator", actor="will", reason="maintenance")
    await repo.transition("HALTED", cause="operator", actor="will", reason="again")  # restates: no row
    resumed = await repo.transition("ACTIVE", cause="operator", actor="will", reason="resume")
    await _drain(sink)
    assert sink.counts == {"sent": 2}                  # the restatement emitted nothing at all
    halted, active = transport.sent
    assert halted.startswith("[bfx][CRITICAL][ci/") and "trading state none -> HALTED" in halted
    for line in ("cause=operator", "actor=will", "reason=maintenance"):
        assert line in halted
    assert active.startswith("[bfx][INFO]") and "HALTED -> ACTIVE" in active
    assert f"state_id={resumed.state.id}" in active


class _CommitFails:
    """A session factory whose transaction is rolled back and then raises on commit."""

    def __init__(self, factory: Any) -> None:
        self._factory = factory

    def __call__(self) -> Any:
        return self._factory()

    def begin(self) -> Any:
        factory = self._factory

        class _Tx:
            async def __aenter__(self) -> Any:
                self._cm = factory.begin()
                return await self._cm.__aenter__()

            async def __aexit__(self, *exc: Any) -> None:
                await self._cm.__aexit__(RuntimeError, RuntimeError("commit failed"), None)
                raise RuntimeError("commit failed")

        return _Tx()


@pytest.mark.asyncio
async def test_no_alert_for_a_transition_that_was_not_committed(installed: Any, trading: Any) -> None:
    transport = Recorder()
    sink = installed(transport)
    factory, repo = trading
    failing = TradingStateRepository(_CommitFails(factory), account_id=repo.account_id,  # type: ignore[arg-type]
                                     deployment_environment="ci")
    with pytest.raises(RuntimeError, match="commit failed"):
        await failing.transition("HALTED", cause="operator", actor="will", reason="x")
    assert await repo.current() is None
    await _drain(sink)
    assert transport.sent == [] and sink.counts == {}


@pytest.mark.asyncio
@pytest.mark.parametrize(("trigger", "title"), [
    ("submit_outcome_unknown", "automatic HALT: UNKNOWN submit"),
    ("orphan_quarantined", "automatic HALT: orphan offer quarantined"),
    ("writer_lock_lost", "automatic HALT: writer lock lost"),
    ("loss_limiter", "automatic HALT: loss limiter"),
])
async def test_protection_trip_alerts_with_its_trigger(installed: Any, trigger: str, title: str) -> None:
    transport = Recorder()
    sink = installed(transport)
    AutomaticProtection(clock=lambda: 1).trip(trigger, "detail text")
    await _drain(sink)
    assert len(transport.sent) == 1
    assert transport.sent[0].startswith("[bfx][CRITICAL]") and title in transport.sent[0]
    assert f"trigger={trigger}" in transport.sent[0] and "detail=detail text" in transport.sent[0]


class Venue:
    def __init__(self, failing: set[str]) -> None:
        self.failing = failing

    async def cancel_all_funding_offers(self, *, currency: str, ctx: Any) -> FundingCancelAllResult:
        if currency in self.failing:
            raise RuntimeError(f"venue unavailable for {currency}")
        return FundingCancelAllResult(outcome="acknowledged", venue_status="SUCCESS",
                                      text="Cancelled all funding offers")


class Lock:
    async def verify_held(self) -> bool:
        return True


@pytest.mark.asyncio
@pytest.mark.parametrize(("failing", "level", "title"), [
    (set(), "WARNING", "cancel-all complete"),
    ({"USD"}, "CRITICAL", "cancel-all INCOMPLETE"),
])
async def test_kill_switch_alerts_its_cancel_all_result(
    installed: Any, trading: Any, failing: set[str], level: str, title: str,
) -> None:
    transport = Recorder()
    sink = installed(transport)
    factory, repo = trading
    ctx = SimpleNamespace(credentials=SimpleNamespace(api_key="key-" * 4, api_secret="sec-" * 4))
    result = await KillSwitch(
        trading_state=repo, session_factory=factory, ctx=ctx,  # type: ignore[arg-type]
        configured_symbols={"fUST", "fUSD"}, venue=Venue(failing), writer_lock=Lock(),
        clock=lambda: 5000,
    ).engage(cause="operator", actor="will", reason="stop everything")
    await _drain(sink)
    kill = next(text for text in transport.sent if "cancel-all" in text)
    assert kill.startswith(f"[bfx][{level}]") and title in kill
    assert f"state_id={result.state.id}" in kill and "cause=operator" in kill
    assert ("not_acknowledged=[('USD', 'failed')]" in kill) == bool(failing)
    assert any("trading state none -> HALTED" in text for text in transport.sent)


def test_boot_refused_fatal_and_future_events_render() -> None:
    for event, fields, expected in (
        (alerts.BOOT_REFUSED, {"error": "ValueError: bad config"}, "[CRITICAL]"),
        (alerts.DAEMON_FATAL, {"error": "RuntimeError: x"}, "[CRITICAL]"),
        ("material_deploy_awaiting_approval", {"revision": "abc"}, "[WARNING]"),  # T5-style
    ):
        text = alerts.render(event, level=alerts.default_level(event, fields), fields=fields,
                             context="prod", host="oci-a1")
        assert expected in text and "[prod/oci-a1]" in text
    assert "bot refused to boot" in alerts.render(alerts.BOOT_REFUSED, level="critical",
                                                  fields={}, host="h")


@pytest.mark.asyncio
async def test_daemon_reports_a_refused_boot_before_it_exits(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    from bfx_funding_bot.modules.marketfeed import daemon as daemon_module

    async def refuse() -> None:
        raise ValueError("config_fatal: BFX_EXCHANGE_ACCOUNT_ID missing")

    previous = alerts.current()
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    monkeypatch.setattr(daemon_module, "build_daemon", refuse)
    try:
        with caplog.at_level(logging.WARNING), pytest.raises(ValueError):
            await daemon_module._run()
        sink = alerts.current()
        assert sink is not previous and sink.counts == {"log_only": 1}
        assert "alerts_telegram_not_configured" in caplog.text
        assert "bot refused to boot" in caplog.text and "BFX_EXCHANGE_ACCOUNT_ID missing" in caplog.text
    finally:
        alerts.install(previous)


# --------------------------------------------------------------------------- configuration


def test_missing_telegram_config_degrades_to_log_only_and_says_so_once(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.INFO):
        sink = alerts.AlertSink.from_environment({"BFX_DEPLOYMENT_ENV": "prod"})
        sink.emit(alerts.PROTECTION_TRIPPED, trigger="writer_lock_lost", detail="x")
        sink.emit(alerts.PROTECTION_TRIPPED, trigger="orphan_quarantined", detail="y")
    assert not sink.delivers and sink.counts == {"log_only": 2}
    assert caplog.text.count("alerts_telegram_not_configured") == 1
    assert "automatic HALT: writer lock lost" in caplog.text


@pytest.mark.parametrize("environ", [
    {"TELEGRAM_BOT_TOKEN": TOKEN},
    {"TELEGRAM_CHAT_ID": "-100123"},
    {"TELEGRAM_BOT_TOKEN": "not-a-token", "TELEGRAM_CHAT_ID": "-100123"},
])
def test_partial_or_malformed_config_is_log_only(environ: dict[str, str],
                                                  caplog: pytest.LogCaptureFixture) -> None:
    sink = alerts.AlertSink.from_environment(environ)
    assert not sink.delivers
    assert "alerts_telegram_misconfigured" in caplog.text


def test_valid_config_delivers_to_telegram() -> None:
    sink = alerts.AlertSink.from_environment({"TELEGRAM_BOT_TOKEN": TOKEN, "TELEGRAM_CHAT_ID": "-100123"})
    assert sink.delivers and isinstance(sink.transport, alerts.TelegramTransport)


def test_secret_values_never_reach_an_alert() -> None:
    environ = {"DATABASE_URL": "postgresql://bfx_bot:S3cretPassw0rd@bfx-postgres:5432/bfx",
               "BFX_API_SECRET": "api-secret-value", "TELEGRAM_BOT_TOKEN": TOKEN,
               "TELEGRAM_CHAT_ID": "-100123"}
    transport = Recorder()
    sink = alerts.AlertSink(transport, secrets=alerts.secret_values(environ))
    sink.emit(alerts.BOOT_REFUSED, error="OperationalError: password S3cretPassw0rd rejected; "
                                         f"api-secret-value {TOKEN}")
    text = sink._queue.get_nowait().text
    for secret in ("S3cretPassw0rd", "api-secret-value", TOKEN):
        assert secret not in text
    assert text.count("[redacted]") == 3


@pytest.mark.asyncio
async def test_telegram_transport_posts_the_message_and_hides_the_token_on_failure() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200 if len(seen) == 1 else 429)

    transport = alerts.TelegramTransport(
        token=TOKEN, chat_id="-100123",
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    await transport.send("hello")
    assert str(seen[0].url) == f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    assert json.loads(seen[0].content) == {"chat_id": "-100123", "text": "hello",
                                           "disable_web_page_preview": True}
    with pytest.raises(alerts.AlertDeliveryError) as refused:
        await transport.send("again")
    assert str(refused.value) == "telegram_http_429" and TOKEN not in str(refused.value)
    await transport.aclose()


@pytest.mark.asyncio
async def test_shutdown_flushes_queued_alerts_within_the_timeout(installed: Any) -> None:
    transport = Recorder()
    installed(transport)
    alerts.emit(alerts.BOOT_REFUSED, error="ValueError: x")
    await alerts.shutdown(timeout_s=1.0)
    assert len(transport.sent) == 1 and transport.closed
