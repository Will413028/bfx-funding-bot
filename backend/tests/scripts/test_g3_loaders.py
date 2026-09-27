"""G3 loader: the pure verdict core over venue credits, and the DB wiring.

Credits are the live account's (2026-09-22..27): 466642176, 150.76884612 fUST
at 0.00019999/day repaid after 842 s (14 min), trade 432914136 on our offer
5123273052; 466451710 at 0.0001482 held 2 days (its expiry).
"""
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

import bfx_funding_bot.modules.accounts.tables
import bfx_funding_bot.modules.execution.audit.tables
import bfx_funding_bot.modules.live_validation.tables  # noqa: F401
from bfx_funding_bot.core.db import Base
from bfx_funding_bot.external.bitfinex.auth_rest import InterestPayment
from bfx_funding_bot.modules.candles.repository import upsert_candles
from bfx_funding_bot.modules.candles.schemas import FundingCandle
from bfx_funding_bot.modules.execution.audit.tables import ExecutionDecisionRow
from bfx_funding_bot.modules.execution.event_store.tables import OfferClaimRow
from bfx_funding_bot.modules.live_validation.credit_attribution import (
    CreditCells,
    CreditLifetime,
)
from bfx_funding_bot.modules.live_validation.live_attribution import (
    MS_PER_DAY,
    MarketRatePoint,
    VerdictState,
)
from bfx_funding_bot.modules.live_validation.tables import (
    FundingCreditHistoryRow,
    FundingTradeRow,
)
from scripts._g3_loaders import (
    _candles_to_market_rate_points,
    _compute_verdict,
    build_verdict_from_neon,
    calendar_window_bounds,
)

C = Decimal("570")
DAY = 86_400_000
WEEK = 7 * DAY
HOUR = 3_600_000
MON = 1789948800000            # 2026-09-21 UTC Monday
AMOUNT = Decimal("150.76884612")
RATE = Decimal("0.00019999")
CREATED = 1790350246000        # 2026-09-25 15:30:46Z
REPAID = 1790351088000         # + 842 s
EXPIRED_OPEN = 1790100510000   # 2026-09-22 18:08:30Z
EXPIRED_CLOSE = 1790273311000  # 2026-09-24 18:08:31Z
EXPIRED_RATE = Decimal("0.0001482")
CELL = "fUST_p2"


def _credit(credit_id: str, opened: int, closed: int | None, *, amount: Decimal = AMOUNT,
            rate: Decimal = RATE, symbol: str = "fUST") -> CreditLifetime:
    return CreditLifetime(credit_id=credit_id, symbol=symbol, amount=amount, rate=rate,
                          period_days=2, mts_create=opened, opened_ms=opened, closed_ms=closed)


EARLY = _credit("466642176", CREATED, REPAID)
EXPIRED = _credit("466451710", EXPIRED_OPEN, EXPIRED_CLOSE, rate=EXPIRED_RATE)


def _cells(*bot: CreditLifetime) -> CreditCells:
    return CreditCells({c.credit_id: CELL for c in bot}, frozenset(), frozenset(), frozenset())


def _points(start: int, rate: str, n: int = 5) -> list[MarketRatePoint]:
    return [MarketRatePoint(mts=start + i * HOUR, rate=Decimal(rate)) for i in range(n)]


def _held_interest(c: CreditLifetime) -> Decimal:
    assert c.closed_ms is not None
    return c.amount * c.rate * Decimal(c.closed_ms - c.opened_ms) / MS_PER_DAY


def _candle(mts: int, close: Decimal | None) -> FundingCandle:
    return FundingCandle(
        symbol="fUST", timeframe="1h", period_agg="p2", mts=mts, close=close
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


def test_calendar_windows_are_the_reconciliation_weeks():
    """First window from the first credit, then UTC Monday weeks, last cut at now."""
    assert calendar_window_bounds(EXPIRED_OPEN, MON + WEEK + DAY) == [
        (EXPIRED_OPEN, MON + WEEK), (MON + WEEK, MON + WEEK + DAY)]
    assert calendar_window_bounds(MON, MON + 2 * WEEK) == [(MON, MON + WEEK),
                                                          (MON + WEEK, MON + 2 * WEEK)]
    assert calendar_window_bounds(MON + DAY, MON + 2 * DAY) == [(MON + DAY, MON + 2 * DAY)]
    assert calendar_window_bounds(MON, MON) == []


# ---------------------------------------------------------------------------
# _compute_verdict: pure verdict core over venue credits
# ---------------------------------------------------------------------------


def test_headline_is_the_credits_actual_held_interest_not_held_to_term():
    """Regression for the owner's finding: 466642176 was repaid after 14 min
    but the fill model booked it as 2 days held-to-term."""
    now = REPAID + DAY
    report = _compute_verdict(credits=[EARLY], cells=_cells(EARLY),
                              market_rate_points=_points(CREATED, "0.0002"),
                              capital=C, now_ms=now)
    fourteen_min = AMOUNT * RATE * Decimal(842_000) / MS_PER_DAY
    assert report.verdict.headline_bot_vs_idle == fourteen_min / C * Decimal("100")
    held_to_term = AMOUNT * RATE * Decimal("2") / C * Decimal("100")
    assert report.verdict.headline_bot_vs_idle * 200 < held_to_term
    assert report.coverage.bot_credits == 1
    assert report.coverage.gross_by_cell == {CELL: fourteen_min}


def test_unattributed_credits_are_reported_but_not_counted():
    now = EXPIRED_CLOSE + 3 * DAY
    report = _compute_verdict(credits=[EARLY, EXPIRED], cells=_cells(EARLY),
                              market_rate_points=_points(CREATED, "0.0002"),
                              capital=C, now_ms=now)
    assert report.verdict.headline_bot_vs_idle == _held_interest(EARLY) / C * Decimal("100")
    assert report.coverage.unattributed_credits == 0   # EXPIRED opened before the window
    assert report.deployment.peak_open_principal == AMOUNT

    both_bot = _compute_verdict(credits=[EARLY, EXPIRED, _credit("x", CREATED, REPAID)],
                                cells=_cells(EARLY, EXPIRED),
                                market_rate_points=[], capital=C, now_ms=now)
    assert both_bot.coverage.bot_credits == 2
    assert both_bot.coverage.unattributed_credits == 1
    assert both_bot.coverage.unattributed_gross == _held_interest(EARLY)
    assert both_bot.verdict.headline_bot_vs_idle == (
        _held_interest(EARLY) + _held_interest(EXPIRED)) / C * Decimal("100")


def test_other_symbols_credits_are_not_the_canary():
    fusd = _credit("7", CREATED, REPAID, symbol="fUSD")
    report = _compute_verdict(credits=[fusd], cells=_cells(fusd), market_rate_points=[],
                              capital=C, now_ms=REPAID + DAY)
    assert report.coverage.bot_credits == 0
    assert report.verdict.headline_bot_vs_idle == 0


def test_frr_scale_points_mark_mr_alpha_unavailable_not_unreliable():
    # bot-vs-idle (idle ≡ 0) needs no market-rate data: the band guard only
    # marks MR-alpha unavailable and leaves a caveat.
    report = _compute_verdict(credits=[EXPIRED], cells=_cells(EXPIRED),
                              market_rate_points=_points(EXPIRED_OPEN, "1.1e-06"),
                              capital=C, now_ms=EXPIRED_CLOSE)
    assert report.verdict.state is VerdictState.INSUFFICIENT_DATA
    assert report.verdict.mr_alpha_available is False
    assert any("plausible per-day band" in r for r in report.verdict.reasons)


def test_legit_points_mr_alpha_available():
    report = _compute_verdict(credits=[EXPIRED], cells=_cells(EXPIRED),
                              market_rate_points=_points(EXPIRED_OPEN, "0.0002"),
                              capital=C, now_ms=EXPIRED_CLOSE)
    assert not any("plausible per-day band" in r for r in report.verdict.reasons)
    assert report.verdict.mr_alpha_available is True
    assert report.verdict.state is VerdictState.INSUFFICIENT_DATA   # 1 window


def test_no_coverage_marks_mr_alpha_unavailable_primary_unblocked():
    # Credits spanning 2 weekly windows, no market points: the active arm runs
    # (headline > 0), MR alpha is unavailable with a coverage caveat.
    later = _credit("2", EXPIRED_OPEN + WEEK, EXPIRED_CLOSE + WEEK, rate=EXPIRED_RATE)
    report = _compute_verdict(credits=[EXPIRED, later], cells=_cells(EXPIRED, later),
                              market_rate_points=[], capital=C,
                              now_ms=EXPIRED_CLOSE + WEEK)
    assert report.verdict.n_windows == 2
    assert report.verdict.mr_alpha_available is False
    assert report.verdict.headline_bot_vs_idle > 0
    assert any("no market-rate coverage" in r for r in report.verdict.reasons)


def test_empty_is_insufficient_no_crash():
    report = _compute_verdict(credits=[], cells=_cells(), market_rate_points=[],
                              capital=C, now_ms=REPAID)
    assert report.verdict.state is VerdictState.INSUFFICIENT_DATA
    assert report.data_window == "n/a"
    assert report.coverage.bot_credits == 0
    assert report.frr.available is False


def test_capital_override_below_lent_principal_is_flagged_not_clamped():
    three = [_credit(str(i), CREATED, REPAID, amount=Decimal("300")) for i in range(3)]
    report = _compute_verdict(credits=three, cells=_cells(*three), market_rate_points=[],
                              capital=C, now_ms=REPAID + DAY, capital_source="--capital 570")
    assert report.deployment.peak_open_principal == Decimal("900")
    assert report.deployment.over_deployed is True
    # no clamp: the full interest of all three counts
    assert report.verdict.headline_bot_vs_idle == 3 * _held_interest(three[0]) / C * 100


# ---------------------------------------------------------------------------
# Ledger reconciliation: the trust gate (replaces the deployment/NAV anchors)
# ---------------------------------------------------------------------------


def _payout(ledger_id: int, mts: int, amount: Decimal) -> InterestPayment:
    return InterestPayment(ledger_id, "UST", "funding", mts, amount, Decimal("395.5"),
                           "Margin Funding Payment on wallet funding")


EXPIRED_NET = _held_interest(EXPIRED) * Decimal("0.85")
# EXPIRED's two days are paid on Wed 09-23 and Thu 09-24 ~01:30Z (and a sliver Fri)
_PAID_AT = [MON + 2 * DAY + 5_400_000, MON + 3 * DAY + 5_400_000]


def test_matching_ledger_keeps_the_verdict_data_driven():
    payments = [_payout(1, _PAID_AT[0], EXPIRED_NET / 2), _payout(2, _PAID_AT[1], EXPIRED_NET / 2)]
    report = _compute_verdict(credits=[EXPIRED], cells=_cells(EXPIRED), market_rate_points=[],
                              capital=C, now_ms=MON + 2 * WEEK, payments=payments)
    assert report.reconciliation_available is True
    week = report.reconciliations[0]
    assert (week.week_start_ms, week.complete, week.flagged) == (MON, True, False)
    assert week.credit_net == EXPIRED_NET
    assert report.verdict.state is VerdictState.INSUFFICIENT_DATA


def test_ledger_divergence_forces_unreliable():
    """Credit interest the venue did not pay (or paid differently) means the
    model is wrong: UNRELIABLE, whatever the CI says."""
    payments = [_payout(1, _PAID_AT[0], Decimal("0.10"))]
    report = _compute_verdict(credits=[EXPIRED], cells=_cells(EXPIRED), market_rate_points=[],
                              capital=C, now_ms=MON + 2 * WEEK, payments=payments)
    assert report.verdict.state is VerdictState.UNRELIABLE
    assert any("diverges from the venue ledger" in r and "2026-09-21" in r
               for r in report.verdict.reasons)


def test_incomplete_week_does_not_gate():
    payments = [_payout(1, _PAID_AT[0], Decimal("0.10"))]
    report = _compute_verdict(credits=[EXPIRED], cells=_cells(EXPIRED), market_rate_points=[],
                              capital=C, now_ms=EXPIRED_CLOSE + DAY, payments=payments)
    assert report.reconciliations[0].complete is False
    assert report.verdict.state is not VerdictState.UNRELIABLE


def test_no_ledger_makes_reconciliation_unavailable_not_unreliable():
    report = _compute_verdict(credits=[EXPIRED], cells=_cells(EXPIRED), market_rate_points=[],
                              capital=C, now_ms=MON + 2 * WEEK)
    assert report.reconciliation_available is False
    assert report.reconciliations == []
    assert report.verdict.state is VerdictState.INSUFFICIENT_DATA
    assert any("ledger reconciliation unavailable" in r for r in report.verdict.reasons)


# ---------------------------------------------------------------------------
# build_verdict_from_neon wiring (seeded in-memory sqlite, session-injected).
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def g3_factory(sqlite_engine: AsyncEngine) -> async_sessionmaker:
    async with sqlite_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return async_sessionmaker(sqlite_engine, expire_on_commit=False)


def _real_now() -> int:
    return int(datetime.now(UTC).timestamp() * 1000)


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
    target cell is seeded frr-scale (~1e-6) while every decoy is legit (2e-4)."""
    now = _real_now()  # candles are final only once in the past
    base = now - 5 * DAY  # within the 30-day no-credits fallback window
    target = [_recent_candle("fUST", "1h", "p2", base + i * HOUR, "1.1e-6") for i in range(12)]
    decoys = (
        [_recent_candle("fUST", "1h", "a30", base + i * HOUR, "0.0002") for i in range(12)]
        + [_recent_candle("fUSD", "1h", "p2", base + i * HOUR, "0.0002") for i in range(12)]
        + [_recent_candle("fUST", "15m", "p2", base + i * HOUR, "0.0002") for i in range(12)]
    )
    async with g3_factory() as s:
        await upsert_candles(s, target + decoys)
        await s.commit()

    report = await build_verdict_from_neon(capital=C, session_factory=g3_factory, now_ms=now)
    assert report.verdict.state is VerdictState.INSUFFICIENT_DATA
    assert report.verdict.mr_alpha_available is False
    assert any("plausible per-day band" in r for r in report.verdict.reasons)
    assert report.coverage.bot_credits == 0
    assert report.frr.available is False     # no funding_stats seeded


@pytest.mark.asyncio
async def test_build_verdict_legit_idle_cell_is_insufficient_not_crash(g3_factory):
    now = _real_now()
    candles = [_recent_candle("fUST", "1h", "p2", now - 5 * DAY + i * HOUR, "0.0002")
               for i in range(12)]
    async with g3_factory() as s:
        await upsert_candles(s, candles)
        await s.commit()

    report = await build_verdict_from_neon(capital=C, session_factory=g3_factory, now_ms=now)
    assert report.verdict.state is VerdictState.INSUFFICIENT_DATA
    assert not any("plausible per-day band" in r for r in report.verdict.reasons)
    assert report.verdict.mr_alpha_available is False
    assert report.capital_source == f"--capital {C}"


_ACCOUNT = UUID("35efed2d-3004-4941-a161-ca025d9c4d53")


def _history_row(credit_id: int, opened: int, closed: int, rate: Decimal) -> FundingCreditHistoryRow:
    return FundingCreditHistoryRow(
        exchange_account_id=_ACCOUNT, kind="credit", credit_id=credit_id,
        deployment_environment="prod", symbol="fUST", side=1, mts_create=opened,
        mts_update=opened, amount=AMOUNT, status="CLOSED", rate=rate, period_days=2,
        mts_opening=opened, mts_last_payout=closed,
    )


@pytest.mark.asyncio
async def test_build_verdict_reads_credits_through_trade_offer_and_decision(g3_factory, monkeypatch):
    """End to end: 466642176 reaches cell fUST_p2 through trade 432914136 →
    offer 5123273052 → execution decision; 466451710 has no trade and is
    reported unattributed. The headline is 842 s of interest."""
    monkeypatch.setenv("BFX_EXCHANGE_ACCOUNT_ID", str(_ACCOUNT))
    monkeypatch.setenv("BFX_DEPLOYMENT_ENV", "prod")
    now = REPAID + DAY
    async with g3_factory() as s:
        s.add_all([
            _history_row(466642176, CREATED, REPAID, RATE),
            _history_row(466451710, EXPIRED_OPEN, EXPIRED_CLOSE, EXPIRED_RATE),
            FundingTradeRow(
                exchange_account_id=_ACCOUNT, trade_id=432914136, deployment_environment="prod",
                symbol="fUST", mts_create=CREATED, offer_id=5123273052, amount=AMOUNT,
                rate=RATE, period_days=2, maker=None,
            ),
            OfferClaimRow(
                cid=1, account_id=str(_ACCOUNT), exchange_account_id=_ACCOUNT,
                deployment_environment="prod", state="FILLED", venue_offer_id="5123273052",
                symbol="fUST", size_usdt=AMOUNT, signal_correlation_id="scid-x",
                execution_decision_id="d1", occurred_at_ms=CREATED, last_updated_ms=CREATED,
                last_event_seq=1,
            ),
            ExecutionDecisionRow(
                decision_id="d1", account_id=str(_ACCOUNT), exchange_account_id=_ACCOUNT,
                deployment_environment="prod", reconcile_id="r", cell_id=CELL, symbol="fUST",
                signal_correlation_id="scid-x", outcome="submitted", signal_rate=RATE,
                amount_usdt=AMOUNT, duration_days=2, model_evidence={}, safety_result={},
                execution_policy="p", service_version="v", config_hash="h",
                occurred_at_ms=CREATED - 60_000, recorded_at_ms=CREATED - 60_000,
            ),
        ])
        await s.commit()

    report = await build_verdict_from_neon(capital=C, session_factory=g3_factory, now_ms=now)
    fourteen_min = AMOUNT * RATE * Decimal(842_000) / MS_PER_DAY
    assert report.coverage.bot_credits == 1
    assert report.coverage.gross_by_cell == {CELL: fourteen_min}
    assert report.verdict.headline_bot_vs_idle == fourteen_min / C * Decimal("100")
    assert report.data_window == "2026-09-25..2026-09-26"


@pytest.mark.asyncio
async def test_non_uuid_account_fails_instead_of_reading_no_credits(g3_factory, monkeypatch):
    """The venue read models are keyed by UUID; a configured non-UUID account
    must fail (account_id_canonical), never silently read as 'no credits'."""
    monkeypatch.setenv("BFX_EXCHANGE_ACCOUNT_ID", "default")
    monkeypatch.setenv("BFX_DEPLOYMENT_ENV", "prod")
    with pytest.raises(ValueError, match="must be a UUID"):
        await build_verdict_from_neon(capital=C, session_factory=g3_factory, now_ms=REPAID)
