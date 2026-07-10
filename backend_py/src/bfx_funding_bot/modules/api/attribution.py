"""E3 (b) — per-cell weekly attribution 讀端點（operator 儀表；read-only）。"""
from __future__ import annotations

import logging
import os
from decimal import Decimal

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bfx_funding_bot.core.auth import Principal, require_user
from bfx_funding_bot.modules.api.deps import get_session
from bfx_funding_bot.modules.api.schemas import WeeklyAttributionResponse
from bfx_funding_bot.modules.live_validation.tables import AttributionWeeklyRow

log = logging.getLogger(__name__)


def _dec_str(value: Decimal) -> str:
    """Canonical decimal string for the FE (`Number()` — precision preserved).

    `normalize()` then fixed-point `format` — NOT bare `str()` — because
    SQLAlchemy's generic `Numeric` type round-trips through a DBAPI-returned
    string on sqlite and reconstructs the Decimal at `decimal_return_scale=10`
    (e.g. `Decimal("6.205")` comes back as `Decimal("6.2050000000")`; plain
    `Numeric` on Postgres/asyncpg doesn't hit this, but sqlite is our test
    backend). `normalize()` alone would emit scientific notation for whole
    numbers (`Decimal("1000").normalize()` == `Decimal("1E+3")`); `format(...,
    "f")` forces fixed-point either way.
    """
    return format(value.normalize(), "f")


def _to_response(row: AttributionWeeklyRow) -> dict[str, object]:
    return WeeklyAttributionResponse(
        cell=row.cell,
        week_start_ms=row.week_start_ms,
        week_end_ms=row.week_end_ms,
        n_fills=row.n_fills,
        gross_interest_usdt=_dec_str(row.gross_interest_usdt),
        net_interest_usdt=_dec_str(row.net_interest_usdt),
        capital_days=_dec_str(row.capital_days),
        realized_apr_net_pct=(
            _dec_str(row.realized_apr_net_pct)
            if row.realized_apr_net_pct is not None else None
        ),
        baseline_close_apr_net_pct=(
            _dec_str(row.baseline_close_apr_net_pct)
            if row.baseline_close_apr_net_pct is not None else None
        ),
        baseline_frr_apr_net_pct=(
            _dec_str(row.baseline_frr_apr_net_pct)
            if row.baseline_frr_apr_net_pct is not None else None
        ),
        baseline_frr_util_apr_net_pct=(
            _dec_str(row.baseline_frr_util_apr_net_pct)
            if row.baseline_frr_util_apr_net_pct is not None else None
        ),
    ).model_dump(by_alias=True)


def build_attribution_router() -> APIRouter:
    router = APIRouter(prefix="/api/v1", tags=["attribution"])
    # 儀表只看單一部署 realm — 多 env/account 的 row 不可混進同一 cell 序列
    # （否則 FE 每週有重複點）。與 loader 寫入時的 env 對齊（同 default）。
    account_id = os.environ.get("BFX_ACCOUNT_ID", "default")
    deployment_environment = os.environ.get("BFX_DEPLOYMENT_ENV", "prod")
    log.info(
        "attribution_router filtering realm account_id=%s deployment_environment=%s",
        account_id, deployment_environment,
    )

    @router.get("/attribution/weekly")
    async def weekly(
        cell: str | None = None,
        user: Principal = Depends(require_user),  # noqa: B008
        session: AsyncSession = Depends(get_session),  # noqa: B008
    ) -> dict[str, object]:
        stmt = (
            select(AttributionWeeklyRow)
            .where(
                AttributionWeeklyRow.account_id == account_id,
                AttributionWeeklyRow.deployment_environment == deployment_environment,
            )
            .order_by(AttributionWeeklyRow.cell, AttributionWeeklyRow.week_start_ms)
        )
        if cell is not None:
            stmt = stmt.where(AttributionWeeklyRow.cell == cell)
        rows = (await session.execute(stmt)).scalars().all()
        return {"data": [_to_response(r) for r in rows]}

    return router
