import math
from decimal import Decimal

from bfx_funding_bot.modules.backtest.config import BacktestConfig, compute_fill_prob
from bfx_funding_bot.modules.backtest.schemas import BacktestResult, LendDecision
from bfx_funding_bot.modules.backtest.strategies.base import Strategy
from bfx_funding_bot.modules.candles.schemas import FundingCandle


def _resolve_market_rate(candle: FundingCandle, source: str) -> Decimal | None:
    """Phase 1 only supports candle_close. Phase 3 will add 'frr' source."""
    if source == "candle_close":
        return candle.close
    raise ValueError(f"unsupported market_rate_source: {source!r}")


def _apply_friction(
    decision: LendDecision,
    candle: FundingCandle,
    config: BacktestConfig,
) -> tuple[Decimal, Decimal]:
    """Compute (gross_rate, fill_prob) for a strategy decision.

    gross_rate = decision.rate * fill_prob   (pre-fee per-period rate)
    Caller multiplies by period_days and (1 - fee_rate) for net.
    """
    market_rate = _resolve_market_rate(candle, config.market_rate_source)
    if market_rate is None or market_rate == 0:
        # Cannot resolve market — assume offer fills at decision rate (no spread penalty)
        return decision.rate, Decimal("1")

    spread_pct = (decision.rate - market_rate) / market_rate
    fill_prob = compute_fill_prob(spread_pct, config.fill_alpha)
    gross_rate = decision.rate * fill_prob
    return gross_rate, fill_prob


def run_backtest(
    candles: list[FundingCandle],
    strategy: Strategy,
    config: BacktestConfig | None = None,
) -> BacktestResult:
    """Run a single strategy over a candle series and return summary metrics.

    Friction model (controlled by `config`):
      - fee_rate: Bitfinex 15% fee on interest paid (default)
      - gap_minutes: idle time between credit return and next offer (default 30)
      - fill_alpha: linear fill probability slope (default 5.0)
      - market_rate_source: "candle_close" (Phase 1 FRR proxy)

    Per-decision flow:
      1. Strategy emits LendDecision (rate, period_days)
      2. Spread vs market -> fill_prob (EV-based, deterministic)
      3. gross_rate = decision.rate * fill_prob   (pre-fee)
      4. net_rate = gross_rate * (1 - fee_rate)
      5. equity *= 1 + rate * period_days  (gross & net tracked separately)
      6. Cooldown extends by ceil(gap_minutes / 60) candles (1h candle assumption)

    Drawdown is computed on net equity (matches what the user actually has).
    """
    config = config or BacktestConfig()

    if not candles:
        return BacktestResult(
            strategy_name=strategy.name,
            symbol="",
            start_mts=0,
            end_mts=0,
            n_candles=0,
            gross_monthly_return_pct=Decimal("0"),
            net_monthly_return_pct=Decimal("0"),
            max_drawdown_pct=Decimal("0"),
            n_trades=0,
            fill_rate=Decimal("0"),
        )

    sorted_candles = sorted(candles, key=lambda c: c.mts)
    symbol = sorted_candles[0].symbol
    start_mts = sorted_candles[0].mts
    end_mts = sorted_candles[-1].mts

    gross_equity = Decimal("1")
    net_equity = Decimal("1")
    peak = net_equity
    max_dd = Decimal("0")
    n_trades = 0
    fill_prob_sum = Decimal("0")  # accumulated fill_prob across trades; final fill_rate = sum / n_trades
    cooldown_until_idx = -1
    # 1h candle assumption: 60 min per candle. If the input series uses a different
    # timeframe (e.g. 5m, 1D), gap_candles miscounts. Phase 1 only loads 1h candles.
    gap_candles = math.ceil(config.gap_minutes / 60)
    one_minus_fee = Decimal("1") - config.fee_rate

    for i, candle in enumerate(sorted_candles):
        if i <= cooldown_until_idx:
            continue
        decision = strategy.decide(candle)
        if decision is None:
            continue

        gross_rate, fill_prob = _apply_friction(decision, candle, config)

        period = Decimal(decision.period_days)
        gross_equity = gross_equity * (Decimal("1") + gross_rate * period)
        net_rate = gross_rate * one_minus_fee
        net_equity = net_equity * (Decimal("1") + net_rate * period)

        n_trades += 1
        fill_prob_sum += fill_prob
        cooldown_until_idx = i + decision.period_days * 24 + gap_candles

        if net_equity > peak:
            peak = net_equity
        dd = (peak - net_equity) / peak if peak > 0 else Decimal("0")
        if dd > max_dd:
            max_dd = dd

    if end_mts > start_mts:
        # Decimal-only arithmetic to avoid float precision drift
        total_hours = Decimal(end_mts - start_mts) / Decimal(3_600_000)
    else:
        total_hours = Decimal("0")
    months_elapsed = total_hours / Decimal("720") if total_hours > 0 else Decimal("0")

    if months_elapsed > 0:
        gross_monthly = (gross_equity - Decimal("1")) / months_elapsed * Decimal("100")
        net_monthly = (net_equity - Decimal("1")) / months_elapsed * Decimal("100")
    else:
        gross_monthly = Decimal("0")
        net_monthly = Decimal("0")

    fill_rate = (fill_prob_sum / Decimal(n_trades)) if n_trades > 0 else Decimal("0")

    return BacktestResult(
        strategy_name=strategy.name,
        symbol=symbol,
        start_mts=start_mts,
        end_mts=end_mts,
        n_candles=len(sorted_candles),
        gross_monthly_return_pct=gross_monthly,
        net_monthly_return_pct=net_monthly,
        max_drawdown_pct=max_dd * Decimal("100"),
        n_trades=n_trades,
        fill_rate=fill_rate,
    )
