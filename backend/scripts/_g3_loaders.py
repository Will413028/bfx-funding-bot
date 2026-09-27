"""Postgres data loader for G3 live validation.

Queries event_log (fills + releases) and funding_candles (per-day market-rate
series, close) from Postgres, builds FillRecord / MarketRatePoint lists, and
delegates all computation to the pure modules/live_validation/live_attribution
module.

account_id  = canonical UUID from BFX_EXCHANGE_ACCOUNT_ID (required for the
production DB path; injected sqlite tests use a synthetic in-memory realm)
environment = BFX_DEPLOYMENT_ENV env-var (required; "prod" for the live canary)
"""
from __future__ import annotations

import os
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bfx_funding_bot.core.db import make_engine, make_session_factory, session_scope
from bfx_funding_bot.core.settings import Settings, require_deployment_environment
from bfx_funding_bot.external.bitfinex.auth_rest import InterestPayment
from bfx_funding_bot.modules.accounts.exchange_accounts import (
    account_id_uuid_or_none,
    account_scope_clause,
)
from bfx_funding_bot.modules.backtest.oos_profitability import bootstrap_ci, paired_active_returns
from bfx_funding_bot.modules.candles.repository import get_candles_in_range
from bfx_funding_bot.modules.candles.schemas import FundingCandle
from bfx_funding_bot.modules.execution.event_store.tables import (
    EventLogRow,
    PositionStateRow,
)
from bfx_funding_bot.modules.funding_stats.schemas import FundingStat
from bfx_funding_bot.modules.funding_stats.tables import FundingStatRow
from bfx_funding_bot.modules.live_validation.interest_ledger import (
    funding_currency,
    wallet_balance_basis,
)
from bfx_funding_bot.modules.live_validation.live_attribution import (
    CapitalBasis,
    ClampDiagnostic,
    CreditCloseRecord,
    FillRecord,
    FrrBenchmark,
    G3Verdict,
    MarketRatePoint,
    _fill_duration_days,
    apply_credit_closes,
    assert_market_rate_band,
    attribute_active,
    attribute_idle,
    attribute_passive,
    capital_for,
    cell_period_days,
    check_deployment_anchor,
    check_nav_anchor,
    clamp_active_window,
    decide_verdict,
    frr_points_from_stats,
    open_principal_at,
    weekly_window_bounds,
)
from bfx_funding_bot.modules.live_validation.tables import (
    FundingCreditHistoryRow,
    FundingInterestPaymentRow,
)

# Event type constants — must match serialization._TYPE_BY_CLASS (UPPERCASE).
_FILL_TYPE = "ORDER_FILL"
_RELEASE_TYPE = "RESERVATION_RELEASED"
_CREDIT_CLOSE_TYPE = "CREDIT_CLOSED"

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
            account_scope_clause(
                session,
                account_id=account_id,
                exchange_account_column=PositionStateRow.exchange_account_id,
                legacy_account_column=PositionStateRow.account_id,
            ),
            PositionStateRow.deployment_environment == deployment_env,
            PositionStateRow.symbol == symbol,
        )
        .limit(1)
    )
    row = (await session.execute(stmt)).scalars().first()
    return Decimal(str(row.realized)) if row is not None else Decimal("0")


def merge_credit_closes(
    history: list[CreditCloseRecord],
    events: list[tuple[dict[str, Any], int]],
    *,
    symbol: str,
) -> list[CreditCloseRecord]:
    """Venue credit history first; a CREDIT_CLOSED event (payload, occurred_at_ms)
    only for a credit the history does not have yet, closed at its
    mts_last_payout when the event carries one."""
    synced = {c.credit_id for c in history}
    return history + [
        CreditCloseRecord(
            credit_id=int(payload["credit_id"]),
            amount=Decimal(str(payload["amount"])),
            mts_create=int(payload["mts_create"]),
            close_ts_ms=int(payload.get("mts_last_payout") or occurred_at_ms),
        )
        for payload, occurred_at_ms in events
        if payload.get("symbol") == symbol and int(payload["credit_id"]) not in synced
    ]


async def _load_ledger_capital(
    session: AsyncSession, *, account_id: str, deployment_env: str, currency: str,
) -> Callable[[int, int], Decimal] | None:
    """C per window from the funding-wallet balance in the interest ledger."""
    account_uuid = account_id_uuid_or_none(account_id)
    if account_uuid is None:
        return None
    rows = (await session.scalars(select(FundingInterestPaymentRow).where(
        FundingInterestPaymentRow.exchange_account_id == account_uuid,
        FundingInterestPaymentRow.deployment_environment == deployment_env,
        FundingInterestPaymentRow.currency == currency,
    ))).all()
    payments = [InterestPayment(r.ledger_id, r.currency, None, r.mts, Decimal(r.amount),
                                Decimal(r.balance), r.description) for r in rows]
    return wallet_balance_basis(payments, currency=currency)


async def _load_credit_history_closes(
    session: AsyncSession, *, account_id: str, deployment_env: str, symbol: str,
) -> list[CreditCloseRecord]:
    """Ended credits from the venue credit history (keyed by account UUID only;
    synthetic legacy realms have none)."""
    account_uuid = account_id_uuid_or_none(account_id)
    if account_uuid is None:
        return []
    rows = (await session.scalars(select(FundingCreditHistoryRow).where(
        FundingCreditHistoryRow.exchange_account_id == account_uuid,
        FundingCreditHistoryRow.deployment_environment == deployment_env,
        FundingCreditHistoryRow.symbol == symbol,
        FundingCreditHistoryRow.kind == "credit",
    ))).all()
    return [
        CreditCloseRecord(
            credit_id=int(r.credit_id), amount=Decimal(r.amount), mts_create=int(r.mts_create),
            close_ts_ms=int(r.mts_last_payout if r.mts_last_payout is not None else r.mts_update),
        )
        for r in rows
    ]


async def build_verdict_from_neon(
    *,
    capital: Decimal | None = None,
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
    raw_account_id = os.environ.get("BFX_EXCHANGE_ACCOUNT_ID", "").strip()
    if not raw_account_id:
        if session_factory is None:
            raise RuntimeError("BFX_EXCHANGE_ACCOUNT_ID is required")
        # Unit tests inject a sqlite session and intentionally use a synthetic
        # realm; no production invocation can reach this branch.
        account_id = "default"
    else:
        from bfx_funding_bot.modules.accounts.exchange_accounts import account_id_canonical

        account_id = account_id_canonical(raw_account_id)
    if session_factory is None:
        deployment_env = require_deployment_environment()
    else:
        # The in-memory unit harness intentionally supplies synthetic legacy
        # rows and no process environment; production always takes the branch
        # above and requires an explicit validated value.
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
                    account_scope_clause(
                        session,
                        account_id=account_id,
                        exchange_account_column=EventLogRow.exchange_account_id,
                        legacy_account_column=EventLogRow.account_id,
                    ),
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
                    account_scope_clause(
                        session,
                        account_id=account_id,
                        exchange_account_column=EventLogRow.exchange_account_id,
                        legacy_account_column=EventLogRow.account_id,
                    ),
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

            # ── 3c. Capital budget C ──────────────────────────────────────────
            # An explicit amount wins. Otherwise C per window is the funding
            # wallet balance the venue ledger reports (report_interest's basis):
            # BFX_ALLOCATION_CAP_USDT is 0 since the capital policy replaced
            # allocation caps, and a zero C made every window raise.
            capital_basis: CapitalBasis
            if capital is not None:
                capital_basis = capital
            else:
                ledger_basis = await _load_ledger_capital(
                    session, account_id=account_id, deployment_env=deployment_env,
                    currency=funding_currency(_MARKET_SYMBOL),
                )
                if ledger_basis is None:
                    raise RuntimeError(
                        "G3 needs a capital budget: pass --capital, or let the bot's "
                        "InterestLedgerSync fill funding_interest_payments for this account"
                    )
                capital_basis = ledger_basis

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

            # ── 5b. Join venue credit-close truth ─────────────────────────────
            # Early borrower returns otherwise double-count re-lent principal in
            # open_principal_at (2026-07-19 anchor divergence root cause).
            # Venue credit history (MTS_LAST_PAYOUT) is authoritative; a
            # CREDIT_CLOSED event is used only for a credit not yet synced, with
            # its mts_last_payout when present. Events written before the
            # 2026-09-27 parser fix carry mts_update as their time, which equals
            # mts_create for a credit repaid early.
            history_closes = await _load_credit_history_closes(
                session, account_id=account_id, deployment_env=deployment_env,
                symbol=_MARKET_SYMBOL,
            )
            close_stmt = select(EventLogRow).where(
                account_scope_clause(
                    session,
                    account_id=account_id,
                    exchange_account_column=EventLogRow.exchange_account_id,
                    legacy_account_column=EventLogRow.account_id,
                ),
                EventLogRow.deployment_environment == deployment_env,
                EventLogRow.event_type == _CREDIT_CLOSE_TYPE,
            )
            close_rows = (await session.execute(close_stmt)).scalars().all()
            closes = merge_credit_closes(
                history_closes,
                [(r.payload, r.occurred_at_ms) for r in close_rows],
                symbol=_MARKET_SYMBOL,
            )
            fills = apply_credit_closes(fills, closes)
    finally:
        if engine is not None:
            await engine.dispose()

    return _compute_verdict(
        fills=fills,
        market_rate_points=market_rate_points,
        observed_realized=observed_realized,
        capital=capital_basis,
        frr_points=frr_points_from_stats(frr_stats),
    )


def _compute_verdict(
    *,
    fills: list[FillRecord],
    market_rate_points: list[MarketRatePoint],
    observed_realized: Decimal,
    capital: CapitalBasis,
    frr_points: list[MarketRatePoint] | None = None,
) -> tuple[G3Verdict, str, int, ClampDiagnostic, FrrBenchmark]:
    """Pure G3 verdict over already-built domain lists. No I/O.

    ``capital`` is C, fixed or per window; whole-span figures (headline,
    over-deploy clamp, min capital-days) use C over the whole data window.

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
    span_capital = capital_for(capital, min_ts, max_ts)

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

        full_clamp = clamp_active_window(fills, cap=span_capital)
        total_capital_days = full_clamp.capital_days
        clamp_diag = ClampDiagnostic(
            cap=span_capital,
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
            cap=span_capital,
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
    min_capital_days = span_capital * Decimal("7")

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
