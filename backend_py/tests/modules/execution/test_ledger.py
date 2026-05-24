"""PaperPositionLedger: live event handler tests."""
from __future__ import annotations

from decimal import Decimal
from uuid import uuid4

from bfx_funding_bot.modules.execution.events import (
    OrderFilled,
    ReservationClaimed,
    ReservationReleased,
)
from bfx_funding_bot.modules.execution.ledger import PaperPositionLedger


async def test_handler_skips_foreign_account_id_claim() -> None:
    ledger = PaperPositionLedger(account_id="default")
    foreign = ReservationClaimed(
        cid=1, venue_offer_id="x", size_usdt=Decimal("100"),
        signal_correlation_id=uuid4(), account_id="smoke_test", is_simulated=True,
    )
    await ledger.on_reservation_claimed(foreign)
    assert ledger.current_exposure() == Decimal("0")


async def test_handler_skips_foreign_account_id_fill() -> None:
    ledger = PaperPositionLedger(account_id="default")
    foreign = OrderFilled(
        cid=1, venue_offer_id="x", credit_id=None, size_usdt=Decimal("100"),
        fill_rate=0.0001, signal_correlation_id=uuid4(),
        account_id="smoke_test", is_simulated=True,
    )
    await ledger.on_order_filled(foreign)
    assert ledger.realized_exposure() == Decimal("0")
    assert ledger.current_exposure() == Decimal("0")
    assert ledger.replay_floor_hit_count == 0


async def test_handler_skips_foreign_account_id_release() -> None:
    ledger = PaperPositionLedger(account_id="default")
    foreign = ReservationReleased(
        cid=1, venue_offer_id="x", size_usdt=Decimal("100"),
        reason="venue_cancel", signal_correlation_id=uuid4(),
        account_id="smoke_test", is_simulated=True,
    )
    await ledger.on_reservation_released(foreign)
    assert ledger.current_exposure() == Decimal("0")
    assert ledger.replay_floor_hit_count == 0


async def test_handler_processes_matching_account_id_unchanged() -> None:
    """Regression: matching account_id still updates counters as before."""
    ledger = PaperPositionLedger(account_id="default")
    matching = ReservationClaimed(
        cid=1, venue_offer_id="x", size_usdt=Decimal("100"),
        signal_correlation_id=uuid4(), account_id="default", is_simulated=True,
    )
    await ledger.on_reservation_claimed(matching)
    assert ledger.current_exposure() == Decimal("100")
