"""G3 live validation: load the credit model and market series, compute the report.

``build_g3_report`` is the I/O shell (Postgres, read-only); ``compute_g3_report``
is pure. The arms and the verdict state machine live in ``live_attribution``;
credit matching and the ledger reconciliation in ``credit_attribution``; the
loaders are shared with the weekly attribution (``attribution_loader``).

account_id  = canonical UUID from BFX_EXCHANGE_ACCOUNT_ID (required for the
production DB path; injected sqlite tests use a synthetic in-memory realm)
environment = BFX_DEPLOYMENT_ENV env-var (required; "prod" for the live canary)
"""
from __future__ import annotations

import os
from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bfx_funding_bot.core.db import make_engine, make_session_factory, session_scope
from bfx_funding_bot.core.settings import Settings, require_deployment_environment
from bfx_funding_bot.external.bitfinex.auth_rest import InterestPayment
from bfx_funding_bot.modules.backtest.oos_profitability import (
    WindowOutcome,
    bootstrap_ci,
    paired_active_returns,
)
from bfx_funding_bot.modules.live_validation.attribution_loader import (
    MARKET_TIMEFRAME,
    funding_stats_of,
    load_credit_inputs,
    load_funding_stat_rows,
    load_market_points,
    reconciliation_weeks,
)
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
    GATE_WEEKS,
    CapitalBasis,
    CellMrAlpha,
    CreditCoverage,
    DataThreshold,
    DeploymentCheck,
    FrrBenchmark,
    G3Report,
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
    reconciliation_status,
)
from bfx_funding_bot.modules.live_validation.weekly_attribution import (
    WEEK_MS,
    calendar_week_start,
)

SYMBOL = "fUST"
_MIN_WINDOWS = 8
_THRESHOLD_DAYS = Decimal("7")

# How far back to look for market-rate data when the bot has no credits at all.
_MARKET_RATE_FALLBACK_MS = 30 * 24 * 60 * 60 * 1000
_ZERO = Decimal("0")


def cell_period_agg(cell: str) -> str | None:
    """period_agg of a canonical ``<symbol>_<period_agg>`` cell id of SYMBOL."""
    symbol, sep, period_agg = cell.partition("_")
    return period_agg if sep and symbol == SYMBOL and period_agg else None


async def build_g3_report(
    *,
    capital: Decimal | None = None,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
    now_ms: int | None = None,
    acks: Mapping[int, str] | None = None,
) -> G3Report:
    """Query Postgres and run G3 attribution (read-only).

    Pass `session_factory` to run against an injected DB (unit tests);
    otherwise an engine is built from env.
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
        deployment_env = os.environ.get("BFX_DEPLOYMENT_ENV", "prod")

    now = now_ms if now_ms is not None else int(datetime.now(UTC).timestamp() * 1000)
    engine = None
    if session_factory is None:
        engine = make_engine(Settings())
        session_factory = make_session_factory(engine)

    try:
        async with session_scope(session_factory) as session:
            inputs = await load_credit_inputs(
                session, account_id=account_id, deployment_environment=deployment_env,
            )
            credits = inputs.credits if inputs is not None else []
            cells = inputs.cells if inputs is not None else CreditCells(
                {}, frozenset(), frozenset(), frozenset())
            payments = inputs.payments if inputs is not None else []

            # An explicit C wins; otherwise C per window is the funding-wallet
            # balance the venue ledger reports.
            capital_basis: CapitalBasis
            if capital is not None:
                capital_basis, capital_source = capital, f"--capital {capital}"
            else:
                ledger_basis = wallet_balance_basis(payments, currency=funding_currency(SYMBOL))
                if ledger_basis is None:
                    raise RuntimeError(
                        "G3 needs a capital budget: pass --capital, or let the bot's "
                        "InterestLedgerSync fill funding_interest_payments for this account"
                    )
                capital_basis, capital_source = ledger_basis, "ledger"

            # Market series: one per bot cell's period_agg, plus funding_stats.
            bot = _bot_slices(credits, cells)
            start = (min(c.start_ms for _, c in bot) if bot
                     else now - _MARKET_RATE_FALLBACK_MS)
            period_aggs = sorted({
                agg for cell, _ in bot if (agg := cell_period_agg(cell)) is not None
            })
            market_points = {
                agg: await load_market_points(
                    session, symbol=SYMBOL, period_agg=agg, start_ms=start, end_ms=now)
                for agg in period_aggs
            }
            frr_rows = await load_funding_stat_rows(
                session, symbol=SYMBOL, start_ms=start, end_ms=now)
    finally:
        if engine is not None:
            await engine.dispose()

    return compute_g3_report(
        credits=credits,
        cells=cells,
        payments=payments,
        market_points=market_points,
        capital=capital_basis,
        capital_source=capital_source,
        now_ms=now,
        frr_points=frr_points_from_stats(funding_stats_of(frr_rows)),
        acks=acks,
    )


def calendar_window_bounds(start_ms: int, end_ms: int) -> list[tuple[int, int]]:
    """UTC calendar-week [lo, hi) windows covering [start_ms, end_ms).

    Same weeks as the ledger reconciliation and attribution_weekly, so the
    trust gate checks the weeks the CI samples. The first window starts at
    start_ms (no pre-inception idle days), the last is truncated to end_ms.
    """
    bounds: list[tuple[int, int]] = []
    lo = start_ms
    while lo < end_ms:
        hi = min(calendar_week_start(lo) + WEEK_MS, end_ms)
        bounds.append((lo, hi))
        lo = hi
    return bounds


def _slices(
    credits: Sequence[CreditLifetime], cells: CreditCells,
) -> list[tuple[str, CreditLifetime]]:
    """SYMBOL's credits as (cell, credit) slices, using credit_attribution's
    allocation: a credit split between cells becomes one slice per cell with
    its amount scaled by that cell's share (interest, capital-days and open
    principal are all linear in the amount)."""
    out: list[tuple[str, CreditLifetime]] = []
    for c in credits:
        if c.symbol != SYMBOL:
            continue
        for cell, share in cells.share_of(c.credit_id).items():
            out.append((cell, c if share == 1 else replace(c, amount=c.amount * share)))
    return out


def _bot_slices(
    credits: Sequence[CreditLifetime], cells: CreditCells,
) -> list[tuple[str, CreditLifetime]]:
    """The slices that funding_trades lead to one of our cells."""
    return [(cell, c) for cell, c in _slices(credits, cells) if cell != UNATTRIBUTED]


def _mean(xs: list[Decimal]) -> Decimal:
    return sum(xs, _ZERO) / Decimal(len(xs))


def _ci(diffs: list[Decimal]) -> tuple[Decimal, Decimal]:
    # bootstrap_ci needs >= 2 samples; a single window gives no CI.
    return bootstrap_ci(diffs, _mean) if len(diffs) >= 2 else (_ZERO, _ZERO)


def _week(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, UTC).strftime("%Y-%m-%d")


def _cell_alphas(
    bot_slices: Sequence[tuple[str, CreditLifetime]],
    market_points: Mapping[str, list[MarketRatePoint]],
    *,
    capital: CapitalBasis,
    bounds: list[tuple[int, int]],
    span: tuple[int, int],
    now_ms: int,
) -> tuple[list[CellMrAlpha], list[Decimal], Decimal]:
    """Per-cell MR alpha, the per-window total over the cells that have a
    baseline, and the share of bot capital-days that total covers."""
    lo, hi = span
    by_cell: dict[str, list[CreditLifetime]] = {}
    for cell, c in bot_slices:
        by_cell.setdefault(cell, []).append(c)
    total_days = credit_capital_days([c for _, c in bot_slices], lo, hi, now_ms=now_ms)

    rows: list[CellMrAlpha] = []
    total_diffs = [_ZERO] * len(bounds)
    covered = _ZERO
    for cell, members in sorted(by_cell.items()):
        share = (credit_capital_days(members, lo, hi, now_ms=now_ms) / total_days
                 if total_days > 0 else _ZERO)
        agg = cell_period_agg(cell)
        points = [p for p in market_points.get(agg or "", []) if lo <= p.mts < hi]
        reason: str | None = None
        if agg is None:
            reason = f"cell id is not {SYMBOL}_<period_agg>"
        elif not points:
            reason = (f"no market-rate coverage in window ({SYMBOL}/{MARKET_TIMEFRAME}/{agg} "
                      "candles) — bot-vs-idle is unaffected")
        else:
            try:
                assert_market_rate_band([p.rate for p in points])
            except ValueError as exc:
                reason = str(exc)
        if reason is not None:
            rows.append(CellMrAlpha(cell, agg or "?", share, False, reason, _ZERO, _ZERO, _ZERO))
            continue

        def scaled(outs: list[WindowOutcome], s: Decimal = share) -> list[WindowOutcome]:
            return [replace(o, net_monthly=o.net_monthly * s) for o in outs]

        active = attribute_active(members, capital=capital, window_bounds=bounds, now_ms=now_ms)
        diffs = paired_active_returns(active, scaled(attribute_passive(points, window_bounds=bounds)))
        span_active = attribute_active(members, capital=capital, window_bounds=[span],
                                       now_ms=now_ms)[0].net_monthly
        span_base = attribute_passive(points, window_bounds=[span])[0].net_monthly * share
        ci_lo, ci_hi = _ci(diffs)
        rows.append(CellMrAlpha(cell, agg or "?", share, True, None, span_active - span_base,
                                ci_lo, ci_hi))
        total_diffs = [t + d for t, d in zip(total_diffs, diffs, strict=True)]
        covered += share
    return rows, total_diffs, covered


def compute_g3_report(
    *,
    credits: Sequence[CreditLifetime],
    cells: CreditCells,
    market_points: Mapping[str, list[MarketRatePoint]],
    capital: CapitalBasis,
    now_ms: int,
    payments: Sequence[InterestPayment] = (),
    capital_source: str = "ledger",
    frr_points: list[MarketRatePoint] | None = None,
    acks: Mapping[int, str] | None = None,
) -> G3Report:
    """Pure G3 report over already-built domain lists. No I/O.

    ``credits`` are all of the account's credits; the active arm counts
    SYMBOL's credits attributed to a bot cell. ``market_points`` maps a
    period_agg to its market-rate series. ``capital`` is C, fixed or per
    window. ``acks`` maps an operator-acknowledged week start to its reason.
    """
    frr_points = frr_points or []
    acks = dict(acks or {})
    slices = _slices(credits, cells)
    bot_slices = [(cell, c) for cell, c in slices if cell != UNATTRIBUTED]
    bot = [c for _, c in bot_slices]

    all_mts = [c.start_ms for c in bot] + [p.mts for pts in market_points.values() for p in pts]
    min_ts = min(all_mts) if all_mts else now_ms
    max_ts = now_ms
    has_data = bool(all_mts)
    span = (min_ts, max_ts)
    bounds = calendar_window_bounds(min_ts, max_ts)
    span_capital = capital_for(capital, min_ts, max_ts)

    # The symbol's other credits (or unattributed shares) lent inside the
    # window: reported, not counted.
    unattributed = [c for cell, c in slices
                    if cell == UNATTRIBUTED and c.held_ms(min_ts, max_ts, now_ms=now_ms) > 0]

    frr_bench = FrrBenchmark(
        available=False, spread=_ZERO, ci_lo=_ZERO, ci_hi=_ZERO,
        reason="funding_stats empty — run backfill (E3 Task 8)",
    )
    mr_cells: list[CellMrAlpha] = []
    mr_coverage = _ZERO
    ci_lo = ci_hi = headline = _ZERO
    mr_spread = mr_ci_lo = mr_ci_hi = _ZERO
    capital_days = _ZERO
    if bot and bounds:
        strat = attribute_active(bot, capital=capital, window_bounds=bounds, now_ms=now_ms)
        # Primary: bot-vs-idle = active − idle (idle ≡ 0) → absolute active return.
        ci_lo, ci_hi = _ci(paired_active_returns(strat, attribute_idle(window_bounds=bounds)))
        headline = attribute_active(bot, capital=capital, window_bounds=[span],
                                    now_ms=now_ms)[0].net_monthly

        # Secondary diagnostic: MR alpha per cell vs its own period's market rate.
        mr_cells, total_diffs, mr_coverage = _cell_alphas(
            bot_slices, market_points, capital=capital, bounds=bounds, span=span,
            now_ms=now_ms)
        mr_spread = sum((c.spread for c in mr_cells if c.available), _ZERO)
        mr_ci_lo, mr_ci_hi = _ci(total_diffs) if mr_coverage > 0 else (_ZERO, _ZERO)

        # AlwaysFRR benchmark (evidence, not fed into decide_verdict).
        if frr_points:
            try:
                assert_market_rate_band([p.rate for p in frr_points])
            except ValueError as exc:
                frr_bench = replace(frr_bench, reason=str(exc))
            else:
                frr_diffs = paired_active_returns(
                    strat, attribute_passive(frr_points, window_bounds=bounds))
                lo, hi = _ci(frr_diffs)
                single_frr = attribute_passive(frr_points, window_bounds=[span])[0].net_monthly
                frr_bench = FrrBenchmark(available=True, spread=headline - single_frr,
                                         ci_lo=lo, ci_hi=hi, reason=None)
        capital_days = credit_capital_days(bot, min_ts, max_ts, now_ms=now_ms)
    mr_available = mr_coverage > 0

    threshold = _data_threshold(payments, capital_days=capital_days, bounds=bounds or [span],
                                span_capital=span_capital)

    # Trust gate: the most recent GATE_WEEKS settled weeks; an older flag is
    # reported but does not block, a recent one only an operator ack clears.
    currency = funding_currency(SYMBOL)
    reconciliation_available = any(p.currency == currency for p in payments)
    gate_weeks = reconciliation_weeks(now_ms, GATE_WEEKS) if reconciliation_available else []
    reconciliations = (
        _reconcile(credits, payments, weeks=_display_weeks(min_ts, now_ms, gate_weeks),
                   now_ms=now_ms)
        if reconciliation_available and has_data else []
    )
    blocking = [
        f"week {_week(r.week_start_ms)} credits net {r.credit_net:.6f} vs ledger {r.ledger_net:.6f}"
        for r in reconciliations
        if reconciliation_status(r, gate_weeks=gate_weeks, acks=acks) == "FLAG"
    ]

    verdict = decide_verdict(
        headline_bot_vs_idle=headline,
        n_windows=len(bounds),
        total_capital_days=capital_days,
        ci_lo=ci_lo,
        ci_hi=ci_hi,
        ledger_divergence=blocking,
        min_windows=_MIN_WINDOWS,
        min_capital_days=threshold.minimum,
        mr_alpha_spread=mr_spread,
        mr_alpha_ci_lo=mr_ci_lo,
        mr_alpha_ci_hi=mr_ci_hi,
        mr_alpha_available=mr_available,
    )

    # Informational caveats — they do NOT change the verdict state.
    caveats = [f"MR-alpha {c.cell}: {c.reason}" for c in mr_cells if not c.available]
    if not reconciliation_available:
        caveats.append(
            f"ledger reconciliation unavailable: no {currency} interest payouts in "
            "funding_interest_payments — the credit model is unchecked against the venue"
        )
    if caveats:
        verdict = replace(verdict, reasons=[*caveats, *verdict.reasons])

    gross_by_cell: dict[str, Decimal] = {}
    for cell, c in bot_slices:
        gross_by_cell[cell] = gross_by_cell.get(cell, _ZERO) + c.gross_interest(
            min_ts, max_ts, now_ms=now_ms)
    bot_ids = {c.credit_id for c in bot}

    return G3Report(
        verdict=verdict,
        data_window=f"{_week(min_ts)}..{_week(max_ts)}" if has_data else "n/a",
        capital_source=capital_source,
        coverage=CreditCoverage(
            bot_credits=len(bot_ids),
            gross_by_cell=dict(sorted(gross_by_cell.items())),
            unattributed_credits=len({c.credit_id for c in unattributed}),
            unattributed_gross=credit_gross_interest(unattributed, min_ts, max_ts,
                                                     now_ms=now_ms),
            ambiguous_credits=len(bot_ids & cells.ambiguous),
        ),
        deployment=DeploymentCheck(
            cap=span_capital, peak_open_principal=peak_open_principal(bot, now_ms=now_ms)),
        frr=frr_bench,
        reconciliations=reconciliations,
        reconciliation_available=reconciliation_available,
        mr_alpha_cells=mr_cells,
        mr_alpha_coverage=mr_coverage,
        data_threshold=threshold,
        gate_weeks=gate_weeks,
        acknowledgements=acks,
    )


def _data_threshold(
    payments: Sequence[InterestPayment],
    *,
    capital_days: Decimal,
    bounds: list[tuple[int, int]],
    span_capital: Decimal,
) -> DataThreshold:
    """Mean ledger wallet balance over the evaluated windows × 7 days, whatever C
    is (an explicit --capital does not move the bar); C × 7 without a ledger."""
    ledger: Callable[[int, int], Decimal] | None = wallet_balance_basis(
        payments, currency=funding_currency(SYMBOL))
    if ledger is None:
        return DataThreshold(capital_days, span_capital * _THRESHOLD_DAYS,
                             f"C × 7 days (no ledger payouts; C = {span_capital})")
    mean_balance = _mean([ledger(lo, hi) for lo, hi in bounds])
    return DataThreshold(
        capital_days, mean_balance * _THRESHOLD_DAYS,
        f"mean ledger wallet balance over the {len(bounds)} evaluated windows "
        f"({mean_balance:.2f}) × 7 days",
    )


def _display_weeks(min_ts: int, now_ms: int, gate_weeks: list[int]) -> list[int]:
    """Every week of the data window, plus the gate weeks (which may predate it)."""
    weeks = set(gate_weeks)
    week = calendar_week_start(min_ts)
    while week < now_ms:
        weeks.add(week)
        week += WEEK_MS
    return sorted(weeks)


def _reconcile(
    credits: Sequence[CreditLifetime],
    payments: Sequence[InterestPayment],
    *,
    weeks: list[int],
    now_ms: int,
) -> list[WeeklyReconciliation]:
    """Credit net interest vs ledger payouts per week (all of SYMBOL's currency's
    credits: the ledger pays the whole wallet, bot or not)."""
    currency = funding_currency(SYMBOL)
    same = [c for c in credits if funding_currency(c.symbol) == currency]
    return [reconcile_week(same, payments, currency=currency, week_start_ms=w, now_ms=now_ms)
            for w in weeks]
