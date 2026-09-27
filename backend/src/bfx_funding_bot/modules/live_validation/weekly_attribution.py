"""Per-cell weekly fee-adjusted realized APR（E3 (b) — 2026-07-06 review §1）。

Pure, I/O-free。與 G3 的差異：
- 分箱 = UTC Monday 00:00 對齊的 calendar weeks（穩定的 upsert key），
  （G3 自 2026-09-27 起也用同一套週，第一窗從第一筆 bot credit 起算）。
- 有 fee：net = gross × (1 − FEE_RATE)。G3 主 headline 維持 gross（報告連續性），
  本模組是 operator 儀表，直接給扣費後數字。
- per-cell：輸入是每 (cell, week) 的 accrual（CellWeekTotals），由
  credit_attribution 從 venue credit history 算出（2026-09-27 起；之前是
  ORDER_FILL × held-to-term 推估）。對不上 offer 的 credit 歸 "unattributed"。

語意：
- realized_apr_net_pct = net_interest / capital_days × 365 × 100
  — 實際部署資本的年化報酬（utilization 無關；capital_days = Σ size×該週實際持有天數）。
- baseline_*_apr_net_pct = mean(rate in week) × 365 × 100 × (1−fee)
  — 滿倉掛市場價/FRR 的理想化年化（full utilization 假設，與 G3 passive arm 同構）。
- baseline_frr_util_apr_net_pct = baseline_frr_apr_net_pct × mean(市場 utilization)
  — funding_amount_used / funding_amount（市場級、每 stats snapshot），把
  「FRR 掛單只在 market ≥ FRR 時成交」的 idle 折進 benchmark。idealized 欄
  保留當 upper bound；cap-gate spread 應讀本欄。utilization per-snapshot
  clamp 到 ≤1（分母近零 glitch 會 >1），保證本欄 ≤ idealized upper bound。
- 缺資料 = None，不是 0（零收益與缺資料必須可區分）。
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from bfx_funding_bot.modules.live_validation.live_attribution import MarketRatePoint

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
class CellWeekTotals:
    """Gross accrual of one cell within one calendar week (credits clipped to it)."""

    n_credits: int              # credits opened in this week
    capital_days: Decimal       # Σ amount × days held inside the week
    gross_interest: Decimal     # Σ amount × rate × days held inside the week


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


def _mean_rate_by_week(
    points: list[MarketRatePoint], *, clamp_one: bool = False
) -> dict[int, Decimal]:
    # clamp_one：utilization = used/total ∈ [0,1]。分母近零的 snapshot 會產生
    # >1 的 ratio outlier，per-point clamp 才擋得住（只 clamp 週均值會讓單點
    # glitch 把整週 util 調整抹平）。close/frr rate 不 clamp。
    by_week: dict[int, list[Decimal]] = {}
    for p in points:
        by_week.setdefault(calendar_week_start(p.mts), []).append(
            min(Decimal("1"), p.rate) if clamp_one else p.rate
        )
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
    totals_by_cell: dict[str, dict[int, CellWeekTotals]],
    close_points: list[MarketRatePoint],
    frr_points: list[MarketRatePoint],
    utilization_points: list[MarketRatePoint],
) -> list[WeeklyCellRow]:
    """cell × calendar-week 的 fee-adjusted 實得 + 三條 baseline。

    row 集合 = (每個 cell) × (該 cell 有 accrual 的週 ∪ 有 baseline 資料的週) —
    baseline 週沒 accrual 也出 row（前端 baseline 線不斷），accrual 週沒 baseline
    也出 row（baseline 欄 None）。n_fills 欄 = 該週開出的 credit 數。
    """
    close_by_week = _mean_rate_by_week(close_points)
    frr_by_week = _mean_rate_by_week(frr_points)
    util_by_week = _mean_rate_by_week(utilization_points, clamp_one=True)
    baseline_weeks = set(close_by_week) | set(frr_by_week)
    empty = CellWeekTotals(0, Decimal("0"), Decimal("0"))

    rows: list[WeeklyCellRow] = []
    for cell, weeks in totals_by_cell.items():
        for wk in sorted(set(weeks) | baseline_weeks):
            t = weeks.get(wk, empty)
            net = t.gross_interest * _ONE_MINUS_FEE
            apr = (
                net / t.capital_days * _DAYS_PER_YEAR * Decimal("100")
                if t.capital_days > 0 else None
            )
            frr_baseline = _baseline_apr_net(frr_by_week.get(wk))
            rows.append(WeeklyCellRow(
                cell=cell,
                week_start_ms=wk,
                week_end_ms=wk + WEEK_MS,
                n_fills=t.n_credits,
                gross_interest_usdt=t.gross_interest,
                net_interest_usdt=net,
                capital_days=t.capital_days,
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
