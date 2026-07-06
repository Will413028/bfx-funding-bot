"""Unit tests for the pure helpers in scripts._g3_loaders.

The DB-backed build_verdict_from_neon path is covered by
test_g3_loaders_integration.py (marked integration, skipped by the commit gate).
"""
from datetime import UTC, datetime
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from bfx_funding_bot.core.db import Base
from bfx_funding_bot.modules.candles.repository import upsert_candles
from bfx_funding_bot.modules.candles.schemas import FundingCandle
from bfx_funding_bot.modules.live_validation.live_attribution import (
    FillRecord,
    MarketRatePoint,
    VerdictState,
)
from scripts._g3_loaders import (
    _candles_to_market_rate_points,
    _compute_verdict,
    build_verdict_from_neon,
)

C = Decimal("570")


def _candle(mts: int, close: Decimal | None) -> FundingCandle:
    return FundingCandle(
        symbol="fUST", timeframe="1h", period_agg="p2", mts=mts, close=close
    )


def _fill(ts: int, size: str, rate: str) -> FillRecord:
    return FillRecord(
        venue_offer_id=str(ts),
        fill_ts_ms=ts,
        size_usdt=Decimal(size),
        rate=Decimal(rate),
        period_days=Decimal("2"),
        release_ts_ms=None,
    )


def test_candles_to_market_rate_points_maps_close_to_rate():
    candles = [_candle(1000, Decimal("0.0002")), _candle(2000, Decimal("0.0003"))]
    assert _candles_to_market_rate_points(candles) == [
        MarketRatePoint(mts=1000, rate=Decimal("0.0002")),
        MarketRatePoint(mts=2000, rate=Decimal("0.0003")),
    ]


def test_candles_to_market_rate_points_skips_none_close():
    candles = [_candle(1000, None), _candle(2000, Decimal("0.0003"))]
    assert _candles_to_market_rate_points(candles) == [
        MarketRatePoint(mts=2000, rate=Decimal("0.0003"))
    ]


def test_candles_to_market_rate_points_empty():
    assert _candles_to_market_rate_points([]) == []


# ---------------------------------------------------------------------------
# _compute_verdict: pure verdict core (band/coverage guards decouple from primary).
# Band-violation now only marks MR-alpha unavailable; the primary bot-vs-idle
# CI (idle ≡ 0) is unaffected by passive-arm data quality.
# ---------------------------------------------------------------------------


def test_compute_verdict_frr_scale_points_mark_mr_alpha_unavailable_not_unreliable():
    # frr-scale (~1e-6) passive series is the wrong source for the MR-alpha
    # diagnostic, but bot-vs-idle (idle ≡ 0) needs no market-rate data. The band
    # guard now ONLY marks MR-alpha unavailable + leaves a caveat; the primary
    # verdict stays data-driven (1 window → INSUFFICIENT_DATA), never UNRELIABLE.
    pts = [MarketRatePoint(mts=1000 + i, rate=Decimal("1.1e-06")) for i in range(5)]
    fills = [_fill(1000, "570", "0.0003")]
    verdict, _window, n_fills, _clamp, _frr = _compute_verdict(
        fills=fills, market_rate_points=pts, observed_realized=Decimal("570"), capital=C
    )
    assert verdict.state is VerdictState.INSUFFICIENT_DATA
    assert verdict.mr_alpha_available is False
    assert any("plausible per-day band" in r for r in verdict.reasons)
    assert n_fills == 1


def test_compute_verdict_legit_points_mr_alpha_available():
    # Realistic candle-close baseline → no band override; near-idle single
    # window stays data-driven (INSUFFICIENT_DATA), never UNRELIABLE-by-band.
    pts = [MarketRatePoint(mts=1000 + i, rate=Decimal("0.0002")) for i in range(5)]
    fills = [_fill(1000, "570", "0.0003")]
    verdict, _window, n_fills, _clamp, _frr = _compute_verdict(
        fills=fills, market_rate_points=pts, observed_realized=Decimal("570"), capital=C
    )
    assert not any("plausible per-day band" in r for r in verdict.reasons)
    assert verdict.mr_alpha_available is True
    assert verdict.state is VerdictState.INSUFFICIENT_DATA
    assert n_fills == 1


def test_compute_verdict_headline_is_absolute_active_return():
    # One full-budget 2-day fill at rate 3e-4, cap 570: bot-vs-idle headline =
    # active net_monthly = 570*3e-4*2 / 570 * 100 = 0.06 (idle subtracts 0).
    pts = [MarketRatePoint(mts=1000 + i, rate=Decimal("0.0002")) for i in range(5)]
    fills = [_fill(1000, "570", "0.0003")]
    verdict, _window, _n, _clamp, _frr = _compute_verdict(
        fills=fills, market_rate_points=pts, observed_realized=Decimal("570"), capital=C
    )
    assert verdict.headline_bot_vs_idle == Decimal("570") * Decimal("0.0003") * Decimal("2") / C * Decimal("100")


def test_compute_verdict_no_coverage_marks_mr_alpha_unavailable_primary_unblocked():
    # Fills present and spanning >=2 weekly windows but ZERO market-rate points:
    # this exercises the `fills and bounds` TRUE path (active arm IS computed) with
    # missing MR coverage. The PRIMARY bot-vs-idle path must run unblocked
    # (headline = absolute active return > 0, proving we did NOT fall through to the
    # zero-data else branch), while the MR-alpha diagnostic is marked unavailable
    # with a coverage caveat — NOT a band reason (band guard no-ops on empty list).
    # Two fills a week apart make weekly_window_bounds non-empty (2 windows < 8 →
    # INSUFFICIENT). observed_realized == open_principal_at(max_ts) == 285 (only the
    # second fill is still open at max_ts) keeps the deployment anchor clean.
    week_ms = 7 * 24 * 3600 * 1000
    fills = [_fill(0, "285", "0.0003"), _fill(week_ms + 1000, "285", "0.0003")]
    verdict, _window, _n, _clamp, _frr = _compute_verdict(
        fills=fills, market_rate_points=[], observed_realized=Decimal("285"), capital=C
    )
    assert verdict.mr_alpha_available is False
    assert verdict.state is VerdictState.INSUFFICIENT_DATA
    assert verdict.headline_bot_vs_idle > Decimal("0")  # fills-and-bounds path ran
    assert any("no market-rate coverage" in r for r in verdict.reasons)
    assert not any("plausible per-day band" in r for r in verdict.reasons)


def test_compute_verdict_empty_is_insufficient_no_crash():
    verdict, window, n_fills, _clamp, _frr = _compute_verdict(
        fills=[], market_rate_points=[], observed_realized=Decimal("0"), capital=C
    )
    assert verdict.state is VerdictState.INSUFFICIENT_DATA
    assert n_fills == 0
    assert window == "n/a"
    # empty-fills else path must still return a defined FrrBenchmark (no UnboundLocalError).
    assert _frr.available is False


def test_compute_verdict_capital_days_is_usdt_days_not_divided():
    # _compute_verdict feeds total_capital_days (USDT·days, NOT the old /capital
    # 'days') to decide_verdict but does not return it; the exact value
    # (570*2 = 1140 USDT·days) is pinned at the primitive level by
    # test_clamp_single_fill_equals_legacy_formula. Here we pin the loader-level
    # diagnostic: one full-budget fill peaks at the cap with no over-deploy.
    pts = [MarketRatePoint(mts=1000 + i, rate=Decimal("0.0002")) for i in range(5)]
    fills = [_fill(1000, "570", "0.0003")]
    _verdict, _window, _n, clamp, _frr = _compute_verdict(
        fills=fills, market_rate_points=pts, observed_realized=Decimal("570"), capital=C
    )
    assert clamp.peak_concurrent == Decimal("570")
    assert clamp.over_deployed is False


def test_compute_verdict_over_deploy_populates_diagnostic():
    # 3 fills 300 each = 900 concurrent > 570 → over-deploy diagnostic set.
    pts = [MarketRatePoint(mts=1000 + i, rate=Decimal("0.0002")) for i in range(5)]
    fills = [_fill(1000, "300", "0.0003") for _ in range(3)]
    _verdict, _window, _n, clamp, _frr = _compute_verdict(
        fills=fills, market_rate_points=pts, observed_realized=Decimal("900"), capital=C
    )
    assert clamp.peak_concurrent == Decimal("900")
    assert clamp.over_deployed is True
    assert clamp.raw_interest > clamp.clamped_interest
    assert clamp.cap == C


# ---------------------------------------------------------------------------
# build_verdict_from_neon wiring (seeded in-memory sqlite, session-injected).
# Pins the passive-arm candle query (symbol/timeframe/period_agg + bounds) and
# the band-guard-in-loader path against silent regressions the commit gate
# would otherwise miss.
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def g3_factory(sqlite_engine: AsyncEngine) -> async_sessionmaker:
    async with sqlite_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return async_sessionmaker(sqlite_engine, expire_on_commit=False)


def _recent_candle(
    symbol: str, timeframe: str, period_agg: str, mts: int, close: str
) -> FundingCandle:
    return FundingCandle(
        symbol=symbol, timeframe=timeframe, period_agg=period_agg, mts=mts, close=Decimal(close)
    )


@pytest.mark.asyncio
async def test_build_verdict_queries_only_fust_p2_1h_cell(g3_factory):
    """The passive arm must read ONLY fUST/p2/1h candles; decoys with another
    symbol / period_agg / timeframe are excluded. Proven via the band guard: the
    target cell is seeded frr-scale (~1e-6) while every decoy is legit (2e-4), so
    a correct query trips the band → UNRELIABLE; a leaky query would not."""
    now_ms = int(datetime.now(UTC).timestamp() * 1000)
    base = now_ms - 5 * 24 * 3600 * 1000  # within the 30-day no-fills fallback window
    hour = 3600 * 1000
    target = [_recent_candle("fUST", "1h", "p2", base + i * hour, "1.1e-6") for i in range(12)]
    decoys = (
        [_recent_candle("fUST", "1h", "a30", base + i * hour, "0.0002") for i in range(12)]
        + [_recent_candle("fUSD", "1h", "p2", base + i * hour, "0.0002") for i in range(12)]
        + [_recent_candle("fUST", "15m", "p2", base + i * hour, "0.0002") for i in range(12)]
    )
    async with g3_factory() as s:
        await upsert_candles(s, target + decoys)
        await s.commit()

    verdict, _window, n_fills, _clamp, _frr = await build_verdict_from_neon(
        capital=C, session_factory=g3_factory
    )
    # A correct fUST/p2/1h query reads the frr-scale target → band caveat fires
    # (proving cell targeting). Band no longer forces UNRELIABLE: idle canary
    # (0 fills) → INSUFFICIENT_DATA with mr_alpha unavailable.
    assert verdict.state is VerdictState.INSUFFICIENT_DATA
    assert verdict.mr_alpha_available is False
    assert any("plausible per-day band" in r for r in verdict.reasons)
    assert n_fills == 0
    # No funding_stats seeded → AlwaysFRR arm unavailable (not a crash).
    assert _frr.available is False


@pytest.mark.asyncio
async def test_build_verdict_legit_idle_cell_is_insufficient_not_crash(g3_factory):
    """Legit fUST/p2/1h closes with no fills → INSUFFICIENT_DATA (idle canary):
    no band trip, no crash — the end-to-end happy path through the I/O shell."""
    now_ms = int(datetime.now(UTC).timestamp() * 1000)
    base = now_ms - 5 * 24 * 3600 * 1000
    hour = 3600 * 1000
    candles = [_recent_candle("fUST", "1h", "p2", base + i * hour, "0.0002") for i in range(12)]
    async with g3_factory() as s:
        await upsert_candles(s, candles)
        await s.commit()

    verdict, _window, n_fills, _clamp, _frr = await build_verdict_from_neon(
        capital=C, session_factory=g3_factory
    )
    assert verdict.state is VerdictState.INSUFFICIENT_DATA
    assert not any("plausible per-day band" in r for r in verdict.reasons)
    assert n_fills == 0
    assert verdict.mr_alpha_available is False
    # No funding_stats seeded → AlwaysFRR arm unavailable (not a crash).
    assert _frr.available is False
