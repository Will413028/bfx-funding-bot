"""Trading readiness is a business gate, never process liveness."""
from __future__ import annotations

from bfx_funding_bot.modules.execution.contracts import BlockReason
from bfx_funding_bot.modules.marketfeed.readiness import TradingReadiness


def test_readiness_block_does_not_change_process_liveness() -> None:
    readiness = TradingReadiness()
    readiness.set_blocked(BlockReason.BOOK_STALE, "market_snapshot")

    snapshot = readiness.snapshot()
    assert snapshot.trading_ready is False
    assert snapshot.reason == BlockReason.BOOK_STALE.value
    assert snapshot.dependency == "market_snapshot"


def test_readiness_set_ready_clears_the_previous_business_block() -> None:
    readiness = TradingReadiness()
    readiness.set_blocked(BlockReason.FILL_MODEL_MISSING, "fill_model")

    readiness.set_ready()

    snapshot = readiness.snapshot()
    assert snapshot.trading_ready is True
    assert snapshot.reason is None
    assert snapshot.dependency is None
