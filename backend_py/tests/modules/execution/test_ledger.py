"""PaperPositionLedger: live event handler tests."""
from __future__ import annotations

from decimal import Decimal
from uuid import UUID, uuid4

from bfx_funding_bot.modules.execution.contracts import ReservationRef
from bfx_funding_bot.modules.execution.events import (
    OrderFilled,
    PositionReconciled,
    ReservationClaimed,
    ReservationReleased,
    ReservationUnknown,
)
from bfx_funding_bot.modules.execution.ledger import PaperPositionLedger


def _ref(cid: int, scid: UUID, venue_offer_id: str) -> ReservationRef:
    return ReservationRef(
        execution_decision_id=f"d-ledger-{cid}-{venue_offer_id}",
        cid=cid,
        signal_correlation_id=scid,
        venue_offer_id=venue_offer_id,
    )


async def test_handler_skips_foreign_account_id_claim() -> None:
    ledger = PaperPositionLedger(account_id="default")
    scid = uuid4()
    foreign = ReservationClaimed(
        cid=1, venue_offer_id="x", size_usdt=Decimal("100"),
        signal_correlation_id=scid, account_id="smoke_test", is_simulated=True,
        symbol="fUST", reservation_ref=_ref(1, scid, "x"))
    await ledger.on_reservation_claimed(foreign)
    assert ledger.current_exposure("fUST") == Decimal("0")


async def test_handler_skips_foreign_account_id_fill() -> None:
    ledger = PaperPositionLedger(account_id="default")
    scid = uuid4()
    foreign = OrderFilled(
        cid=1, venue_offer_id="x", credit_id=None, size_usdt=Decimal("100"),
        fill_rate=0.0001, signal_correlation_id=scid,
        account_id="smoke_test", is_simulated=True,
        symbol="fUST", reservation_ref=_ref(1, scid, "x"))
    await ledger.on_order_filled(foreign)
    assert ledger.realized_exposure("fUST") == Decimal("0")
    assert ledger.current_exposure("fUST") == Decimal("0")
    assert ledger.replay_floor_hit_count == 0


async def test_handler_skips_foreign_account_id_release() -> None:
    ledger = PaperPositionLedger(account_id="default")
    scid = uuid4()
    foreign = ReservationReleased(
        cid=1, venue_offer_id="x", size_usdt=Decimal("100"),
        reason="venue_cancel", signal_correlation_id=scid,
        account_id="smoke_test", is_simulated=True,
        symbol="fUST", reservation_ref=_ref(1, scid, "x"))
    await ledger.on_reservation_released(foreign)
    assert ledger.current_exposure("fUST") == Decimal("0")
    assert ledger.replay_floor_hit_count == 0


async def test_handler_processes_matching_account_id_unchanged() -> None:
    """Regression: matching account_id still updates counters as before."""
    ledger = PaperPositionLedger(account_id="default")
    scid = uuid4()
    matching = ReservationClaimed(
        cid=1, venue_offer_id="x", size_usdt=Decimal("100"),
        signal_correlation_id=scid, account_id="default", is_simulated=True,
        symbol="fUST", reservation_ref=_ref(1, scid, "x"))
    await ledger.on_reservation_claimed(matching)
    assert ledger.current_exposure("fUST") == Decimal("100")


async def test_unknown_submit_opens_symbol_block_and_is_counted_pessimistically() -> None:
    ledger = PaperPositionLedger(account_id="default")
    scid = uuid4()
    event = ReservationUnknown(
        cid=9,
        size_usdt=Decimal("100"),
        signal_correlation_id=scid,
        account_id="default",
        is_simulated=False,
        reason="timeout",
        symbol="fUST",
        reservation_ref=_ref(9, scid, "unknown-9"),
    )

    await ledger.on_reservation_unknown(event)
    await ledger.on_reservation_unknown(event)  # at-least-once delivery is safe

    assert ledger.uncertain_exposure("fUST") == Decimal("100")
    assert ledger.current_exposure("fUST") == Decimal("100")
    assert ledger.is_uncertain("fUST") is True
    assert ledger.is_uncertain("fUSD") is False


async def test_available_balance_default_zero():
    led = PaperPositionLedger(account_id="default")
    assert led.available_balance("fUST") == Decimal("0")


async def test_on_position_reconciled_sets_available():
    led = PaperPositionLedger(account_id="default")
    await led.on_position_reconciled(PositionReconciled(
        account_id="default",
        reserved_usdt=Decimal("0"),
        realized_usdt=Decimal("406.89"),
        available_usdt=Decimal("147.5"),
        n_offers=0,
        n_credits=2,
        occurred_at_ms=1_000,
    symbol="fUST"))
    assert led.available_balance("fUST") == Decimal("147.5")
    assert led.current_exposure("fUST") == Decimal("406.89")


async def test_on_position_reconciled_other_account_ignored():
    led = PaperPositionLedger(account_id="default")
    await led.on_position_reconciled(PositionReconciled(
        account_id="other",
        reserved_usdt=Decimal("1"), realized_usdt=Decimal("1"),
        available_usdt=Decimal("99"), n_offers=1, n_credits=1, occurred_at_ms=1,
    symbol="fUST"))
    assert led.available_balance("fUST") == Decimal("0")
