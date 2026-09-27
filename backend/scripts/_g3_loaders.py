"""Postgres data loader for G3 live validation.

Loads the venue credit model (credits, credit -> cell via funding_trades, ledger
payouts; the same loader as the weekly attribution) and the market-rate series
(funding_candles.close, funding_stats) from Postgres, and delegates all
computation to the pure modules/live_validation/live_attribution module.

account_id  = canonical UUID from BFX_EXCHANGE_ACCOUNT_ID (required for the
production DB path; injected sqlite tests use a synthetic in-memory realm)
environment = BFX_DEPLOYMENT_ENV env-var (required; "prod" for the live canary)
"""
from __future__ import annotations

import os
from collections.abc import Sequence
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bfx_funding_bot.core.db import make_engine, make_session_factory, session_scope
from bfx_funding_bot.core.settings import Settings, require_deployment_environment
from bfx_funding_bot.external.bitfinex.auth_rest import InterestPayment
from bfx_funding_bot.modules.backtest.oos_profitability import bootstrap_ci, paired_active_returns
from bfx_funding_bot.modules.candles.repository import get_candles_in_range
from bfx_funding_bot.modules.candles.schemas import FundingCandle
from bfx_funding_bot.modules.funding_stats.schemas import FundingStat
from bfx_funding_bot.modules.funding_stats.tables import FundingStatRow
from bfx_funding_bot.modules.live_validation.credit_attribution import (
    UNATTRIBUTED,
    CreditCells,
    CreditLifetime,
    WeeklyReconciliation,
    reconcile_week,
)
from bfx_funding_bot.modules.live_validation.interest_ledger import (
    funding_currency,
    wallet_balance_basis,
)
from bfx_funding_bot.modules.live_validation.live_attribution import (
    CapitalBasis,
    CreditCoverage,
    DeploymentCheck,
    FrrBenchmark,
    G3Report,
    G3Verdict,
    MarketRatePoint,
    assert_market_rate_band,
    attribute_active,
    attribute_idle,
    attribute_passive,
    capital_for,
    credit_capital_days,
    credit_gross_interest,
    decide_verdict,
    frr_points_from_stats,
    peak_open_principal,
)
from bfx_funding_bot.modules.live_validation.weekly_attribution import (
    WEEK_MS,
    calendar_week_start,
)
from scripts.run_weekly_attribution import load_credit_inputs

# Thresholds for decide_verdict (spec: min_windows=8, min_capital_days=capital*7)
_MIN_WINDOWS = 8

# How far back to look for market-rate data when the bot has no credits at all.
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
    capital: Decimal | None = None,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
    now_ms: int | None = None,
) -> G3Report:
    """Query Postgres and run G3 attribution.

    I/O shell only: loads the credit model and the market series, then
    delegates to the pure _compute_verdict. Pass `session_factory` to run
    against an injected DB (used by the seeded unit tests); otherwise an engine
    is built from env. Function name `build_verdict_from_neon` is a historical
    holdover from the pre-2026-06-23 Neon era.
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
        # The in-memory unit harness supplies no process environment; production
        # always takes the branch above and requires an explicit validated value.
        deployment_env = os.environ.get("BFX_DEPLOYMENT_ENV", "prod")

    now = now_ms if now_ms is not None else int(datetime.now(UTC).timestamp() * 1000)
    engine = None
    if session_factory is None:
        settings = Settings()
        engine = make_engine(settings)
        session_factory = make_session_factory(engine)

    try:
        async with session_scope(session_factory) as session:
            # ── 1. Credit model: credits, their cells, ledger payouts ─────────
            inputs = await load_credit_inputs(
                session, account_id=account_id, deployment_environment=deployment_env,
            )
            credits = inputs.credits if inputs is not None else []
            cells = inputs.cells if inputs is not None else CreditCells(
                {}, frozenset(), frozenset(), frozenset())
            payments = inputs.payments if inputs is not None else []

            # ── 2. Capital budget C ───────────────────────────────────────────
            # An explicit amount wins. Otherwise C per window is the funding
            # wallet balance the venue ledger reports: BFX_ALLOCATION_CAP_USDT
            # is 0 since the capital policy replaced allocation caps.
            capital_basis: CapitalBasis
            if capital is not None:
                capital_basis = capital
                capital_source = f"--capital {capital}"
            else:
                ledger_basis = wallet_balance_basis(
                    payments, currency=funding_currency(_MARKET_SYMBOL))
                if ledger_basis is None:
                    raise RuntimeError(
                        "G3 needs a capital budget: pass --capital, or let the bot's "
                        "InterestLedgerSync fill funding_interest_payments for this account"
                    )
                capital_basis = ledger_basis
                capital_source = "ledger"

            # ── 3. Market-rate series (funding_candles.close, funding_stats) ──
            bot = _bot_credits(credits, cells)
            rate_start_ms = (min(c.opened_ms for c in bot) if bot
                             else now - _MARKET_RATE_FALLBACK_MS)
            candles = await get_candles_in_range(
                session,
                symbol=_MARKET_SYMBOL,
                timeframe=_MARKET_TIMEFRAME,
                period_agg=_MARKET_PERIOD_AGG,
                start_mts=rate_start_ms,
                end_mts=now,
            )
            # funding_stats is shared market data (realm-agnostic), queried over
            # the same window as the candle series.
            frr_rows = (
                await session.execute(
                    select(FundingStatRow)
                    .where(
                        FundingStatRow.symbol == _MARKET_SYMBOL,
                        FundingStatRow.mts >= rate_start_ms,
                        FundingStatRow.mts <= now,
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
    finally:
        if engine is not None:
            await engine.dispose()

    return _compute_verdict(
        credits=credits,
        cells=cells,
        payments=payments,
        market_rate_points=_candles_to_market_rate_points(candles),
        capital=capital_basis,
        capital_source=capital_source,
        now_ms=now,
        frr_points=frr_points_from_stats(frr_stats),
    )


def calendar_window_bounds(start_ms: int, end_ms: int) -> list[tuple[int, int]]:
    """UTC calendar-week [lo, hi) windows covering [start_ms, end_ms).

    Same weeks as the ledger reconciliation and attribution_weekly, so the
    trust gate checks exactly the weeks the CI samples. The first window starts
    at start_ms (no pre-inception idle days), the last is truncated to end_ms.
    """
    bounds: list[tuple[int, int]] = []
    lo = start_ms
    while lo < end_ms:
        hi = min(calendar_week_start(lo) + WEEK_MS, end_ms)
        bounds.append((lo, hi))
        lo = hi
    return bounds


def _bot_credits(credits: Sequence[CreditLifetime], cells: CreditCells) -> list[CreditLifetime]:
    """The canary symbol's credits that funding_trades lead to one of our cells."""
    return [
        c for c in credits
        if c.symbol == _MARKET_SYMBOL
        and cells.cell_by_credit.get(c.credit_id, UNATTRIBUTED) != UNATTRIBUTED
    ]


def _reconcile(
    credits: Sequence[CreditLifetime],
    payments: Sequence[InterestPayment],
    *,
    start_ms: int,
    now_ms: int,
) -> list[WeeklyReconciliation]:
    """Credit net interest vs ledger payouts for every calendar week from the
    one holding `start_ms` to the current one (all of the symbol's credits:
    the ledger pays the whole wallet, bot or not)."""
    currency = funding_currency(_MARKET_SYMBOL)
    same_currency = [c for c in credits if funding_currency(c.symbol) == currency]
    out: list[WeeklyReconciliation] = []
    week = calendar_week_start(start_ms)
    while week < now_ms:
        out.append(reconcile_week(same_currency, payments, currency=currency,
                                  week_start_ms=week, now_ms=now_ms))
        week += WEEK_MS
    return out


def _divergence(r: WeeklyReconciliation) -> str:
    week = datetime.fromtimestamp(r.week_start_ms / 1000, UTC).strftime("%Y-%m-%d")
    return f"week {week} credits net {r.credit_net:.6f} vs ledger {r.ledger_net:.6f}"


def _compute_verdict(
    *,
    credits: Sequence[CreditLifetime],
    cells: CreditCells,
    market_rate_points: list[MarketRatePoint],
    capital: CapitalBasis,
    now_ms: int,
    payments: Sequence[InterestPayment] = (),
    capital_source: str = "ledger",
    frr_points: list[MarketRatePoint] | None = None,
) -> G3Report:
    """Pure G3 verdict over already-built domain lists. No I/O.

    ``credits`` are all of the account's credits; the active arm counts the
    canary symbol's credits attributed to a bot cell. ``capital`` is C, fixed
    or per window; whole-span figures (headline, deployment check, min
    capital-days) use C over the whole data window.
    """
    frr_points = frr_points or []
    bot = _bot_credits(credits, cells)

    # ── Windows: from the first bot credit (or market point) to now ─────────
    all_mts = [c.opened_ms for c in bot] + [p.mts for p in market_rate_points]
    min_ts = min(all_mts) if all_mts else now_ms
    max_ts = now_ms
    has_data = bool(all_mts)

    # The symbol's other credits lent inside the window: reported, not counted.
    unattributed = [
        c for c in credits
        if c.symbol == _MARKET_SYMBOL
        and cells.cell_by_credit.get(c.credit_id, UNATTRIBUTED) == UNATTRIBUTED
        and c.held_ms(min_ts, max_ts, now_ms=now_ms) > 0
    ]

    bounds = calendar_window_bounds(min_ts, max_ts)
    span_capital = capital_for(capital, min_ts, max_ts)

    mean_fn = lambda xs: sum(xs, Decimal("0")) / Decimal(len(xs))  # noqa: E731

    # window-coverage of the passive arm decides the MR-alpha diagnostic only.
    window_rate_points = [p for p in market_rate_points if min_ts <= p.mts < max_ts]

    # AlwaysFRR benchmark default; the bot branch overwrites it when frr_points exist.
    frr_bench = FrrBenchmark(
        available=False, spread=Decimal("0"), ci_lo=Decimal("0"),
        ci_hi=Decimal("0"),
        reason="funding_stats empty — run backfill (E3 Task 8)",
    )

    if bot and bounds:
        strat_outcomes = attribute_active(bot, capital=capital, window_bounds=bounds,
                                          now_ms=now_ms)
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
        span = [(min_ts, max_ts)]
        single_strat = attribute_active(bot, capital=capital, window_bounds=span, now_ms=now_ms)
        single_base = attribute_passive(market_rate_points, window_bounds=span)
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

        # AlwaysFRR benchmark (policy bar; NOT fed into decide_verdict). An
        # out-of-band FRR series marks the arm unavailable rather than crashing.
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
                frr_diffs = paired_active_returns(strat_outcomes, frr_arm)
                if frr_diffs:
                    lo, hi = bootstrap_ci(frr_diffs, mean_fn)
                    single_frr = attribute_passive(frr_points, window_bounds=span)
                    spread = single_strat[0].net_monthly - single_frr[0].net_monthly
                    frr_bench = FrrBenchmark(
                        available=True, spread=spread, ci_lo=lo, ci_hi=hi, reason=None,
                    )
                else:
                    frr_bench = FrrBenchmark(
                        available=False, spread=Decimal("0"), ci_lo=Decimal("0"),
                        ci_hi=Decimal("0"), reason="no overlapping windows",
                    )

        total_capital_days = credit_capital_days(bot, min_ts, max_ts, now_ms=now_ms)
    else:
        # No active arm: the bot has no attributed credit in the window.
        ci_lo, ci_hi = Decimal("0"), Decimal("0")
        headline_bot_vs_idle = Decimal("0")
        mr_alpha_spread = Decimal("0")
        mr_alpha_ci_lo, mr_alpha_ci_hi = Decimal("0"), Decimal("0")
        mr_alpha_available = False
        total_capital_days = Decimal("0")

    # Band guard: a wrong-scale market series corrupts the MR-alpha diagnostic
    # only — bot-vs-idle (idle ≡ 0) needs no market-rate data.
    band_reason: str | None = None
    try:
        assert_market_rate_band([p.rate for p in window_rate_points])
    except ValueError as exc:
        band_reason = str(exc)
        mr_alpha_available = False

    # Trust gate: complete weeks whose credit interest disagrees with the ledger.
    currency = funding_currency(_MARKET_SYMBOL)
    reconciliation_available = any(p.currency == currency for p in payments)
    reconciliations = (
        _reconcile(credits, payments, start_ms=min_ts, now_ms=now_ms)
        if reconciliation_available and has_data else []
    )

    verdict = decide_verdict(
        headline_bot_vs_idle=headline_bot_vs_idle,
        n_windows=len(bounds),
        total_capital_days=total_capital_days,
        ci_lo=ci_lo,
        ci_hi=ci_hi,
        ledger_divergence=[_divergence(r) for r in reconciliations if r.flagged],
        min_windows=_MIN_WINDOWS,
        min_capital_days=span_capital * Decimal("7"),
        mr_alpha_spread=mr_alpha_spread,
        mr_alpha_ci_lo=mr_alpha_ci_lo,
        mr_alpha_ci_hi=mr_alpha_ci_hi,
        mr_alpha_available=mr_alpha_available,
    )

    # Informational caveats — they do NOT change the verdict state.
    caveats: list[str] = []
    if band_reason is not None:
        caveats.append(band_reason)
    elif not window_rate_points and has_data:
        caveats.append(
            "MR-alpha diagnostic unavailable: no market-rate coverage in window — "
            "bot-vs-idle (idle ≡ 0) is unaffected"
        )
    if not reconciliation_available:
        caveats.append(
            f"ledger reconciliation unavailable: no {currency} interest payouts in "
            "funding_interest_payments — the credit model is unchecked against the venue"
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

    gross_by_cell: dict[str, Decimal] = {}
    for c in bot:
        cell = cells.cell_by_credit[c.credit_id]
        gross_by_cell[cell] = gross_by_cell.get(cell, Decimal("0")) + c.gross_interest(
            min_ts, max_ts, now_ms=now_ms)
    coverage = CreditCoverage(
        bot_credits=len(bot),
        gross_by_cell=dict(sorted(gross_by_cell.items())),
        unattributed_credits=len(unattributed),
        unattributed_gross=credit_gross_interest(unattributed, min_ts, max_ts, now_ms=now_ms),
        ambiguous_credits=sum(1 for c in bot if c.credit_id in cells.ambiguous),
    )

    if has_data:
        min_dt = datetime.fromtimestamp(min_ts / 1000, UTC).strftime("%Y-%m-%d")
        max_dt = datetime.fromtimestamp(max_ts / 1000, UTC).strftime("%Y-%m-%d")
        data_window = f"{min_dt}..{max_dt}"
    else:
        data_window = "n/a"

    return G3Report(
        verdict=verdict,
        data_window=data_window,
        capital_source=capital_source,
        coverage=coverage,
        deployment=DeploymentCheck(
            cap=span_capital, peak_open_principal=peak_open_principal(bot, now_ms=now_ms)),
        frr=frr_bench,
        reconciliations=reconciliations,
        reconciliation_available=reconciliation_available,
    )
