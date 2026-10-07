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


def test_a_stale_dependency_overrides_a_ready_decision_until_it_clears() -> None:
    changes: list[bool] = []
    readiness = TradingReadiness(on_change=changes.append)
    readiness.set_ready()

    readiness.set_dependency_stale("ws_data")
    readiness.set_dependency_stale("db")
    stale = readiness.snapshot()
    assert stale.trading_ready is False
    assert stale.reason == "dependency_stale"
    assert stale.dependency == "db"  # deterministic: the smallest name

    readiness.clear_dependency("db")
    assert readiness.snapshot().dependency == "ws_data"
    readiness.set_ready()  # a decision cannot override a stale dependency
    assert readiness.snapshot().trading_ready is False

    readiness.clear_dependency("ws_data")
    assert readiness.snapshot().trading_ready is True
    assert changes[0] is False and changes[-1] is True


def test_clearing_a_dependency_keeps_the_last_decision() -> None:
    readiness = TradingReadiness()
    readiness.set_dependency_stale("ws_data")
    readiness.set_blocked(BlockReason.BOOK_STALE, "market_snapshot")
    readiness.clear_dependency("ws_data")
    snapshot = readiness.snapshot()
    assert snapshot.trading_ready is False
    assert snapshot.reason == BlockReason.BOOK_STALE.value
