"""Neon data loader for G3 live validation.

Queries event_log (fills + releases) and funding_candles (per-day market-rate
series, close) from Neon, builds FillRecord / MarketRatePoint lists, and delegates
all computation to the pure modules/live_validation/live_attribution module.

account_id  = BFX_ACCOUNT_ID env-var (defaults "default", same as daemon.py)
environment = BFX_DEPLOYMENT_ENV env-var (required; "prod" for the live canary)
"""
from __future__ import annotations

import os
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bfx_funding_bot.core.db import make_engine, make_session_factory, session_scope
from bfx_funding_bot.core.settings import Settings
from bfx_funding_bot.modules.backtest.oos_profitability import bootstrap_ci, paired_active_returns
from bfx_funding_bot.modules.candles.repository import get_candles_in_range
from bfx_funding_bot.modules.candles.schemas import FundingCandle
from bfx_funding_bot.modules.execution.event_store.tables import (
    EventLogRow,
    PositionStateRow,
)
from bfx_funding_bot.modules.live_validation.live_attribution import (
    ClampDiagnostic,
    FillRecord,
    G3Verdict,
    MarketRatePoint,
    VerdictState,
    _fill_duration_days,
    assert_market_rate_band,
    attribute_active,
    attribute_passive,
    cell_period_days,
    check_deployment_anchor,
    check_nav_anchor,
    clamp_active_window,
    decide_verdict,
    open_principal_at,
    weekly_window_bounds,
)

# Event type constants — must match serialization._TYPE_BY_CLASS (UPPERCASE).
_FILL_TYPE = "ORDER_FILL"
_RELEASE_TYPE = "RESERVATION_RELEASED"

# Thresholds for decide_verdict (spec: min_windows=8, min_capital_days=capital*7)
_MIN_WINDOWS = 8
_DEPLOY_TOL = Decimal("0.05")
_NAV_TOL = Decimal("0.1")

# How far back to look for market-rate data when there are no fills at all.
_MARKET_RATE_FALLBACK_DAYS = 30
_MARKET_RATE_FALLBACK_MS = _MARKET_RATE_FALLBACK_DAYS * 24 * 60 * 60 * 1000

# Passive-arm market-rate cell: the canary's 2-day-offer cell. funding_candles is
# shared market data (no deployment_environment/account_id), so it is queried
# realm-agnostic — no realm filter, matching the backtest AlwaysMarketRateStrategy.
_MARKET_SYMBOL = "fUST"
_MARKET_TIMEFRAME = "1h"
_MARKET_PERIOD_AGG = "p2"


def _candles_to_market_rate_points(candles: list[FundingCandle]) -> list[MarketRatePoint]:
    """Passive-arm points from funding candles; skip candles with a null close."""
    return [
        MarketRatePoint(mts=c.mts, rate=c.close)
        for c in candles
        if c.close is not None
    ]


async def build_verdict_from_neon(
    *,
    capital: Decimal,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
) -> tuple[G3Verdict, str, int, ClampDiagnostic]:
    """Query Neon and run G3 attribution.  Returns (verdict, data_window_str, n_fills, clamp_diag).

    I/O shell only: fetches fills/releases/candles/position_state, builds the
    FillRecord + MarketRatePoint domain lists, then delegates to the pure
    _compute_verdict. Pass `session_factory` to run against an injected DB
    (used by the seeded unit test); otherwise a Neon engine is built from env.
    """
    account_id = os.environ.get("BFX_ACCOUNT_ID", "default")
    deployment_env = os.environ.get("BFX_DEPLOYMENT_ENV", "prod")

    engine = None
    if session_factory is None:
        settings = Settings()
        engine = make_engine(settings)
        session_factory = make_session_factory(engine)

    try:
        async with session_scope(session_factory) as session:
            # ── 1. Fetch ORDER_FILL rows ──────────────────────────────────────
            fill_stmt = (
                select(EventLogRow)
                .where(
                    EventLogRow.account_id == account_id,
                    EventLogRow.deployment_environment == deployment_env,
                    EventLogRow.event_type == _FILL_TYPE,
                )
                .order_by(EventLogRow.occurred_at_ms.asc())
            )
            fill_rows = (await session.execute(fill_stmt)).scalars().all()

            # ── 2. Fetch RESERVATION_RELEASED rows ───────────────────────────
            release_stmt = (
                select(EventLogRow)
                .where(
                    EventLogRow.account_id == account_id,
                    EventLogRow.deployment_environment == deployment_env,
                    EventLogRow.event_type == _RELEASE_TYPE,
                )
            )
            release_rows = (await session.execute(release_stmt)).scalars().all()

            # Build venue_offer_id -> release_ts_ms map (take latest if duplicates)
            release_map: dict[str, int] = {}
            for r in release_rows:
                voi = r.venue_offer_id
                if voi is not None:
                    existing = release_map.get(voi)
                    if existing is None or r.occurred_at_ms > existing:
                        release_map[voi] = r.occurred_at_ms

            # ── 3. Fetch market-rate series (funding_candles.close) ───────────
            now_ms = int(datetime.now(UTC).timestamp() * 1000)
            if fill_rows:
                rate_start_ms = fill_rows[0].occurred_at_ms
                rate_end_ms = now_ms
            else:
                rate_start_ms = now_ms - _MARKET_RATE_FALLBACK_MS
                rate_end_ms = now_ms

            candles = await get_candles_in_range(
                session,
                symbol=_MARKET_SYMBOL,
                timeframe=_MARKET_TIMEFRAME,
                period_agg=_MARKET_PERIOD_AGG,
                start_mts=rate_start_ms,
                end_mts=rate_end_ms,
            )

            # ── 4. Fetch observed_realized from position_state snapshot ───────
            pos_stmt = (
                select(PositionStateRow)
                .where(
                    PositionStateRow.account_id == account_id,
                    PositionStateRow.deployment_environment == deployment_env,
                )
                .limit(1)
            )
            pos_row = (await session.execute(pos_stmt)).scalars().first()
            observed_realized = (
                Decimal(str(pos_row.realized_usdt)) if pos_row is not None else Decimal("0")
            )

            # ── 5. Build domain lists (frozen dataclasses, session-detached) ──
            market_rate_points = _candles_to_market_rate_points(candles)
            # Fills carry no cell identity, so the spec mandates the conservative
            # p2 path: held-to-term = 2 days (cell_period_days("p2", …) ignores
            # its second arg).
            conservative_period = cell_period_days("p2", Decimal("2"))
            fills: list[FillRecord] = []
            for row in fill_rows:
                payload = row.payload
                size_usdt = Decimal(str(payload.get("size_usdt", "0")))
                # fill_rate is stored as float in OrderFilled; Decimal(str(float))
                # avoids scientific-notation issues (live executor fix ae2c59d).
                fill_rate = Decimal(str(payload.get("fill_rate", "0")))
                venue_offer_id = str(
                    payload.get("venue_offer_id") or row.venue_offer_id or ""
                )
                fills.append(
                    FillRecord(
                        venue_offer_id=venue_offer_id,
                        fill_ts_ms=row.occurred_at_ms,
                        size_usdt=size_usdt,
                        rate=fill_rate,
                        period_days=conservative_period,
                        release_ts_ms=release_map.get(venue_offer_id),
                    )
                )
    finally:
        if engine is not None:
            await engine.dispose()

    return _compute_verdict(
        fills=fills,
        market_rate_points=market_rate_points,
        observed_realized=observed_realized,
        capital=capital,
    )


def _compute_verdict(
    *,
    fills: list[FillRecord],
    market_rate_points: list[MarketRatePoint],
    observed_realized: Decimal,
    capital: Decimal,
) -> tuple[G3Verdict, str, int, ClampDiagnostic]:
    """Pure G3 verdict over already-built domain lists. No I/O.

    Returns (verdict, data_window_str, n_fills, clamp_diag).
    """
    # Band guard: a wrong-scale passive series (funding_stats.frr ~1e-6, or a
    # percentage-scaled rate) makes the active spread meaningless. Capture the
    # violation and degrade to UNRELIABLE below rather than crashing the report.
    band_reason: str | None = None
    try:
        assert_market_rate_band([p.rate for p in market_rate_points])
    except ValueError as exc:
        band_reason = str(exc)

    n_fills = len(fills)

    # ── Compute windows + attribution ────────────────────────────────────────
    if fills or market_rate_points:
        all_mts = (
            [f.fill_ts_ms for f in fills]
            + [p.mts for p in market_rate_points]
        )
        min_ts = min(all_mts)
        max_ts = max(all_mts)
    else:
        # Truly empty — no data at all; produce a zero-data verdict.
        now_ms = int(datetime.now(UTC).timestamp() * 1000)
        min_ts = now_ms
        max_ts = now_ms

    bounds = weekly_window_bounds(min_ts, max_ts)

    if fills and bounds:
        strat_outcomes = attribute_active(fills, capital=capital, window_bounds=bounds)
        base_outcomes = attribute_passive(market_rate_points, window_bounds=bounds)
        actives = paired_active_returns(strat_outcomes, base_outcomes)
        if len(actives) >= 2:
            mean_fn = lambda xs: sum(xs, Decimal("0")) / Decimal(len(xs))  # noqa: E731
            ci_lo, ci_hi = bootstrap_ci(actives, mean_fn)
        else:
            ci_lo, ci_hi = Decimal("0"), Decimal("0")

        # Headline: single-window attribution over full span
        single_strat = attribute_active(fills, capital=capital, window_bounds=[(min_ts, max_ts)])
        single_base = attribute_passive(market_rate_points, window_bounds=[(min_ts, max_ts)])
        headline_active_spread = single_strat[0].net_monthly - single_base[0].net_monthly

        # Budget-clamped capital-days (USDT·days) and over-deploy diagnostic come
        # from one full-span sweep. total_capital_days is USDT·days to match
        # decide_verdict's contract (min_capital_days = capital*7); the old
        # `/ capital` made it 'days' and mismatched the threshold.
        full_clamp = clamp_active_window(fills, cap=capital)
        total_capital_days = full_clamp.capital_days
        clamp_diag = ClampDiagnostic(
            cap=capital,
            peak_concurrent=full_clamp.peak_concurrent,
            raw_interest=full_clamp.raw_interest,
            clamped_interest=full_clamp.interest,
        )

        # attributed_deployed = open principal at the end of the data window.
        # Uses point-in-time snapshot (credits still open at max_ts) so it is
        # directly comparable to observed_realized from position_state, which is
        # also a point-in-time snapshot — not a time-average. The old time-averaged
        # formula Σsize·duration / window-span ≈ 776 diverged from the 550 snapshot
        # when fills were uniformly spread over the window.
        attributed_deployed = open_principal_at(fills, max_ts)

        # attributed_interest for nav anchor
        attributed_interest = sum(
            (f.size_usdt * f.rate * _fill_duration_days(f) for f in fills), Decimal("0")
        )

    else:
        # No fills (expected for an idle canary): make verdict data-driven.
        strat_outcomes = []
        base_outcomes = []
        ci_lo, ci_hi = Decimal("0"), Decimal("0")
        headline_active_spread = Decimal("0")
        total_capital_days = Decimal("0")
        # No attributed deployment when there are no fills. If observed_realized is
        # nonetheless > 0, event_log (the SoT) is missing fills it should contain —
        # let the anchor diverge to UNRELIABLE rather than fabricate agreement.
        attributed_deployed = Decimal("0")
        attributed_interest = Decimal("0")
        clamp_diag = ClampDiagnostic(
            cap=capital,
            peak_concurrent=Decimal("0"),
            raw_interest=Decimal("0"),
            clamped_interest=Decimal("0"),
        )

    n_windows = len(bounds)
    min_capital_days = capital * Decimal("7")

    deployment_anchor = check_deployment_anchor(
        attributed_deployed=attributed_deployed,
        observed_realized=observed_realized,
        tol=_DEPLOY_TOL,
    )
    nav_anchor = check_nav_anchor(
        nav_delta=None,  # NAV unavailable in v1
        attributed_interest=attributed_interest,
        tol=_NAV_TOL,
    )

    verdict = decide_verdict(
        headline_active_spread=headline_active_spread,
        n_windows=n_windows,
        total_capital_days=total_capital_days,
        ci_lo=ci_lo,
        ci_hi=ci_hi,
        deployment_anchor=deployment_anchor,
        nav_anchor=nav_anchor,
        min_windows=_MIN_WINDOWS,
        min_capital_days=min_capital_days,
    )

    # ── Market-rate coverage guard ────────────────────────────────────────────
    # When no market-rate points cover the data window the passive benchmark is
    # all-zero, making headline_active_spread equal to the active return rather
    # than a real spread against AlwaysMarketRate. Override to INSUFFICIENT_DATA
    # with an explicit reason so the caller is not misled by a spurious "active
    # spread" figure. headline_active_spread is preserved for diagnostic purposes.
    window_rate_points = (
        [p for p in market_rate_points if min_ts <= p.mts < max_ts]
        if (fills or market_rate_points)
        else []
    )
    if len(window_rate_points) == 0 and verdict.state is not VerdictState.UNRELIABLE:
        no_rate_reason = (
            "passive benchmark unavailable: no market-rate coverage in window — "
            "'active spread' reflects active return only"
        )
        verdict = G3Verdict(
            state=VerdictState.INSUFFICIENT_DATA,
            headline_active_spread=verdict.headline_active_spread,
            n_windows=verdict.n_windows,
            ci_lo=verdict.ci_lo,
            ci_hi=verdict.ci_hi,
            reasons=[no_rate_reason, *verdict.reasons],
        )

    # ── Band-violation override (top priority) ────────────────────────────────
    # A wrong-scale passive series ⇒ the entire active spread is untrustworthy.
    # Degrade to UNRELIABLE (same class as a diverged anchor) and surface the band
    # message so the operator fixes the attribution source rather than reading a
    # bogus spread — and crucially the report still renders instead of crashing.
    if band_reason is not None:
        verdict = G3Verdict(
            state=VerdictState.UNRELIABLE,
            headline_active_spread=verdict.headline_active_spread,
            n_windows=verdict.n_windows,
            ci_lo=verdict.ci_lo,
            ci_hi=verdict.ci_hi,
            reasons=[band_reason, *verdict.reasons],
        )

    # Human-readable data window
    if fills or market_rate_points:
        min_dt = datetime.fromtimestamp(min_ts / 1000, UTC).strftime("%Y-%m-%d")
        max_dt = datetime.fromtimestamp(max_ts / 1000, UTC).strftime("%Y-%m-%d")
        data_window = f"{min_dt}..{max_dt}"
    else:
        data_window = "n/a"

    return verdict, data_window, n_fills, clamp_diag
