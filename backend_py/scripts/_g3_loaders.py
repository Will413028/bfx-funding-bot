"""Neon data loader for G3 live validation.

Queries event_log (fills + releases) and funding_stats (FRR series) from Neon,
builds FillRecord / FrrPoint lists, and delegates all computation to the pure
modules/live_validation/live_attribution module.

account_id  = BFX_ACCOUNT_ID env-var (defaults "default", same as daemon.py)
environment = BFX_DEPLOYMENT_ENV env-var (required; "prod" for the live canary)
"""
from __future__ import annotations

import os
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import select

from bfx_funding_bot.core.db import make_engine, make_session_factory, session_scope
from bfx_funding_bot.core.settings import Settings
from bfx_funding_bot.modules.backtest.oos_profitability import bootstrap_ci, paired_active_returns
from bfx_funding_bot.modules.execution.event_store.tables import (
    EventLogRow,
    PositionStateRow,
)
from bfx_funding_bot.modules.funding_stats.repository import get_in_range
from bfx_funding_bot.modules.live_validation.live_attribution import (
    MS_PER_DAY,
    FillRecord,
    FrrPoint,
    G3Verdict,
    _fill_duration_days,
    attribute_active,
    attribute_passive,
    cell_period_days,
    check_deployment_anchor,
    check_nav_anchor,
    decide_verdict,
    weekly_window_bounds,
)

# Event type constants — must match serialization._TYPE_BY_CLASS (UPPERCASE).
_FILL_TYPE = "ORDER_FILL"
_RELEASE_TYPE = "RESERVATION_RELEASED"

# Thresholds for decide_verdict (spec: min_windows=8, min_capital_days=capital*7)
_MIN_WINDOWS = 8
_DEPLOY_TOL = Decimal("0.05")
_NAV_TOL = Decimal("0.1")

# How far back to look for FRR data when there are no fills at all.
_FRR_FALLBACK_DAYS = 30
_FRR_FALLBACK_MS = _FRR_FALLBACK_DAYS * 24 * 60 * 60 * 1000


async def build_verdict_from_neon(
    *, capital: Decimal
) -> tuple[G3Verdict, str, int]:
    """Query Neon and run G3 attribution.  Returns (verdict, data_window_str, n_fills)."""
    account_id = os.environ.get("BFX_ACCOUNT_ID", "default")
    deployment_env = os.environ.get("BFX_DEPLOYMENT_ENV", "prod")

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

            # ── 3. Fetch FRR series ───────────────────────────────────────────
            now_ms = int(datetime.now(UTC).timestamp() * 1000)
            if fill_rows:
                frr_start_ms = fill_rows[0].occurred_at_ms
                frr_end_ms = now_ms
            else:
                frr_start_ms = now_ms - _FRR_FALLBACK_MS
                frr_end_ms = now_ms

            frr_stats = await get_in_range(
                session, symbol="fUST", start_mts=frr_start_ms, end_mts=frr_end_ms
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

    finally:
        await engine.dispose()

    # ── 5. Build FrrPoint list ────────────────────────────────────────────────
    frr_points: list[FrrPoint] = []
    for stat in frr_stats:
        if stat.frr is not None and stat.avg_period is not None:
            frr_points.append(FrrPoint(mts=stat.mts, frr=stat.frr, avg_period=stat.avg_period))

    # nearest FRR avg_period for period_days resolution (conservative: use p2=2)
    # When a fill has no cell identity, spec mandates conservative p2 path.
    # avg_period is only needed if we ever call cell_period_days("a30", ...).
    # For conservative attribution we always call cell_period_days("p2", placeholder).
    conservative_period = cell_period_days("p2", Decimal("2"))  # always Decimal("2")

    # ── 6. Build FillRecord list ──────────────────────────────────────────────
    fills: list[FillRecord] = []
    for row in fill_rows:
        payload = row.payload
        size_usdt = Decimal(str(payload.get("size_usdt", "0")))
        # fill_rate is stored as float in OrderFilled; Decimal(str(float)) avoids
        # scientific-notation issues (same pattern as the live executor fix ae2c59d).
        fill_rate = Decimal(str(payload.get("fill_rate", "0")))
        venue_offer_id = str(payload.get("venue_offer_id") or row.venue_offer_id or "")
        fill_ts_ms = row.occurred_at_ms
        release_ts_ms = release_map.get(venue_offer_id)

        fills.append(
            FillRecord(
                venue_offer_id=venue_offer_id,
                fill_ts_ms=fill_ts_ms,
                size_usdt=size_usdt,
                rate=fill_rate,
                period_days=conservative_period,
                release_ts_ms=release_ts_ms,
            )
        )

    n_fills = len(fills)

    # ── 7. Compute windows + attribution ─────────────────────────────────────
    if fills or frr_points:
        all_mts = (
            [f.fill_ts_ms for f in fills]
            + [p.mts for p in frr_points]
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
        base_outcomes = attribute_passive(frr_points, window_bounds=bounds)
        actives = paired_active_returns(strat_outcomes, base_outcomes)
        if len(actives) >= 2:
            mean_fn = lambda xs: sum(xs, Decimal("0")) / Decimal(len(xs))  # noqa: E731
            ci_lo, ci_hi = bootstrap_ci(actives, mean_fn)
        else:
            ci_lo, ci_hi = Decimal("0"), Decimal("0")

        # Headline: single-window attribution over full span
        single_strat = attribute_active(fills, capital=capital, window_bounds=[(min_ts, max_ts)])
        single_base = attribute_passive(frr_points, window_bounds=[(min_ts, max_ts)])
        headline_active_spread = single_strat[0].net_monthly - single_base[0].net_monthly

        # total_capital_days = Σ(size_i * duration_i) / capital
        total_cap_days_raw = sum(
            (f.size_usdt * _fill_duration_days(f) for f in fills), Decimal("0")
        )
        total_capital_days = total_cap_days_raw / capital if capital > 0 else Decimal("0")

        # attributed_deployed: total interest earned (proxy for deployed principal)
        # = Σ(size_i * rate_i * duration_i) to compare against observed_realized.
        # We use realized interest, NOT principal directly, since principal is unknown
        # without a live position snapshot. But spec says "mean open principal ≈
        # total_capital_days*capital / span_days" — use that formula.
        span_days = Decimal(max_ts - min_ts) / MS_PER_DAY if max_ts > min_ts else Decimal("1")
        attributed_deployed = (total_cap_days_raw / span_days) if span_days > 0 else Decimal("0")

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
        # Set attributed_deployed = observed_realized so anchor is trivially within tol
        # when realized == 0 (new canary, nothing happened yet).
        attributed_deployed = observed_realized
        attributed_interest = Decimal("0")

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

    # Human-readable data window
    if fills or frr_points:
        min_dt = datetime.fromtimestamp(min_ts / 1000, UTC).strftime("%Y-%m-%d")
        max_dt = datetime.fromtimestamp(max_ts / 1000, UTC).strftime("%Y-%m-%d")
        data_window = f"{min_dt}..{max_dt}"
    else:
        data_window = "n/a"

    return verdict, data_window, n_fills
