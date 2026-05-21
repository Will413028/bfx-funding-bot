"""Calibrated guards: enabled=false → always allowed (4.2 default).
enabled=true paths covered to prevent dead-code rot (Coverage gate)."""
from __future__ import annotations

from decimal import Decimal
from uuid import uuid4

import pytest

from bfx_funding_bot.modules.execution.protocols import (
    AccountContext,
    Credentials,
)
from bfx_funding_bot.modules.execution.safety.calibrated_guards import (
    DivergenceRateGuard,
    DrawdownGuard,
    RealizedLossGuard,
)
from bfx_funding_bot.modules.marketfeed.schemas import (
    DecisionOutcome,
    DecisionPayload,
)


def _ctx() -> AccountContext:
    return AccountContext("default", Credentials("k", "s"), Decimal("500"))


def _post() -> DecisionPayload:
    return DecisionPayload(
        decision_outcome=DecisionOutcome.POST,
        signal_correlation_id=uuid4(),
        offer_rate=0.0001, offer_amount_usdt=100.0, offer_duration_days=2,
    )


class _FakePnLSource:
    def __init__(self, loss_24h: Decimal, peak: Decimal, current: Decimal) -> None:
        self.loss_24h = loss_24h
        self.peak = peak
        self.current = current

    def realized_loss_24h(self) -> Decimal:
        return self.loss_24h

    def drawdown_pct(self) -> float:
        if self.peak == 0:
            return 0.0
        return float((self.peak - self.current) / self.peak)


class _FakeDivergenceSource:
    def __init__(self, rate: float) -> None:
        self.rate = rate

    def divergence_rate_pct(self, window_minutes: int) -> float:
        return self.rate


@pytest.mark.asyncio
async def test_realized_loss_disabled_always_allows() -> None:
    g = RealizedLossGuard(enabled=False, threshold_usdt=None,
                          source=_FakePnLSource(Decimal("99999"), Decimal("0"), Decimal("0")))
    r = await g.evaluate(_post(), _ctx())
    assert r.allowed is True


@pytest.mark.asyncio
async def test_realized_loss_enabled_under_threshold_allows() -> None:
    g = RealizedLossGuard(enabled=True, threshold_usdt=50.0,
                          source=_FakePnLSource(Decimal("30"), Decimal("0"), Decimal("0")))
    r = await g.evaluate(_post(), _ctx())
    assert r.allowed is True


@pytest.mark.asyncio
async def test_realized_loss_enabled_over_threshold_blocks() -> None:
    g = RealizedLossGuard(enabled=True, threshold_usdt=50.0,
                          source=_FakePnLSource(Decimal("75"), Decimal("0"), Decimal("0")))
    r = await g.evaluate(_post(), _ctx())
    assert r.allowed is False


@pytest.mark.asyncio
async def test_drawdown_disabled_always_allows() -> None:
    g = DrawdownGuard(enabled=False, threshold_pct=None,
                      source=_FakePnLSource(Decimal("0"), Decimal("1000"), Decimal("500")))
    r = await g.evaluate(_post(), _ctx())
    assert r.allowed is True


@pytest.mark.asyncio
async def test_drawdown_enabled_under_threshold_allows() -> None:
    g = DrawdownGuard(enabled=True, threshold_pct=0.5,
                      source=_FakePnLSource(Decimal("0"), Decimal("1000"), Decimal("700")))
    r = await g.evaluate(_post(), _ctx())
    assert r.allowed is True  # 30% drawdown < 50% threshold


@pytest.mark.asyncio
async def test_drawdown_enabled_over_threshold_blocks() -> None:
    g = DrawdownGuard(enabled=True, threshold_pct=0.5,
                      source=_FakePnLSource(Decimal("0"), Decimal("1000"), Decimal("400")))
    r = await g.evaluate(_post(), _ctx())
    assert r.allowed is False  # 60% drawdown > 50%


@pytest.mark.asyncio
async def test_divergence_disabled_always_allows() -> None:
    g = DivergenceRateGuard(enabled=False, threshold_pct=None, window_minutes=None,
                            source=_FakeDivergenceSource(99.0))
    r = await g.evaluate(_post(), _ctx())
    assert r.allowed is True


@pytest.mark.asyncio
async def test_divergence_enabled_under_threshold_allows() -> None:
    g = DivergenceRateGuard(enabled=True, threshold_pct=0.1, window_minutes=60,
                            source=_FakeDivergenceSource(0.05))
    r = await g.evaluate(_post(), _ctx())
    assert r.allowed is True


@pytest.mark.asyncio
async def test_divergence_enabled_over_threshold_blocks() -> None:
    g = DivergenceRateGuard(enabled=True, threshold_pct=0.1, window_minutes=60,
                            source=_FakeDivergenceSource(0.2))
    r = await g.evaluate(_post(), _ctx())
    assert r.allowed is False


@pytest.mark.asyncio
async def test_all_calibrated_guards_marked_is_calibrated() -> None:
    src = _FakePnLSource(Decimal("0"), Decimal("0"), Decimal("0"))
    div_src = _FakeDivergenceSource(0.0)
    assert RealizedLossGuard(enabled=False, threshold_usdt=None, source=src).is_calibrated
    assert DrawdownGuard(enabled=False, threshold_pct=None, source=src).is_calibrated
    assert DivergenceRateGuard(
        enabled=False, threshold_pct=None, window_minutes=None, source=div_src,
    ).is_calibrated
