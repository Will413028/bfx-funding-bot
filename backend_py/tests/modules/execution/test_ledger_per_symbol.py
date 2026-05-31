"""PaperPositionLedger per-symbol isolation (Phase 1 native-units)."""
from __future__ import annotations

from decimal import Decimal
from uuid import uuid4

from bfx_funding_bot.modules.execution.events import (
    OrderFilled,
    PositionReconciled,
    ReservationClaimed,
    ReservationReleased,
)
from bfx_funding_bot.modules.execution.ledger import PaperPositionLedger


def _claim(symbol: str, amount: str, account_id: str = "default") -> ReservationClaimed:
    return ReservationClaimed(
        cid=1, venue_offer_id="x", symbol=symbol, amount=Decimal(amount),
        signal_correlation_id=uuid4(), account_id=account_id, is_simulated=True,
    )


def _fill(symbol: str, amount: str, venue_offer_id: str = "x",
          venue_seq: int | None = None, account_id: str = "default") -> OrderFilled:
    return OrderFilled(
        cid=1, venue_offer_id=venue_offer_id, credit_id=None, symbol=symbol,
        amount=Decimal(amount), fill_rate=0.0001, signal_correlation_id=uuid4(),
        account_id=account_id, is_simulated=True, venue_seq=venue_seq,
    )


def _release(symbol: str, amount: str, venue_offer_id: str = "x",
             venue_seq: int | None = None, account_id: str = "default") -> ReservationReleased:
    return ReservationReleased(
        cid=1, venue_offer_id=venue_offer_id, symbol=symbol, amount=Decimal(amount),
        reason="venue_cancel", signal_correlation_id=uuid4(),
        account_id=account_id, is_simulated=True, venue_seq=venue_seq,
    )


def _reconciled(symbol: str, reserved: str, realized: str, available: str,
                account_id: str = "default") -> PositionReconciled:
    return PositionReconciled(
        account_id=account_id, symbol=symbol, reserved=Decimal(reserved),
        realized=Decimal(realized), available=Decimal(available),
        n_offers=0, n_credits=0, occurred_at_ms=1_000,
    )


async def test_unknown_symbol_reads_zero() -> None:
    led = PaperPositionLedger(account_id="default")
    assert led.current_exposure("fUST") == Decimal("0")
    assert led.reserved_exposure("fUST") == Decimal("0")
    assert led.realized_exposure("fUST") == Decimal("0")
    assert led.available_balance("fUST") == Decimal("0")


async def test_claim_increments_only_its_symbol_bucket() -> None:
    led = PaperPositionLedger(account_id="default")
    await led.on_reservation_claimed(_claim("fUST", "100"))
    await led.on_reservation_claimed(_claim("fUSD", "30"))
    assert led.reserved_exposure("fUST") == Decimal("100")
    assert led.reserved_exposure("fUSD") == Decimal("30")
    assert led.current_exposure("fUST") == Decimal("100")
    assert led.current_exposure("fUSD") == Decimal("30")


async def test_fill_moves_only_its_symbol_reserved_to_realized() -> None:
    led = PaperPositionLedger(account_id="default")
    await led.on_reservation_claimed(_claim("fUST", "100"))
    await led.on_reservation_claimed(_claim("fUSD", "30"))
    await led.on_order_filled(_fill("fUST", "100", venue_offer_id="o-ust", venue_seq=1))
    assert led.reserved_exposure("fUST") == Decimal("0")
    assert led.realized_exposure("fUST") == Decimal("100")
    # fUSD untouched
    assert led.reserved_exposure("fUSD") == Decimal("30")
    assert led.realized_exposure("fUSD") == Decimal("0")


async def test_fill_dedup_per_venue_offer_seq() -> None:
    led = PaperPositionLedger(account_id="default")
    await led.on_reservation_claimed(_claim("fUST", "100"))
    await led.on_order_filled(_fill("fUST", "100", venue_offer_id="o-ust", venue_seq=7))
    await led.on_order_filled(_fill("fUST", "100", venue_offer_id="o-ust", venue_seq=7))  # dup
    assert led.realized_exposure("fUST") == Decimal("100")


async def test_release_floors_per_symbol_without_claim() -> None:
    led = PaperPositionLedger(account_id="default")
    # fUSD has a live claim; releasing fUST (no claim) floors fUST only.
    await led.on_reservation_claimed(_claim("fUSD", "40"))
    await led.on_reservation_released(_release("fUST", "50", venue_offer_id="o-ust"))
    assert led.reserved_exposure("fUST") == Decimal("0")
    assert led.replay_floor_hit_count == 1
    assert led.reserved_exposure("fUSD") == Decimal("40")  # untouched


async def test_release_dedup_per_venue_offer_seq() -> None:
    led = PaperPositionLedger(account_id="default")
    await led.on_reservation_claimed(_claim("fUST", "100"))
    await led.on_reservation_released(_release("fUST", "100", venue_offer_id="o-ust", venue_seq=9))
    await led.on_reservation_released(_release("fUST", "100", venue_offer_id="o-ust", venue_seq=9))  # dup
    assert led.reserved_exposure("fUST") == Decimal("0")


async def test_reconciled_absolute_sets_only_its_symbol() -> None:
    led = PaperPositionLedger(account_id="default")
    # seed fUST via a claim then absolute-set fUSD; fUST must remain.
    await led.on_reservation_claimed(_claim("fUST", "100"))
    await led.on_position_reconciled(_reconciled("fUSD", reserved="5", realized="200", available="50"))
    assert led.reserved_exposure("fUSD") == Decimal("5")
    assert led.realized_exposure("fUSD") == Decimal("200")
    assert led.available_balance("fUSD") == Decimal("50")
    # fUST untouched by the fUSD reconcile.
    assert led.reserved_exposure("fUST") == Decimal("100")


async def test_reconciled_overwrites_same_symbol() -> None:
    led = PaperPositionLedger(account_id="default")
    await led.on_position_reconciled(_reconciled("fUST", reserved="0", realized="406.89", available="147.5"))
    await led.on_position_reconciled(_reconciled("fUST", reserved="10", realized="300", available="90"))
    assert led.reserved_exposure("fUST") == Decimal("10")
    assert led.realized_exposure("fUST") == Decimal("300")
    assert led.available_balance("fUST") == Decimal("90")
    assert led.current_exposure("fUST") == Decimal("310")


async def test_reconciled_other_account_ignored() -> None:
    led = PaperPositionLedger(account_id="default")
    await led.on_position_reconciled(_reconciled("fUST", "1", "1", "99", account_id="other"))
    assert led.available_balance("fUST") == Decimal("0")
    assert led.current_exposure("fUST") == Decimal("0")
