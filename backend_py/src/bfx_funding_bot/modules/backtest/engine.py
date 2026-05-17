import math
from decimal import Decimal

from bfx_funding_bot.modules.backtest.config import BacktestConfig, compute_fill_prob
from bfx_funding_bot.modules.backtest.schemas import BacktestResult, LendDecision
from bfx_funding_bot.modules.backtest.sortino import (
    compute_sortino,
    month_end_timestamps_within,
    monthly_returns_from_equity_curve,
)
from bfx_funding_bot.modules.backtest.strategies.base import Strategy
from bfx_funding_bot.modules.candles.schemas import FundingCandle


def _resolve_market_rate(candle: FundingCandle, source: str) -> Decimal | None:
    if source == "candle_close":
        return candle.close
    raise ValueError(f"unsupported market_rate_source: {source!r}")


def _apply_friction(
    decision: LendDecision,
    candle: FundingCandle,
    config: BacktestConfig,
) -> tuple[Decimal, Decimal]:
    market_rate = _resolve_market_rate(candle, config.market_rate_source)
    if market_rate is None or market_rate == 0:
        return decision.rate, Decimal("1")
    spread_pct = (decision.rate - market_rate) / market_rate
    fill_prob = compute_fill_prob(spread_pct, config.fill_alpha)
    gross_rate = decision.rate * fill_prob
    return gross_rate, fill_prob


def run_backtest(
    candles: list[FundingCandle],
    strategy: Strategy,
    config: BacktestConfig | None = None,
    record_start_mts: int | None = None,
    record_end_mts: int | None = None,
) -> BacktestResult:
    """Run a strategy over a candle series and return summary metrics.

    Record window (Phase 3b):
      strategy.observe(candle) is called on every candle (incl. cooldown / outside window)
      so stateful indicators stay fresh during warmup. decide() is called only when:
        - candle index > cooldown_until_idx, AND
        - candle.mts in [record_start_mts, record_end_mts] (defaults: full span)
      Trades recorded only in this window contribute to n_trades / equity / sortino.
    """
    config = config or BacktestConfig()

    if not candles:
        return BacktestResult(
            strategy_name=strategy.name, symbol="",
            start_mts=0, end_mts=0, n_candles=0,
            gross_monthly_return_pct=Decimal("0"),
            net_monthly_return_pct=Decimal("0"),
            max_drawdown_pct=Decimal("0"),
            n_trades=0, fill_rate=Decimal("0"),
            sortino=Decimal("0"),
        )

    sorted_candles = sorted(candles, key=lambda c: c.mts)
    symbol = sorted_candles[0].symbol
    full_start_mts = sorted_candles[0].mts
    full_end_mts = sorted_candles[-1].mts

    effective_start = record_start_mts if record_start_mts is not None else full_start_mts
    effective_end = record_end_mts if record_end_mts is not None else full_end_mts

    gross_equity = Decimal("1")
    net_equity = Decimal("1")
    peak = net_equity
    max_dd = Decimal("0")
    n_trades = 0
    fill_prob_sum = Decimal("0")
    cooldown_until_idx = -1
    gap_candles = math.ceil(config.gap_minutes / 60)
    one_minus_fee = Decimal("1") - config.fee_rate

    equity_timeline: list[tuple[int, Decimal]] = []

    for i, candle in enumerate(sorted_candles):
        strategy.observe(candle)

        if i <= cooldown_until_idx:
            continue
        if candle.mts < effective_start or candle.mts > effective_end:
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

        equity_timeline.append((candle.mts, net_equity))

    if effective_end > effective_start:
        total_hours = Decimal(effective_end - effective_start) / Decimal(3_600_000)
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

    sortino_value = _compute_sortino_from_timeline(
        equity_timeline, effective_start, effective_end
    )

    return BacktestResult(
        strategy_name=strategy.name, symbol=symbol,
        start_mts=effective_start, end_mts=effective_end,
        n_candles=len(sorted_candles),
        gross_monthly_return_pct=gross_monthly,
        net_monthly_return_pct=net_monthly,
        max_drawdown_pct=max_dd * Decimal("100"),
        n_trades=n_trades, fill_rate=fill_rate,
        sortino=sortino_value,
    )


def _compute_sortino_from_timeline(
    equity_timeline: list[tuple[int, Decimal]],
    window_start_mts: int,
    window_end_mts: int,
) -> Decimal:
    """Sample net_equity at each month-end within the window, then compute
    Sortino on month-over-month returns.

    For month-end ts, take the most recent (mts, equity) point at or before
    that ts. If no trade has happened by month-end, equity = 1.0 (initial).
    """
    month_ends = month_end_timestamps_within(window_start_mts, window_end_mts)
    if not month_ends:
        return Decimal("0")
    samples: list[tuple[int, Decimal]] = []
    cursor = 0
    last_equity = Decimal("1")
    sorted_timeline = sorted(equity_timeline, key=lambda p: p[0])
    for me in month_ends:
        while cursor < len(sorted_timeline) and sorted_timeline[cursor][0] <= me:
            last_equity = sorted_timeline[cursor][1]
            cursor += 1
        samples.append((me, last_equity))
    rets = monthly_returns_from_equity_curve(samples)
    return compute_sortino(rets)
