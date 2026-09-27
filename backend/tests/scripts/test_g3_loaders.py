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
from bfx_funding_bot.modules.execution.event_store.tables import (
    EventLogRow,
    PositionStateRow,
)
from bfx_funding_bot.modules.live_validation.live_attribution import (
    CreditCloseRecord,
    FillRecord,
    MarketRatePoint,
    VerdictState,
)
from scripts._g3_loaders import (
    _candles_to_market_rate_points,
    _compute_verdict,
    _load_credit_history_closes,
    _load_observed_realized,
    build_verdict_from_neon,
    merge_credit_closes,
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


# ---------------------------------------------------------------------------
# position_state → observed_realized (regression: per-symbol rename e7b20dc)
# ---------------------------------------------------------------------------


def _position_row(symbol: str, realized: str) -> PositionStateRow:
    return PositionStateRow(
        account_id="default",
        deployment_environment="prod",
        symbol=symbol,
        reserved=Decimal("0"),
        realized=Decimal(realized),
        last_updated_ms=1,
        last_event_seq=0,
    )


@pytest.mark.asyncio
async def test_load_observed_realized_filters_symbol(g3_factory):
    """position_state is per-symbol (composite PK since e7b20dc): the loader
    must read the measured cell's `realized`, not whichever row .first()
    happens to return. fUSD is seeded FIRST so an unfiltered query would
    pick it and this test would fail."""
    async with g3_factory() as s:
        s.add(_position_row("fUSD", "999"))
        s.add(_position_row("fUST", "42.5"))
        await s.commit()

    async with g3_factory() as s:
        got = await _load_observed_realized(
            s, account_id="default", deployment_env="prod", symbol="fUST"
        )
    assert got == Decimal("42.5")


@pytest.mark.asyncio
async def test_load_observed_realized_missing_row_is_zero(g3_factory):
    async with g3_factory() as s:
        got = await _load_observed_realized(
            s, account_id="default", deployment_env="prod", symbol="fUST"
        )
    assert got == Decimal("0")


@pytest.mark.asyncio
async def test_build_verdict_survives_position_state_row(g3_factory):
    """2026-07-13 prod crash regression: with a position_state row present the
    loader read the stale pre-rename attribute (realized_usdt) and the whole
    weekly chain died on AttributeError. A seeded row must flow through."""
    now_ms = int(datetime.now(UTC).timestamp() * 1000)
    base = now_ms - 5 * 24 * 3600 * 1000
    hour = 3600 * 1000
    candles = [_recent_candle("fUST", "1h", "p2", base + i * hour, "0.0002") for i in range(12)]
    async with g3_factory() as s:
        await upsert_candles(s, candles)
        s.add(_position_row("fUST", "12.34"))
        await s.commit()

    verdict, _window, n_fills, _clamp, _frr = await build_verdict_from_neon(
        capital=C, session_factory=g3_factory
    )
    # 0 fills + nonzero observed realized = legit anchor divergence → UNRELIABLE;
    # the seeded value surfacing in the reason proves it flowed through the query.
    assert verdict.state is VerdictState.UNRELIABLE
    assert any("12.34" in r for r in verdict.reasons)
    assert n_fills == 0


@pytest.mark.asyncio
async def test_build_verdict_credit_close_resolves_anchor_divergence(g3_factory):
    """End-to-end regression for the 2026-07-19 divergence: borrower returned
    1338.03 early, bot re-lent it 40 min later. Without CREDIT_CLOSED the
    anchor sees 2×1338.03 attributed vs 1338.03 observed → diverged; with the
    close event joined, attributed == observed and the reason disappears."""
    now_ms = int(datetime.now(UTC).timestamp() * 1000)
    hour = 3600 * 1000
    candles = [
        _recent_candle("fUST", "1h", "p2", now_ms - 12 * hour + i * hour, "0.0002")
        for i in range(12)
    ]
    t_fill1 = now_ms - 2 * hour
    t_fill2 = now_ms - 1 * hour
    amount = "1338.03"

    def _fill_row(ts: int, voi: str) -> EventLogRow:
        return EventLogRow(
            account_id="default", deployment_environment="prod",
            event_type="ORDER_FILL", venue_offer_id=voi,
            payload={"symbol": "fUST", "size_usdt": amount, "fill_rate": 0.0002,
                     "venue_offer_id": voi,
                     "signal_correlation_id": "11111111-1111-1111-1111-111111111111"},
            occurred_at_ms=ts,
        )

    close_row = EventLogRow(
        account_id="default", deployment_environment="prod",
        event_type="CREDIT_CLOSED",
        payload={"symbol": "fUST", "credit_id": 555, "amount": amount,
                 "mts_create": t_fill1 + 2_000},
        occurred_at_ms=t_fill2 - 300_000,  # returned 5 min before the re-lend
    )
    async with g3_factory() as s:
        await upsert_candles(s, candles)
        s.add(_fill_row(t_fill1, "voi-1"))
        s.add(_fill_row(t_fill2, "voi-2"))
        s.add(close_row)
        s.add(_position_row("fUST", amount))  # venue truth: ONE credit open
        await s.commit()

    verdict, _window, n_fills, _clamp, _frr = await build_verdict_from_neon(
        capital=C, session_factory=g3_factory
    )
    assert n_fills == 2
    assert not any("deployment anchor diverged" in r for r in verdict.reasons)


# Credit 466642177 (live, 2026-09-25): repaid after 842 s. Its pre-fix
# CREDIT_CLOSED event carried occurred_at_ms == mts_create (0 days held).
_CREATED = 1790350246000
_REPAID = 1790351088000
_STALE_EVENT = ({"symbol": "fUST", "credit_id": 466642177, "amount": "150.76884612",
                 "mts_create": _CREATED}, _CREATED)


def test_credit_history_close_wins_over_a_stale_credit_closed_event():
    history = [CreditCloseRecord(466642177, Decimal("150.76884612"), _CREATED, _REPAID)]
    assert merge_credit_closes(history, [_STALE_EVENT], symbol="fUST") == history


def test_unsynced_event_uses_its_mts_last_payout_else_its_time():
    fixed = ({**_STALE_EVENT[0], "credit_id": 1, "mts_last_payout": _REPAID}, _CREATED)
    closes = merge_credit_closes([], [fixed, _STALE_EVENT], symbol="fUST")
    assert [(c.credit_id, c.close_ts_ms) for c in closes] == [(1, _REPAID), (466642177, _CREATED)]
    assert merge_credit_closes([], [_STALE_EVENT], symbol="fUSD") == []


@pytest.mark.asyncio
async def test_load_credit_history_closes_reads_last_payout(g3_factory):
    from uuid import UUID

    from bfx_funding_bot.modules.live_validation.tables import FundingCreditHistoryRow

    account = UUID("aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee")
    async with g3_factory() as s:
        for kind in ("credit", "loan"):
            s.add(FundingCreditHistoryRow(
                exchange_account_id=account, kind=kind, credit_id=466642177,
                deployment_environment="prod", symbol="fUST", side=1, mts_create=_CREATED,
                mts_update=_CREATED, amount=Decimal("150.76884612"), status="CLOSED",
                rate=Decimal("0.00019999"), period_days=2, mts_opening=_CREATED,
                mts_last_payout=_REPAID,
            ))
        await s.commit()
    async with g3_factory() as s:
        closes = await _load_credit_history_closes(
            s, account_id=str(account), deployment_env="prod", symbol="fUST")
        legacy = await _load_credit_history_closes(
            s, account_id="default", deployment_env="prod", symbol="fUST")
    assert closes == [CreditCloseRecord(466642177, Decimal("150.76884612"), _CREATED, _REPAID)]
    assert legacy == []
