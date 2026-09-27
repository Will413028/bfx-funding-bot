"""run_weekly_attribution: per-cell interest from stored venue credits, the
offer → cell resolution, the ledger reconciliation, and the replace semantics.

Credit 466642176 / trade 432914136 / offer 5123273052 are the live account's
(2026-09-25: 150.76884612 fUST at 0.00019999, repaid after 842 s)."""
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

import bfx_funding_bot.modules.accounts.tables
import bfx_funding_bot.modules.execution.audit.tables
import bfx_funding_bot.modules.execution.diagnostics.tables
import bfx_funding_bot.modules.execution.event_store.tables
import bfx_funding_bot.modules.funding_stats.tables
import bfx_funding_bot.modules.live_validation.tables  # noqa: F401
from bfx_funding_bot.core.db import Base
from bfx_funding_bot.modules.execution.audit.tables import ExecutionDecisionRow
from bfx_funding_bot.modules.execution.diagnostics.tables import DiagnosticsRow
from bfx_funding_bot.modules.execution.event_store.tables import (
    EventLogRow,
    OfferClaimRow,
    VenueCreditStateRow,
)
from bfx_funding_bot.modules.funding_stats.tables import FundingStatRow
from bfx_funding_bot.modules.live_validation.tables import (
    AttributionWeeklyRow,
    FundingCreditHistoryRow,
    FundingInterestPaymentRow,
    FundingTradeRow,
)
from bfx_funding_bot.modules.live_validation.weekly_attribution import WeeklyCellRow
from scripts.run_weekly_attribution import (
    OfferLink,
    load_and_compute,
    persist_rows,
    reconciliation_weeks,
    render_reconciliation,
    resolve_offer_cells,
)

_ENV = "prod"
_UUID = UUID("35efed2d-3004-4941-a161-ca025d9c4d53")
_ACCT = str(_UUID)
_LEGACY = "default"
_MON = 1_789_948_800_000          # 2026-09-21 UTC Monday
_DAY = 86_400_000
AMOUNT = Decimal("150.76884612")
RATE = Decimal("0.00019999")
CREATED = 1_790_350_246_000       # 2026-09-25 15:30:46Z
REPAID = 1_790_351_088_000        # + 842 s
OFFER = 5_123_273_052
NOW = _MON + 3 * 7 * _DAY


@pytest_asyncio.fixture
async def sf(sqlite_engine):
    async with sqlite_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return async_sessionmaker(sqlite_engine, expire_on_commit=False)


def _credit(credit_id: int = 466642176, *, amount: Decimal = AMOUNT, kind: str = "credit",
            closed: int | None = REPAID) -> FundingCreditHistoryRow:
    return FundingCreditHistoryRow(
        exchange_account_id=_UUID, kind=kind, credit_id=credit_id, deployment_environment=_ENV,
        symbol="fUST", side=1, mts_create=CREATED, mts_update=CREATED, amount=amount,
        status="CLOSED", rate=RATE, period_days=2, mts_opening=CREATED, mts_last_payout=closed,
    )


def _trade(trade_id: int = 432914136, offer_id: int = OFFER) -> FundingTradeRow:
    return FundingTradeRow(
        exchange_account_id=_UUID, trade_id=trade_id, deployment_environment=_ENV,
        symbol="fUST", mts_create=CREATED, offer_id=offer_id, amount=AMOUNT, rate=RATE,
        period_days=2, maker=None,
    )


def _decision(decision_id: str, cell: str, scid: str = "scid-x") -> ExecutionDecisionRow:
    return ExecutionDecisionRow(
        decision_id=decision_id, account_id=_ACCT, exchange_account_id=_UUID,
        deployment_environment=_ENV, reconcile_id="r", cell_id=cell, symbol="fUST",
        signal_correlation_id=scid, outcome="submitted", signal_rate=RATE,
        amount_usdt=AMOUNT, duration_days=2, model_evidence={}, safety_result={},
        execution_policy="p", service_version="v", config_hash="h",
        occurred_at_ms=CREATED - 60_000, recorded_at_ms=CREATED - 60_000,
    )


def _claim(voi: str, decision_id: str | None) -> OfferClaimRow:
    return OfferClaimRow(
        cid=1, account_id=_ACCT, exchange_account_id=_UUID, deployment_environment=_ENV,
        state="FILLED", venue_offer_id=voi, symbol="fUST", size_usdt=AMOUNT,
        signal_correlation_id="scid-x", execution_decision_id=decision_id,
        occurred_at_ms=CREATED, last_updated_ms=CREATED, last_event_seq=1,
    )


GROSS_842S = AMOUNT * RATE * Decimal(842_000) / Decimal(_DAY)


async def test_credit_attributed_through_trade_offer_and_execution_decision(sf):
    async with sf() as s:
        s.add_all([_credit(), _trade(), _claim(str(OFFER), "d1"), _decision("d1", "fUST_p2")])
        await s.commit()
    result = await load_and_compute(sf, account_id=_ACCT, deployment_environment=_ENV, now_ms=NOW)
    row = next(r for r in result.rows if r.cell == "fUST_p2" and r.week_start_ms == _MON)
    assert row.n_fills == 1
    assert row.gross_interest_usdt == GROSS_842S          # 842 s, not 0 or 2 days
    assert row.net_interest_usdt == GROSS_842S * Decimal("0.85")


async def test_fill_signal_correlation_falls_back_to_diagnostics_decision(sf):
    async with sf() as s:
        s.add_all([_credit(), _trade()])
        s.add(EventLogRow(
            account_id=_ACCT, exchange_account_id=_UUID, deployment_environment=_ENV,
            event_type="ORDER_FILL", venue_offer_id=str(OFFER),
            payload={"venue_offer_id": str(OFFER), "signal_correlation_id": "scid-old"},
            occurred_at_ms=CREATED,
        ))
        s.add(DiagnosticsRow(
            account_id=_ACCT, exchange_account_id=_UUID, deployment_environment=_ENV,
            kind="decision", payload={"cell": "fUST_a30", "correlation_id": "scid-old"},
            occurred_at=datetime.now(UTC),
        ))
        await s.commit()
    result = await load_and_compute(sf, account_id=_ACCT, deployment_environment=_ENV, now_ms=NOW)
    assert {r.cell for r in result.rows if r.n_fills} == {"fUST_a30"}


async def test_unmatched_credit_and_open_credit_are_unattributed_and_accrue(sf):
    async with sf() as s:
        s.add(_credit(1, amount=Decimal("99")))            # no trade
        s.add(VenueCreditStateRow(
            exchange_account_id=_UUID, deployment_environment=_ENV, credit_id="loan:7",
            symbol="fUST", amount=Decimal("100"), rate=Decimal("0.0002"), period_days=2,
            status="ACTIVE", flags={}, mts_created=NOW - _DAY, mts_updated=NOW - _DAY,
            first_seen_event_seq=1, last_seen_event_seq=1, is_terminal=False,
        ))
        await s.commit()
    result = await load_and_compute(sf, account_id=_ACCT, deployment_environment=_ENV, now_ms=NOW)
    assert result.cells is not None and result.cells.without_trade == {"1", "loan:7"}
    open_week = next(r for r in result.rows if r.week_start_ms == NOW - 7 * _DAY and r.n_fills)
    assert open_week.cell == "unattributed"
    assert open_week.gross_interest_usdt == Decimal("100") * Decimal("0.0002")   # one day so far


async def test_reconciliation_compares_the_shifted_ledger_week(sf):
    async with sf() as s:
        s.add_all([_credit(), _trade()])
        # the payout for Friday 09-25 lands Saturday ~01:30Z
        s.add(FundingInterestPaymentRow(
            exchange_account_id=_UUID, ledger_id=1, deployment_environment=_ENV,
            currency="UST", mts=CREATED + _DAY // 2, amount=Decimal("0.00025"),
            balance=Decimal("395"), description="Margin Funding Payment on wallet funding",
        ))
        await s.commit()
    result = await load_and_compute(sf, account_id=_ACCT, deployment_environment=_ENV,
                                    now_ms=NOW, reconcile_weeks=2)
    week = next(r for r in result.reconciliations if r.week_start_ms == _MON)
    assert week.payouts == 1 and week.complete
    assert week.credit_net == GROSS_842S * Decimal("0.85")
    assert not week.flagged                   # |diff| under the 0.01 floor
    assert "| UST | 2026-09-21 |" in render_reconciliation(result)


def test_reconciliation_weeks_wait_for_tuesday_payouts():
    monday_run = _MON + 7 * _DAY + 4 * 3_600_000       # Mon 04:00Z after the week
    assert reconciliation_weeks(monday_run, 1) == [_MON - 7 * _DAY]
    assert reconciliation_weeks(monday_run + 2 * _DAY, 1) == [_MON]


def test_audited_decision_wins_over_signal_correlation():
    links = [OfferLink("1", None, "s"), OfferLink("1", "d", None), OfferLink("2", None, "s")]
    cells = resolve_offer_cells(links, cell_by_decision={"d": "fUST_p2"},
                                cell_by_scid={"s": "fUST_a30"})
    assert cells == {"1": "fUST_p2", "2": "fUST_a30"}


async def test_no_credit_history_reports_it(sf):
    result = await load_and_compute(sf, account_id=_ACCT, deployment_environment=_ENV, now_ms=NOW)
    assert not result.has_credit_history and result.rows == []
    legacy = await load_and_compute(sf, account_id=_LEGACY, deployment_environment=_ENV)
    assert not legacy.has_credit_history


async def test_loader_builds_utilization_points_from_funding_stats(sf):
    """funding_amount(_used) present → utilization point used/total; missing or
    zero total → no point for that snapshot (None column ≠ 0 utilization)."""
    async with sf() as s:
        s.add(_credit())
        # 三筆 snapshot：(1000, 800) → 0.8；(None, 500) → skip；(0, 0) → skip。週均 = 0.8。
        s.add(FundingStatRow(symbol="fUST", mts=CREATED + 2000, frr=0.0002, avg_period=2.0,
                             funding_amount=1000.0, funding_amount_used=800.0))
        s.add(FundingStatRow(symbol="fUST", mts=CREATED + 3000, frr=0.0002, avg_period=2.0,
                             funding_amount=None, funding_amount_used=500.0))
        s.add(FundingStatRow(symbol="fUST", mts=CREATED + 4000, frr=0.0002, avg_period=2.0,
                             funding_amount=0.0, funding_amount_used=0.0))
        await s.commit()
    result = await load_and_compute(sf, account_id=_ACCT, deployment_environment=_ENV, now_ms=NOW)
    row = next(r for r in result.rows if r.baseline_frr_apr_net_pct is not None)
    assert row.baseline_frr_util_apr_net_pct == row.baseline_frr_apr_net_pct * Decimal("0.8")


def _wcr(cell: str, wk: int = _MON) -> WeeklyCellRow:
    return WeeklyCellRow(
        cell=cell, week_start_ms=wk, week_end_ms=wk + 604_800_000, n_fills=1,
        gross_interest_usdt=Decimal("0.2"), net_interest_usdt=Decimal("0.17"),
        capital_days=Decimal("1000"), realized_apr_net_pct=Decimal("6.205"),
        baseline_close_apr_net_pct=None, baseline_frr_apr_net_pct=None,
        baseline_frr_util_apr_net_pct=None,
    )


async def test_persist_rows_upserts_idempotently(sf):
    rows = [_wcr("fUST_p2"), _wcr("unattributed")]
    n1 = await persist_rows(sf, rows, account_id=_LEGACY, deployment_environment=_ENV)
    n2 = await persist_rows(sf, rows, account_id=_LEGACY, deployment_environment=_ENV)
    assert n1 == n2 == 2
    async with sf() as s:
        db_rows = (await s.execute(select(AttributionWeeklyRow))).scalars().all()
    assert len(db_rows) == 2  # 重跑不重複


async def test_persist_rows_replaces_stale_rows(sf):
    # full-recompute replace：某 credit 重歸 unattributed 後，舊 cell 的 row 不可殘留。
    await persist_rows(
        sf, [_wcr("fUST_p2"), _wcr("unattributed")],
        account_id=_LEGACY, deployment_environment=_ENV,
    )
    n = await persist_rows(
        sf, [_wcr("fUST_p2")], account_id=_LEGACY, deployment_environment=_ENV,
    )
    assert n == 1
    async with sf() as s:
        db_rows = (await s.execute(select(AttributionWeeklyRow))).scalars().all()
    assert {r.cell for r in db_rows} == {"fUST_p2"}


async def test_persist_rows_replace_is_realm_scoped(sf):
    # delete 以 (env,account) 為界 — 不可波及其他 realm 的 rows。
    async with sf() as s:
        s.add(AttributionWeeklyRow(
            deployment_environment="other", account_id=_LEGACY, cell="fUST_p2",
            week_start_ms=_MON, week_end_ms=_MON + 604_800_000, n_fills=9,
            gross_interest_usdt=Decimal("1"), net_interest_usdt=Decimal("1"),
            capital_days=Decimal("1"), realized_apr_net_pct=None,
            baseline_close_apr_net_pct=None, baseline_frr_apr_net_pct=None,
        ))
        await s.commit()
    await persist_rows(
        sf, [_wcr("fUST_p2")], account_id=_LEGACY, deployment_environment=_ENV,
    )
    async with sf() as s:
        others = (await s.execute(
            select(AttributionWeeklyRow).where(
                AttributionWeeklyRow.deployment_environment == "other"
            )
        )).scalars().all()
    assert len(others) == 1
