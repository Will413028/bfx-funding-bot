import math
from decimal import Decimal
from typing import Literal, NoReturn

from bfx_funding_bot.modules.backtest.config import BacktestConfig, compute_fill_prob
from bfx_funding_bot.modules.backtest.schemas import BacktestResult, LendDecision
from bfx_funding_bot.modules.backtest.sortino import (
    compute_sortino,
    month_end_timestamps_within,
    monthly_returns_from_equity_curve,
)
from bfx_funding_bot.modules.backtest.strategies.base import Strategy
from bfx_funding_bot.modules.candles.schemas import FundingCandle
from bfx_funding_bot.modules.lending.tracking.artifact import FillModelUnavailable
from bfx_funding_bot.modules.lending.tracking.model import FillRateModel

BacktestIncompleteReason = Literal[
    "fill_model_missing", "fill_model_low_confidence", "fill_model_scope_mismatch"
]


class BacktestIncomplete(RuntimeError):  # noqa: N818 - contract name is prescribed
    """A requested empirical backtest lacks usable versioned fill evidence."""

    def __init__(self, reason: BacktestIncompleteReason) -> None:
        super().__init__(reason)
        self.reason = reason


def _raise_for_unavailable(evidence: FillModelUnavailable) -> NoReturn:
    if evidence.reason == "low_confidence":
        raise BacktestIncomplete("fill_model_low_confidence")
    if evidence.reason == "scope_mismatch":
        raise BacktestIncomplete("fill_model_scope_mismatch")
    raise BacktestIncomplete("fill_model_missing")


def _preflight_empirical_model(
    candles: list[FundingCandle], fill_model: FillRateModel | None
) -> None:
    if fill_model is None or fill_model.artifact is None:
        raise BacktestIncomplete("fill_model_missing")
    if fill_model.unavailable_reason is not None:
        _raise_for_unavailable(FillModelUnavailable(fill_model.unavailable_reason))

    artifact = fill_model.artifact
    if artifact.source != "candle" or any(
        candle.symbol != artifact.symbol for candle in candles
    ):
        _raise_for_unavailable(FillModelUnavailable("scope_mismatch"))


def _validate_candle_artifact_scope(
    candle: FundingCandle, fill_model: FillRateModel
) -> None:
    artifact = fill_model.artifact
    if artifact is not None and (
        artifact.source != "candle" or artifact.symbol != candle.symbol
    ):
        _raise_for_unavailable(FillModelUnavailable("scope_mismatch"))


def _resolve_market_rate(candle: FundingCandle, source: str) -> Decimal | None:
    # candle_close is the canonical per-day market funding rate. FRR is a
    # distinct quantity, not a unit-convertible market-rate proxy — see
    # docs/superpowers/specs/2026-05-28-frr-market-rate-decoupling-design.md.
    # The raise is defense-in-depth; BacktestConfig already rejects others.
    if source == "candle_close":
        return candle.close
    raise ValueError(f"unsupported market_rate_source: {source!r}")


def _apply_friction(
    decision: LendDecision,
    candle: FundingCandle,
    config: BacktestConfig,
    fill_model: FillRateModel | None = None,
) -> tuple[Decimal, Decimal]:
    market_rate = _resolve_market_rate(candle, config.market_rate_source)
    if market_rate is None or market_rate == 0:
        return decision.rate, Decimal("1")
    spread_pct = (decision.rate - market_rate) / market_rate

    if config.fill_model == "empirical":
        if fill_model is None:
            raise BacktestIncomplete("fill_model_missing")
        if fill_model.artifact is None:
            raise BacktestIncomplete("fill_model_missing")
        _validate_candle_artifact_scope(candle, fill_model)
        est = fill_model.estimate_fill(
            reference_rate=market_rate, offer_rate=decision.rate,
            period_agg=candle.period_agg, horizon_h=config.fill_horizon_h,
        )
        if isinstance(est, FillModelUnavailable):
            _raise_for_unavailable(est)
        fill_prob = est.fill_prob
    elif config.fill_model == "linear-baseline":
        fill_prob = compute_fill_prob(spread_pct, config.fill_alpha)
    else:  # pragma: no cover - BacktestConfig validates the literal at construction.
        raise ValueError(f"unsupported fill_model: {config.fill_model!r}")

    gross_rate = decision.rate * fill_prob
    return gross_rate, fill_prob


def run_backtest(
    candles: list[FundingCandle],
    strategy: Strategy,
    config: BacktestConfig,
    record_start_mts: int | None = None,
    record_end_mts: int | None = None,
    fill_model: FillRateModel | None = None,
    market_candles: list[FundingCandle] | None = None,
) -> BacktestResult:
    """Run a strategy over a candle series and return summary metrics.

    Record window (Phase 3b):
      strategy.observe(candle) is called on every candle (incl. cooldown / outside window)
      so stateful indicators stay fresh during warmup. decide() is called only when:
        - candle index > cooldown_until_idx, AND
        - candle.mts in [record_start_mts, record_end_mts] (defaults: full span)
      Trades recorded only in this window contribute to n_trades / equity / sortino.

    `market_candles` (optional) separates what the strategy SEES from where the
    market actually IS. `candles` drives observe/decide; `market_candles` prices
    fills, matched by mts. Defaults to `candles`, i.e. unchanged behaviour.

    This exists to measure candle distortion honestly. Live observed a value the
    venue later revised, and quoted from it, while fills settled against the true
    rate. Passing one series to both sides makes `spread_pct` collapse to 0 and
    the distortion appear harmless — the artifact that invalidated the first L4 run.
    """
    if config.fill_model == "empirical":
        _preflight_empirical_model(candles, fill_model)
    if not candles:
        return BacktestResult(
            strategy_name=strategy.name, symbol="",
            start_mts=0, end_mts=0, n_candles=0,
            gross_monthly_return_pct=Decimal("0"),
            net_monthly_return_pct=Decimal("0"),
            max_drawdown_pct=Decimal("0"),
            n_trades=0, fill_rate=Decimal("0"),
            sortino=Decimal("0"),
            model_kind=config.fill_model,
        )

    sorted_candles = sorted(candles, key=lambda c: c.mts)
    # Fills price off the market series; absent one, the observed series is the
    # market (unchanged behaviour). Matched by mts so a gap in either series
    # degrades to "use the observed candle" rather than silently misaligning.
    market_by_mts = (
        {c.mts: c for c in market_candles} if market_candles is not None else None
    )
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
    n_window_candles = 0  # candles inside [effective_start, effective_end] regardless of cooldown
    fill_prob_sum = Decimal("0")
    cooldown_until_idx = -1
    # Assumes 1h candles: gap_minutes / 60 gives candle count. Other timeframes (5m, 1D) miscount.
    gap_candles = math.ceil(config.gap_minutes / 60)
    one_minus_fee = Decimal("1") - config.fee_rate

    equity_timeline: list[tuple[int, Decimal]] = []

    for i, candle in enumerate(sorted_candles):
        strategy.observe(candle)
        in_window = effective_start <= candle.mts <= effective_end
        if in_window:
            n_window_candles += 1

        if i <= cooldown_until_idx:
            continue
        if not in_window:
            continue

        decision = strategy.decide(candle)
        if decision is None:
            continue

        pricing_candle = (
            market_by_mts.get(candle.mts, candle) if market_by_mts is not None else candle
        )
        gross_rate, fill_prob = _apply_friction(
            decision, pricing_candle, config, fill_model
        )
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
        n_candles=n_window_candles,
        gross_monthly_return_pct=gross_monthly,
        net_monthly_return_pct=net_monthly,
        max_drawdown_pct=max_dd * Decimal("100"),
        n_trades=n_trades, fill_rate=fill_rate,
        sortino=sortino_value,
        model_kind=config.fill_model,
        model_version=(fill_model.artifact.model_version if fill_model and fill_model.artifact else None),
        artifact_hash=(fill_model.artifact.artifact_hash if fill_model and fill_model.artifact else None),
        model_cutoff_ms=(fill_model.artifact.cutoff_ms if fill_model and fill_model.artifact else None),
        model_sample_count=(
            fill_model.artifact.sample_count if fill_model and fill_model.artifact else None
        ),
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
    # equity_timeline is already sorted by construction (loop iterates sorted_candles
    # in mts ascending order; appends only on trade fires which preserve order).
    sorted_timeline = equity_timeline
    for me in month_ends:
        while cursor < len(sorted_timeline) and sorted_timeline[cursor][0] <= me:
            last_equity = sorted_timeline[cursor][1]
            cursor += 1
        samples.append((me, last_equity))
    rets = monthly_returns_from_equity_curve(samples)
    return compute_sortino(rets)
