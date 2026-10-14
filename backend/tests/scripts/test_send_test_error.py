"""scripts.send_test_error goes through init_error_tracking, the same path as real errors."""
from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
import sentry_sdk

from scripts import send_test_error


@pytest.fixture(autouse=True)
def _no_global_client() -> Iterator[None]:
    yield
    sentry_sdk.get_client().close(timeout=0)
    sentry_sdk.get_global_scope().set_client(None)


def test_without_dsn_reports_not_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SENTRY_DSN", raising=False)
    assert send_test_error.send("bot") == 1


def test_sends_through_init_error_tracking_and_its_scrubber(
        monkeypatch: pytest.MonkeyPatch) -> None:
    started: list[str] = []
    sent: list[Any] = []
    real_init = send_test_error.init_error_tracking

    def init(service: str, environ: Any) -> bool:
        started.append(service)
        return real_init(service, {**environ, "SENTRY_DSN": "https://k3y456@o1.ingest.example/1"})

    class Capture(sentry_sdk.transport.Transport):
        def capture_envelope(self, envelope: Any) -> None:
            sent.append(envelope)

    monkeypatch.setattr(send_test_error, "init_error_tracking", init)
    real_sdk_init = sentry_sdk.init
    monkeypatch.setattr(sentry_sdk, "init",
                        lambda **options: real_sdk_init(**options, transport=Capture()))

    assert send_test_error.send("webapi") == 0

    assert started == ["webapi"]
    body = b"".join(envelope.serialize() for envelope in sent).decode()
    assert "ErrorTrackingTestError" in body
    assert send_test_error.SYNTHETIC_EMAIL not in body
    assert send_test_error.SYNTHETIC_IP not in body
