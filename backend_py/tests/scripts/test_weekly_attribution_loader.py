from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

import bfx_funding_bot.modules.execution.diagnostics.tables
import bfx_funding_bot.modules.execution.event_store.tables
import bfx_funding_bot.modules.funding_stats.tables
import bfx_funding_bot.modules.live_validation.tables  # noqa: F401
from bfx_funding_bot.core.db import Base
from bfx_funding_bot.modules.execution.diagnostics.tables import DiagnosticsRow
from bfx_funding_bot.modules.execution.event_store.tables import EventLogRow
from bfx_funding_bot.modules.live_validation.tables import AttributionWeeklyRow
from bfx_funding_bot.modules.live_validation.weekly_attribution import WeeklyCellRow
from scripts.run_weekly_attribution import load_and_compute, persist_rows

_MON = 1_782_691_200_000  # 2026-06-29 UTC Monday
_ENV = "prod"
_ACCT = "default"


@pytest_asyncio.fixture
async def sf(sqlite_engine):
    async with sqlite_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return async_sessionmaker(sqlite_engine, expire_on_commit=False)


def _fill_event(scid: str, voi: str, ts: int) -> EventLogRow:
    return EventLogRow(
        account_id=_ACCT, deployment_environment=_ENV, event_type="ORDER_FILL",
        venue_offer_id=voi,
        payload={
            "symbol": "fUST", "signal_correlation_id": scid,
            "size_usdt": "500", "fill_rate": 0.0002, "venue_offer_id": voi,
        },
        occurred_at_ms=ts,
    )


def _decision_row(scid: str, cell: str) -> DiagnosticsRow:
    return DiagnosticsRow(
        account_id=_ACCT, deployment_environment=_ENV, kind="decision",
        payload={"cell": cell, "correlation_id": scid, "event_type": "DECISION"},
        occurred_at=datetime.now(UTC),
    )


async def test_fills_joined_to_cell_via_decision_diagnostics(sf):
    scid = str(uuid4())
    async with sf() as s:
        s.add(_fill_event(scid, "42", _MON + 1000))
        s.add(_decision_row(scid, "fUST_p2"))
        await s.commit()
    rows = await load_and_compute(sf, account_id=_ACCT, deployment_environment=_ENV)
    cells = {r.cell for r in rows}
    assert "fUST_p2" in cells
    p2 = next(r for r in rows if r.cell == "fUST_p2" and r.n_fills == 1)
    assert p2.gross_interest_usdt == Decimal("500") * Decimal("0.0002") * Decimal("2")


async def test_unjoinable_fill_lands_in_unattributed(sf):
    async with sf() as s:
        s.add(_fill_event(str(uuid4()), "43", _MON + 1000))  # 無對應 DECISION row
        await s.commit()
    rows = await load_and_compute(sf, account_id=_ACCT, deployment_environment=_ENV)
    assert any(r.cell == "unattributed" and r.n_fills == 1 for r in rows)


async def test_p30_cell_uses_conservative_period_not_crash(sf):
    # cell_period_days 不認 p30 → _period_for_cell fail-soft 回 p2=2d，不炸整個 job
    scid = str(uuid4())
    async with sf() as s:
        s.add(_fill_event(scid, "46", _MON + 1000))
        s.add(_decision_row(scid, "fUST_p30"))
        await s.commit()
    rows = await load_and_compute(sf, account_id=_ACCT, deployment_environment=_ENV)
    p30 = next(r for r in rows if r.cell == "fUST_p30" and r.n_fills == 1)
    # 保守 p2=2d：500 × 0.0002 × 2 = 0.2
    assert p30.gross_interest_usdt == Decimal("500") * Decimal("0.0002") * Decimal("2")


async def test_persist_rows_upserts_idempotently(sf):
    scid = str(uuid4())
    async with sf() as s:
        s.add(_fill_event(scid, "44", _MON + 1000))
        s.add(_decision_row(scid, "fUST_p2"))
        await s.commit()
    rows = await load_and_compute(sf, account_id=_ACCT, deployment_environment=_ENV)
    n1 = await persist_rows(sf, rows, account_id=_ACCT, deployment_environment=_ENV)
    n2 = await persist_rows(sf, rows, account_id=_ACCT, deployment_environment=_ENV)
    assert n1 == n2 == len(rows)
    async with sf() as s:
        db_rows = (await s.execute(select(AttributionWeeklyRow))).scalars().all()
    assert len(db_rows) == len(rows)  # 重跑不重複


async def test_release_shortens_duration(sf):
    scid = str(uuid4())
    one_day = 24 * 60 * 60 * 1000
    async with sf() as s:
        s.add(_fill_event(scid, "45", _MON + 1000))
        s.add(_decision_row(scid, "fUST_p2"))
        s.add(EventLogRow(
            account_id=_ACCT, deployment_environment=_ENV,
            event_type="RESERVATION_RELEASED", venue_offer_id="45",
            payload={"venue_offer_id": "45"}, occurred_at_ms=_MON + 1000 + one_day,
        ))
        await s.commit()
    rows = await load_and_compute(sf, account_id=_ACCT, deployment_environment=_ENV)
    p2 = next(r for r in rows if r.cell == "fUST_p2")
    # duration 被 release 截到 1 天
    assert p2.gross_interest_usdt == Decimal("500") * Decimal("0.0002") * Decimal("1")


def _wcr(cell: str, wk: int = _MON) -> WeeklyCellRow:
    return WeeklyCellRow(
        cell=cell, week_start_ms=wk, week_end_ms=wk + 604_800_000, n_fills=1,
        gross_interest_usdt=Decimal("0.2"), net_interest_usdt=Decimal("0.17"),
        capital_days=Decimal("1000"), realized_apr_net_pct=Decimal("6.205"),
        baseline_close_apr_net_pct=None, baseline_frr_apr_net_pct=None,
    )


async def test_persist_rows_replaces_stale_rows(sf):
    # full-recompute replace：diagnostics pruning 把某 fill 重歸 unattributed 後，
    # 舊 cell 的 row 不可殘留（merge-only 會漏；delete+insert 修掉）。
    await persist_rows(
        sf, [_wcr("fUST_p2"), _wcr("unattributed")],
        account_id=_ACCT, deployment_environment=_ENV,
    )
    n = await persist_rows(
        sf, [_wcr("fUST_p2")],  # 第二次少了 unattributed
        account_id=_ACCT, deployment_environment=_ENV,
    )
    assert n == 1
    async with sf() as s:
        db_rows = (await s.execute(select(AttributionWeeklyRow))).scalars().all()
    assert {r.cell for r in db_rows} == {"fUST_p2"}  # stale "unattributed" 已消失


async def test_persist_rows_replace_is_realm_scoped(sf):
    # delete 以 (env,account) 為界 — 不可波及其他 realm 的 rows。
    async with sf() as s:
        s.add(AttributionWeeklyRow(
            deployment_environment="other", account_id=_ACCT, cell="fUST_p2",
            week_start_ms=_MON, week_end_ms=_MON + 604_800_000, n_fills=9,
            gross_interest_usdt=Decimal("1"), net_interest_usdt=Decimal("1"),
            capital_days=Decimal("1"), realized_apr_net_pct=None,
            baseline_close_apr_net_pct=None, baseline_frr_apr_net_pct=None,
        ))
        await s.commit()
    await persist_rows(
        sf, [_wcr("fUST_p2")], account_id=_ACCT, deployment_environment=_ENV,
    )
    async with sf() as s:
        others = (await s.execute(
            select(AttributionWeeklyRow).where(
                AttributionWeeklyRow.deployment_environment == "other"
            )
        )).scalars().all()
    assert len(others) == 1  # 別的 realm 不受影響
