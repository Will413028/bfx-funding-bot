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
    TradeRecord,
    assign_cells,
)
from bfx_funding_bot.modules.live_validation.g3 import (
    build_g3_report,
    calendar_window_bounds,
    compute_g3_report,
)
from bfx_funding_bot.modules.live_validation.live_attribution import (
    MS_PER_DAY,
    MarketRatePoint,
    VerdictState,
    reconciliation_status,
)
from bfx_funding_bot.modules.live_validation.tables import (
    FundingCreditHistoryRow,
    FundingTradeRow,
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


def test_calendar_windows_are_the_reconciliation_weeks():
    """First window from the first credit, then UTC Monday weeks, last cut at now."""
    assert calendar_window_bounds(EXPIRED_OPEN, MON + WEEK + DAY) == [
        (EXPIRED_OPEN, MON + WEEK), (MON + WEEK, MON + WEEK + DAY)]
    assert calendar_window_bounds(MON, MON + 2 * WEEK) == [(MON, MON + WEEK),
                                                          (MON + WEEK, MON + 2 * WEEK)]
    assert calendar_window_bounds(MON + DAY, MON + 2 * DAY) == [(MON + DAY, MON + 2 * DAY)]
    assert calendar_window_bounds(MON, MON) == []


# ---------------------------------------------------------------------------
# compute_g3_report: pure core over venue credits
# ---------------------------------------------------------------------------


def test_headline_is_the_credits_actual_held_interest_not_held_to_term():
    """Regression for the owner's finding: 466642176 was repaid after 14 min
    but the fill model booked it as 2 days held-to-term."""
    now = REPAID + DAY
    report = compute_g3_report(credits=[EARLY], cells=_cells(EARLY),
                              market_points={"p2": _points(CREATED, "0.0002")},
                              capital=C, now_ms=now)
    fourteen_min = AMOUNT * RATE * Decimal(842_000) / MS_PER_DAY
    assert report.verdict.headline_bot_vs_idle == fourteen_min / C * Decimal("100")
    held_to_term = AMOUNT * RATE * Decimal("2") / C * Decimal("100")
    assert report.verdict.headline_bot_vs_idle * 200 < held_to_term
    assert report.coverage.bot_credits == 1
    assert report.coverage.gross_by_cell == {CELL: fourteen_min}


def test_unattributed_credits_are_reported_but_not_counted():
    now = EXPIRED_CLOSE + 3 * DAY
    report = compute_g3_report(credits=[EARLY, EXPIRED], cells=_cells(EARLY),
                              market_points={"p2": _points(CREATED, "0.0002")},
                              capital=C, now_ms=now)
    assert report.verdict.headline_bot_vs_idle == _held_interest(EARLY) / C * Decimal("100")
    assert report.coverage.unattributed_credits == 0   # EXPIRED opened before the window
    assert report.deployment.peak_open_principal == AMOUNT

    both_bot = compute_g3_report(credits=[EARLY, EXPIRED, _credit("x", CREATED, REPAID)],
                                cells=_cells(EARLY, EXPIRED),
                                market_points={}, capital=C, now_ms=now)
    assert both_bot.coverage.bot_credits == 2
    assert both_bot.coverage.unattributed_credits == 1
    assert both_bot.coverage.unattributed_gross == _held_interest(EARLY)
    assert both_bot.verdict.headline_bot_vs_idle == (
        _held_interest(EARLY) + _held_interest(EXPIRED)) / C * Decimal("100")


def test_other_symbols_credits_are_not_the_canary():
    fusd = _credit("7", CREATED, REPAID, symbol="fUSD")
    report = compute_g3_report(credits=[fusd], cells=_cells(fusd), market_points={},
                              capital=C, now_ms=REPAID + DAY)
    assert report.coverage.bot_credits == 0
    assert report.verdict.headline_bot_vs_idle == 0


def test_frr_scale_points_mark_mr_alpha_unavailable_not_unreliable():
    # bot-vs-idle (idle ≡ 0) needs no market-rate data: the band guard only
    # marks MR-alpha unavailable and leaves a caveat.
    report = compute_g3_report(credits=[EXPIRED], cells=_cells(EXPIRED),
                              market_points={"p2": _points(EXPIRED_OPEN, "1.1e-06")},
                              capital=C, now_ms=EXPIRED_CLOSE)
    assert report.verdict.state is VerdictState.INSUFFICIENT_DATA
    assert report.verdict.mr_alpha_available is False
    assert any("plausible per-day band" in r for r in report.verdict.reasons)


def test_legit_points_mr_alpha_available():
    report = compute_g3_report(credits=[EXPIRED], cells=_cells(EXPIRED),
                              market_points={"p2": _points(EXPIRED_OPEN, "0.0002")},
                              capital=C, now_ms=EXPIRED_CLOSE)
    assert not any("plausible per-day band" in r for r in report.verdict.reasons)
    assert report.verdict.mr_alpha_available is True
    assert report.verdict.state is VerdictState.INSUFFICIENT_DATA   # 1 window


def test_no_coverage_marks_mr_alpha_unavailable_primary_unblocked():
    # Credits spanning 2 weekly windows, no market points: the active arm runs
    # (headline > 0), MR alpha is unavailable with a coverage caveat.
    later = _credit("2", EXPIRED_OPEN + WEEK, EXPIRED_CLOSE + WEEK, rate=EXPIRED_RATE)
    report = compute_g3_report(credits=[EXPIRED, later], cells=_cells(EXPIRED, later),
                              market_points={}, capital=C,
                              now_ms=EXPIRED_CLOSE + WEEK)
    assert report.verdict.n_windows == 2
    assert report.verdict.mr_alpha_available is False
    assert report.verdict.headline_bot_vs_idle > 0
    assert any("no market-rate coverage" in r for r in report.verdict.reasons)


def test_empty_is_insufficient_no_crash():
    report = compute_g3_report(credits=[], cells=_cells(), market_points={},
                              capital=C, now_ms=REPAID)
    assert report.verdict.state is VerdictState.INSUFFICIENT_DATA
    assert report.data_window == "n/a"
    assert report.coverage.bot_credits == 0
    assert report.frr.available is False


def test_a_loan_and_the_credit_it_became_are_not_counted_twice():
    """credit_attribution's rule: a credit converted from a loan keeps the
    loan's MTS_OPENING but earns only from its own MTS_CREATE."""
    converted = CREATED + HOUR
    loan = CreditLifetime(credit_id="loan:1", symbol="fUST", amount=AMOUNT, rate=RATE, period_days=2,
                          mts_create=CREATED, opened_ms=CREATED, closed_ms=converted)
    credit = CreditLifetime(credit_id="2", symbol="fUST", amount=AMOUNT, rate=RATE,
                            period_days=2, mts_create=converted, opened_ms=CREATED,
                            closed_ms=CREATED + DAY)
    trade = TradeRecord(trade_id=1, symbol="fUST", mts_create=CREATED, offer_id="o1",
                        amount=AMOUNT, rate=RATE, period_days=2)
    cells = assign_cells([loan, credit], [trade], {"o1": CELL})
    report = compute_g3_report(credits=[loan, credit], cells=cells, market_points={},
                               capital=C, now_ms=CREATED + 2 * DAY)
    one_day = AMOUNT * RATE * Decimal(DAY) / MS_PER_DAY
    assert report.verdict.headline_bot_vs_idle == one_day / C * 100
    assert report.deployment.peak_open_principal == AMOUNT     # not 2 x AMOUNT
    assert report.data_window.startswith("2026-09-25")


def test_a_credit_split_between_cells_counts_its_share_in_each():
    """Two trades at one instant lead to different cells and one credit holds
    both amounts: credit_attribution splits it by amount, G3 follows."""
    trades = [TradeRecord(trade_id=1, symbol="fUST", mts_create=CREATED, offer_id="o1",
                          amount=Decimal("100"), rate=RATE, period_days=2),
              TradeRecord(trade_id=2, symbol="fUST", mts_create=CREATED, offer_id="o2",
                          amount=Decimal("300"), rate=RATE, period_days=2)]
    merged = _credit("5", CREATED, CREATED + DAY, amount=Decimal("400"))
    cells = assign_cells([merged], trades, {"o1": "fUST_p2", "o2": "fUST_a30"})
    assert cells.shares["5"] == {"fUST_p2": Decimal("0.25"), "fUST_a30": Decimal("0.75")}
    report = compute_g3_report(credits=[merged], cells=cells, market_points={},
                               capital=C, now_ms=CREATED + 2 * DAY)
    whole = Decimal("400") * RATE * Decimal(DAY) / MS_PER_DAY
    assert report.coverage.gross_by_cell == {"fUST_a30": whole * Decimal("0.75"),
                                             "fUST_p2": whole * Decimal("0.25")}
    assert report.coverage.bot_credits == 1
    assert report.verdict.headline_bot_vs_idle == whole / C * 100
    assert {c.cell: c.capital_share for c in report.mr_alpha_cells} == {
        "fUST_a30": Decimal("0.75"), "fUST_p2": Decimal("0.25")}


def test_capital_override_below_lent_principal_is_flagged_not_clamped():
    three = [_credit(str(i), CREATED, REPAID, amount=Decimal("300")) for i in range(3)]
    report = compute_g3_report(credits=three, cells=_cells(*three), market_points={},
                              capital=C, now_ms=REPAID + DAY, capital_source="--capital 570")
    assert report.deployment.peak_open_principal == Decimal("900")
    assert report.deployment.over_deployed is True
    # no clamp: the full interest of all three counts
    assert report.verdict.headline_bot_vs_idle == 3 * _held_interest(three[0]) / C * 100


# ---------------------------------------------------------------------------
# MR alpha per cell: each cell against the market rate of its own period
# ---------------------------------------------------------------------------

A30_TWIN = CreditLifetime(credit_id="9", symbol="fUST", amount=AMOUNT, rate=EXPIRED_RATE,
                          period_days=30, mts_create=EXPIRED_OPEN, opened_ms=EXPIRED_OPEN,
                          closed_ms=EXPIRED_CLOSE)
TWO_CELLS = CreditCells({EXPIRED.credit_id: "fUST_p2", A30_TWIN.credit_id: "fUST_a30"},
                        frozenset(), frozenset(), frozenset())
SPAN_DAYS = Decimal(EXPIRED_CLOSE - EXPIRED_OPEN) / MS_PER_DAY


def test_each_cell_is_compared_with_its_own_period_series():
    report = compute_g3_report(
        credits=[EXPIRED, A30_TWIN], cells=TWO_CELLS, capital=C, now_ms=EXPIRED_CLOSE,
        market_points={"p2": _points(EXPIRED_OPEN, "0.0002"),
                       "a30": _points(EXPIRED_OPEN, "0.0003")})
    rows = {c.cell: c for c in report.mr_alpha_cells}
    assert set(rows) == {"fUST_a30", "fUST_p2"}
    active = _held_interest(EXPIRED) / C * 100          # both cells earn the same
    half = Decimal("0.5")                                # equal capital-days
    assert rows["fUST_p2"].capital_share == half
    assert rows["fUST_p2"].spread == active - half * Decimal("0.0002") * SPAN_DAYS * 100
    assert rows["fUST_a30"].spread == active - half * Decimal("0.0003") * SPAN_DAYS * 100
    # total row = sum of the cells; covers all bot capital-days
    assert report.verdict.mr_alpha_spread == rows["fUST_p2"].spread + rows["fUST_a30"].spread
    assert report.mr_alpha_coverage == 1
    assert report.verdict.mr_alpha_available is True


def test_a_cell_without_its_series_is_unavailable_and_left_out_of_the_total():
    report = compute_g3_report(
        credits=[EXPIRED, A30_TWIN], cells=TWO_CELLS, capital=C, now_ms=EXPIRED_CLOSE,
        market_points={"p2": _points(EXPIRED_OPEN, "0.0002")})
    rows = {c.cell: c for c in report.mr_alpha_cells}
    assert rows["fUST_a30"].available is False
    assert "fUST/1h/a30" in (rows["fUST_a30"].reason or "")
    assert report.mr_alpha_coverage == Decimal("0.5")
    assert report.verdict.mr_alpha_spread == rows["fUST_p2"].spread
    assert any(r.startswith("MR-alpha fUST_a30: no market-rate coverage")
               for r in report.verdict.reasons)


# ---------------------------------------------------------------------------
# Minimum data: mean ledger wallet balance over the windows × 7 days
# ---------------------------------------------------------------------------


def _payout(ledger_id: int, mts: int, amount: Decimal, balance: str = "395.5") -> InterestPayment:
    return InterestPayment(ledger_id, "UST", "funding", mts, amount, Decimal(balance),
                           "Margin Funding Payment on wallet funding")


EXPIRED_NET = _held_interest(EXPIRED) * Decimal("0.85")
# EXPIRED's two days are paid on Wed 09-23 and Thu 09-24 ~01:30Z (and a sliver Fri)
_PAID_AT = [MON + 2 * DAY + 5_400_000, MON + 3 * DAY + 5_400_000]


def test_threshold_is_the_mean_ledger_balance_times_seven_even_with_explicit_capital():
    payments = [_payout(1, _PAID_AT[0], Decimal("0.05"), balance="395.55")]
    report = compute_g3_report(credits=[EXPIRED], cells=_cells(EXPIRED), market_points={},
                               capital=C, now_ms=EXPIRED_CLOSE, payments=payments,
                               capital_source="--capital 570")
    t = report.data_threshold
    assert t.minimum == Decimal("395.50") * 7          # not C × 7 = 3990
    assert "mean ledger wallet balance over the 1 evaluated windows" in t.basis
    assert t.capital_days == AMOUNT * Decimal(EXPIRED_CLOSE - EXPIRED_OPEN) / MS_PER_DAY


def test_threshold_falls_back_to_c_without_a_ledger():
    report = compute_g3_report(credits=[EXPIRED], cells=_cells(EXPIRED), market_points={},
                               capital=C, now_ms=EXPIRED_CLOSE)
    assert report.data_threshold.minimum == C * 7
    assert "no ledger payouts" in report.data_threshold.basis


# ---------------------------------------------------------------------------
# Ledger reconciliation gate: the most recent 8 settled weeks, operator acks
# ---------------------------------------------------------------------------

GATE_NOW = MON + 2 * WEEK          # gate = the 8 settled weeks ending with MON's week
LATER_NOW = MON + 10 * WEEK        # MON's week has aged out of the gate


def _week_of(report, week_start):  # type: ignore[no-untyped-def]
    return next(r for r in report.reconciliations if r.week_start_ms == week_start)


def test_matching_ledger_keeps_the_verdict_data_driven():
    payments = [_payout(1, _PAID_AT[0], EXPIRED_NET / 2), _payout(2, _PAID_AT[1], EXPIRED_NET / 2)]
    report = compute_g3_report(credits=[EXPIRED], cells=_cells(EXPIRED), market_points={},
                               capital=C, now_ms=GATE_NOW, payments=payments)
    assert report.reconciliation_available is True
    assert report.gate_weeks == [MON - i * WEEK for i in reversed(range(8))]
    week = _week_of(report, MON)
    assert (week.complete, week.flagged) == (True, False)
    assert week.credit_net == EXPIRED_NET
    assert report.verdict.state is VerdictState.INSUFFICIENT_DATA


def test_recent_ledger_divergence_forces_unreliable():
    """Credit interest the venue did not pay (or paid differently) in a recent
    settled week means the model is wrong: UNRELIABLE, whatever the CI says."""
    payments = [_payout(1, _PAID_AT[0], Decimal("0.10"))]
    report = compute_g3_report(credits=[EXPIRED], cells=_cells(EXPIRED), market_points={},
                               capital=C, now_ms=GATE_NOW, payments=payments)
    assert report.verdict.state is VerdictState.UNRELIABLE
    assert any("diverges from the venue ledger" in r and "2026-09-21" in r
               for r in report.verdict.reasons)


def test_a_flag_older_than_the_gate_no_longer_blocks():
    payments = [_payout(1, _PAID_AT[0], Decimal("0.10"))]
    report = compute_g3_report(credits=[EXPIRED], cells=_cells(EXPIRED), market_points={},
                               capital=C, now_ms=LATER_NOW, payments=payments)
    assert MON not in report.gate_weeks
    assert _week_of(report, MON).flagged is True
    assert report.verdict.state is not VerdictState.UNRELIABLE
    assert reconciliation_status(_week_of(report, MON), gate_weeks=report.gate_weeks,
                                 acks={}) == "FLAG, outside gate"


def test_an_operator_ack_clears_a_recent_flag_and_is_recorded():
    payments = [_payout(1, _PAID_AT[0], Decimal("0.10"))]
    acks = {MON: "venue paid a late correction; checked ledger 2026-09-30"}
    report = compute_g3_report(credits=[EXPIRED], cells=_cells(EXPIRED), market_points={},
                               capital=C, now_ms=GATE_NOW, payments=payments, acks=acks)
    assert report.verdict.state is not VerdictState.UNRELIABLE
    assert report.acknowledgements == acks
    assert reconciliation_status(_week_of(report, MON), gate_weeks=report.gate_weeks,
                                 acks=acks) == f"FLAG, acknowledged: {acks[MON]}"


def test_incomplete_week_does_not_gate():
    payments = [_payout(1, _PAID_AT[0], Decimal("0.10"))]
    report = compute_g3_report(credits=[EXPIRED], cells=_cells(EXPIRED), market_points={},
                               capital=C, now_ms=EXPIRED_CLOSE + DAY, payments=payments)
    assert _week_of(report, MON).complete is False
    assert report.verdict.state is not VerdictState.UNRELIABLE


def test_no_ledger_makes_reconciliation_unavailable_not_unreliable():
    report = compute_g3_report(credits=[EXPIRED], cells=_cells(EXPIRED), market_points={},
                               capital=C, now_ms=GATE_NOW)
    assert report.reconciliation_available is False
    assert report.reconciliations == []
    assert report.verdict.state is VerdictState.INSUFFICIENT_DATA
    assert any("ledger reconciliation unavailable" in r for r in report.verdict.reasons)


# ---------------------------------------------------------------------------
# build_g3_report wiring (seeded in-memory sqlite, session-injected).
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


_ACCOUNT = UUID("35efed2d-3004-4941-a161-ca025d9c4d53")


def _history_row(credit_id: int, opened: int, closed: int, rate: Decimal) -> FundingCreditHistoryRow:
    return FundingCreditHistoryRow(
        exchange_account_id=_ACCOUNT, kind="credit", credit_id=credit_id,
        deployment_environment="prod", symbol="fUST", side=1, mts_create=opened,
        mts_update=opened, amount=AMOUNT, status="CLOSED", rate=rate, period_days=2,
        mts_opening=opened, mts_last_payout=closed,
    )


def _ours(credit_id: int, opened: int, cell: str, rate: Decimal = RATE) -> list[object]:
    """Trade → our offer → execution decision in `cell`, so the credit is the bot's."""
    return [
        FundingTradeRow(
            exchange_account_id=_ACCOUNT, trade_id=credit_id, deployment_environment="prod",
            symbol="fUST", mts_create=opened, offer_id=credit_id, amount=AMOUNT,
            rate=rate, period_days=2, maker=None,
        ),
        OfferClaimRow(
            cid=credit_id, account_id=str(_ACCOUNT), exchange_account_id=_ACCOUNT,
            deployment_environment="prod", state="FILLED", venue_offer_id=str(credit_id),
            symbol="fUST", size_usdt=AMOUNT, signal_correlation_id=f"s{credit_id}",
            execution_decision_id=f"d{credit_id}", occurred_at_ms=opened,
            last_updated_ms=opened, last_event_seq=1,
        ),
        ExecutionDecisionRow(
            decision_id=f"d{credit_id}", account_id=str(_ACCOUNT), exchange_account_id=_ACCOUNT,
            deployment_environment="prod", reconcile_id="r", cell_id=cell, symbol="fUST",
            signal_correlation_id=f"s{credit_id}", outcome="submitted", signal_rate=rate,
            amount_usdt=AMOUNT, duration_days=2, model_evidence={}, safety_result={},
            execution_policy="p", service_version="v", config_hash="h",
            occurred_at_ms=opened - 60_000, recorded_at_ms=opened - 60_000,
        ),
    ]


@pytest.mark.asyncio
async def test_build_reads_only_the_cells_own_fust_1h_series(g3_factory, monkeypatch):
    """A p2 bot cell must be compared with fUST/1h/p2 candles only; decoys with
    another symbol / period_agg / timeframe are excluded. Proven via the band
    guard: the target series is frr-scale (~1e-6), every decoy legit (2e-4)."""
    monkeypatch.setenv("BFX_EXCHANGE_ACCOUNT_ID", str(_ACCOUNT))
    monkeypatch.setenv("BFX_DEPLOYMENT_ENV", "prod")
    now = _real_now()  # candles are final only once in the past
    base = now - 5 * DAY
    target = [_recent_candle("fUST", "1h", "p2", base + i * HOUR, "1.1e-6") for i in range(12)]
    decoys = (
        [_recent_candle("fUST", "1h", "a30", base + i * HOUR, "0.0002") for i in range(12)]
        + [_recent_candle("fUSD", "1h", "p2", base + i * HOUR, "0.0002") for i in range(12)]
        + [_recent_candle("fUST", "15m", "p2", base + i * HOUR, "0.0002") for i in range(12)]
    )
    async with g3_factory() as s:
        await upsert_candles(s, target + decoys)
        s.add_all([_history_row(1, base - HOUR, base + DAY, RATE),
                   *_ours(1, base - HOUR, "fUST_p2")])
        await s.commit()

    report = await build_g3_report(capital=C, session_factory=g3_factory, now_ms=now)
    assert [c.cell for c in report.mr_alpha_cells] == ["fUST_p2"]
    assert report.mr_alpha_cells[0].available is False
    assert "plausible per-day band" in (report.mr_alpha_cells[0].reason or "")
    assert report.verdict.mr_alpha_available is False
    assert report.verdict.state is VerdictState.INSUFFICIENT_DATA
    assert report.frr.available is False     # no funding_stats seeded


@pytest.mark.asyncio
async def test_build_loads_one_series_per_bot_cell(g3_factory, monkeypatch):
    monkeypatch.setenv("BFX_EXCHANGE_ACCOUNT_ID", str(_ACCOUNT))
    monkeypatch.setenv("BFX_DEPLOYMENT_ENV", "prod")
    now = _real_now()
    base = now - 5 * DAY
    candles = ([_recent_candle("fUST", "1h", "p2", base + i * HOUR, "0.0002") for i in range(12)]
               + [_recent_candle("fUST", "1h", "a30", base + i * HOUR, "0.0003")
                  for i in range(12)])
    async with g3_factory() as s:
        await upsert_candles(s, candles)
        s.add_all([_history_row(1, base - HOUR, base + DAY, RATE), *_ours(1, base - HOUR, "fUST_p2"),
                   _history_row(2, base - HOUR, base + DAY, RATE), *_ours(2, base - HOUR, "fUST_a30")])
        await s.commit()

    report = await build_g3_report(capital=C, session_factory=g3_factory, now_ms=now)
    assert {(c.cell, c.available) for c in report.mr_alpha_cells} == {
        ("fUST_a30", True), ("fUST_p2", True)}
    assert report.mr_alpha_coverage == 1
    assert report.capital_source == f"--capital {C}"


@pytest.mark.asyncio
async def test_build_without_bot_credits_is_insufficient_not_crash(g3_factory):
    report = await build_g3_report(capital=C, session_factory=g3_factory, now_ms=_real_now())
    assert report.verdict.state is VerdictState.INSUFFICIENT_DATA
    assert report.verdict.mr_alpha_available is False
    assert report.mr_alpha_cells == []


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

    report = await build_g3_report(capital=C, session_factory=g3_factory, now_ms=now)
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
        await build_g3_report(capital=C, session_factory=g3_factory, now_ms=REPAID)
