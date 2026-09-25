"""An UNKNOWN that keeps its currency quarantined is reported, never escalated (D3 level 2)."""
from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import pytest

from bfx_funding_bot.modules.execution import boot_recovery
from bfx_funding_bot.modules.execution.boot_recovery import QuarantineAgeMonitor

MINUTE = 60_000


def attempt(started_at_ms: int, symbol: str = "fUST") -> SimpleNamespace:
    return SimpleNamespace(attempt_id=uuid4(), symbol=symbol, started_at_ms=started_at_ms,
                           amount=Decimal("199.99990042"))


@pytest.fixture
def sent(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    captured: list[dict] = []
    monkeypatch.setattr(boot_recovery.alerts, "emit",
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


# ------------------------------------------- foreign fills for ledger conservation


@pytest.mark.asyncio
async def test_foreign_executed_counts_only_unmanaged_offers_that_ended_in_the_window() -> None:
    """D2: what a foreign offer lent between two snapshots, from the offer history."""
    from bfx_funding_bot.external.bitfinex.auth_rest import ActiveFundingOffer

    def offer(offer_id: str, status: str, *, original: str, remaining: str, updated: int,
              symbol: str = "fUST") -> ActiveFundingOffer:
        return ActiveFundingOffer(offer_id, symbol, Decimal(remaining), 0.0002, 2, 1, status,
                                  amount_original=Decimal(original), mts_updated=updated)

    history = (
        offer("f1", "EXECUTED at 0.02% (200.0)", original="200", remaining="0", updated=5_000),
        offer("f2", "CANCELED was: PARTIALLY FILLED at 0.02%", original="300", remaining="100",
              updated=5_000),
        offer("f3", "EXECUTED at 0.02%", original="50", remaining="0", updated=900),   # before
        offer("m1", "EXECUTED at 0.02%", original="150", remaining="0", updated=5_000),  # ours
        offer("f4", "EXECUTED at 0.02%", original="80", remaining="0", updated=5_000,
              symbol="fUSD"),
    )
    recovery = object.__new__(boot_recovery.BootRecovery)
    recovery._last_accepted_query_ms = 1_000

    async def unattributed(session, offers):  # type: ignore[no-untyped-def]
        return {o.venue_offer_id for o in offers if o.venue_offer_id.startswith("f")}

    recovery._unattributed_offer_ids = unattributed  # type: ignore[method-assign]
    lent = await recovery._foreign_executed(None, history, since_ms=0)  # type: ignore[arg-type]
    assert lent == {"fUST": Decimal("400"), "fUSD": Decimal("80")}
