"""scripts.send_test_alert proves delivery through alerts.emit, not a side channel."""
from __future__ import annotations

import pytest

from bfx_funding_bot.modules.observability import alerts
from scripts import send_test_alert


class _Transport:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.sent: list[str] = []

    async def send(self, text: str) -> None:
        if self.fail:
            raise alerts.AlertDeliveryError("telegram said no")
        self.sent.append(text)

    async def aclose(self) -> None:
        pass


async def test_sends_alert_test_through_alerts_emit(monkeypatch: pytest.MonkeyPatch) -> None:
    emitted: list[str] = []
    real_emit = alerts.emit

    def spy(event: str, **fields: object) -> None:
        emitted.append(event)
        real_emit(event, **fields)

    monkeypatch.setattr(alerts, "emit", spy)
    transport = _Transport()
    previous = alerts.current()

    assert await send_test_alert.send(alerts.AlertSink(transport), note="hello") == 0

    assert emitted == [alerts.ALERT_TEST]
    assert len(transport.sent) == 1
    assert "test alert (no action needed)" in transport.sent[0]
    assert "note=hello" in transport.sent[0]
    assert alerts.current() is previous   # the process sink is restored


async def test_log_only_sink_is_a_failure() -> None:
    assert await send_test_alert.send(alerts.AlertSink(transport=None), note="x") == 1


async def test_failed_delivery_is_a_failure() -> None:
    sink = alerts.AlertSink(_Transport(fail=True))
    assert await send_test_alert.send(sink, note="x") == 1
    assert sink.counts == {"failed": 1}


def test_missing_telegram_keys_exit_nonzero(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    assert send_test_alert.main([]) == 1
