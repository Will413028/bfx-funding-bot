"""PaperPositionLedger reserved+realized 雙 counter 行為 (Phase 4.3)."""
from __future__ import annotations

from decimal import Decimal
from uuid import uuid4

import pytest

from bfx_funding_bot.modules.execution.events import (
    OrderFilled,
    ReservationClaimed,
    ReservationReleased,
)
from bfx_funding_bot.modules.execution.ledger import PaperPositionLedger


def _claim(size: float, account_id: str = "default") -> ReservationClaimed:
    return ReservationClaimed(
        cid=1, venue_offer_id="x", size_usdt=Decimal(str(size)),
        signal_correlation_id=uuid4(), account_id=account_id, is_simulated=True,
        symbol="fUST", is_legacy_uncorrelated=True)


def _fill(size: float, account_id: str = "default") -> OrderFilled:
    return OrderFilled(
        cid=1, venue_offer_id="x", credit_id=None,
        size_usdt=Decimal(str(size)), fill_rate=0.0001,
        signal_correlation_id=uuid4(), account_id=account_id, is_simulated=True,
        symbol="fUST", is_legacy_uncorrelated=True)


def _release(size: float, account_id: str = "default") -> ReservationReleased:
    return ReservationReleased(
        cid=1, venue_offer_id="x", size_usdt=Decimal(str(size)),
        reason="venue_cancel", signal_correlation_id=uuid4(),
        account_id=account_id, is_simulated=True,
        symbol="fUST", is_legacy_uncorrelated=True)


@pytest.mark.asyncio
async def test_initial_state_zero() -> None:
    ledger = PaperPositionLedger(account_id="default")
    assert ledger.current_exposure("fUST") == Decimal("0")
    assert ledger.realized_exposure("fUST") == Decimal("0")


@pytest.mark.asyncio
async def test_reservation_claimed_increments_reserved() -> None:
    ledger = PaperPositionLedger(account_id="default")
    await ledger.on_reservation_claimed(_claim(100))
    assert ledger.current_exposure("fUST") == Decimal("100")
    assert ledger.realized_exposure("fUST") == Decimal("0")


@pytest.mark.asyncio
async def test_order_filled_transfers_reserved_to_realized() -> None:
    ledger = PaperPositionLedger(account_id="default")
    await ledger.on_reservation_claimed(_claim(100))
    await ledger.on_order_filled(_fill(100))
    assert ledger.current_exposure("fUST") == Decimal("100")  # 0 reserved + 100 realized
    assert ledger.realized_exposure("fUST") == Decimal("100")


@pytest.mark.asyncio
async def test_reservation_released_decrements_reserved() -> None:
    ledger = PaperPositionLedger(account_id="default")
    await ledger.on_reservation_claimed(_claim(100))
    await ledger.on_reservation_released(_release(100))
    assert ledger.current_exposure("fUST") == Decimal("0")
    assert ledger.realized_exposure("fUST") == Decimal("0")


@pytest.mark.asyncio
async def test_release_floors_at_zero_when_no_claim_present(
    caplog: pytest.LogCaptureFixture,
) -> None:
    ledger = PaperPositionLedger(account_id="default")
    # Release without prior CLAIMED (e.g., CLAIMED out of 30d retention)
    await ledger.on_reservation_released(_release(50))
    assert ledger.current_exposure("fUST") == Decimal("0")
    assert ledger.replay_floor_hit_count == 1
    assert "reservation_release_without_claim" in caplog.text


@pytest.mark.asyncio
async def test_release_partial_floor() -> None:
    ledger = PaperPositionLedger(account_id="default")
    await ledger.on_reservation_claimed(_claim(30))
    await ledger.on_reservation_released(_release(80))  # 50 over-released
    assert ledger.current_exposure("fUST") == Decimal("0")
    assert ledger.replay_floor_hit_count == 1


@pytest.mark.asyncio
async def test_realized_exposure_independent_of_reserved() -> None:
    ledger = PaperPositionLedger(account_id="default")
    await ledger.on_reservation_claimed(_claim(200))
    await ledger.on_order_filled(_fill(150))   # partial fill scenario
    await ledger.on_reservation_released(_release(50))  # remaining 50 cancelled
    assert ledger.realized_exposure("fUST") == Decimal("150")
    assert ledger.current_exposure("fUST") == Decimal("150")  # 0 reserved + 150 realized
