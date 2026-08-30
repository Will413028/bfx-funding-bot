from decimal import Decimal
from uuid import uuid4

import pytest

from bfx_funding_bot.modules.execution.contracts import ReservationRef
from bfx_funding_bot.modules.execution.events import (
    OrderFilled,
    ReservationClaimed,
    ReservationReleased,
)
from bfx_funding_bot.modules.execution.ledger import PaperPositionLedger


def _filled(venue_seq: int | None, venue_offer_id: str = "v1") -> OrderFilled:
    scid = uuid4()
    return OrderFilled(
        cid=42, venue_offer_id=venue_offer_id, credit_id="C-1",
        size_usdt=Decimal("100"), fill_rate=0.0005,
        signal_correlation_id=scid, account_id="default", is_simulated=False,
        venue_seq=venue_seq,
        symbol="fUST", reservation_ref=ReservationRef(
            execution_decision_id=f"d-dedup-{venue_offer_id}", cid=42,
            signal_correlation_id=scid, venue_offer_id=venue_offer_id,
        ))


def _released(venue_seq: int | None, venue_offer_id: str = "v1") -> ReservationReleased:
    scid = uuid4()
    return ReservationReleased(
        cid=42, venue_offer_id=venue_offer_id, size_usdt=Decimal("100"),
        reason="venue_cancel", signal_correlation_id=scid,
        account_id="default", is_simulated=False, venue_seq=venue_seq,
        symbol="fUST", reservation_ref=ReservationRef(
            execution_decision_id=f"d-dedup-{venue_offer_id}", cid=42,
            signal_correlation_id=scid, venue_offer_id=venue_offer_id,
        ))


def _claimed(venue_offer_id: str = "v1") -> ReservationClaimed:
    scid = uuid4()
    return ReservationClaimed(
        cid=42, venue_offer_id=venue_offer_id, size_usdt=Decimal("100"),
        signal_correlation_id=scid, account_id="default", is_simulated=False,
        symbol="fUST", reservation_ref=ReservationRef(
            execution_decision_id=f"d-dedup-{venue_offer_id}", cid=42,
            signal_correlation_id=scid, venue_offer_id=venue_offer_id,
        ))


@pytest.mark.asyncio
async def test_duplicate_orderfilled_does_not_double_realize() -> None:
    ledger = PaperPositionLedger(account_id="default")
    await ledger.on_reservation_claimed(_claimed())
    await ledger.on_order_filled(_filled(venue_seq=100))
    await ledger.on_order_filled(_filled(venue_seq=100))  # duplicate
    assert ledger.realized_exposure("fUST") == Decimal("100")
    assert ledger.current_exposure("fUST") == Decimal("100")  # reserved 0 + realized 100


@pytest.mark.asyncio
async def test_duplicate_reservation_released_does_not_double_decrement() -> None:
    ledger = PaperPositionLedger(account_id="default")
    await ledger.on_reservation_claimed(_claimed())
    await ledger.on_reservation_released(_released(venue_seq=200))
    await ledger.on_reservation_released(_released(venue_seq=200))
    assert ledger.current_exposure("fUST") == Decimal("0")


@pytest.mark.asyncio
async def test_different_venue_seq_not_dedupd() -> None:
    ledger = PaperPositionLedger(account_id="default")
    await ledger.on_reservation_claimed(_claimed(venue_offer_id="v1"))
    await ledger.on_reservation_claimed(_claimed(venue_offer_id="v2"))
    await ledger.on_order_filled(_filled(venue_seq=100, venue_offer_id="v1"))
    await ledger.on_order_filled(_filled(venue_seq=200, venue_offer_id="v2"))
    assert ledger.realized_exposure("fUST") == Decimal("200")


@pytest.mark.asyncio
async def test_none_venue_seq_still_dedupd_by_venue_offer_id() -> None:
    """4.3-era event with venue_seq=None still dedupd by (venue_offer_id, None) key."""
    ledger = PaperPositionLedger(account_id="default")
    await ledger.on_reservation_claimed(_claimed())
    await ledger.on_order_filled(_filled(venue_seq=None))
    await ledger.on_order_filled(_filled(venue_seq=None))  # duplicate with None key
    assert ledger.realized_exposure("fUST") == Decimal("100")
