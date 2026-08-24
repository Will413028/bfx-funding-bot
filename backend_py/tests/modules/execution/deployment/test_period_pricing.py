from decimal import Decimal
from uuid import uuid4

from bfx_funding_bot.external.bitfinex.rest import FundingBookLevel
from bfx_funding_bot.modules.execution.contracts import BlockedExecution, BlockReason
from bfx_funding_bot.modules.execution.deployment.period_pricing import (
    PeriodPricer,
    PriceBranch,
    PriceDecision,
)
from bfx_funding_bot.modules.marketfeed.funding_book import MarketSnapshot
from bfx_funding_bot.modules.marketfeed.schemas import DecisionOutcome, DecisionPayload


def _decision(
    *,
    symbol: str = "fUST",
    offer_rate: str = "0.00020",
    offer_amount_usdt: str = "100",
    offer_duration_days: int = 14,
) -> DecisionPayload:
    return DecisionPayload(
        decision_outcome=DecisionOutcome.POST,
        signal_correlation_id=uuid4(),
        offer_rate=float(offer_rate),
        offer_amount_usdt=float(offer_amount_usdt),
        offer_duration_days=offer_duration_days,
        symbol=symbol,
    )


def _snapshot(*levels: FundingBookLevel) -> MarketSnapshot:
    return MarketSnapshot(
        snapshot_id="book-1",
        symbol="fUST",
        bids=tuple(level for level in levels if level.amount < 0),
        asks=tuple(level for level in levels if level.amount > 0),
        captured_at_ms=1_000,
        received_at_ms=1_000,
        source="ws",
        sequence_valid=True,
        checksum_valid=True,
        sequence=2,
    )


def _level(*, rate: float, amount: float, period: int) -> FundingBookLevel:
    return FundingBookLevel(rate=rate, period=period, count=1, amount=amount)


def _pricer() -> PeriodPricer:
    return PeriodPricer(max_down_pct=Decimal("0.15"), tick=Decimal("0.00000001"))


def test_missing_exact_period_returns_period_not_found() -> None:
    candidate = _decision(offer_duration_days=14)

    result = _pricer().price(
        candidate=candidate,
        snapshot=_snapshot(_level(rate=0.00021, amount=500, period=7)),
    )

    assert isinstance(result, BlockedExecution)
    assert result.reason is BlockReason.PERIOD_NOT_FOUND
    assert result.candidate is candidate


def test_exact_period_bid_with_sufficient_depth_keeps_signal_rate_as_taker() -> None:
    candidate = _decision()

    result = _pricer().price(
        candidate=candidate,
        snapshot=_snapshot(_level(rate=0.00021, amount=-100, period=14)),
    )

    assert isinstance(result, PriceDecision)
    assert result.rate == Decimal("0.00020")
    assert result.branch is PriceBranch.TAKER


def test_exact_period_ask_is_undercut_when_competitive_rate_is_above_signal() -> None:
    candidate = _decision()

    result = _pricer().price(
        candidate=candidate,
        snapshot=_snapshot(_level(rate=0.00023, amount=100, period=14)),
    )

    assert isinstance(result, PriceDecision)
    assert result.rate == Decimal("0.00022999")
    assert result.branch is PriceBranch.UNDERCUT


def test_exact_period_ask_raises_when_competitive_rate_is_between_floor_and_signal() -> None:
    candidate = _decision()

    result = _pricer().price(
        candidate=candidate,
        snapshot=_snapshot(_level(rate=0.00019, amount=100, period=14)),
    )

    assert isinstance(result, PriceDecision)
    assert result.rate == Decimal("0.00018999")
    assert result.branch is PriceBranch.RAISE


def test_exact_period_ask_below_downside_floor_keeps_signal_rate() -> None:
    candidate = _decision()

    result = _pricer().price(
        candidate=candidate,
        snapshot=_snapshot(_level(rate=0.00016, amount=100, period=14)),
    )

    assert isinstance(result, PriceDecision)
    assert result.rate == Decimal("0.00020")
    assert result.branch is PriceBranch.SIGNAL_FLOOR


def test_exact_period_requires_ask_depth_after_bid_cannot_take() -> None:
    candidate = _decision()

    result = _pricer().price(
        candidate=candidate,
        snapshot=_snapshot(
            _level(rate=0.00019, amount=-100, period=14),
            _level(rate=0.00023, amount=99, period=14),
        ),
    )

    assert isinstance(result, BlockedExecution)
    assert result.reason is BlockReason.INSUFFICIENT_PERIOD_DEPTH
