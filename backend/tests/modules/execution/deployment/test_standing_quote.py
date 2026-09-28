from uuid import uuid4

from bfx_funding_bot.modules.execution.deployment.standing_quote import (
    StandingQuote,
    StandingQuoteStore,
)
from bfx_funding_bot.modules.strategy import DecisionOutcome


def _post(cell_id: str, created_at_ms: int) -> StandingQuote:
    return StandingQuote(
        cell_id=cell_id,
        outcome=DecisionOutcome.POST,
        rate=0.00012,
        period_days=2,
        signal_correlation_id=uuid4(),
        created_at_ms=created_at_ms,
    )


def test_get_active_returns_fresh_post():
    store = StandingQuoteStore(ttl_ms=3_900_000)
    store.update(_post("fUST_a30", created_at_ms=1_000))
    got = store.get_active("fUST_a30", now_ms=1_000)
    assert got is not None
    assert got.rate == 0.00012
    assert got.period_days == 2


def test_skip_quote_is_not_active():
    store = StandingQuoteStore(ttl_ms=3_900_000)
    store.update(
        StandingQuote(
            cell_id="fUST_a30", outcome=DecisionOutcome.SKIP,
            rate=None, period_days=None,
            signal_correlation_id=uuid4(), created_at_ms=1_000,
        )
    )
    assert store.get_active("fUST_a30", now_ms=1_000) is None


def test_expired_quote_is_not_active():
    store = StandingQuoteStore(ttl_ms=3_900_000)
    store.update(_post("fUST_a30", created_at_ms=1_000))
    assert store.get_active("fUST_a30", now_ms=1_000 + 3_900_001) is None


def test_missing_cell_returns_none():
    store = StandingQuoteStore(ttl_ms=3_900_000)
    assert store.get_active("nope", now_ms=1_000) is None


def test_update_overwrites_previous():
    store = StandingQuoteStore(ttl_ms=3_900_000)
    store.update(_post("fUST_a30", created_at_ms=1_000))
    store.update(_post("fUST_a30", created_at_ms=2_000))
    got = store.get_active("fUST_a30", now_ms=2_000)
    assert got is not None and got.created_at_ms == 2_000
