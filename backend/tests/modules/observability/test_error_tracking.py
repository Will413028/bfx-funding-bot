"""Error tracking: nothing secret or personal leaves the process, and no DSN means off.

The end-to-end tests run the real SDK against a local HTTP server standing in for
Sentry, so they cover what actually goes over the wire (and what urllib3 logs while
sending it), not only what ``before_send`` returns.
"""
from __future__ import annotations

import gzip
import http.server
import importlib
import logging
import threading
from collections.abc import Iterator
from typing import Any, ClassVar

import pytest
import sentry_sdk

from bfx_funding_bot.modules.observability import alerts, error_tracking
from bfx_funding_bot.modules.observability.error_tracking import (
    REDACTED,
    init_error_tracking,
    scrub_event,
)

# Fake secrets assembled at run time so no credential-shaped literal sits in the source.
TOKEN = "1234" + "56789:" + "fake-" + "TelegramToken" + "x" * 17
API_SECRET = "fake-" + "bitfinex-api-secret-" + "y" * 12
DB_PASSWORD = "fake-" + "db-password-zz"
DSN_KEY = "fake" + "sentrykey" + "0123456789abcdef"
EMAIL = "operator@example.com"
IPV4 = "203.0.113.7"
IPV6 = "2001:db8:85a3::8a2e:370:7334"
LOCAL_ONLY = "fake-" + "value-only-in-a-local-variable"


def _environ(dsn: str = "") -> dict[str, str]:
    return {
        "SENTRY_DSN": dsn,
        "TELEGRAM_BOT_TOKEN": TOKEN,
        "BFX_API_SECRET": API_SECRET,
        "DATABASE_URL": f"postgresql://bfx_bot:{DB_PASSWORD}@bfx-postgres:5432/bfx",
        "BFX_DEPLOYMENT_ENV": "ci",
        "BFX_SERVICE_VERSION": "aaaaaaa",
    }


LEAKS = (TOKEN, API_SECRET, DB_PASSWORD, DSN_KEY, EMAIL, IPV4, IPV6)


def _assert_clean(text: str) -> None:
    found = [leak for leak in LEAKS if leak in text]
    assert not found, f"leaked: {found}"


@pytest.fixture(autouse=True)
def _no_global_client() -> Iterator[None]:
    yield
    sentry_sdk.get_client().close(timeout=0)
    sentry_sdk.get_global_scope().set_client(None)


def test_scrub_removes_secrets_emails_ips_and_identity_fields() -> None:
    secrets = alerts.secret_values(_environ(f"https://{DSN_KEY}@o1.ingest.example/1"))
    detail = f"token={TOKEN} secret={API_SECRET} pw={DB_PASSWORD} key={DSN_KEY} {EMAIL} {IPV4} {IPV6}"
    event: Any = {
        "message": detail,
        "exception": {"values": [{"type": "RuntimeError", "value": detail}]},
        "breadcrumbs": {"values": [{"message": f"POST https://api.telegram.org/bot{TOKEN}/send"}]},
        "logentry": {"message": "failed %s", "params": [detail]},
        "user": {"email": EMAIL, "ip_address": IPV4},
        "request": {"url": "https://example.invalid/api", "cookies": {"session": "s"},
                    "headers": {"Authorization": "Bearer abc", "X-Forwarded-For": IPV4,
                                "Accept": "application/json"},
                    "env": {"REMOTE_ADDR": IPV4}},
        "tags": {"service": "bot", "peer": IPV4},
    }

    scrubbed = scrub_event(event, secrets)

    assert scrubbed is not None
    _assert_clean(repr(scrubbed))
    assert "user" not in scrubbed
    request: Any = scrubbed["request"]
    assert set(request) == {"url", "headers"}
    assert request["headers"] == {"Accept": "application/json"}
    assert REDACTED in scrubbed["message"]
    assert scrubbed["tags"] == {"service": "bot", "peer": REDACTED}


def test_scrub_keeps_ordinary_text() -> None:
    text = "deployment_skip symbol=fUSD rate=0.00021 period=2 at 12:30:45 v1.2.3"
    assert error_tracking.scrub_text(text, ()) == text


def test_a_failing_scrub_drops_the_event() -> None:
    class Unscrubbable(dict[str, Any]):
        def items(self) -> Any:
            raise RuntimeError("cannot scrub")

    event: Any = {"message": "x", "extra": Unscrubbable(a="b")}
    assert scrub_event(event, ()) is None


def test_no_dsn_does_not_initialize() -> None:
    assert init_error_tracking("bot", _environ("")) is False
    assert init_error_tracking("bot", _environ("   ")) is False
    assert not sentry_sdk.get_client().is_active()


class _Ingest(http.server.BaseHTTPRequestHandler):
    bodies: ClassVar[list[bytes]] = []

    def do_POST(self) -> None:
        raw = self.rfile.read(int(self.headers["Content-Length"]))
        if self.headers.get("Content-Encoding") == "gzip":
            raw = gzip.decompress(raw)
        _Ingest.bodies.append(raw)
        self.send_response(200)
        self.end_headers()

    def log_message(self, *_args: object) -> None:
        pass


@pytest.fixture
def ingest() -> Iterator[str]:
    _Ingest.bodies = []
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Ingest)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://{DSN_KEY}@127.0.0.1:{server.server_address[1]}/1"
    finally:
        sentry_sdk.get_client().close(timeout=0)  # the SDK's pooled connection
        server.shutdown()
        server.server_close()


def _fail_with_secrets_in_scope() -> None:
    api_secret_in_scope = LOCAL_ONLY  # noqa: F841 - must never be sent
    raise RuntimeError(f"venue refused {TOKEN} for {EMAIL} from {IPV4}")


def test_events_on_the_wire_carry_no_secret_personal_data_or_locals(
        ingest: str, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    assert init_error_tracking("bot", _environ(ingest)) is True
    logger = logging.getLogger("bfx_funding_bot.test_error_tracking")

    logger.info("calling https://api.telegram.org/bot%s/sendMessage", TOKEN)  # breadcrumb
    logger.error("reconcile failed for %s at %s", EMAIL, IPV6)  # event via logging
    try:
        _fail_with_secrets_in_scope()
    except RuntimeError:
        sentry_sdk.capture_exception()
    sentry_sdk.flush(timeout=5)

    wire = b"\n".join(_Ingest.bodies).decode()
    assert "venue refused" in wire and "reconcile failed" in wire
    assert '"service":"bot"' in wire.replace(" ", "")
    assert '"environment":"ci"' in wire.replace(" ", "")
    _assert_clean(wire)
    assert LOCAL_ONLY not in wire
    # urllib3 and the SDK log while sending; none of it may carry the DSN key.
    sending = [r.getMessage() for r in caplog.records if r.name != logger.name]
    assert any("/api/1/envelope/" in message for message in sending)
    _assert_clean("\n".join(sending))


def test_outgoing_requests_get_no_trace_headers(ingest: str) -> None:
    assert init_error_tracking("bot", _environ(ingest)) is True
    assert sentry_sdk.get_client().options["trace_propagation_targets"] == []
    integrations = set(sentry_sdk.get_client().integrations)
    assert "httpx" not in integrations
    assert "sqlalchemy" not in integrations


def test_bot_main_starts_error_tracking_before_the_daemon(monkeypatch: pytest.MonkeyPatch) -> None:
    from bfx_funding_bot.apps import bot

    order: list[str] = []
    monkeypatch.setattr(bot, "init_error_tracking", lambda service, _env: order.append(service))

    async def run() -> None:
        order.append("daemon")

    monkeypatch.setattr(bot, "_run", run)
    bot.main()
    assert order == ["bot", "daemon"]


def test_webapi_starts_error_tracking_with_the_fastapi_integration(
        monkeypatch: pytest.MonkeyPatch) -> None:
    from bfx_funding_bot.apps import webapi

    calls: list[tuple[str, tuple[str, ...]]] = []

    def record(service: str, _environ: object, *, integrations: tuple[Any, ...] = ()) -> bool:
        calls.append((service, tuple(i.identifier for i in integrations)))
        return False

    monkeypatch.setattr(error_tracking, "init_error_tracking", record)
    try:
        importlib.reload(webapi)
    finally:
        monkeypatch.undo()
        importlib.reload(webapi)
    assert calls == [("webapi", ("starlette", "fastapi"))]
