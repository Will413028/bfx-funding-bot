"""Postgres data loader for G3 live validation.

Queries event_log (fills + releases) and funding_candles (per-day market-rate
series, close) from Postgres, builds FillRecord / MarketRatePoint lists, and
delegates all computation to the pure modules/live_validation/live_attribution
module.

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
from bfx_funding_bot.modules.funding_stats.schemas import FundingStat
from bfx_funding_bot.modules.funding_stats.tables import FundingStatRow
from bfx_funding_bot.modules.live_validation.live_attribution import (
    ClampDiagnostic,
    FillRecord,
    FrrBenchmark,
    G3Verdict,
    MarketRatePoint,
    _fill_duration_days,
    assert_market_rate_band,
    attribute_active,
    attribute_idle,
    attribute_passive,
    cell_period_days,
    check_deployment_anchor,
    check_nav_anchor,
    clamp_active_window,
    decide_verdict,
    frr_points_from_stats,
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


async def _load_observed_realized(
    session: AsyncSession,
    *,
    account_id: str,
    deployment_env: str,
    symbol: str,
) -> Decimal:
    """observed_realized for ONE symbol cell. position_state is per-symbol
    (composite PK account/env/symbol since the per-symbol refactor) — an
    unfiltered .first() would return an arbitrary symbol's row once fUSD
    coexists with fUST. Missing row → 0 (idle canary)."""
    stmt = (
        select(PositionStateRow)
        .where(
            PositionStateRow.account_id == account_id,
            PositionStateRow.deployment_environment == deployment_env,
            PositionStateRow.symbol == symbol,
        )
        .limit(1)
    )
    row = (await session.execute(stmt)).scalars().first()
    return Decimal(str(row.realized)) if row is not None else Decimal("0")


async def build_verdict_from_neon(
    *,
    capital: Decimal,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
) -> tuple[G3Verdict, str, int, ClampDiagnostic, FrrBenchmark]:
    """Query Postgres and run G3 attribution.  Returns (verdict, data_window_str, n_fills, clamp_diag, frr_bench).

    I/O shell only: fetches fills/releases/candles/position_state, builds the
    FillRecord + MarketRatePoint domain lists, then delegates to the pure
    _compute_verdict. Pass `session_factory` to run against an injected DB
    (used by the seeded unit test); otherwise an engine is built from env.
    Function name `build_verdict_from_neon` is a historical holdover from the
    pre-2026-06-23 Neon era; kept as-is (many call sites, rename is behavior-free
    churn).
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

            # ── 3b. Fetch funding_stats range (AlwaysFRR arm; same window) ────
            # funding_stats is shared market data (realm-agnostic), queried over
            # the SAME [rate_start_ms, rate_end_ms] window as the candle series.
            frr_rows = (
                await session.execute(
                    select(FundingStatRow)
                    .where(
                        FundingStatRow.symbol == _MARKET_SYMBOL,
                        FundingStatRow.mts >= rate_start_ms,
                        FundingStatRow.mts <= rate_end_ms,
                    )
                    .order_by(FundingStatRow.mts)
                )
            ).scalars().all()
            frr_stats = [
                FundingStat(
                    symbol=r.symbol, mts=r.mts,
                    frr=Decimal(str(r.frr)) if r.frr is not None else None,
                    avg_period=Decimal(str(r.avg_period)) if r.avg_period is not None else None,
                )
                for r in frr_rows
            ]

            # ── 4. Fetch observed_realized from position_state snapshot ───────
            observed_realized = await _load_observed_realized(
                session,
                account_id=account_id,
                deployment_env=deployment_env,
                symbol=_MARKET_SYMBOL,
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
        frr_points=frr_points_from_stats(frr_stats),
    )


def _compute_verdict(
    *,
    fills: list[FillRecord],
    market_rate_points: list[MarketRatePoint],
    observed_realized: Decimal,
    capital: Decimal,
    frr_points: list[MarketRatePoint] | None = None,
) -> tuple[G3Verdict, str, int, ClampDiagnostic, FrrBenchmark]:
    """Pure G3 verdict over already-built domain lists. No I/O.

    Returns (verdict, data_window_str, n_fills, clamp_diag, frr_bench).
    """
    frr_points = frr_points or []
    n_fills = len(fills)

    # ── Compute windows + attribution ────────────────────────────────────────
    if fills or market_rate_points:
        all_mts = [f.fill_ts_ms for f in fills] + [p.mts for p in market_rate_points]
        min_ts = min(all_mts)
        max_ts = max(all_mts)
    else:
        # Truly empty — no data at all; produce a zero-data verdict.
        now_ms = int(datetime.now(UTC).timestamp() * 1000)
        min_ts = now_ms
        max_ts = now_ms

    bounds = weekly_window_bounds(min_ts, max_ts)

    mean_fn = lambda xs: sum(xs, Decimal("0")) / Decimal(len(xs))  # noqa: E731

    # window-coverage of the passive arm decides the MR-alpha diagnostic only.
    window_rate_points = (
        [p for p in market_rate_points if min_ts <= p.mts < max_ts]
        if (fills or market_rate_points)
        else []
    )

    # AlwaysFRR benchmark default — MUST be defined before the fills/else split so
    # the empty-fills else path returns a full 5-tuple (no UnboundLocalError). The
    # fills branch overwrites it below when frr_points are present.
    frr_bench = FrrBenchmark(
        available=False, spread=Decimal("0"), ci_lo=Decimal("0"),
        ci_hi=Decimal("0"),
        reason="funding_stats empty — run backfill (E3 Task 8)",
    )

    if fills and bounds:
        strat_outcomes = attribute_active(fills, capital=capital, window_bounds=bounds)
        idle_outcomes = attribute_idle(window_bounds=bounds)
        base_outcomes = attribute_passive(market_rate_points, window_bounds=bounds)

        # Primary: bot-vs-idle = active − idle (idle ≡ 0) → absolute active return.
        # bootstrap_ci requires >= 2 samples; a single window cannot give a CI, so
        # zero it (→ straddles 0 → INSUFFICIENT_DATA, also gated by min_windows).
        bot_vs_idle = paired_active_returns(strat_outcomes, idle_outcomes)
        if len(bot_vs_idle) >= 2:
            ci_lo, ci_hi = bootstrap_ci(bot_vs_idle, mean_fn)
        else:
            ci_lo, ci_hi = Decimal("0"), Decimal("0")

        # Headline: single-window absolute active return over the full span.
        single_strat = attribute_active(fills, capital=capital, window_bounds=[(min_ts, max_ts)])
        single_base = attribute_passive(market_rate_points, window_bounds=[(min_ts, max_ts)])  # consumed by mr_alpha_spread below
        headline_bot_vs_idle = single_strat[0].net_monthly

        # Secondary diagnostic: MR alpha = active − AlwaysMarketRate.
        mr_actives = paired_active_returns(strat_outcomes, base_outcomes)
        if len(mr_actives) >= 2:
            mr_alpha_ci_lo, mr_alpha_ci_hi = bootstrap_ci(mr_actives, mean_fn)
        else:
            mr_alpha_ci_lo, mr_alpha_ci_hi = Decimal("0"), Decimal("0")
        mr_alpha_spread = single_strat[0].net_monthly - single_base[0].net_monthly
        # MR-alpha is trustworthy only with real market-rate coverage and an
        # in-band series. The band check is deferred to band_reason below.
        mr_alpha_available = len(window_rate_points) > 0

        # AlwaysFRR benchmark (policy bar; NOT fed into decide_verdict). Statistical
        # handling is isomorphic to the mr_alpha block above: paired_active_returns
        # already returns a per-window diff list (not a tuple list), bootstrap_ci
        # takes mean_fn as its second positional stat_fn, and the headline spread is
        # the full-span single-window diff. The band guard is the "double insurance"
        # deferred from Task 2: an out-of-band FRR series marks the arm unavailable
        # (reason = the ValueError) rather than crashing.
        if frr_points:
            try:
                assert_market_rate_band([p.rate for p in frr_points])
            except ValueError as exc:
                frr_bench = FrrBenchmark(
                    available=False, spread=Decimal("0"), ci_lo=Decimal("0"),
                    ci_hi=Decimal("0"), reason=str(exc),
                )
            else:
                frr_arm = attribute_passive(frr_points, window_bounds=bounds)
                frr_diffs = paired_active_returns(strat_outcomes, frr_arm)  # already a diff list
                if frr_diffs:
                    lo, hi = bootstrap_ci(frr_diffs, mean_fn)
                    single_frr = attribute_passive(frr_points, window_bounds=[(min_ts, max_ts)])
                    spread = single_strat[0].net_monthly - single_frr[0].net_monthly
                    frr_bench = FrrBenchmark(
                        available=True, spread=spread, ci_lo=lo, ci_hi=hi, reason=None,
                    )
                else:
                    frr_bench = FrrBenchmark(
                        available=False, spread=Decimal("0"), ci_lo=Decimal("0"),
                        ci_hi=Decimal("0"), reason="no overlapping windows",
                    )

        full_clamp = clamp_active_window(fills, cap=capital)
        total_capital_days = full_clamp.capital_days
        clamp_diag = ClampDiagnostic(
            cap=capital,
            peak_concurrent=full_clamp.peak_concurrent,
            raw_interest=full_clamp.raw_interest,
            clamped_interest=full_clamp.interest,
        )

        # attributed_deployed = open principal at the end of the data window
        # (point-in-time, comparable to the position_state realized snapshot).
        attributed_deployed = open_principal_at(fills, max_ts)
        attributed_interest = sum(
            (f.size_usdt * f.rate * _fill_duration_days(f) for f in fills), Decimal("0")
        )
    else:
        # No active arm (idle canary with no fills, or a single instant → empty
        # window bounds): no bot-vs-idle, no MR-alpha. Not reachable via the live
        # build_verdict_from_neon (it always pulls candles to now → non-empty bounds).
        ci_lo, ci_hi = Decimal("0"), Decimal("0")
        headline_bot_vs_idle = Decimal("0")
        mr_alpha_spread = Decimal("0")
        mr_alpha_ci_lo, mr_alpha_ci_hi = Decimal("0"), Decimal("0")
        mr_alpha_available = False
        total_capital_days = Decimal("0")
        attributed_deployed = Decimal("0")
        attributed_interest = Decimal("0")
        clamp_diag = ClampDiagnostic(
            cap=capital,
            peak_concurrent=Decimal("0"),
            raw_interest=Decimal("0"),
            clamped_interest=Decimal("0"),
        )

    # Band guard: a wrong-scale market series corrupts the MR-alpha diagnostic
    # only — bot-vs-idle (idle ≡ 0) needs no market-rate data, so the primary
    # verdict is unaffected. Mark MR-alpha unavailable and surface the message.
    band_reason: str | None = None
    try:
        assert_market_rate_band([p.rate for p in window_rate_points])
    except ValueError as exc:
        band_reason = str(exc)
        mr_alpha_available = False

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
        headline_bot_vs_idle=headline_bot_vs_idle,
        n_windows=n_windows,
        total_capital_days=total_capital_days,
        ci_lo=ci_lo,
        ci_hi=ci_hi,
        deployment_anchor=deployment_anchor,
        nav_anchor=nav_anchor,
        min_windows=_MIN_WINDOWS,
        min_capital_days=min_capital_days,
        mr_alpha_spread=mr_alpha_spread,
        mr_alpha_ci_lo=mr_alpha_ci_lo,
        mr_alpha_ci_hi=mr_alpha_ci_hi,
        mr_alpha_available=mr_alpha_available,
    )

    # MR-alpha unavailability caveats are informational — they do NOT change the
    # primary bot-vs-idle state (decoupled from passive-arm data quality). Prepend
    # so the operator sees why the secondary diagnostic is missing.
    caveats: list[str] = []
    if band_reason is not None:
        caveats.append(band_reason)
    elif len(window_rate_points) == 0 and (fills or market_rate_points):
        caveats.append(
            "MR-alpha diagnostic unavailable: no market-rate coverage in window — "
            "bot-vs-idle (idle ≡ 0) is unaffected"
        )
    if caveats:
        verdict = G3Verdict(
            state=verdict.state,
            headline_bot_vs_idle=verdict.headline_bot_vs_idle,
            n_windows=verdict.n_windows,
            ci_lo=verdict.ci_lo,
            ci_hi=verdict.ci_hi,
            reasons=[*caveats, *verdict.reasons],
            mr_alpha_spread=verdict.mr_alpha_spread,
            mr_alpha_ci_lo=verdict.mr_alpha_ci_lo,
            mr_alpha_ci_hi=verdict.mr_alpha_ci_hi,
            mr_alpha_available=verdict.mr_alpha_available,
        )

    # Human-readable data window
    if fills or market_rate_points:
        min_dt = datetime.fromtimestamp(min_ts / 1000, UTC).strftime("%Y-%m-%d")
        max_dt = datetime.fromtimestamp(max_ts / 1000, UTC).strftime("%Y-%m-%d")
        data_window = f"{min_dt}..{max_dt}"
    else:
        data_window = "n/a"

    return verdict, data_window, n_fills, clamp_diag, frr_bench
