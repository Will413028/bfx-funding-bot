"""Public marketing endpoints — proof page data + free funding-rate CSV.

Deliberately carries NO auth dependency and NO SP5 per-user rate limiter
(`modules/api/ratelimit.py` keys on `Principal.user_id`, which anonymous
marketing-page/CSV visitors never have — see
docs/superpowers/specs/2026-07-19-borrowrate-proof-page-csv.md).

Compliance red line (binding — 2026-05-26 productization ADR, constraint 2):
marketing surfaces must never promise returns and must never disclose
absolute-dollar figures (capital size, USDT interest amounts) — both a legal
constraint (最高法院 112台上字第317號) and a privacy one (it would leak Will's
personal capital scale). Every value this module serializes is percent-only.
`gross_interest_usdt` / `net_interest_usdt` / `capital_days` are read off the
ORM row for internal aggregation math ONLY and must never reach a response
model — see the explicit "never leaks" tests in tests/test_public_router.py.
"""
from __future__ import annotations

import os
from collections import defaultdict
from collections.abc import Iterator
from datetime import UTC, datetime
from decimal import Decimal
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Response, status
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bfx_funding_bot.modules.api.deps import get_session
from bfx_funding_bot.modules.api.schemas import (
    PublicProofSummaryResponse,
    PublicWeeklyPoint,
)
from bfx_funding_bot.modules.candles.tables import FundingCandleRow
from bfx_funding_bot.modules.live_validation.tables import AttributionWeeklyRow

_CACHE_CONTROL = "public, max-age=3600"
_DAYS_PER_YEAR = Decimal("365")
_HUNDRED = Decimal("100")


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


def _dec_str(value: Decimal) -> str:
    """Canonical decimal string (mirrors modules/api/attribution.py._dec_str —
    `normalize()` alone emits scientific notation for whole numbers on
    sqlite's Numeric round-trip; fixed-point `format` avoids that)."""
    return format(value.normalize(), "f")


def _aggregate_weeks(rows: list[AttributionWeeklyRow]) -> list[PublicWeeklyPoint]:
    """Blend every cell in the realm into one bot-vs-baseline weekly series.

    realized_apr_net_pct: capital-days-weighted APR across cells for that
    week — Σnet_interest / Σcapital_days × 365 × 100 (the correct blended
    yield; a naive per-cell average would misweight small vs large cells).
    None when the week's total capital_days is 0 (no fills that week).

    baseline_frr_util_apr_net_pct: plain mean of each cell's own
    util-adjusted FRR baseline for that week (cells can carry different
    symbols/utilization, so there is no single "market" baseline to sum).
    None when no cell has a baseline for that week.

    `net_interest_usdt` / `capital_days` are read here purely as internal
    weights — the returned PublicWeeklyPoint never carries them.
    """
    by_week: dict[int, list[AttributionWeeklyRow]] = defaultdict(list)
    for row in rows:
        by_week[row.week_start_ms].append(row)

    points: list[PublicWeeklyPoint] = []
    for week_start_ms in sorted(by_week):
        week_rows = by_week[week_start_ms]
        net_total = sum((r.net_interest_usdt for r in week_rows), Decimal("0"))
        capital_days_total = sum((r.capital_days for r in week_rows), Decimal("0"))
        realized = (
            net_total / capital_days_total * _DAYS_PER_YEAR * _HUNDRED
            if capital_days_total > 0
            else None
        )
        baselines = [
            r.baseline_frr_util_apr_net_pct
            for r in week_rows
            if r.baseline_frr_util_apr_net_pct is not None
        ]
        baseline = (
            sum(baselines, Decimal("0")) / Decimal(len(baselines))
            if baselines
            else None
        )
        points.append(
            PublicWeeklyPoint(
                week_start_ms=week_start_ms,
                realized_apr_net_pct=_dec_str(realized) if realized is not None else None,
                baseline_frr_util_apr_net_pct=(
                    _dec_str(baseline) if baseline is not None else None
                ),
            )
        )
    return points


def build_public_router() -> APIRouter:
    """Public marketing router — no auth and no per-user rate limiter."""
    router = APIRouter(prefix="/api/v1/public", tags=["public"])

    @router.get("/proof-summary")
    async def proof_summary(
        response: Response,
        session: AsyncSession = Depends(get_session),  # noqa: B008
    ) -> dict[str, object]:
        response.headers["Cache-Control"] = _CACHE_CONTROL
        account_id = _public_exchange_account_id()
        deployment_environment = os.environ.get("BFX_DEPLOYMENT_ENV", "prod").strip() or "prod"
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
        payload = PublicProofSummaryResponse(weeks=weeks, as_of=as_of.isoformat())
        return {"data": payload.model_dump(by_alias=True)}

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
