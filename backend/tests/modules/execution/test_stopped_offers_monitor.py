"""A currency that must hold no managed offer, yet still does, is reported after 30 minutes."""
from __future__ import annotations

import pytest

from bfx_funding_bot.modules.execution import reconcile_monitors
from bfx_funding_bot.modules.execution.reconcile_monitors import StoppedOffersMonitor
from bfx_funding_bot.modules.observability import alerts

MINUTE = 60_000


@pytest.fixture
def sent(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    captured: list[dict] = []
    monkeypatch.setattr(reconcile_monitors.alerts, "emit",
                        lambda event, **fields: captured.append({"event": event, **fields}))
    return captured


def test_cancels_that_keep_failing_under_a_stop_alert_after_30_minutes_then_every_6_hours(
    sent,
) -> None:
    monitor = StoppedOffersMonitor()
    monitor.observe("fUST", reason="halted", venue_offer_ids=("101", "102"), now_ms=0)
    monitor.observe("fUST", reason="halted", venue_offer_ids=("101",), now_ms=29 * MINUTE)
    assert sent == []
    monitor.observe("fUST", reason="halted", venue_offer_ids=("101",), now_ms=30 * MINUTE)
    assert sent == [{"event": "stopped_offers_standing", "symbol": "fUST", "reason": "halted",
                     "minutes": 30, "offers": 1, "venue_offer_ids": "101"}]
    monitor.observe("fUST", reason="halted", venue_offer_ids=("101",), now_ms=5 * 60 * MINUTE)
    assert len(sent) == 1
    monitor.observe("fUST", reason="disabled", venue_offer_ids=("101",),
                    now_ms=30 * MINUTE + 6 * 60 * MINUTE)
    assert [s["reason"] for s in sent] == ["halted", "disabled"]


def test_the_clock_restarts_once_every_cancel_went_through_or_the_currency_runs_again(sent) -> None:
    monitor = StoppedOffersMonitor()
    monitor.observe("fUST", reason="halted", venue_offer_ids=("101",), now_ms=0)
    monitor.observe("fUST", reason="halted", venue_offer_ids=(), now_ms=20 * MINUTE)
    monitor.observe("fUST", reason="halted", venue_offer_ids=("103",), now_ms=40 * MINUTE)
    assert sent == []
    monitor.observe("fUST", reason=None, venue_offer_ids=(), now_ms=50 * MINUTE)
    monitor.observe("fUST", reason="disabled", venue_offer_ids=("103",), now_ms=75 * MINUTE)
    assert sent == []


def test_offers_that_cannot_be_listed_count_as_standing(sent) -> None:
    monitor = StoppedOffersMonitor()
    monitor.observe("fUST", reason="halted", venue_offer_ids=None, now_ms=0)
    monitor.observe("fUST", reason="halted", venue_offer_ids=None, now_ms=30 * MINUTE)
    assert [(s["reason"], s["offers"], s["venue_offer_ids"]) for s in sent] == [
        ("halted", "unknown", "")]


def test_an_unreadable_stop_state_has_its_own_clock(sent) -> None:
    """One unreadable tick neither reports 30 minutes of unreadability nor silences the
    standing alert, and an unreadable half hour does not backdate a standing one."""
    monitor = StoppedOffersMonitor()
    monitor.observe("fUST", reason="halted", venue_offer_ids=("101",), now_ms=0)
    monitor.observe("fUST", reason="stop_state_unreachable", venue_offer_ids=None,
                    now_ms=29 * MINUTE)
    monitor.observe("fUST", reason="halted", venue_offer_ids=("101",), now_ms=30 * MINUTE)
    assert [(s["reason"], s["minutes"]) for s in sent] == [("halted", 30)]

    sent.clear()
    monitor = StoppedOffersMonitor()
    monitor.observe("fUSD", reason="stop_state_failed", venue_offer_ids=None, now_ms=0)
    monitor.observe("fUSD", reason="stop_state_failed", venue_offer_ids=None, now_ms=30 * MINUTE)
    monitor.observe("fUSD", reason="halted", venue_offer_ids=("7",), now_ms=31 * MINUTE)
    assert [(s["reason"], s["minutes"], s["offers"]) for s in sent] == [
        ("stop_state_failed", 30, "unknown")]


def test_only_an_unreachable_database_lowers_the_level() -> None:
    def level(reason: str) -> str:
        return alerts.default_level(alerts.STOPPED_OFFERS_STANDING, {"reason": reason})

    assert [level(r) for r in ("halted", "disabled", "stop_state_failed")] == [alerts.CRITICAL] * 3
    assert level("stop_state_unreachable") == alerts.WARNING
