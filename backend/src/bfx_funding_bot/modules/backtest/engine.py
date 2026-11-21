import math
from collections.abc import Callable, Mapping
from decimal import Decimal
from typing import NoReturn

from bfx_funding_bot.external.bitfinex.funding_candle_row import FundingCandle
from bfx_funding_bot.modules.backtest.config import BacktestConfig, compute_fill_prob
from bfx_funding_bot.modules.backtest.incomplete import BacktestIncomplete as BacktestIncomplete
from bfx_funding_bot.modules.backtest.replay import HypotheticalOffer, TapeDays, replay_offer
from bfx_funding_bot.modules.backtest.schemas import BacktestResult
from bfx_funding_bot.modules.lending.tracking.artifact import FillModelUnavailable
from bfx_funding_bot.modules.lending.tracking.model import FillRateModel
from bfx_funding_bot.modules.performance.sortino import (
    compute_sortino,
    month_end_timestamps_within,
    monthly_returns_from_equity_curve,
)
from bfx_funding_bot.modules.strategy import LendDecision, Strategy

_MS_PER_DAY = Decimal(86_400_000)
_MS_PER_HOUR = 3_600_000
_AGGREGATE_SERIES_KEY = "a30"
# Empirical evidence sources the engine accepts. "candle" = G13 path-crossing,
# "book" = queue-position replay over recorded funding books. Rows and artifacts
# of different sources never mix: the artifact hash binds each stat row to one.
EMPIRICAL_SOURCES = frozenset({"candle", "book"})


def resolve_market_series_key(period_days: int, available: Mapping[str, object]) -> str:
    """Pick the market series that prices a lock of `period_days`.

    Exact tenor first (`p2`, `p30`, ...); otherwise Bitfinex's 2-30d aggregate
    (`a30`) as the stated approximation. Raises KeyError when neither exists so a
    missing series cannot silently degrade to the observed candle.
    """
    exact = f"p{period_days}"
    if exact in available:
        return exact
    if _AGGREGATE_SERIES_KEY in available:
        return _AGGREGATE_SERIES_KEY
    raise KeyError(
        f"no market series for period {period_days}: have {sorted(available)}"
    )


def _raise_for_unavailable(evidence: FillModelUnavailable) -> NoReturn:
    if evidence.reason == "low_confidence":
        raise BacktestIncomplete("fill_model_low_confidence")
    if evidence.reason == "scope_mismatch":
        raise BacktestIncomplete("fill_model_scope_mismatch")
    raise BacktestIncomplete("fill_model_missing")


def _preflight_empirical_model(
    candles: list[FundingCandle], fill_horizon_h: int, fill_model: FillRateModel | None
) -> None:
    if fill_model is None or fill_model.artifact is None:
        raise BacktestIncomplete("fill_model_missing")
    if fill_model.unavailable_reason is not None:
        _raise_for_unavailable(FillModelUnavailable(fill_model.unavailable_reason))

    artifact = fill_model.artifact
    if (
        artifact.source not in EMPIRICAL_SOURCES
        or artifact.horizon_h != fill_horizon_h
        or any(
            candle.symbol != artifact.symbol
            or candle.period_agg != artifact.period_agg
            for candle in candles
        )
    ):
        _raise_for_unavailable(FillModelUnavailable("scope_mismatch"))


def _validate_candle_artifact_scope(
    candle: FundingCandle, fill_model: FillRateModel
) -> None:
    artifact = fill_model.artifact
    if artifact is not None and (
        artifact.source not in EMPIRICAL_SOURCES or artifact.symbol != candle.symbol
    ):
        _raise_for_unavailable(FillModelUnavailable("scope_mismatch"))


def _preflight_series_models(
    series: Mapping[str, list[FundingCandle]],
    fill_horizon_h: int,
    models: Mapping[str, FillRateModel],
) -> None:
    """Each per-series model must be scoped to exactly the series it will price."""
    for key, model in models.items():
        if key not in series:
            _raise_for_unavailable(FillModelUnavailable("scope_mismatch"))
        _preflight_empirical_model(series[key], fill_horizon_h, model)
        artifact = model.artifact
        if artifact is not None and artifact.period_agg != key:
            _raise_for_unavailable(FillModelUnavailable("scope_mismatch"))


def _resolve_market_rate(candle: FundingCandle, source: str) -> Decimal | None:
    # candle_close is the canonical per-day market funding rate. FRR is a
    # distinct quantity, not a unit-convertible market-rate proxy (ARCHITECTURE.md,
    # backtest engine contract).
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
    if market_rate is None or market_rate <= 0:
        # No usable market print for this fill: refuse rather than assume a 100%
        # fill at the quoted rate (that assumption is exactly what a research run
        # must not smuggle in).
        raise BacktestIncomplete("market_series_gap")
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


def run_backtest(  # noqa: C901, PLR0912, PLR0913, PLR0915, PLR0917
    candles: list[FundingCandle],
    strategy: Strategy,
    config: BacktestConfig,
    record_start_mts: int | None = None,
    record_end_mts: int | None = None,
    fill_model: FillRateModel | None = None,
    market_candles: list[FundingCandle] | None = None,
    market_series_by_agg: Mapping[str, list[FundingCandle]] | None = None,
    fill_models_by_agg: Mapping[str, FillRateModel] | None = None,
    tape: TapeDays | None = None,
    same_rate_queue_ahead: Callable[[HypotheticalOffer], Decimal | None] | None = None,
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

    `market_series_by_agg` (optional, exclusive with `market_candles`) prices each
    fill off the series matching the decision's lock tenor — `p2` for 2-day
    offers, `p30` for 30-day, `a30` as the aggregate fallback — via
    `resolve_market_series_key`. A strategy that observes the a30 series but
    posts 2-day offers is then scored against the 2-day market it actually
    competes in, instead of being credited the aggregate rate for a 2-day lock.

    `tape` (required by, and only allowed with, `fill_model="tape-replay"`) books
    each decision as a hypothetical offer replayed against the public trade tape
    (`backtest/replay.py`): the chosen bound's filled fraction earns the rate, the
    lock starts when the offer finishes filling, and an offer never fully filled
    rests `replay_max_wait_h` before the strategy decides again. A decision whose
    full wait would pass the record window end is not placed, so the last
    `replay_max_wait_h` hours of each window never trade
    (`replay_no_placement_tail_h` widens that tail). The offer is placed when the decision candle closes.
    `idle_hours` counts the waits plus the unfilled share of each lock.
    `same_rate_queue_ahead` (optional) returns the same-rate queue ahead of an
    offer, from book snapshots where they exist; `queue_fill_rate` averages the
    queue estimate over the offers it covered. A missing tape day raises
    `BacktestIncomplete("tape_day_missing")`.
    """
    if market_candles is not None and market_series_by_agg is not None:
        raise ValueError("pass market_candles or market_series_by_agg, not both")
    if fill_models_by_agg is not None and fill_model is not None:
        raise ValueError("pass fill_model or fill_models_by_agg, not both")
    if fill_models_by_agg is not None and market_series_by_agg is None:
        raise ValueError("fill_models_by_agg requires market_series_by_agg")
    replaying = config.fill_model == "tape-replay"
    if replaying and tape is None:
        raise ValueError("fill_model='tape-replay' requires tape")
    if not replaying and tape is not None:
        raise ValueError("tape is only used with fill_model='tape-replay'")
    if not replaying and same_rate_queue_ahead is not None:
        raise ValueError("same_rate_queue_ahead is only used with fill_model='tape-replay'")
    if replaying and (fill_model is not None or fill_models_by_agg is not None
                      or market_candles is not None or market_series_by_agg is not None):
        raise ValueError("tape-replay prices fills from the tape: no fill models or market series")
    if config.fill_model == "empirical":
        if fill_models_by_agg is not None:
            assert market_series_by_agg is not None  # guarded above
            _preflight_series_models(market_series_by_agg, config.fill_horizon_h, fill_models_by_agg)
        else:
            _preflight_empirical_model(candles, config.fill_horizon_h, fill_model)
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
            replay_bound=config.replay_bound if replaying else None,
            idle_hours=Decimal("0") if replaying else None,
        )

    sorted_candles = sorted(candles, key=lambda c: c.mts)
    # Fills price off the market series; absent one, the observed series is the
    # market (unchanged behaviour). Matched by mts; a decision whose mts has no
    # market print is `market_series_gap`, never a fallback to the observed candle.
    market_by_mts = (
        {c.mts: c for c in market_candles} if market_candles is not None else None
    )
    series_maps: dict[str, dict[int, FundingCandle]] | None = (
        {key: {c.mts: c for c in series} for key, series in market_series_by_agg.items()}
        if market_series_by_agg is not None
        else None
    )
    series_used: dict[str, int] = {}
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
    idle_ms = 0
    queue_fill_sum = Decimal("0")
    queue_trades = 0
    max_wait_ms = config.replay_max_wait_h * 3_600_000
    no_placement_tail_ms = (config.replay_no_placement_tail_h or config.replay_max_wait_h) * 3_600_000
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

        lock_start_mts = candle.mts
        if replaying:
            assert tape is not None  # guarded above
            # decide() read this candle's close, known only when the hour ends:
            # the offer rests from then on, never on trades inside its own hour.
            placed_mts = candle.mts + _MS_PER_HOUR
            # A decision whose full wait would run past the record window is not
            # placed: a shortened wait would read as a market non-fill and bias
            # fill_rate down. The tape is then only needed through effective_end.
            # `replay_no_placement_tail_h` widens that tail so arms with different
            # waits stop placing at the same point.
            if placed_mts + no_placement_tail_ms > effective_end:
                continue
            window_ms = max_wait_ms
            offer = HypotheticalOffer(rate=decision.rate, period_days=decision.period_days,
                                      placed_mts=placed_mts, amount=config.replay_offer_amount)
            queue_ahead = same_rate_queue_ahead(offer) if same_rate_queue_ahead else None
            replayed = replay_offer(offer, tape, max_wait_ms=window_ms,
                                    same_rate_queue_ahead=queue_ahead)
            if config.replay_bound == "conservative":
                path = replayed.conservative
            elif config.replay_bound == "optimistic":
                path = replayed.optimistic
            else:  # pragma: no cover - BacktestConfig validates the literal.
                raise ValueError(f"unsupported replay_bound: {config.replay_bound!r}")
            fill_prob = path.filled / config.replay_offer_amount
            if replayed.queue_estimate is not None:
                queue_fill_sum += replayed.queue_estimate.filled / config.replay_offer_amount
                queue_trades += 1
            gross_rate = decision.rate * fill_prob
            wait_ms = (path.full_fill_mts - candle.mts if path.full_fill_mts is not None
                       else placed_mts - candle.mts + window_ms)
            idle_ms += wait_ms
            lock_start_mts = candle.mts + wait_ms
            lock_candles = decision.period_days * 24 if fill_prob > 0 else 0
            next_cooldown = i + math.ceil(wait_ms / _MS_PER_HOUR) + lock_candles + gap_candles
        else:
            if series_maps is not None:
                series_key = resolve_market_series_key(decision.period_days, series_maps)
                priced = series_maps[series_key].get(candle.mts)
                if priced is None:
                    raise BacktestIncomplete("market_series_gap")
                pricing_candle = priced
                series_used[series_key] = series_used.get(series_key, 0) + 1
            elif market_by_mts is not None:
                priced = market_by_mts.get(candle.mts)
                if priced is None:
                    raise BacktestIncomplete("market_series_gap")
                pricing_candle = priced
            else:
                pricing_candle = candle
            trade_model = fill_model
            if fill_models_by_agg is not None:
                trade_model = fill_models_by_agg.get(pricing_candle.period_agg)
                if trade_model is None and config.fill_model == "empirical":
                    # Strict: a series without book evidence cannot borrow another's.
                    raise BacktestIncomplete("fill_model_missing")
            gross_rate, fill_prob = _apply_friction(
                decision, pricing_candle, config, trade_model
            )
            next_cooldown = i + decision.period_days * 24 + gap_candles
        period = Decimal(decision.period_days)
        if config.truncate_at_window_end:
            remaining_days = Decimal(effective_end - lock_start_mts) / _MS_PER_DAY
            period = min(period, max(remaining_days, Decimal("0")))
        gross_equity = gross_equity * (Decimal("1") + gross_rate * period)
        net_rate = gross_rate * one_minus_fee
        net_equity = net_equity * (Decimal("1") + net_rate * period)
        if replaying and fill_prob > 0:
            # The unfilled share of a partly filled offer sits out the lock too,
            # clipped to the record window whether or not interest is truncated.
            in_window_days = min(Decimal(decision.period_days),
                                 max(Decimal(effective_end - lock_start_mts) / _MS_PER_DAY,
                                     Decimal(0)))
            idle_ms += int((Decimal(1) - fill_prob) * in_window_days * _MS_PER_DAY)

        n_trades += 1
        fill_prob_sum += fill_prob
        cooldown_until_idx = next_cooldown

        if net_equity > peak:
            peak = net_equity
        dd = (peak - net_equity) / peak if peak > 0 else Decimal("0")
        if dd > max_dd:
            max_dd = dd

        equity_timeline.append((lock_start_mts, net_equity))

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
        pricing_series_used=series_used if series_maps is not None else None,
        fill_models_by_series=(
            {
                key: (m.artifact.artifact_hash if m.artifact is not None else "unversioned")
                for key, m in fill_models_by_agg.items()
            }
            if fill_models_by_agg is not None
            else None
        ),
        replay_bound=config.replay_bound if replaying else None,
        idle_hours=Decimal(idle_ms) / _MS_PER_HOUR if replaying else None,
        queue_fill_rate=(queue_fill_sum / Decimal(queue_trades)) if queue_trades else None,
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
