"""An UNKNOWN that keeps its currency quarantined is reported, never escalated (D3 level 2)."""
from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import pytest

from bfx_funding_bot.modules.execution import reconcile_monitors
from bfx_funding_bot.modules.execution.reconcile_monitors import QuarantineAgeMonitor

MINUTE = 60_000


def attempt(started_at_ms: int, symbol: str = "fUST") -> SimpleNamespace:
    return SimpleNamespace(attempt_id=uuid4(), symbol=symbol, started_at_ms=started_at_ms,
                           amount=Decimal("199.99990042"))


@pytest.fixture
def sent(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    captured: list[dict] = []
    monkeypatch.setattr(reconcile_monitors.alerts, "emit",
                        lambda event, **fields: captured.append({"event": event, **fields}))
    return captured


def test_alerts_after_30_minutes_then_every_6_hours(sent) -> None:
    monitor = QuarantineAgeMonitor()
    held = attempt(0)
    monitor.observe([held], now_ms=29 * MINUTE)
    assert sent == []
    monitor.observe([held], now_ms=30 * MINUTE)
    assert [(s["event"], s["symbol"], s["minutes"]) for s in sent] == [
        ("unknown_quarantine_aged", "fUST", 30)]
    monitor.observe([held], now_ms=5 * 60 * MINUTE)          # within the repeat window
    assert len(sent) == 1
    monitor.observe([held], now_ms=30 * MINUTE + 6 * 60 * MINUTE)
    assert len(sent) == 2


def test_a_resolved_unknown_is_forgotten_and_others_are_independent(sent) -> None:
    monitor = QuarantineAgeMonitor()
    first, second = attempt(0), attempt(20 * MINUTE, "fUSD")
    monitor.observe([first, second], now_ms=31 * MINUTE)
    assert [s["symbol"] for s in sent] == ["fUST"]
    monitor.observe([second], now_ms=51 * MINUTE)            # first resolved
    assert [s["symbol"] for s in sent] == ["fUST", "fUSD"]
    monitor.observe([first], now_ms=52 * MINUTE)             # an id seen again is new again
    assert [s["symbol"] for s in sent] == ["fUST", "fUSD", "fUST"]
