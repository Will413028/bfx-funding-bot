from decimal import Decimal
from uuid import uuid4

from bfx_funding_bot.modules.execution.events import OrderFilled, PositionReconciled
from bfx_funding_bot.modules.execution.ledger import PaperPositionLedger


async def test_fill_and_reconcile_are_isolated_per_symbol() -> None:
    led = PaperPositionLedger(account_id="default")
    # fUST realized via reconcile absolute-set; fUSD untouched
    await led.on_position_reconciled(
        PositionReconciled(
            account_id="default", symbol="fUST",
            reserved=Decimal("0"), realized=Decimal("300"), available=Decimal("250"),
            n_offers=0, n_credits=3, occurred_at_ms=1,
        )
    )
    await led.on_order_filled(
        OrderFilled(
            cid=1, venue_offer_id="v1", credit_id="C1",
            amount=Decimal("50"), symbol="fUST", fill_rate=0.0005,
            signal_correlation_id=uuid4(), account_id="default", is_simulated=False,
            is_legacy_uncorrelated=True,
        )
    )
    assert led.current_exposure("fUST") == Decimal("350")  # 300 realized + 50 filled
    assert led.current_exposure("fUSD") == Decimal("0")    # isolated
    assert led.available_balance("fUST") == Decimal("250")
    assert led.available_balance("fUSD") == Decimal("0")
    # explicit cross-symbol total helper (still fUST-only here)
    assert led.total_exposure_all_symbols() == Decimal("350")
