# tests/modules/execution/deployment/test_reprice.py
from decimal import Decimal

import pytest

from bfx_funding_bot.external.bitfinex.auth_rest import ActiveFundingOffer
from bfx_funding_bot.modules.execution.deployment.reprice import (
    RepricePolicy,
    policy_from_env,
    stale_offers,
    stale_offers_with_refs,
)

_NOW = 10_000_000
_POLICY = RepricePolicy(
    enabled=True, tolerance_pct=0.10, min_age_ms=1_800_000, max_cancels_per_tick=3,
)


def _offer(
    voi: str = "1", rate: float = 0.001, age_ms: int = 3_600_000, symbol: str = "fUST",
) -> ActiveFundingOffer:
    return ActiveFundingOffer(
        venue_offer_id=voi, symbol=symbol, amount=Decimal("200"), rate=rate,
        period_days=2, mts_created=_NOW - age_ms, status="ACTIVE",
    )


def test_overpriced_old_offer_is_stale():
    # ref 0.0002, offer 0.001 = 5x → 遠超 +10% tolerance，齡 60min ≥ 30min
    got = stale_offers(offers=[_offer()], ref_rate=0.0002, now_ms=_NOW, policy=_POLICY)
    assert [o.venue_offer_id for o in got] == ["1"]


def test_within_tolerance_not_stale():
    # offer 僅高於 ref 5%（tolerance 10%）→ 留著
    got = stale_offers(
        offers=[_offer(rate=0.00021)], ref_rate=0.0002, now_ms=_NOW, policy=_POLICY,
    )
    assert got == []


def test_exactly_at_threshold_not_stale():
    # 邊界：恰等於 ref*(1+tol) 不砍（嚴格大於才砍）
    got = stale_offers(
        offers=[_offer(rate=0.00022)], ref_rate=0.0002, now_ms=_NOW, policy=_POLICY,
    )
    assert got == []


def test_below_quote_never_stale():
    # 低於現行 quote 的 offer 即將被市場吃掉，不砍
    got = stale_offers(
        offers=[_offer(rate=0.0001)], ref_rate=0.0002, now_ms=_NOW, policy=_POLICY,
    )
    assert got == []


def test_young_offer_not_stale():
    # anti-chase：spike 當小時（齡 10min < 30min）即使超價也不砍
    got = stale_offers(
        offers=[_offer(age_ms=600_000)], ref_rate=0.0002, now_ms=_NOW, policy=_POLICY,
    )
    assert got == []


def test_sorted_most_overpriced_first():
    got = stale_offers(
        offers=[_offer(voi="a", rate=0.0005), _offer(voi="b", rate=0.002)],
        ref_rate=0.0002, now_ms=_NOW, policy=_POLICY,
    )
    assert [o.venue_offer_id for o in got] == ["b", "a"]


def test_policy_from_env_defaults():
    p = policy_from_env({})
    assert p == RepricePolicy(
        enabled=False, tolerance_pct=0.10, min_age_ms=1_800_000, max_cancels_per_tick=3,
    )


def test_policy_from_env_enabled_variants():
    for truthy in ("1", "true", "TRUE", "yes"):
        assert policy_from_env({"BFX_REPRICE_ENABLED": truthy}).enabled is True
    assert policy_from_env({"BFX_REPRICE_ENABLED": "false"}).enabled is False


def test_policy_from_env_overrides():
    p = policy_from_env({
        "BFX_REPRICE_TOLERANCE_PCT": "0.05",
        "BFX_REPRICE_MIN_AGE_S": "600",
        "BFX_REPRICE_MAX_CANCELS_PER_TICK": "1",
    })
    assert p.tolerance_pct == 0.05
    assert p.min_age_ms == 600_000
    assert p.max_cancels_per_tick == 1


def test_with_refs_judges_each_offer_against_its_own_reference():
    offers = [_offer(voi="a", rate=0.001), _offer(voi="b", rate=0.001), _offer(voi="c", rate=0.001)]
    refs = {"a": 0.0002, "b": 0.00095}  # c has no reference -> no evidence, no cancel
    got = stale_offers_with_refs(
        offers=offers, ref_rate_by_offer=refs, now_ms=_NOW, policy=_POLICY,
    )
    assert [o.venue_offer_id for o in got] == ["a"]


def test_policy_from_env_reference_defaults_to_quote_and_validates():
    assert policy_from_env({}).reference == "quote"
    assert policy_from_env({"BFX_REPRICE_REFERENCE": "Book"}).reference == "book"
    with pytest.raises(ValueError):
        policy_from_env({"BFX_REPRICE_REFERENCE": "ticker"})
