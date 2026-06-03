"""Pure logic for the AdaptivePeriod band (t1,t2) sweep.

Reuses the OOS engine + stats primitives; adds the two new statistics the
shared build_cell_report does not compute (active-series DSR; paired
band-vs-band difference CI). No engine/oos_eval changes. See
docs/superpowers/specs/2026-06-04-adaptive-period-band-sweep-design.md.
"""
from __future__ import annotations

import math
from decimal import Decimal

_T1_VALUES = [Decimal("0.5"), Decimal("1.0"), Decimal("1.5")]
_T2_VALUES = [Decimal("1.5"), Decimal("2.0"), Decimal("2.5")]


def enumerate_bands() -> list[tuple[Decimal, Decimal]]:
    """The 8 (t1, t2) variants, strict t1 < t2 (drops the (1.5,1.5) cell)."""
    return [(t1, t2) for t1 in _T1_VALUES for t2 in _T2_VALUES if t1 < t2]


from bfx_funding_bot.modules.backtest.strategies.adaptive_period import AdaptivePeriodStrategy
from bfx_funding_bot.modules.candles.schemas import FundingCandle

_GAP_MINUTES_DEFAULT = 30  # matches BacktestConfig.gap_minutes default


def simulate_period_path(
    candles: list[FundingCandle],
    *,
    ema_span: int,
    ratio_sigma: Decimal,
    t1: Decimal,
    t2: Decimal,
    p_mid: int,
    p_long: int,
    gap_minutes: int = _GAP_MINUTES_DEFAULT,
) -> list[int]:
    """Trade-level period sequence, mirroring engine.run_backtest's
    observe -> cooldown-skip -> decide loop (engine.py:104-127) so the
    distribution matches the actual backtest trades. Full series, no record
    window (record window defaults to full span in the engine)."""
    strat = AdaptivePeriodStrategy(
        ema_span=ema_span, ratio_sigma=ratio_sigma, t1=t1, t2=t2,
        p_mid=p_mid, p_long=p_long,
    )
    ordered = sorted(candles, key=lambda c: c.mts)
    gap_candles = math.ceil(gap_minutes / 60)
    cooldown_until_idx = -1
    periods: list[int] = []
    for i, candle in enumerate(ordered):
        strat.observe(candle)
        if i <= cooldown_until_idx:
            continue
        decision = strat.decide(candle)
        if decision is None:
            continue
        periods.append(decision.period_days)
        cooldown_until_idx = i + decision.period_days * 24 + gap_candles
    return periods


def period_profile(periods: list[int], *, p_long: int) -> dict[str, Decimal]:
    """avg_period (trade-mean) + p14_share (time-weighted: fraction of total
    locked-days spent in p_long locks — the §3 tail-risk axis)."""
    if not periods:
        return {"avg_period": Decimal("0"), "p14_share": Decimal("0")}
    total_days = Decimal(sum(periods))
    long_days = Decimal(sum(p for p in periods if p == p_long))
    return {
        "avg_period": Decimal(sum(periods)) / Decimal(len(periods)),
        "p14_share": (long_days / total_days) if total_days > 0 else Decimal("0"),
    }
