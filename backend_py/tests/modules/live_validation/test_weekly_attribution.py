from decimal import Decimal

from bfx_funding_bot.modules.live_validation.live_attribution import (
    FillRecord,
    MarketRatePoint,
)
from bfx_funding_bot.modules.live_validation.weekly_attribution import (
    FEE_RATE,
    WEEK_MS,
    WeeklyCellRow,
    calendar_week_start,
    compute_weekly_rows,
)

# 2026-06-29 是週一。UTC 2026-06-29T00:00:00 = 1782691200000 ms
_MON = 1_782_691_200_000
_DAY = 24 * 60 * 60 * 1000


def _fill(ts: int, size: str, rate: str, period: str = "2") -> FillRecord:
    return FillRecord(
        venue_offer_id=str(ts), fill_ts_ms=ts, size_usdt=Decimal(size),
        rate=Decimal(rate), period_days=Decimal(period), release_ts_ms=None,
    )


def test_calendar_week_start_aligns_to_utc_monday():
    assert calendar_week_start(_MON) == _MON               # 週一 00:00 本身
    assert calendar_week_start(_MON + 3 * _DAY + 5) == _MON  # 週四某刻 → 本週一
    assert calendar_week_start(_MON - 1) == _MON - WEEK_MS   # 週日深夜 → 上週一


def test_compute_weekly_rows_fee_and_apr():
    # 單 cell 單 fill：500 USDT × 0.0002/day × 2 天 = gross 0.2
    fills = {"fUST_p2": [_fill(_MON + _DAY, "500", "0.0002")]}
    rows = compute_weekly_rows(
        fills_by_cell=fills, close_points=[], frr_points=[],
    )
    assert len(rows) == 1
    r = rows[0]
    assert isinstance(r, WeeklyCellRow)
    assert r.cell == "fUST_p2"
    assert r.week_start_ms == _MON
    assert r.week_end_ms == _MON + WEEK_MS
    assert r.n_fills == 1
    assert r.gross_interest_usdt == Decimal("0.2")
    assert r.net_interest_usdt == Decimal("0.2") * (Decimal("1") - FEE_RATE)
    # capital_days = 500 × 2 = 1000；APR_net = 0.17/1000 × 365 × 100 = 6.205%
    assert r.capital_days == Decimal("1000")
    assert r.realized_apr_net_pct == (
        Decimal("0.17") / Decimal("1000") * Decimal("365") * Decimal("100")
    )
    # 無 baseline 資料 → None（不是 0 — 缺資料與零收益必須可區分）
    assert r.baseline_close_apr_net_pct is None
    assert r.baseline_frr_apr_net_pct is None


def test_compute_weekly_rows_bins_by_fill_week():
    fills = {"fUST_p2": [
        _fill(_MON + _DAY, "500", "0.0002"),
        _fill(_MON + WEEK_MS + _DAY, "300", "0.0003"),
    ]}
    rows = compute_weekly_rows(fills_by_cell=fills, close_points=[], frr_points=[])
    assert [(r.week_start_ms, r.n_fills) for r in rows] == [
        (_MON, 1), (_MON + WEEK_MS, 1),
    ]


def test_compute_weekly_rows_baselines_use_week_mean_rate():
    # close 均值 0.0002/day → APR_net = 0.0002×365×100×0.85 = 6.205%
    fills = {"fUST_p2": [_fill(_MON + _DAY, "500", "0.0002")]}
    close = [
        MarketRatePoint(mts=_MON + i * _DAY, rate=Decimal("0.0002"))
        for i in range(3)
    ]
    frr = [MarketRatePoint(mts=_MON + _DAY, rate=Decimal("0.0003"))]
    rows = compute_weekly_rows(fills_by_cell=fills, close_points=close, frr_points=frr)
    r = rows[0]
    expected_close = Decimal("0.0002") * Decimal("365") * Decimal("100") * Decimal("0.85")
    expected_frr = Decimal("0.0003") * Decimal("365") * Decimal("100") * Decimal("0.85")
    assert r.baseline_close_apr_net_pct == expected_close
    assert r.baseline_frr_apr_net_pct == expected_frr


def test_compute_weekly_rows_baseline_weeks_without_fills_still_emitted():
    # 有市場資料但該週無 fill 的 cell 也要出 row（三線圖的 baseline 線不能斷）
    close = [MarketRatePoint(mts=_MON + _DAY, rate=Decimal("0.0002"))]
    rows = compute_weekly_rows(
        fills_by_cell={"fUST_p2": []}, close_points=close, frr_points=[],
    )
    assert len(rows) == 1
    r = rows[0]
    assert r.n_fills == 0
    assert r.gross_interest_usdt == Decimal("0")
    assert r.realized_apr_net_pct is None  # capital_days=0 → APR 未定義
    assert r.baseline_close_apr_net_pct is not None


def test_unattributed_bucket_is_a_normal_cell_key():
    rows = compute_weekly_rows(
        fills_by_cell={"unattributed": [_fill(_MON, "200", "0.0002")]},
        close_points=[], frr_points=[],
    )
    assert rows[0].cell == "unattributed"
