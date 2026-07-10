"""Per-cell weekly fee-adjusted realized APR（E3 (b) — 2026-07-06 review §1）。

Pure, I/O-free。與 G3 的差異：
- 分箱 = UTC Monday 00:00 對齊的 calendar weeks（穩定的 upsert key），
  非 weekly_window_bounds 的「資料 min_ts 起算 rolling 7 天 bins」（G3 保持原樣）。
- 有 fee：net = gross × (1 − FEE_RATE)。G3 主 headline 維持 gross（報告連續性），
  本模組是 operator 儀表，直接給扣費後數字。
- per-cell：cell key 由 loader（scripts/run_weekly_attribution.py）經 diagnostics
  DECISION join 提供；join 不到的 fills 歸 "unattributed"（loader 保證，非本模組）。

語意：
- realized_apr_net_pct = net_interest / capital_days × 365 × 100
  — 實際部署資本的年化報酬（utilization 無關；capital_days = Σ size×duration）。
- baseline_*_apr_net_pct = mean(rate in week) × 365 × 100 × (1−fee)
  — 滿倉掛市場價/FRR 的理想化年化（full utilization 假設，與 G3 passive arm 同構）。
- baseline_frr_util_apr_net_pct = baseline_frr_apr_net_pct × mean(市場 utilization)
  — funding_amount_used / funding_amount（市場級、每 stats snapshot），把
  「FRR 掛單只在 market ≥ FRR 時成交」的 idle 折進 benchmark。idealized 欄
  保留當 upper bound；cap-gate spread 應讀本欄。
- 缺資料 = None，不是 0（零收益與缺資料必須可區分）。
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from bfx_funding_bot.modules.live_validation.live_attribution import (
    FillRecord,
    MarketRatePoint,
    fill_duration_days,
)

WEEK_MS = 7 * 24 * 60 * 60 * 1000
# 1970-01-01 是週四；第一個 UTC 週一 = 1970-01-05 = 345_600_000 ms
_EPOCH_MONDAY_MS = 4 * 24 * 60 * 60 * 1000
# Bitfinex 抽 15% 利息。keep in sync with modules/backtest/config.py fee_rate。
FEE_RATE = Decimal("0.15")
_ONE_MINUS_FEE = Decimal("1") - FEE_RATE
_DAYS_PER_YEAR = Decimal("365")


def calendar_week_start(mts: int) -> int:
    """該 timestamp 所屬 UTC calendar week 的週一 00:00（ms）。"""
    return mts - (mts - _EPOCH_MONDAY_MS) % WEEK_MS


@dataclass(frozen=True)
class WeeklyCellRow:
    cell: str
    week_start_ms: int
    week_end_ms: int
    n_fills: int
    gross_interest_usdt: Decimal
    net_interest_usdt: Decimal
    capital_days: Decimal
    realized_apr_net_pct: Decimal | None
    baseline_close_apr_net_pct: Decimal | None
    baseline_frr_apr_net_pct: Decimal | None
    baseline_frr_util_apr_net_pct: Decimal | None


def _mean_rate_by_week(points: list[MarketRatePoint]) -> dict[int, Decimal]:
    by_week: dict[int, list[Decimal]] = {}
    for p in points:
        by_week.setdefault(calendar_week_start(p.mts), []).append(p.rate)
    return {
        wk: sum(rates, Decimal("0")) / Decimal(len(rates))
        for wk, rates in by_week.items()
    }


def _baseline_apr_net(mean_rate: Decimal | None) -> Decimal | None:
    if mean_rate is None:
        return None
    return mean_rate * _DAYS_PER_YEAR * Decimal("100") * _ONE_MINUS_FEE


def compute_weekly_rows(
    *,
    fills_by_cell: dict[str, list[FillRecord]],
    close_points: list[MarketRatePoint],
    frr_points: list[MarketRatePoint],
    utilization_points: list[MarketRatePoint],
) -> list[WeeklyCellRow]:
    """cell × calendar-week 的 fee-adjusted 實得 + 三條 baseline。

    row 集合 = (每個 cell) × (該 cell 有 fill 的週 ∪ 有 baseline 資料的週) —
    baseline 週沒 fill 也出 row（前端 baseline 線不斷），fill 週沒 baseline
    也出 row（baseline 欄 None）。fill 歸屬其 fill_ts 所在週（G3 同慣例）。
    """
    close_by_week = _mean_rate_by_week(close_points)
    frr_by_week = _mean_rate_by_week(frr_points)
    util_by_week = _mean_rate_by_week(utilization_points)
    baseline_weeks = set(close_by_week) | set(frr_by_week)

    rows: list[WeeklyCellRow] = []
    for cell, fills in fills_by_cell.items():
        fills_by_week: dict[int, list[FillRecord]] = {}
        for f in fills:
            fills_by_week.setdefault(calendar_week_start(f.fill_ts_ms), []).append(f)
        for wk in sorted(set(fills_by_week) | baseline_weeks):
            wk_fills = fills_by_week.get(wk, [])
            gross = sum(
                (f.size_usdt * f.rate * fill_duration_days(f) for f in wk_fills),
                Decimal("0"),
            )
            capital_days = sum(
                (f.size_usdt * fill_duration_days(f) for f in wk_fills),
                Decimal("0"),
            )
            net = gross * _ONE_MINUS_FEE
            apr = (
                net / capital_days * _DAYS_PER_YEAR * Decimal("100")
                if capital_days > 0 else None
            )
            frr_baseline = _baseline_apr_net(frr_by_week.get(wk))
            rows.append(WeeklyCellRow(
                cell=cell,
                week_start_ms=wk,
                week_end_ms=wk + WEEK_MS,
                n_fills=len(wk_fills),
                gross_interest_usdt=gross,
                net_interest_usdt=net,
                capital_days=capital_days,
                realized_apr_net_pct=apr,
                baseline_close_apr_net_pct=_baseline_apr_net(close_by_week.get(wk)),
                baseline_frr_apr_net_pct=frr_baseline,
                baseline_frr_util_apr_net_pct=(
                    frr_baseline * util_by_week[wk]
                    if frr_baseline is not None and wk in util_by_week
                    else None
                ),
            ))
    return rows
