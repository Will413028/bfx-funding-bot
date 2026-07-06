"""Book-aware rate clamp (E2 — docs/research/2026-07-06-profit-design-review.md §1).

純 policy：submit 前把 signal 層的 quote rate 對齊 live funding book。
執行（ticker fetch、observe/enforce）由 DeploymentReconciler 負責；本模組零 I/O。

分支（優先序）：
  1. TAKER    — bid ≥ quote.rate 且 bid_size ≥ amount 且 bid_period ≤ 上限
                → 掛 quote.rate 直接吃單（唯一保證成交路徑；fill 繼承 bid 的
                rate/period，rate ≥ quote.rate = signal floor 不破）。
  2. FALLBACK — 無 ticker / book 退化（bid≤0 或 ask≤TICK）→ quote.rate 原樣
                （= 現狀行為，fail-closed）。
  3. FLOOR    — 競爭價（ask − TICK）低於 quote.rate×(1−max_down_pct)
                → 放棄 clamp、掛原價排隊。>max_down 的下移是 regime 判斷，
                屬 signal 層職權（下一個 1h boundary 由 MR gate 裁決）。
  4. UNDERCUT — quote 高於競爭價（排隊尾）→ 降到 ask − TICK 搶隊首。
     RAISE    — quote 低於競爭價（賤賣 spread）→ 抬到 ask − TICK。

風險不對稱（為什麼 down 有 floor、up 沒有）：clamp-up 最壞 = 掛太高不成交，
E1 reprice sweep 在 min_age 後收斂（閒置有界 ~30-60min）；clamp-down 最壞 =
以爛 rate 成交鎖 2 天（不可逆）。
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum

from bfx_funding_bot.external.bitfinex.rest import FundingTicker

# Bitfinex funding rate 最小跳動（Go-era pricing.go minTickSize；venue 屬性，
# 非 policy）。比 best ask 低 1 tick = price priority 隊首。
TICK: float = 1e-8


class ClampBranch(StrEnum):
    TAKER = "taker"
    UNDERCUT = "undercut"
    RAISE = "raise"
    FLOOR = "floor"
    FALLBACK = "fallback"


@dataclass(frozen=True, slots=True)
class ClampPolicy:
    enabled: bool                # False = observe-only（log would_adjust，掛原 rate）
    max_down_pct: float          # 0.15 = 競爭價低於 quote 15% 以上 → 不追，掛原價
    taker_max_period_days: int   # taker fill 繼承 bid_period；超過此天期不吃單


def clamp_policy_from_env(environ: Mapping[str, str]) -> ClampPolicy:
    """從 env 建 policy；全部未設定 = observe-only 安全預設。"""
    return ClampPolicy(
        enabled=environ.get("BFX_CLAMP_ENABLED", "false").lower() in ("1", "true", "yes"),
        max_down_pct=float(environ.get("BFX_CLAMP_MAX_DOWN_PCT", "0.15")),
        taker_max_period_days=int(environ.get("BFX_CLAMP_TAKER_MAX_PERIOD_D", "7")),
    )


@dataclass(frozen=True, slots=True)
class ClampDecision:
    rate: float
    branch: ClampBranch


def clamp_rate(
    *,
    quote_rate: float,
    amount: float,
    ticker: FundingTicker | None,
    policy: ClampPolicy,
) -> ClampDecision:
    """單筆 fill 的 clamp 決策。純函式；對 policy.enabled 無感 —
    observe/enforce 由呼叫方（reconciler）決定。"""
    if ticker is None or ticker.bid <= 0.0 or ticker.ask <= TICK:
        return ClampDecision(rate=quote_rate, branch=ClampBranch.FALLBACK)
    if (
        ticker.bid >= quote_rate
        and ticker.bid_size >= amount
        and ticker.bid_period <= policy.taker_max_period_days
    ):
        return ClampDecision(rate=quote_rate, branch=ClampBranch.TAKER)
    # round 消 float 減法尾差；rate 值 ~1e-4、grid 1e-8 → 10 位小數無損
    competitive = round(ticker.ask - TICK, 10)
    if competitive < quote_rate * (1.0 - policy.max_down_pct):
        return ClampDecision(rate=quote_rate, branch=ClampBranch.FLOOR)
    if competitive >= quote_rate:
        return ClampDecision(rate=competitive, branch=ClampBranch.RAISE)
    return ClampDecision(rate=competitive, branch=ClampBranch.UNDERCUT)
