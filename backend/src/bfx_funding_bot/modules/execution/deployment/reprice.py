"""Stale-offer reprice policy (E1).

純 policy：哪些 resting venue offer 該 cancel，讓 reserved 資金能以現行
standing quote 重掛。執行（venue call）由 DeploymentReconciler 負責
（single-writer）；本模組零 I/O。

Policy = reprice-DOWN only，anti-chase by construction：
  cancel iff offer.rate > ref_rate * (1 + tolerance_pct)
       and (now_ms - offer.mts_created) >= min_age_ms
ref_rate 依 `reference` 而定：
  quote — 該 symbol 所有 active POST quote 的最高 rate（只要還有任何 cell
          願意掛到那個價位，offer 就不算 stale）。
  book  — 把每個 active POST quote 以 PeriodPricer 對當下 exact-period book
          重新定價（同 symbol、同期限、該 offer 的剩餘金額），取最高者。
          送單價本來就由 book 定（E2），參考價若仍用 signal quote，會在
          市場沒動時把 book 定出的合法價位當 stale 砍掉。book 或對應期限的
          quote 不可用時不砍。
低於參考價的 offer 會自然成交，不砍；配合 min_age 使 spike 當小時不會自追。
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from bfx_funding_bot.external.bitfinex.auth_rest import ActiveFundingOffer

REFERENCE_MODES = ("quote", "book")


@dataclass(frozen=True, slots=True)
class RepricePolicy:
    enabled: bool               # False = observe-only（log would_cancel，不打 venue）
    tolerance_pct: float        # 0.10 = offer rate 高於參考價 10% 以上才砍
    min_age_ms: int             # 齡低於此值一律不砍（anti-chase）
    max_cancels_per_tick: int   # 跨 symbol 的每 tick 上限（churn bound）
    reference: str = "quote"    # "quote"（現行）| "book"（PeriodPricer 對當下 book 重定價）

    def __post_init__(self) -> None:
        if self.reference not in REFERENCE_MODES:
            raise ValueError(f"reprice reference must be one of {REFERENCE_MODES}")


def policy_from_env(environ: Mapping[str, str]) -> RepricePolicy:
    """從 env 建 policy；全部未設定 = observe-only、quote 參考的安全預設。"""
    return RepricePolicy(
        enabled=environ.get("BFX_REPRICE_ENABLED", "false").lower() in ("1", "true", "yes"),
        tolerance_pct=float(environ.get("BFX_REPRICE_TOLERANCE_PCT", "0.10")),
        min_age_ms=int(float(environ.get("BFX_REPRICE_MIN_AGE_S", "1800")) * 1000),
        max_cancels_per_tick=int(environ.get("BFX_REPRICE_MAX_CANCELS_PER_TICK", "3")),
        reference=environ.get("BFX_REPRICE_REFERENCE", "quote").strip().lower(),
    )


def stale_offers(
    *,
    offers: list[ActiveFundingOffer],
    ref_rate: float,
    now_ms: int,
    policy: RepricePolicy,
) -> list[ActiveFundingOffer]:
    """回傳 stale-high 且夠老的 offers，最超價的排最前（cancel budget 先救
    卡最多收益的資金）。嚴格大於 threshold 才算 stale。"""
    threshold = ref_rate * (1.0 + policy.tolerance_pct)
    out = [
        o for o in offers
        if o.rate > threshold and (now_ms - o.mts_created) >= policy.min_age_ms
    ]
    out.sort(key=lambda o: o.rate, reverse=True)
    return out


def stale_offers_with_refs(
    *,
    offers: list[ActiveFundingOffer],
    ref_rate_by_offer: Mapping[str, float],
    now_ms: int,
    policy: RepricePolicy,
) -> list[ActiveFundingOffer]:
    """Same predicate as `stale_offers`, but each offer is judged against its own
    reference (book mode prices per offer: period and remaining amount differ).
    Offers without a reference are never stale — no evidence, no cancel."""
    out = [
        o for o in offers
        if o.venue_offer_id in ref_rate_by_offer
        and o.rate > ref_rate_by_offer[o.venue_offer_id] * (1.0 + policy.tolerance_pct)
        and (now_ms - o.mts_created) >= policy.min_age_ms
    ]
    out.sort(key=lambda o: o.rate, reverse=True)
    return out
