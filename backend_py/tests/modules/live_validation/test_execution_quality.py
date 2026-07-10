"""Pure pairing/bucketing/summary logic — no I/O."""
from decimal import Decimal

from bfx_funding_bot.modules.live_validation.execution_quality import (
    ClaimEvent,
    ClaimOutcome,
    FillEvent,
    bucket_by_regime,
    pair_claims_to_fills,
    summarize,
)

TTL_MS = 3_900_000  # 65 min, same default as BFX_QUOTE_TTL_MS


def test_pairs_first_fill_per_cid_and_leaves_unfilled_none():
    claims = [
        ClaimEvent(cid=1, claimed_at_ms=1_000),
        ClaimEvent(cid=2, claimed_at_ms=2_000),
    ]
    fills = [
        FillEvent(cid=1, filled_at_ms=61_000),
        FillEvent(cid=1, filled_at_ms=99_000),  # second partial fill — ignored
    ]
    outcomes = pair_claims_to_fills(claims, fills)
    by_cid = {o.cid: o for o in outcomes}
    assert by_cid[1].latency_ms == 60_000
    assert by_cid[2].latency_ms is None


def test_pairs_clamps_negative_latency_to_zero():
    """Immediate fill (venue mts_update before local-clock claim) yields 0, not negative."""
    claims = [
        ClaimEvent(cid=1, claimed_at_ms=10_000),
    ]
    fills = [
        FillEvent(cid=1, filled_at_ms=9_000),  # "before" claim due to clock skew
    ]
    outcomes = pair_claims_to_fills(claims, fills)
    assert outcomes[0].latency_ms == 0


def test_bucket_by_regime_assigns_claims_to_latest_boot_before_them():
    outcomes = [
        ClaimOutcome(cid=1, claimed_at_ms=5_000, latency_ms=100),
        ClaimOutcome(cid=2, claimed_at_ms=15_000, latency_ms=200),
        ClaimOutcome(cid=3, claimed_at_ms=500, latency_ms=None),  # before any regime
    ]
    buckets = bucket_by_regime(outcomes, regime_starts=[1_000, 10_000])
    assert [o.cid for o in buckets[1_000]] == [1]
    assert [o.cid for o in buckets[10_000]] == [2]
    assert all(o.cid != 3 for b in buckets.values() for o in b)


def test_summarize_reports_fill_rate_and_percentiles():
    outcomes = [
        ClaimOutcome(cid=i, claimed_at_ms=1_000, latency_ms=lat)
        for i, lat in enumerate([10_000, 20_000, 30_000, None])
    ]
    [s] = summarize({1_000: outcomes}, ttl_ms=TTL_MS)
    assert s.regime_start_ms == 1_000
    assert s.n_claims == 4
    assert s.n_filled == 3
    assert s.fill_rate == Decimal("0.75")
    assert s.p50_latency_ms == 20_000
    assert s.p90_latency_ms == 30_000
    assert s.n_unfilled_past_ttl == 1
