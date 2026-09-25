"""Operator alerts to Telegram from inside the bot, never in the way of trading.

Plan §1 "Alerts" / ADR 2026-09-25 D5: trading-state changes, automatic
protections (UNKNOWN submit, orphan quarantine, writer lock loss, ...), the
kill switch's cancel-all result and a refused boot reach the operator within
seconds. Grafana stays the second path; every accepted alert is also logged.

Contract (the reason this module exists in this shape):

- :func:`emit` is synchronous, O(1) and never raises or waits. It renders the
  message, applies de-duplication and a rate limit, and ``put_nowait``s it on a
  bounded queue. A full queue drops the alert (logged and counted), it never
  blocks the caller -- callers sit on the submit, kill and reconcile paths.
- One background task delivers, with a per-message timeout. A failed or slow
  Telegram is logged and counted; nothing is retried into a backlog.
- Without ``TELEGRAM_BOT_TOKEN`` / ``TELEGRAM_CHAT_ID`` the sink logs only, and
  says so once at boot.
- De-duplication: the same event with the same identifying fields within
  ``dedup_window_s`` is suppressed; the next one that gets through says how
  many were. Rate limit: a token bucket over all events; alerts dropped by it
  are counted and reported on the next delivered message.

Wiring: the daemon installs the configured sink with :func:`install`; hooks
call the module-level :func:`emit`. Before installation (tests, scripts) the
default sink only logs. New event names need no registration -- a hook just
calls ``emit("foreign_exposure", level="warning", ...)``.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import socket
import time
import urllib.parse
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Final, Protocol

import httpx

log = logging.getLogger(__name__)

INFO: Final = "info"
WARNING: Final = "warning"
CRITICAL: Final = "critical"
_LOG_LEVELS: Final = {INFO: logging.INFO, WARNING: logging.WARNING, CRITICAL: logging.CRITICAL}

# Event names used by the hooks in this repository. Others are accepted as-is.
TRADING_STATE_CHANGED: Final = "trading_state_changed"
PROTECTION_TRIPPED: Final = "protection_tripped"
KILL_SWITCH_ENGAGED: Final = "kill_switch_engaged"
BOOT_REFUSED: Final = "boot_refused"
DAEMON_FATAL: Final = "daemon_fatal"

# Fields that identify "the same event" for de-duplication. Unlisted events
# de-duplicate on all of their fields.
DEDUP_FIELDS: Final[Mapping[str, tuple[str, ...]]] = {
    TRADING_STATE_CHANGED: ("state_id",),          # every committed transition is news
    PROTECTION_TRIPPED: ("trigger",),              # a storm of one trigger is one alert
    KILL_SWITCH_ENGAGED: ("state_id", "complete"),  # retries of the same kill
    BOOT_REFUSED: ("error",),
    DAEMON_FATAL: ("error",),
}

# Human titles for the protection triggers the plan names explicitly.
TRIGGER_TITLES: Final[Mapping[str, str]] = {
    "submit_outcome_unknown": "UNKNOWN submit",
    "orphan_quarantined": "orphan offer quarantined",
    "writer_lock_lost": "writer lock lost",
    "command_rate_exceeded": "venue write rate kept exceeding its limit",
    "loss_limiter": "loss limiter",
}

# Values of these keys are scrubbed from every alert (exception text can carry them).
SECRET_ENV_KEYS: Final = ("TELEGRAM_BOT_TOKEN", "BFX_API_KEY", "BFX_API_SECRET",
                          "BFX_ADMIN_TOKEN", "BFX_VAULT_KEK")
_TOKEN = re.compile(r"[0-9]{3,20}:[A-Za-z0-9_-]{20,100}")
_CHAT_ID = re.compile(r"-?[0-9]{1,20}|@[A-Za-z0-9_]{4,64}")
_MAX_TEXT = 3900
_FIELD_LIMIT = 300


class Transport(Protocol):
    async def send(self, text: str) -> None: ...
    async def aclose(self) -> None: ...


class AlertDeliveryError(RuntimeError):
    """Telegram did not accept the message. Carries a status, never the URL."""


class TelegramTransport:
    """POST sendMessage. The token is in the URL, so no error text is ever logged."""

    def __init__(self, *, token: str, chat_id: str, timeout_s: float = 5.0,
                 client: httpx.AsyncClient | None = None) -> None:
        self._url = f"https://api.telegram.org/bot{token}/sendMessage"
        self._chat_id = chat_id
        self._client = client or httpx.AsyncClient(timeout=timeout_s)

    async def send(self, text: str) -> None:
        response = await self._client.post(self._url, json={
            "chat_id": self._chat_id, "text": text, "disable_web_page_preview": True,
        })
        if response.status_code != 200:
            raise AlertDeliveryError(f"telegram_http_{response.status_code}")

    async def aclose(self) -> None:
        await self._client.aclose()


# (event, outcome) -> None; outcome in sent|failed|deduplicated|rate_limited|queue_full|log_only
Observer = Callable[[str, str], None]


@dataclass(slots=True)
class _Recent:
    at: float
    suppressed: int = 0


@dataclass(frozen=True, slots=True)
class _Alert:
    event: str
    text: str


class AlertSink:
    """Bounded, de-duplicated, rate-limited alert queue with one delivery task."""

    def __init__(
        self,
        transport: Transport | None,
        *,
        context: str = "",
        queue_size: int = 100,
        send_timeout_s: float = 10.0,
        dedup_window_s: float = 600.0,
        rate_capacity: float = 10.0,
        rate_refill_per_s: float = 1.0 / 6.0,
        clock: Callable[[], float] = time.monotonic,
        observer: Observer | None = None,
        secrets: tuple[str, ...] = (),
    ) -> None:
        self.transport = transport
        self.secrets = tuple(secret for secret in secrets if len(secret) >= 6)
        self.context = context
        self.queue_size = queue_size
        self.send_timeout_s = send_timeout_s
        self.dedup_window_s = dedup_window_s
        self.rate_capacity = rate_capacity
        self.rate_refill_per_s = rate_refill_per_s
        self.clock = clock
        self.observer = observer
        self.counts: dict[str, int] = {}
        self._queue: asyncio.Queue[_Alert] = asyncio.Queue(maxsize=queue_size)
        self._recent: dict[tuple[Any, ...], _Recent] = {}
        self._tokens = rate_capacity
        self._refilled_at = clock()
        self._rate_dropped = 0
        self._worker: asyncio.Task[None] | None = None

    @classmethod
    def from_environment(cls, environ: Mapping[str, str], **options: Any) -> AlertSink:
        """Telegram when both keys are valid; otherwise log-only (said once, at boot)."""
        token = environ.get("TELEGRAM_BOT_TOKEN", "").strip()
        chat_id = environ.get("TELEGRAM_CHAT_ID", "").strip()
        context = options.pop("context", None) or environ.get("BFX_DEPLOYMENT_ENV", "")
        options.setdefault("secrets", secret_values(environ))
        if not token and not chat_id:
            log.warning("alerts_telegram_not_configured: alerts are logged only "
                        "(set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in bot.env)")
            return cls(transport=None, context=context, **options)
        if _TOKEN.fullmatch(token) is None or _CHAT_ID.fullmatch(chat_id) is None:
            log.warning("alerts_telegram_misconfigured: TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID "
                        "missing or malformed; alerts are logged only")
            return cls(transport=None, context=context, **options)
        return cls(transport=TelegramTransport(token=token, chat_id=chat_id),
                   context=context, **options)

    @property
    def delivers(self) -> bool:
        return self.transport is not None

    def emit(self, event: str, *, level: str | None = None, **fields: object) -> None:
        """Queue one alert. Never raises, never blocks."""
        try:
            self._emit(event, level=level or default_level(event, fields), fields=fields)
        except Exception:
            with contextlib.suppress(Exception):
                log.exception("alert_emit_failed event=%s", event)

    def _emit(self, event: str, *, level: str, fields: Mapping[str, object]) -> None:
        now = self.clock()
        key = (event, *((name, str(fields.get(name))) for name in
                        DEDUP_FIELDS.get(event, tuple(sorted(fields)))))
        self._prune(now)
        recent = self._recent.get(key)
        if recent is not None and now - recent.at < self.dedup_window_s:
            recent.suppressed += 1
            self._count(event, "deduplicated")
            return
        self._refill(now)
        if self._tokens < 1:
            self._rate_dropped += 1
            self._count(event, "rate_limited")
            log.warning("alert_rate_limited event=%s", event)
            return
        self._tokens -= 1
        notes = []
        if recent is not None and recent.suppressed:
            notes.append(f"{recent.suppressed} repeat(s) suppressed")
        if self._rate_dropped:
            notes.append(f"{self._rate_dropped} earlier alert(s) dropped by the rate limit")
        text = render(event, level=level, fields=fields, context=self.context, notes=notes)
        for secret in self.secrets:
            text = text.replace(secret, "[redacted]")
        log.log(_LOG_LEVELS.get(level, logging.WARNING), "alert %s", text)
        self._recent[key] = _Recent(at=now)
        if self.transport is None:
            self._rate_dropped = 0
            self._count(event, "log_only")
            return
        try:
            self._queue.put_nowait(_Alert(event=event, text=text))
        except asyncio.QueueFull:
            self._count(event, "queue_full")
            log.warning("alert_dropped_queue_full event=%s size=%d", event, self.queue_size)
            return
        self._rate_dropped = 0
        self._ensure_worker()

    def _refill(self, now: float) -> None:
        elapsed = max(0.0, now - self._refilled_at)
        self._tokens = min(self.rate_capacity, self._tokens + elapsed * self.rate_refill_per_s)
        self._refilled_at = now

    def _prune(self, now: float) -> None:
        if len(self._recent) < 256:
            return
        for key in [k for k, v in self._recent.items() if now - v.at >= self.dedup_window_s]:
            del self._recent[key]

    def _count(self, event: str, outcome: str) -> None:
        self.counts[outcome] = self.counts.get(outcome, 0) + 1
        if self.observer is not None:
            with contextlib.suppress(Exception):
                self.observer(event, outcome)

    def _ensure_worker(self) -> None:
        if self._worker is not None and not self._worker.done():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return  # queued; delivered once a loop runs and another alert starts the worker
        self._worker = loop.create_task(self._deliver(), name="telegram_alerts")

    async def _deliver(self) -> None:
        assert self.transport is not None
        while True:
            alert = await self._queue.get()
            try:
                await asyncio.wait_for(self.transport.send(alert.text), timeout=self.send_timeout_s)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._count(alert.event, "failed")
                log.warning("alert_delivery_failed event=%s error=%s", alert.event,
                            type(exc).__name__ if not isinstance(exc, AlertDeliveryError) else exc)
            else:
                self._count(alert.event, "sent")
            finally:
                self._queue.task_done()

    async def aclose(self, *, timeout_s: float = 5.0) -> None:
        """Deliver what is queued within ``timeout_s``, then stop. For exits and tests."""
        if self._worker is None and not self._queue.empty():
            self._ensure_worker()
        if self._worker is not None:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._queue.join(), timeout=timeout_s)
            self._worker.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._worker
            self._worker = None
        if self.transport is not None:
            with contextlib.suppress(Exception):
                await self.transport.aclose()


def secret_values(environ: Mapping[str, str]) -> tuple[str, ...]:
    """Secret values present in the environment, including the database password."""
    values = [environ.get(key, "") for key in SECRET_ENV_KEYS]
    with contextlib.suppress(ValueError):
        values.append(urllib.parse.urlsplit(environ.get("DATABASE_URL", "")).password or "")
    return tuple(value for value in values if value)


def default_level(event: str, fields: Mapping[str, object]) -> str:
    if event == TRADING_STATE_CHANGED:
        return CRITICAL if fields.get("state") == "HALTED" else INFO
    if event == KILL_SWITCH_ENGAGED:
        return WARNING if fields.get("complete") is True else CRITICAL
    if event in {PROTECTION_TRIPPED, BOOT_REFUSED, DAEMON_FATAL}:
        return CRITICAL
    return WARNING


def title(event: str, fields: Mapping[str, object]) -> str:
    if event == TRADING_STATE_CHANGED:
        return f"trading state {fields.get('previous', '?')} -> {fields.get('state', '?')}"
    if event == PROTECTION_TRIPPED:
        trigger = str(fields.get("trigger", "?"))
        return f"automatic HALT: {TRIGGER_TITLES.get(trigger, trigger)}"
    if event == KILL_SWITCH_ENGAGED:
        return "cancel-all complete" if fields.get("complete") is True else "cancel-all INCOMPLETE"
    if event == BOOT_REFUSED:
        return "bot refused to boot"
    if event == DAEMON_FATAL:
        return "bot stopped on a fatal error"
    return event


def render(event: str, *, level: str, fields: Mapping[str, object], context: str = "",
           notes: list[str] | None = None, host: str | None = None) -> str:
    scope = "/".join(part for part in (context, host or socket.gethostname()) if part)
    lines = [f"[bfx][{level.upper()}][{scope}] {title(event, fields)}"]
    lines += [f"{name}={_short(value)}" for name, value in fields.items()]
    lines += [f"({note})" for note in notes or ()]
    text = "\n".join(lines)
    return text if len(text) <= _MAX_TEXT else text[: _MAX_TEXT - 3] + "..."


def _short(value: object) -> str:
    text = " ".join(str(value).split())
    return text if len(text) <= _FIELD_LIMIT else text[: _FIELD_LIMIT - 3] + "..."


_sink: AlertSink = AlertSink(transport=None)


def install(sink: AlertSink) -> AlertSink:
    """Make ``sink`` the process-wide destination; returns the previous one."""
    global _sink
    previous, _sink = _sink, sink
    return previous


def current() -> AlertSink:
    return _sink


def emit(event: str, *, level: str | None = None, **fields: object) -> None:
    """Alert the operator. Synchronous, non-blocking, never raises."""
    _sink.emit(event, level=level, **fields)


async def shutdown(*, timeout_s: float = 5.0) -> None:
    """Flush the installed sink before the process exits; never raises."""
    with contextlib.suppress(Exception):
        await _sink.aclose(timeout_s=timeout_s)


__all__ = [
    "BOOT_REFUSED",
    "CRITICAL",
    "DAEMON_FATAL",
    "DEDUP_FIELDS",
    "INFO",
    "KILL_SWITCH_ENGAGED",
    "PROTECTION_TRIPPED",
    "TRADING_STATE_CHANGED",
    "TRIGGER_TITLES",
    "WARNING",
    "AlertDeliveryError",
    "AlertSink",
    "TelegramTransport",
    "current",
    "default_level",
    "emit",
    "install",
    "render",
    "secret_values",
    "shutdown",
]
