from decimal import Decimal

import pytest

from bfx_funding_bot.modules.backtest.config import BacktestConfig, compute_fill_prob


def test_config_default_is_realistic_friction() -> None:
    """Defaults should model real Bitfinex funding conditions."""
    cfg = BacktestConfig()
    assert cfg.fee_rate == Decimal("0.15")
    assert cfg.gap_minutes == 30
    assert cfg.fill_alpha == Decimal("5.0")
    assert cfg.market_rate_source == "candle_close"


def test_config_rejects_negative_fee() -> None:
    with pytest.raises(ValueError, match="fee_rate"):
        BacktestConfig(fee_rate=Decimal("-0.1"))


def test_config_rejects_fee_above_one() -> None:
    with pytest.raises(ValueError, match="fee_rate"):
        BacktestConfig(fee_rate=Decimal("1.5"))


def test_config_rejects_negative_gap_minutes() -> None:
    with pytest.raises(ValueError, match="gap_minutes"):
        BacktestConfig(gap_minutes=-1)


def test_config_rejects_negative_fill_alpha() -> None:
    with pytest.raises(ValueError, match="fill_alpha"):
        BacktestConfig(fill_alpha=Decimal("-1"))


def test_config_rejects_zero_fill_alpha() -> None:
    """fill_alpha=0 makes fill_prob always return 1.0 (degenerate); reject."""
    with pytest.raises(ValueError, match="fill_alpha"):
        BacktestConfig(fill_alpha=Decimal("0"))


def test_fill_prob_at_market_is_one() -> None:
    """spread_pct=0 → fill_prob=1.0."""
    assert compute_fill_prob(Decimal("0"), Decimal("5")) == Decimal("1.0")


def test_fill_prob_below_market_is_one() -> None:
    """spread_pct<0 (offer below market) → always fills."""
    assert compute_fill_prob(Decimal("-0.10"), Decimal("5")) == Decimal("1.0")


def test_fill_prob_linear_decay() -> None:
    """spread_pct=0.10, alpha=5 → fill_prob = 1 - 5*0.10 = 0.5."""
    result = compute_fill_prob(Decimal("0.10"), Decimal("5"))
    assert result == Decimal("0.5")


def test_fill_prob_clamps_at_zero() -> None:
    """spread_pct=0.30, alpha=5 → 1 - 1.5 = -0.5 → clamped to 0."""
    result = compute_fill_prob(Decimal("0.30"), Decimal("5"))
    assert result == Decimal("0")
