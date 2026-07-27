import time
from collections import defaultdict
from decimal import Decimal
from typing import Any, cast

from sqlalchemy import CursorResult, func, insert, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.postgresql.dml import Insert as PGInsert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.dialects.sqlite.dml import Insert as SQLiteInsert
from sqlalchemy.ext.asyncio import AsyncSession

from bfx_funding_bot.modules.candles.schemas import FundingCandle
from bfx_funding_bot.modules.candles.tables import (
    FundingCandleRevisionRow,
    FundingCandleRow,
)

# Period length per timeframe. Mirrors marketfeed.scheduler._TIMEFRAME_MS; kept
# here so the candles layer does not import upward into marketfeed.
_TIMEFRAME_MS = {"15m": 15 * 60_000, "30m": 30 * 60_000, "1h": 60 * 60_000}

_UPSERT_INDEX = ["symbol", "timeframe", "period_agg", "mts"]
_UPSERT_SET_COLS = ["open", "close", "high", "low", "volume"]

# asyncpg / PostgreSQL caps bind parameters per statement at 32767 (sqlite's
# SQLITE_MAX_VARIABLE_NUMBER is 32766). With 12 columns per row, 2500 rows =
# 30000 placeholders — under both limits. Bitfinex returns up to 10000 rows per
# page, so chunking is required. NOTE: adding a column shrinks the safe chunk —
# 3000 was fine at 9 columns (27000) but overflows at 12 (36000).
_CANDLE_CHUNK = 2500


def _is_period_closed(timeframe: str, mts: int, now_ms: int) -> bool:
    """True when `mts` belongs to a period strictly before the one holding `now_ms`.

    Unknown timeframes fall back to "not closed" — refusing to seal something we
    cannot reason about is the safe direction: an unsealed row can still be sealed
    later, while a wrongly sealed one is immutable.
    """
    step = _TIMEFRAME_MS.get(timeframe)
    if step is None:
        return False
    return mts < (now_ms // step) * step


def _to_float_or_none(d: Decimal | None) -> float | None:
    return float(d) if d is not None else None


def _row_to_domain(row: FundingCandleRow) -> FundingCandle:
    return FundingCandle(
        symbol=row.symbol,
        timeframe=row.timeframe,
        period_agg=row.period_agg,
        mts=row.mts,
        open=Decimal(str(row.open)) if row.open is not None else None,
        close=Decimal(str(row.close)) if row.close is not None else None,
        high=Decimal(str(row.high)) if row.high is not None else None,
        low=Decimal(str(row.low)) if row.low is not None else None,
        volume=Decimal(str(row.volume)) if row.volume is not None else None,
    )


async def upsert_candles(
    session: AsyncSession,
    candles: list[FundingCandle],
    *,
    now_ms: int | None = None,
) -> None:
    """Upsert candles by composite PK (symbol, timeframe, period_agg, mts).

    Postgres: ON CONFLICT DO UPDATE. Sqlite (test only): same semantics via
    sqlite dialect's insert().on_conflict_do_update.

    Chunked at _CANDLE_CHUNK rows per execute() to stay under asyncpg's
    32767 bind-parameter limit.

    A row lands sealed iff its period has already closed (`mts` before the period
    containing `now`). The still-forming period stays open because Bitfinex keeps
    re-pushing that same mts with a moving close; everything older is settled
    history and must be readable by final-only consumers immediately — REST
    backfill writes only settled history, and `fetch_and_store` reads back what it
    just wrote, so landing it unsealed would make backfill silently produce
    nothing visible.

    `first_seen_at_ms` records when we first observed the row (knowledge time) and
    is never overwritten by later pushes — only the OHLCV columns are, and only
    while the row is still open.
    """
    if not candles:
        return

    now = now_ms if now_ms is not None else int(time.time() * 1000)

    dialect_name = session.bind.dialect.name if session.bind else "postgresql"

    for i in range(0, len(candles), _CANDLE_CHUNK):
        chunk = candles[i : i + _CANDLE_CHUNK]
        rows = [
            {
                "symbol": c.symbol,
                "timeframe": c.timeframe,
                "period_agg": c.period_agg,
                "mts": c.mts,
                "open": _to_float_or_none(c.open),
                "close": _to_float_or_none(c.close),
                "high": _to_float_or_none(c.high),
                "low": _to_float_or_none(c.low),
                "volume": _to_float_or_none(c.volume),
                "is_final": _is_period_closed(c.timeframe, c.mts, now),
                "first_seen_at_ms": now,
                "finalized_at_ms": None,
            }
            for c in chunk
        ]

        await _record_rejected_revisions(session, chunk, now_ms=now)

        stmt: PGInsert | SQLiteInsert
        if dialect_name == "postgresql":
            stmt = pg_insert(FundingCandleRow).values(rows)
        else:
            stmt = sqlite_insert(FundingCandleRow).values(rows)

        # `where` makes the UPDATE arm a no-op for sealed rows: a final candle is
        # immutable regardless of who re-pushes it (WS re-send, REST backfill).
        # Without this the venue's late revision silently replaces a value the
        # strategy already observed, and live state drifts from replay forever.
        stmt = stmt.on_conflict_do_update(
            index_elements=_UPSERT_INDEX,
            set_={col: getattr(stmt.excluded, col) for col in _UPSERT_SET_COLS},
            where=FundingCandleRow.is_final.is_(False),
        )
        await session.execute(stmt)


async def _record_rejected_revisions(
    session: AsyncSession,
    chunk: list[FundingCandle],
    *,
    now_ms: int,
) -> None:
    """Log any incoming value that differs from an already-sealed candle.

    Runs before the upsert, whose `where is_final = false` clause will silently
    drop these writes. Silent is the problem — the pre-2026-07-27 code dropped
    nothing and overwrote everything, and the resulting drift was only provable
    from container logs that expire.
    """
    by_series: dict[tuple[str, str, str], list[FundingCandle]] = defaultdict(list)
    for c in chunk:
        by_series[(c.symbol, c.timeframe, c.period_agg)].append(c)

    revisions: list[dict[str, object]] = []
    for (symbol, timeframe, period_agg), series in by_series.items():
        sealed = (
            await session.execute(
                select(FundingCandleRow).where(
                    FundingCandleRow.symbol == symbol,
                    FundingCandleRow.timeframe == timeframe,
                    FundingCandleRow.period_agg == period_agg,
                    FundingCandleRow.mts.in_([c.mts for c in series]),
                    FundingCandleRow.is_final.is_(True),
                )
            )
        ).scalars().all()
        sealed_by_mts = {r.mts: r for r in sealed}
        for c in series:
            row = sealed_by_mts.get(c.mts)
            if row is None:
                continue
            incoming = _to_float_or_none(c.close)
            if incoming == row.close:
                continue
            revisions.append({
                "symbol": symbol,
                "timeframe": timeframe,
                "period_agg": period_agg,
                "mts": c.mts,
                "observed_at_ms": now_ms,
                "rejected_close": incoming,
                "final_close": row.close,
            })

    if revisions:
        await session.execute(insert(FundingCandleRevisionRow), revisions)


async def mark_candles_final(
    session: AsyncSession,
    *,
    symbol: str,
    timeframe: str,
    period_agg: str,
    through_mts: int,
    now_ms: int | None = None,
) -> int:
    """Seal every still-open candle at or before `through_mts`; return the count.

    Called when a LATER mts arrives for the same series — the venue moving on to
    a new period is the only reliable signal that the previous one closed
    (waiting a fixed number of seconds is a guess, and guessing 5s then 30s is
    what this replaces). Already-final rows are left alone so `finalized_at_ms`
    records the first sealing, not the latest sweep.
    """
    now = now_ms if now_ms is not None else int(time.time() * 1000)
    result = cast(
        "CursorResult[Any]",
        await session.execute(
            update(FundingCandleRow)
            .where(
                FundingCandleRow.symbol == symbol,
                FundingCandleRow.timeframe == timeframe,
                FundingCandleRow.period_agg == period_agg,
                FundingCandleRow.mts <= through_mts,
                FundingCandleRow.is_final.is_(False),
            )
            .values(is_final=True, finalized_at_ms=now)
        ),
    )
    return result.rowcount or 0


async def seal_closed_periods(
    session: AsyncSession,
    *,
    symbol: str,
    timeframe: str,
    period_agg: str,
    now_ms: int | None = None,
) -> int:
    """Seal every candle whose period has closed by `now_ms`; return the count.

    `CandleWriter` seals on "a later mts arrived", which is the strongest possible
    evidence but not always timely: the scheduler reads mts=T-1h at T+30s, and if
    the venue is slow to push mts=T that row is still unsealed at read time. A
    final-only reader then skips it, leaving a hole in the live observe sequence
    that replay — reading moments later, after the seal — does not have. Two arms
    with different sequences is exactly the divergence this whole ADR is about.

    Time is sufficient evidence on its own: a period that has elapsed cannot
    receive more trades. Call this before any strategy-facing read.
    """
    now = now_ms if now_ms is not None else int(time.time() * 1000)
    step = _TIMEFRAME_MS.get(timeframe)
    if step is None:
        return 0
    current_period_start = (now // step) * step
    return await mark_candles_final(
        session,
        symbol=symbol,
        timeframe=timeframe,
        period_agg=period_agg,
        through_mts=current_period_start - 1,
        now_ms=now,
    )


async def get_candles_in_range(
    session: AsyncSession,
    *,
    symbol: str,
    timeframe: str,
    period_agg: str,
    start_mts: int,
    end_mts: int,
    final_only: bool = True,
) -> list[FundingCandle]:
    """Fetch candles in [start_mts, end_mts] (inclusive) ordered by mts ASC.

    `final_only` defaults to True, matching get_up_to. warmup reads through here,
    and when only get_up_to filtered, live state (warmup) absorbed the in-flight
    candle while replay (get_up_to) did not — the two arms then differed by
    exactly one observe() of a still-forming close, which is a ~0.7% EMA shift at
    ema_span=24. Both readers must apply the same rule or I-CI is only half true.
    """
    conditions = [
        FundingCandleRow.symbol == symbol,
        FundingCandleRow.timeframe == timeframe,
        FundingCandleRow.period_agg == period_agg,
        FundingCandleRow.mts >= start_mts,
        FundingCandleRow.mts <= end_mts,
    ]
    if final_only:
        conditions.append(FundingCandleRow.is_final.is_(True))
    stmt = (
        select(FundingCandleRow)
        .where(*conditions)
        .order_by(FundingCandleRow.mts.asc())
    )
    result = await session.execute(stmt)
    return [_row_to_domain(row) for row in result.scalars().all()]


async def get_up_to(
    session: AsyncSession,
    *,
    symbol: str,
    timeframe: str,
    period_agg: str,
    mts_inclusive: int,
    lookback: int,
    final_only: bool = True,
) -> list[FundingCandle]:
    """Fetch up to `lookback` candles with mts <= mts_inclusive, returned mts ASC.

    Used by signal_engine for divergence replay: feed history[:-1] into a fresh
    strategy, then extract on history[-1] to compare against the live signal.

    `final_only` defaults to True: every strategy-facing path (warmup, scheduler
    tick, divergence replay) must see only sealed candles, so a new call site is
    safe by construction. Opting out is for tooling that deliberately wants the
    in-flight row.
    """
    conditions = [
        FundingCandleRow.symbol == symbol,
        FundingCandleRow.timeframe == timeframe,
        FundingCandleRow.period_agg == period_agg,
        FundingCandleRow.mts <= mts_inclusive,
    ]
    if final_only:
        conditions.append(FundingCandleRow.is_final.is_(True))
    stmt = (
        select(FundingCandleRow)
        .where(*conditions)
        .order_by(FundingCandleRow.mts.desc())
        .limit(lookback)
    )
    result = await session.execute(stmt)
    candles = [_row_to_domain(row) for row in result.scalars().all()]
    return sorted(candles, key=lambda c: c.mts)


async def get_min_mts(
    session: AsyncSession,
    *,
    symbol: str,
    timeframe: str,
    period_agg: str,
) -> int | None:
    """Return smallest mts for the given (symbol, timeframe, period_agg) series,
    or None if no rows exist."""
    stmt = select(func.min(FundingCandleRow.mts)).where(
        FundingCandleRow.symbol == symbol,
        FundingCandleRow.timeframe == timeframe,
        FundingCandleRow.period_agg == period_agg,
    )
    result = await session.execute(stmt)
    return result.scalar_one_or_none()
