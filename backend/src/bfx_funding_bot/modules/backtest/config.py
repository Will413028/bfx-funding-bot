from dataclasses import dataclass
from decimal import Decimal
from typing import Literal


@dataclass(frozen=True)
class BacktestConfig:
    """Backtest engine friction parameters.

    Defaults model realistic Bitfinex funding market conditions:
      - fee_rate=0.15 (Bitfinex platform fee, takes 15% of interest)
      - gap_minutes=30 (avg credit return → next offer fill latency)
      - fill_alpha=5.0 (fill probability slope; 10% above market = 50% fill)
      - market_rate_source="candle_close" — the canonical per-day market
        funding rate (Bitfinex funding-candle close). FRR is NOT a
        unit-convertible market-rate proxy (see the backtest engine contract
        in backend/ARCHITECTURE.md).
    """

    fee_rate: Decimal = Decimal("0.15")
    gap_minutes: int = 30
    fill_alpha: Decimal = Decimal("5.0")
    market_rate_source: Literal["candle_close"] = "candle_close"
    # "empirical" requires versioned high-confidence FillRateModel evidence.
    # "linear-baseline" is an explicit offline sensitivity baseline only.
    fill_model: Literal["empirical", "linear-baseline"] = "empirical"
    fill_horizon_h: int = 4
    # Credit only the part of a lock that falls inside the record window. Off by
    # default so existing gate/derivation numbers are byte-stable; the
    # period-structure research runner turns it on because fresh-per-window WFO
    # would otherwise credit a 14d/30d lock opened late in a month in full and
    # then let the next window trade again from hour 0 (double counting).
    truncate_at_window_end: bool = False

    def __post_init__(self) -> None:
        if not (Decimal("0") <= self.fee_rate <= Decimal("1")):
            raise ValueError(f"fee_rate must be in [0,1], got {self.fee_rate}")
        if self.gap_minutes < 0:
            raise ValueError(f"gap_minutes must be non-negative, got {self.gap_minutes}")
        if self.fill_alpha <= 0:
            raise ValueError(f"fill_alpha must be positive, got {self.fill_alpha}")
        if self.fill_horizon_h <= 0:
            raise ValueError(f"fill_horizon_h must be positive, got {self.fill_horizon_h}")
        if self.market_rate_source != "candle_close":
            raise ValueError(
                f"market_rate_source must be 'candle_close', got "
                f"{self.market_rate_source!r}"
            )


def compute_fill_prob(spread_pct: Decimal, fill_alpha: Decimal) -> Decimal:
    """Linear fill probability model.

    Args:
        spread_pct: (offer_rate - market_rate) / market_rate.
                    Negative = offer below market → always fills.
                    Positive = offer above market → linear decay.
        fill_alpha: slope; alpha=5 means 10% above market = 50% fill, 20% = 0%.

    Returns:
        Probability in [0, 1].
    """
    if spread_pct <= 0:
        return Decimal("1.0")
    return max(Decimal("0"), Decimal("1") - fill_alpha * spread_pct)
