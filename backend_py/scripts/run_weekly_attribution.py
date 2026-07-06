"""E3 (b) — per-cell weekly fee-adjusted realized APR → attribution_weekly 表。

Read-only over event_log/diagnostics/funding_candles/funding_stats；唯一寫入
= attribution_weekly（全量重算 + upsert，event_log 是 SoT、本表是 read model）。
cell identity：ORDER_FILL.signal_correlation_id → diagnostics kind='decision'
payload.correlation_id → payload.cell。diagnostics 是 best-effort/prunable —
join 不到的 fills 歸 "unattributed"（保守 p2 period），絕不丟棄。

Run from backend_py/ (env: DATABASE_URL / BFX_ACCOUNT_ID / BFX_DEPLOYMENT_ENV):
  uv run python -m scripts.run_weekly_attribution
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
from decimal import Decimal

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bfx_funding_bot.core.db import make_engine, make_session_factory
from bfx_funding_bot.core.settings import Settings
from bfx_funding_bot.modules.candles.repository import get_candles_in_range
from bfx_funding_bot.modules.execution.diagnostics.tables import DiagnosticsRow
from bfx_funding_bot.modules.execution.event_store.tables import EventLogRow
from bfx_funding_bot.modules.funding_stats.schemas import FundingStat
from bfx_funding_bot.modules.funding_stats.tables import FundingStatRow
from bfx_funding_bot.modules.live_validation.live_attribution import (
    FillRecord,
    MarketRatePoint,
    cell_period_days,
    frr_points_from_stats,
)
from bfx_funding_bot.modules.live_validation.tables import AttributionWeeklyRow
from bfx_funding_bot.modules.live_validation.weekly_attribution import (
    WeeklyCellRow,
    compute_weekly_rows,
)

log = logging.getLogger(__name__)

_FILL_TYPE = "ORDER_FILL"
_RELEASE_TYPE = "RESERVATION_RELEASED"
_DECISION_KIND = "decision"
_MARKET_SYMBOL = "fUST"
_MARKET_TIMEFRAME = "1h"
_MARKET_PERIOD_AGG = "p2"
_UNATTRIBUTED = "unattributed"


def _period_for_cell(cell: str, frr_avg_period: Decimal) -> Decimal:
    """cell id 形如 '<symbol>_<period_agg>'（fUST_p2 / fUST_a30 / fUST_p30 …）。

    cell_period_days 只認 p2/a30，對 p30（shadow realm cells.yaml 有）、
    未來 p7/p14 會 raise ValueError。這裡 fail-soft → 保守 p2（G3 同慣例）+ log，
    絕不讓一筆 p30 fill 炸掉整個 weekly sweep（那會重演「量測斷線」的問題）。
    """
    if cell == _UNATTRIBUTED:
        return Decimal("2")
    period_agg = cell.rsplit("_", 1)[-1]
    try:
        return cell_period_days(period_agg, frr_avg_period)
    except ValueError:
        log.warning(
            "unknown period_agg %r for cell %r; using conservative p2=2d",
            period_agg, cell,
        )
        return Decimal("2")


async def load_and_compute(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    account_id: str,
    deployment_environment: str,
) -> list[WeeklyCellRow]:
    async with session_factory() as session:
        fill_rows = (
            await session.execute(
                select(EventLogRow)
                .where(
                    EventLogRow.event_type == _FILL_TYPE,
                    EventLogRow.account_id == account_id,
                    EventLogRow.deployment_environment == deployment_environment,
                )
                .order_by(EventLogRow.occurred_at_ms)
            )
        ).scalars().all()
        release_rows = (
            await session.execute(
                select(EventLogRow).where(
                    EventLogRow.event_type == _RELEASE_TYPE,
                    EventLogRow.account_id == account_id,
                    EventLogRow.deployment_environment == deployment_environment,
                )
            )
        ).scalars().all()
        decision_rows = (
            await session.execute(
                select(DiagnosticsRow).where(
                    DiagnosticsRow.kind == _DECISION_KIND,
                    DiagnosticsRow.account_id == account_id,
                    DiagnosticsRow.deployment_environment == deployment_environment,
                )
            )
        ).scalars().all()

        if not fill_rows:
            return []
        min_ts = fill_rows[0].occurred_at_ms
        max_ts = fill_rows[-1].occurred_at_ms + 1

        candles = await get_candles_in_range(
            session, symbol=_MARKET_SYMBOL, timeframe=_MARKET_TIMEFRAME,
            period_agg=_MARKET_PERIOD_AGG, start_mts=min_ts, end_mts=max_ts,
        )
        frr_rows = (
            await session.execute(
                select(FundingStatRow)
                .where(
                    FundingStatRow.symbol == _MARKET_SYMBOL,
                    FundingStatRow.mts >= min_ts,
                    FundingStatRow.mts <= max_ts,
                )
                .order_by(FundingStatRow.mts)
            )
        ).scalars().all()

    scid_to_cell = {
        str(d.payload.get("correlation_id")): str(d.payload.get("cell"))
        for d in decision_rows
        if d.payload.get("correlation_id") and d.payload.get("cell")
    }
    release_map = {
        str(r.payload.get("venue_offer_id") or r.venue_offer_id or ""): r.occurred_at_ms
        for r in release_rows
    }
    frr_stats = [
        FundingStat(
            symbol=r.symbol, mts=r.mts,
            frr=Decimal(str(r.frr)) if r.frr is not None else None,
            avg_period=Decimal(str(r.avg_period)) if r.avg_period is not None else None,
        )
        for r in frr_rows
    ]
    latest_avg_period = next(
        (s.avg_period for s in reversed(frr_stats) if s.avg_period is not None),
        Decimal("30"),
    )

    fills_by_cell: dict[str, list[FillRecord]] = {}
    for row in fill_rows:
        payload = row.payload
        scid = str(payload.get("signal_correlation_id") or "")
        cell = scid_to_cell.get(scid, _UNATTRIBUTED)
        voi = str(payload.get("venue_offer_id") or row.venue_offer_id or "")
        # Decimal(str(float)) 防科學記號（live executor 踩坑 ae2c59d 同慣例）
        fills_by_cell.setdefault(cell, []).append(FillRecord(
            venue_offer_id=voi,
            fill_ts_ms=row.occurred_at_ms,
            size_usdt=Decimal(str(payload.get("size_usdt", "0"))),
            rate=Decimal(str(payload.get("fill_rate", "0"))),
            period_days=_period_for_cell(cell, latest_avg_period),
            release_ts_ms=release_map.get(voi),
        ))

    close_points = [
        MarketRatePoint(mts=c.mts, rate=c.close)
        for c in candles if c.close is not None
    ]
    return compute_weekly_rows(
        fills_by_cell=fills_by_cell,
        close_points=close_points,
        frr_points=frr_points_from_stats(frr_stats),
    )


async def persist_rows(
    session_factory: async_sessionmaker[AsyncSession],
    rows: list[WeeklyCellRow],
    *,
    account_id: str,
    deployment_environment: str,
) -> int:
    """Full-recompute replace：單一 transaction 內先刪本 realm 既有 rows 再插新的。

    event_log 是 append-only SoT、本表是純 read model，故 replace 語意讓表永遠
    等於「以現在的 SoT 重算」。merge-only 只能新增/覆蓋、不能縮減：一旦
    diagnostics（best-effort、30-90d prunable）把某 fill 重歸 unattributed，舊
    (cell,week) row 會殘留舊 interest（read model 漏）。delete+insert 同 transaction
    是 atomic（失敗 rollback，表不變）；空 rows 正確清空該 realm（SoT 無 fill →
    表就該無該 realm）。delete 以 (env,account) 為界，不動其他 realm。
    """
    async with session_factory() as session:
        await session.execute(
            delete(AttributionWeeklyRow).where(
                AttributionWeeklyRow.account_id == account_id,
                AttributionWeeklyRow.deployment_environment == deployment_environment,
            )
        )
        session.add_all([
            AttributionWeeklyRow(
                deployment_environment=deployment_environment,
                account_id=account_id,
                cell=r.cell,
                week_start_ms=r.week_start_ms,
                week_end_ms=r.week_end_ms,
                n_fills=r.n_fills,
                gross_interest_usdt=r.gross_interest_usdt,
                net_interest_usdt=r.net_interest_usdt,
                capital_days=r.capital_days,
                realized_apr_net_pct=r.realized_apr_net_pct,
                baseline_close_apr_net_pct=r.baseline_close_apr_net_pct,
                baseline_frr_apr_net_pct=r.baseline_frr_apr_net_pct,
            )
            for r in rows
        ])
        await session.commit()
    return len(rows)


async def _amain() -> int:
    settings = Settings()
    engine = make_engine(settings)
    sf = make_session_factory(engine)
    account_id = os.environ.get("BFX_ACCOUNT_ID", "default")
    env = os.environ.get("BFX_DEPLOYMENT_ENV", "prod")
    try:
        rows = await load_and_compute(
            sf, account_id=account_id, deployment_environment=env,
        )
        n = await persist_rows(
            sf, rows, account_id=account_id, deployment_environment=env,
        )
    finally:
        await engine.dispose()
    unattributed = sum(1 for r in rows if r.cell == _UNATTRIBUTED)
    print(f"attribution_weekly upserted={n} unattributed_rows={unattributed}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(_amain()))
