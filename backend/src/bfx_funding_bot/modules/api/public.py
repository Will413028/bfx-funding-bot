"""Public marketing endpoints — proof page data + free funding-rate CSV.

Deliberately carries NO auth dependency and NO SP5 per-user rate limiter
(`modules/api/ratelimit.py` keys on `Principal.user_id`, which anonymous
marketing-page/CSV visitors never have — see backend/ARCHITECTURE.md, public
read model).

Compliance red line (binding — 2026-05-26 productization ADR, constraint 2):
marketing surfaces must never promise returns and must never disclose
absolute-dollar figures (capital size, USDT interest amounts) — both a legal
constraint (最高法院 112台上字第317號) and a privacy one (it would leak the operator's
personal capital scale). Every value this module serializes is percent-only.
`gross_interest_usdt` / `net_interest_usdt` / `capital_days` are read off the
ORM row for internal aggregation math ONLY and must never reach a response
model — see the explicit "never leaks" tests in tests/test_public_router.py.
"""
from __future__ import annotations

import os
from collections.abc import Iterator
from datetime import UTC, datetime
from decimal import Decimal
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Response, status
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bfx_funding_bot.core.settings import require_deployment_environment
from bfx_funding_bot.modules.api.deps import get_session
from bfx_funding_bot.modules.api.schemas import (
    Envelope,
    PublicProofSummaryResponse,
    PublicWeeklyPoint,
)
from bfx_funding_bot.modules.candles.tables import FundingCandleRow
from bfx_funding_bot.modules.live_validation.public_site import aggregate_weeks, dec_str
from bfx_funding_bot.modules.live_validation.tables import AttributionWeeklyRow

_CACHE_CONTROL = "public, max-age=3600"


def _public_exchange_account_id() -> UUID:
    """Resolve the explicitly configured public proof account.

    The anonymous proof endpoint cannot receive a membership path parameter,
    but it still must never silently select ``default`` or a legacy realm.
    """
    raw = os.environ.get("BFX_PUBLIC_EXCHANGE_ACCOUNT_ID", "").strip()
    try:
        return UUID(raw)
    except (TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="public_account_not_configured",
        ) from exc


def _aggregate_weeks(rows: list[AttributionWeeklyRow]) -> list[PublicWeeklyPoint]:
    """The proof series (live_validation.public_site.aggregate_weeks) as response points."""
    def pct(value: Decimal | None) -> str | None:
        return dec_str(value) if value is not None else None

    return [PublicWeeklyPoint(week_start_ms=w.week_start_ms,
                              realized_apr_net_pct=pct(w.realized_apr_net_pct),
                              baseline_frr_util_apr_net_pct=pct(w.baseline_frr_util_apr_net_pct))
            for w in aggregate_weeks(rows)]


def build_public_router() -> APIRouter:
    """Public marketing router — no auth and no per-user rate limiter."""
    router = APIRouter(prefix="/api/v1/public", tags=["public"])

    @router.get("/proof-summary")
    async def proof_summary(
        response: Response,
        session: AsyncSession = Depends(get_session),  # noqa: B008
    ) -> Envelope[PublicProofSummaryResponse]:
        response.headers["Cache-Control"] = _CACHE_CONTROL
        account_id = _public_exchange_account_id()
        try:
            deployment_environment = require_deployment_environment()
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="deployment_environment_not_configured",
            ) from exc
        stmt = (
            select(AttributionWeeklyRow)
            .where(
                AttributionWeeklyRow.exchange_account_id == account_id,
                AttributionWeeklyRow.deployment_environment == deployment_environment,
            )
            .order_by(AttributionWeeklyRow.week_start_ms)
        )
        rows = list((await session.execute(stmt)).scalars().all())
        weeks = _aggregate_weeks(rows)
        as_of = max((r.computed_at for r in rows), default=datetime.now(UTC))
        return Envelope(data=PublicProofSummaryResponse(weeks=weeks, as_of=as_of.isoformat()))

    @router.get("/funding-rates.csv")
    async def funding_rates_csv(
        response: Response,
        symbol: Literal["fUST", "fUSD"],
        session: AsyncSession = Depends(get_session),  # noqa: B008
    ) -> StreamingResponse:
        # symbol is FastAPI/pydantic-validated against the Literal above —
        # any other value 422s before this body ever runs (spec: whitelist).
        response.headers["Cache-Control"] = _CACHE_CONTROL
        stmt = (
            select(FundingCandleRow.mts, FundingCandleRow.close)
            .where(
                FundingCandleRow.symbol == symbol,
                FundingCandleRow.timeframe == "1h",
                FundingCandleRow.period_agg == "p2",
                FundingCandleRow.close.is_not(None),
            )
            .order_by(FundingCandleRow.mts)
        )
        # ~87600 rows (10y hourly) for one symbol — small enough to fetch in
        # one shot and stream line-by-line to the HTTP response; a true
        # server-side-cursor stream isn't worth the sqlite/asyncpg dialect
        # divergence for this dataset size.
        rows = (await session.execute(stmt)).all()

        def _iter_csv() -> Iterator[str]:
            yield "date,close_apr_pct\n"
            for mts, close in rows:
                date_str = datetime.fromtimestamp(mts / 1000, tz=UTC).strftime(
                    "%Y-%m-%dT%H:%M:%SZ"
                )
                apr_pct = close * 365.0 * 100.0
                yield f"{date_str},{apr_pct:.6f}\n"

        return StreamingResponse(
            _iter_csv(),
            media_type="text/csv",
            headers={
                "Cache-Control": _CACHE_CONTROL,
                "Content-Disposition": (
                    f'attachment; filename="funding-rates-{symbol}.csv"'
                ),
            },
        )

    return router
