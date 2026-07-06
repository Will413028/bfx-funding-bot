from bfx_funding_bot.external.bitfinex.rest import FundingTicker
from bfx_funding_bot.modules.execution.deployment.book_clamp import (
    TICK,
    ClampBranch,
    ClampPolicy,
    clamp_policy_from_env,
    clamp_rate,
)

_POLICY = ClampPolicy(enabled=True, max_down_pct=0.15, taker_max_period_days=7)


def _ticker(
    *, bid: float = 0.00018, bid_period: int = 2, bid_size: float = 1_000.0,
    ask: float = 0.00021,
) -> FundingTicker:
    return FundingTicker(
        symbol="fUST", frr=0.0002, bid=bid, bid_period=bid_period,
        bid_size=bid_size, ask=ask, ask_period=2, ask_size=5_000.0,
    )


def test_no_ticker_falls_back():
    got = clamp_rate(quote_rate=0.0002, amount=200.0, ticker=None, policy=_POLICY)
    assert (got.rate, got.branch) == (0.0002, ClampBranch.FALLBACK)


def test_degenerate_book_falls_back():
    for t in (_ticker(bid=0.0), _ticker(ask=TICK)):
        got = clamp_rate(quote_rate=0.0002, amount=200.0, ticker=t, policy=_POLICY)
        assert got.branch is ClampBranch.FALLBACK
        assert got.rate == 0.0002


def test_taker_when_bid_at_or_above_quote():
    # bid 0.00018 ≥ quote 0.00015 → 吃單保證成交，rate 維持 quote（fill 繼承
    # bid 的 rate/period，實得 ≥ quote = signal floor 不破）
    got = clamp_rate(quote_rate=0.00015, amount=200.0, ticker=_ticker(), policy=_POLICY)
    assert (got.rate, got.branch) == (0.00015, ClampBranch.TAKER)


def test_taker_requires_full_bid_size():
    # bid_size 100 < amount 200 → partial fill 殘量會以低價 rest → 不吃，走 maker
    got = clamp_rate(
        quote_rate=0.00015, amount=200.0, ticker=_ticker(bid_size=100.0), policy=_POLICY,
    )
    assert got.branch is ClampBranch.RAISE


def test_taker_requires_short_bid_period():
    # bid_period 30 > 7 → 吃單會鎖 30 天倉 → 不吃，走 maker
    got = clamp_rate(
        quote_rate=0.00015, amount=200.0, ticker=_ticker(bid_period=30), policy=_POLICY,
    )
    assert got.branch is ClampBranch.RAISE


def test_raise_lifts_stale_quote_to_book_front():
    # bid 0.00018 < quote 0.00019 < ask−tick → 掛 ask−tick 搶隊首，不賤賣 spread
    got = clamp_rate(quote_rate=0.00019, amount=200.0, ticker=_ticker(), policy=_POLICY)
    assert (got.rate, got.branch) == (0.00020999, ClampBranch.RAISE)


def test_undercut_when_queued_behind():
    # quote 0.00023 > ask−tick 0.00020999，且下移 < 15% → 降到隊首防排隊
    got = clamp_rate(quote_rate=0.00023, amount=200.0, ticker=_ticker(), policy=_POLICY)
    assert (got.rate, got.branch) == (0.00020999, ClampBranch.UNDERCUT)


def test_floor_stops_deep_down_clamp():
    # 競爭價 0.00020999 < quote 0.0005 × 0.85 → 不追砍，維持 quote（regime
    # 下移交給下一個 1h boundary 的 signal 層裁決）
    got = clamp_rate(quote_rate=0.0005, amount=200.0, ticker=_ticker(), policy=_POLICY)
    assert (got.rate, got.branch) == (0.0005, ClampBranch.FLOOR)


def test_floor_boundary_exact_is_undercut():
    # 邊界：競爭價恰等於 quote×(1−max_down) → 不觸 floor（嚴格小於才觸）。
    # max_down=0.5、quote=0.0004 → 界 = 0.0002（×0.5 是 exact halving，float 安全）；
    # ask = 0.0002 + TICK → 競爭價 round 後恰為 0.0002。
    policy = ClampPolicy(enabled=True, max_down_pct=0.5, taker_max_period_days=7)
    got = clamp_rate(
        quote_rate=0.0004, amount=200.0,
        ticker=_ticker(bid=0.0001, ask=0.00020001), policy=policy,
    )
    assert (got.rate, got.branch) == (0.0002, ClampBranch.UNDERCUT)


def test_competitive_rate_rounds_float_dust():
    # 0.0002 − 1e-8 的浮點尾差要被 round 掉，輸出正好在 1e-8 grid 上。
    # quote 取 0.00021：下移 <15% 不觸 FLOOR（0.00021×0.85=0.0001785 < 0.00019999）
    # → 走 UNDERCUT。（勿用更高的 quote — 會觸 FLOOR 回原價，測不到 rounding。）
    got = clamp_rate(
        quote_rate=0.00021, amount=200.0,
        ticker=_ticker(bid=0.0001, ask=0.0002), policy=_POLICY,
    )
    assert (got.rate, got.branch) == (0.00019999, ClampBranch.UNDERCUT)


def test_clamp_policy_from_env_defaults():
    p = clamp_policy_from_env({})
    assert p == ClampPolicy(enabled=False, max_down_pct=0.15, taker_max_period_days=7)


def test_clamp_policy_from_env_enabled_variants():
    for truthy in ("1", "true", "TRUE", "yes"):
        assert clamp_policy_from_env({"BFX_CLAMP_ENABLED": truthy}).enabled is True
    assert clamp_policy_from_env({"BFX_CLAMP_ENABLED": "false"}).enabled is False


def test_clamp_policy_from_env_overrides():
    p = clamp_policy_from_env({
        "BFX_CLAMP_MAX_DOWN_PCT": "0.30",
        "BFX_CLAMP_TAKER_MAX_PERIOD_D": "2",
    })
    assert p.max_down_pct == 0.30
    assert p.taker_max_period_days == 2
