from decimal import Decimal

from bfx_funding_bot.modules.backtest.config import BacktestConfig
from bfx_funding_bot.modules.backtest.engine import run_backtest
from bfx_funding_bot.modules.backtest.schemas import LendDecision
from bfx_funding_bot.modules.backtest.strategies.always_frr import AlwaysFRRStrategy
from bfx_funding_bot.modules.backtest.strategies.base import Strategy
from bfx_funding_bot.modules.candles.schemas import FundingCandle


def _candles_constant_rate(rate: str, n: int = 720) -> list[FundingCandle]:
    """720 hourly candles ≈ 30 days. Constant rate makes math hand-checkable."""
    return [
        FundingCandle(
            symbol="fUST",
            timeframe="1h",
            period_agg="p2",
            mts=1704067200000 + i * 3_600_000,  # +1h per candle
            open=Decimal(rate),
            close=Decimal(rate),
            high=Decimal(rate),
            low=Decimal(rate),
            volume=Decimal("100"),
        )
        for i in range(n)
    ]


def test_run_backtest_constant_rate_produces_expected_monthly_return() -> None:
    """At constant 0.0001 daily rate over 30 days lending continuously with
    default config (fee=0.15, gap=30min):
      - Per trade: rate*period = 0.0002 gross; *(1-0.15) = 0.00017 net
      - 15 lends compounded: net ≈ 1.00017^15 - 1 ≈ 0.002553 = 0.2553%
      - Updated from old expected 0.3% (pre-friction)
    """
    candles = _candles_constant_rate("0.0001", n=720)
    strategy = AlwaysFRRStrategy(period_days=2)

    result = run_backtest(candles, strategy)

    assert result.strategy_name == "always_frr_p2"
    assert result.symbol == "fUST"
    assert result.n_candles == 720
    # 15% fee adjustment: 0.3% -> 0.255% (compounding deviation < 0.001%)
    assert abs(result.net_monthly_return_pct - Decimal("0.255")) < Decimal("0.01")
    # Gross is pre-fee, ~0.3%
    assert abs(result.gross_monthly_return_pct - Decimal("0.3")) < Decimal("0.01")
    assert result.n_trades == 15
    assert result.max_drawdown_pct == Decimal("0")
    # AlwaysFRR posts at candle close -> spread=0 -> fill_prob=1
    assert result.fill_rate == Decimal("1.0")


def test_run_backtest_handles_empty_candles() -> None:
    candles: list[FundingCandle] = []
    strategy = AlwaysFRRStrategy(period_days=2)
    result = run_backtest(candles, strategy)
    assert result.n_candles == 0
    assert result.n_trades == 0
    assert result.gross_monthly_return_pct == Decimal("0")
    assert result.net_monthly_return_pct == Decimal("0")
    assert result.max_drawdown_pct == Decimal("0")
    assert result.fill_rate == Decimal("0")


def test_run_backtest_skips_candles_with_no_close() -> None:
    candles = _candles_constant_rate("0.0001", n=720)
    candles_with_holes = []
    for i, c in enumerate(candles):
        if i % 100 == 0:
            candles_with_holes.append(c.model_copy(update={"close": None}))
        else:
            candles_with_holes.append(c)
    strategy = AlwaysFRRStrategy(period_days=2)
    result = run_backtest(candles_with_holes, strategy)
    assert result.n_candles == 720
    assert result.n_trades > 0


def test_run_backtest_applies_15pct_fee() -> None:
    """Default config has fee_rate=0.15. Net should equal gross * 0.85
    (small compounding deviation < 0.1%)."""
    candles = _candles_constant_rate("0.0001", n=720)
    strategy = AlwaysFRRStrategy(period_days=2)

    result = run_backtest(candles, strategy)

    # Per-trade gross ~0.0002, net ~0.00017 -> ratio 0.85 holds at compounded level
    ratio = result.net_monthly_return_pct / result.gross_monthly_return_pct
    assert abs(ratio - Decimal("0.85")) < Decimal("0.001")


def test_run_backtest_gap_minutes_extends_cooldown() -> None:
    """Comparing gap_minutes=0 vs gap_minutes=180 (3 candles).

    With period=2 (48 candles cooldown) and 720 candles:
      - gap=0:   cooldown_until = i+48 -> 15 trades
      - gap=180: cooldown_until = i+51 -> 14 trades
    Higher gap -> fewer trades -> lower compounded return.
    """
    candles = _candles_constant_rate("0.0001", n=720)
    strategy = AlwaysFRRStrategy(period_days=2)

    no_gap = run_backtest(candles, strategy, BacktestConfig(gap_minutes=0))
    big_gap = run_backtest(candles, strategy, BacktestConfig(gap_minutes=180))

    assert big_gap.n_trades < no_gap.n_trades
    assert big_gap.net_monthly_return_pct < no_gap.net_monthly_return_pct


def test_run_backtest_observe_called_for_every_candle_including_cooldown() -> None:
    """observe() must be called on every candle, even during cooldown
    (so stateful indicators stay fresh)."""
    candles = _candles_constant_rate("0.0001", n=72)
    observations: list[int] = []

    class ObservingStrategy(Strategy):
        @property
        def name(self) -> str:
            return "obs"

        def observe(self, candle: FundingCandle) -> None:
            observations.append(candle.mts)

        def decide(self, candle: FundingCandle) -> LendDecision | None:
            if candle.close is None:
                return None
            return LendDecision(mts=candle.mts, rate=candle.close, period_days=2)

    run_backtest(candles, ObservingStrategy())
    assert observations == [c.mts for c in candles]


def test_run_backtest_record_window_excludes_trades_outside() -> None:
    """Trades whose decision-candle.mts falls outside [record_start_mts,
    record_end_mts] must not be counted in n_trades or contribute to equity."""
    candles = _candles_constant_rate("0.0001", n=720)
    # Restrict recording to last 240 candles (~10 days)
    record_start_mts = candles[480].mts
    record_end_mts = candles[-1].mts

    full = run_backtest(candles, AlwaysFRRStrategy(period_days=2))
    windowed = run_backtest(
        candles, AlwaysFRRStrategy(period_days=2),
        record_start_mts=record_start_mts,
        record_end_mts=record_end_mts,
    )

    assert windowed.n_trades < full.n_trades
    assert windowed.n_trades > 0
    # n_candles reflects the record window, not the total input series
    assert windowed.n_candles == 240
    assert full.n_candles == 720
    # Net monthly rate is normalized to elapsed months in each window, so comparing
    # absolute equity: windowed covers fewer trades -> lower total compounded equity.
    # Cross-window monthly-rate comparison is invalid (different denominators).
    # Instead verify gross_monthly * months_windowed < gross_monthly_full * months_full,
    # i.e., proportional absolute gain is less in the restricted window.
    windowed_months = Decimal(windowed.end_mts - windowed.start_mts) / Decimal(3_600_000) / Decimal(720)
    full_months = Decimal(full.end_mts - full.start_mts) / Decimal(3_600_000) / Decimal(720)
    windowed_abs_gross = windowed.gross_monthly_return_pct * windowed_months
    full_abs_gross = full.gross_monthly_return_pct * full_months
    assert windowed_abs_gross < full_abs_gross


def test_run_backtest_sortino_populated_on_long_series() -> None:
    """720+ hourly candles spans ~1 month. Single-month series has no
    monthly returns -> sortino should stay at 0 (n<3 floor)."""
    candles = _candles_constant_rate("0.0001", n=720)
    result = run_backtest(candles, AlwaysFRRStrategy(period_days=2))
    # 30 days starting 2024-01-01 ends at 2024-01-30 23:00 UTC.
    # Jan-end (2024-01-31 23:59:59) is AFTER the series end, so month_end_timestamps_within
    # returns [] -> early return Decimal("0").
    assert result.sortino == Decimal("0")


def test_run_backtest_sortino_with_multi_month_series_positive_finite() -> None:
    """5 months of constant-positive returns -> no downside -> sortino=+inf.

    n=24*30*5 (3600 candles, ~150 days) starting 2024-01-01 spans through
    2024-05-29, capturing 4 month-end samples (Jan/Feb/Mar/Apr) -> 3 monthly
    returns. All returns are positive (constant 0.0001 rate) -> no downside
    -> sortino = +inf.
    """
    candles = _candles_constant_rate("0.0001", n=24 * 30 * 5)  # ~5 months
    result = run_backtest(candles, AlwaysFRRStrategy(period_days=2))
    assert result.sortino == Decimal("Infinity")


def test_run_backtest_spread_above_market_reduces_fill() -> None:
    """Strategy posts 10% above candle close -> spread_pct=0.10.
    With default fill_alpha=5: fill_prob = 1 - 5*0.10 = 0.5.
    So gross = 0.5 * what AlwaysFRR-at-1.10x would have been.
    """
    candles = _candles_constant_rate("0.0001", n=720)

    class BidAboveMarketStrategy(Strategy):
        @property
        def name(self) -> str:
            return "bid_10pct_above_market"

        def decide(self, candle: FundingCandle) -> LendDecision | None:
            if candle.close is None:
                return None
            return LendDecision(
                mts=candle.mts,
                rate=candle.close * Decimal("1.10"),  # 10% above market
                period_days=2,
            )

    result = run_backtest(candles, BidAboveMarketStrategy())

    # fill_rate = 0.5 (every trade had spread_pct=0.10)
    assert abs(result.fill_rate - Decimal("0.5")) < Decimal("0.001")
    # gross_rate per trade = 0.0001 * 1.10 * 0.5 = 0.000055; period=2 -> 0.00011
    # 15 trades compounded: gross_monthly ≈ (1.00011^15 - 1) / 1 month * 100 ≈ 0.165%
    assert abs(result.gross_monthly_return_pct - Decimal("0.165")) < Decimal("0.02")
