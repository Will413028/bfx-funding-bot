"""R4.1.6: scheduler logic must be timezone-independent — candle mts is
UTC ms epoch; scheduling must align to UTC boundaries regardless of
container TZ.
"""
from __future__ import annotations

import pytest

from bfx_funding_bot.modules.marketfeed.scheduler import (
    next_candle_close_mts,
    now_ms_utc,
)


@pytest.mark.parametrize("tz", ["UTC", "Asia/Taipei", "America/Los_Angeles", "Pacific/Auckland"])
def test_scheduler_aligns_to_utc_regardless_of_tz(
    tz: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TZ", tz)
    # 1747584000000 = aligned at hour boundary in UTC
    now_ms = 1747584000000 + 123_456  # ~2 minutes past
    nxt = next_candle_close_mts(timeframe="1h", now_ms=now_ms)
    assert nxt == 1747584000000 + 3600_000


def test_now_ms_utc_returns_epoch_ms(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TZ", "Asia/Taipei")
    import time as t
    expected_approx = int(t.time() * 1000)
    actual = now_ms_utc()
    assert abs(actual - expected_approx) < 1000  # within 1s
