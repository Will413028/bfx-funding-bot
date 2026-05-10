"""4 PASS check functions for Phase 2 backfill orchestrator.

Each returns a CheckResult; orchestrator OR's `passed` and prints summary.
Pure-ish: takes a session + specs + (for round-trip) a Bitfinex client.
"""
from dataclasses import dataclass, field
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from bfx_funding_bot.external.bitfinex.rest import BitfinexREST
from bfx_funding_bot.modules.backfill.schemas import SeriesSpec
from bfx_funding_bot.modules.candles.repository import get_candles_in_range
from bfx_funding_bot.modules.candles.tables import FundingCandleRow
from bfx_funding_bot.modules.funding_stats.repository import get_in_range
from bfx_funding_bot.modules.funding_stats.tables import FundingStatRow

FRR_RATIO_LOW = Decimal("0.1")
FRR_RATIO_HIGH = Decimal("10")
FRR_SECONDS_PER_DAY = Decimal("86400")
ROUND_TRIP_TOLERANCE = Decimal("1e-15")
# Round-trip sample shift: Bitfinex actively updates the latest (still-forming)
# candle's close as new ticks arrive, so sampling near max_mts produces a
# false-positive drift signal. Step back 7 days to a stable, immutable region.
ROUND_TRIP_SAMPLE_SHIFT_MS = 7 * 24 * 3_600_000
# Continuity threshold for candles: max consecutive gap. Sparse aggregations
# (e.g. p30 30-day funding) have inherent gaps from low activity, and
# historical outages / quiet periods on rare fUSD-a30 produce real-data
# gaps up to ~47 days observed at backfill commit 1065be2. A count-ratio
# rule produces false positives on sparse series. Threshold = 60 days:
# accommodates observed real data while still catching resume-from-DB
# middle-skip bugs (those would skip months of contiguous data).
CANDLE_MAX_GAP_MS = 60 * 24 * 3_600_000
FUNDING_STATS_MAX_GAP_MS = 24 * 3_600_000


@dataclass(frozen=True)
class CheckResult:
    passed: bool
    message: str
    failures: list[str] = field(default_factory=list)


# ---------- 1. row counts ----------

async def check_row_counts(
    session: AsyncSession, specs: list[SeriesSpec],
) -> CheckResult:
    """Every series must have row_count > 0."""
    failures: list[str] = []
    for spec in specs:
        if spec.kind == "candles":
            stmt = select(func.count()).select_from(FundingCandleRow).where(
                FundingCandleRow.symbol == spec.symbol,
                FundingCandleRow.timeframe == spec.timeframe,
                FundingCandleRow.period_agg == spec.period_agg,
            )
        else:
            stmt = select(func.count()).select_from(FundingStatRow).where(
                FundingStatRow.symbol == spec.symbol,
            )
        n = (await session.execute(stmt)).scalar_one()
        if n == 0:
            failures.append(f"{spec.label()} has 0 rows")

    return CheckResult(
        passed=not failures,
        message=f"row_count > 0 for {len(specs) - len(failures)}/{len(specs)} series",
        failures=failures,
    )


# ---------- 2. round-trip exact-match ----------

async def check_round_trip(
    session: AsyncSession,
    client: BitfinexREST,
    specs: list[SeriesSpec],
    sample_size: int = 10,
) -> CheckResult:
    """For up to one candle-spec and one funding_stats-spec, fetch from
    Bitfinex again and exact-match against DB."""
    failures: list[str] = []
    sampled = 0
    skipped: list[str] = []

    candle_specs = [s for s in specs if s.kind == "candles"]
    fs_specs = [s for s in specs if s.kind == "funding_stats"]

    if candle_specs:
        spec = candle_specs[0]
        if spec.timeframe is None or spec.period_agg is None:
            raise ValueError(
                f"candles SeriesSpec missing timeframe/period_agg: {spec!r}"
            )
        # Get newest + oldest mts in DB for this series. Shift the sample
        # endpoint back by 7 days so we sample stable (immutable) candles —
        # Bitfinex actively updates the latest candle as new ticks arrive.
        stmt = select(
            func.min(FundingCandleRow.mts),
            func.max(FundingCandleRow.mts),
        ).where(
            FundingCandleRow.symbol == spec.symbol,
            FundingCandleRow.timeframe == spec.timeframe,
            FundingCandleRow.period_agg == spec.period_agg,
        )
        min_mts, max_mts = (await session.execute(stmt)).one()
        if max_mts is not None and min_mts is not None:
            sample_end = max_mts - ROUND_TRIP_SAMPLE_SHIFT_MS
            if sample_end < min_mts:
                skipped.append(
                    f"{spec.label()}: skipped (not enough history; need >7d)"
                )
            else:
                fetched = await client.get_funding_candles(
                    symbol=spec.symbol,
                    timeframe=spec.timeframe,
                    period_agg=spec.period_agg,
                    start=0, end=sample_end, limit=sample_size,
                )
                if fetched:
                    stored = await get_candles_in_range(
                        session,
                        symbol=spec.symbol,
                        timeframe=spec.timeframe,
                        period_agg=spec.period_agg,
                        start_mts=min(c.mts for c in fetched),
                        end_mts=max(c.mts for c in fetched),
                    )
                    stored_by_mts = {c.mts: c for c in stored}
                    for f in fetched:
                        s = stored_by_mts.get(f.mts)
                        if s is None:
                            failures.append(f"{spec.label()} mts={f.mts} missing in DB")
                            continue
                        for fld in ("open", "close", "high", "low", "volume"):
                            fv = getattr(f, fld)
                            sv = getattr(s, fld)
                            if fv is None and sv is None:
                                continue
                            if fv is None or sv is None:
                                failures.append(
                                    f"{spec.label()} mts={f.mts} {fld} nullness mismatch"
                                )
                                break
                            if abs(fv - sv) > ROUND_TRIP_TOLERANCE:
                                failures.append(
                                    f"{spec.label()} mts={f.mts} {fld}: f={fv} != s={sv}"
                                )
                                break
                    sampled += 1

    if fs_specs:
        spec = fs_specs[0]
        stmt2 = select(
            func.min(FundingStatRow.mts),
            func.max(FundingStatRow.mts),
        ).where(FundingStatRow.symbol == spec.symbol)
        min_mts2, max_mts2 = (await session.execute(stmt2)).one()
        if max_mts2 is not None and min_mts2 is not None:
            sample_end2 = max_mts2 - ROUND_TRIP_SAMPLE_SHIFT_MS
            if sample_end2 < min_mts2:
                skipped.append(
                    f"{spec.label()}: skipped (not enough history; need >7d)"
                )
            else:
                fetched_fs = await client.get_funding_stats(
                    symbol=spec.symbol, end=sample_end2, limit=sample_size,
                )
                if fetched_fs:
                    stored_fs = await get_in_range(
                        session,
                        symbol=spec.symbol,
                        start_mts=min(s.mts for s in fetched_fs),
                        end_mts=max(s.mts for s in fetched_fs),
                    )
                    stored_fs_by_mts = {row.mts: row for row in stored_fs}
                    for ff in fetched_fs:
                        sf = stored_fs_by_mts.get(ff.mts)
                        if sf is None:
                            failures.append(f"{spec.label()} mts={ff.mts} missing in DB")
                            continue
                        for fld in (
                            "frr", "avg_period", "funding_amount",
                            "funding_amount_used", "funding_below_threshold",
                        ):
                            fv = getattr(ff, fld)
                            sv = getattr(sf, fld)
                            if fv is None and sv is None:
                                continue
                            if fv is None or sv is None:
                                failures.append(
                                    f"{spec.label()} mts={ff.mts} {fld} nullness mismatch"
                                )
                                break
                            if abs(fv - sv) > ROUND_TRIP_TOLERANCE:
                                failures.append(
                                    f"{spec.label()} mts={ff.mts} {fld}: f={fv} != s={sv}"
                                )
                                break
                    sampled += 1

    msg = f"round-trip exact-match: {sampled} series sampled"
    if skipped:
        msg += f"; {len(skipped)} skipped ({'; '.join(skipped)})"
    return CheckResult(
        passed=not failures,
        message=msg,
        failures=failures,
    )


# ---------- 3. FRR unit sanity ----------

async def check_frr_unit(
    session: AsyncSession,
    symbol: str = "fUSD",
) -> CheckResult:
    """Take one funding_stats row, find a candle within +/-30min, compare
    (frr * 86400) to candle close. Ratio should be in [0.1, 10] (one order
    of magnitude tolerance -- this catches unit errors, not precision)."""
    fs_stmt = select(FundingStatRow).where(
        FundingStatRow.symbol == symbol, FundingStatRow.frr.is_not(None),
    ).limit(1)
    fs_row = (await session.execute(fs_stmt)).scalars().first()
    if fs_row is None or fs_row.frr is None:
        return CheckResult(passed=True, message="FRR unit check skipped: no funding_stats data")

    window_ms = 30 * 60 * 1000
    candle_stmt = (
        select(FundingCandleRow)
        .where(
            FundingCandleRow.symbol == symbol,
            FundingCandleRow.timeframe == "1h",
            FundingCandleRow.period_agg == "p2",
            FundingCandleRow.close.is_not(None),
            FundingCandleRow.mts >= fs_row.mts - window_ms,
            FundingCandleRow.mts <= fs_row.mts + window_ms,
        )
        .limit(1)
    )
    candle = (await session.execute(candle_stmt)).scalars().first()
    if candle is None or candle.close is None:
        return CheckResult(
            passed=True,
            message=f"FRR unit check skipped: no fUSD 1h p2 candle near mts={fs_row.mts}",
        )

    frr_dec = Decimal(str(fs_row.frr))
    close_dec = Decimal(str(candle.close))
    if close_dec == 0:
        return CheckResult(passed=True, message="FRR unit check skipped: candle close = 0")

    ratio = (frr_dec * FRR_SECONDS_PER_DAY) / close_dec
    if FRR_RATIO_LOW <= ratio <= FRR_RATIO_HIGH:
        return CheckResult(
            passed=True,
            message=(
                f"FRR unit sanity: frr * 86400 / candle.close = {ratio:.3f} "
                f"(in [{FRR_RATIO_LOW}, {FRR_RATIO_HIGH}]) -> FRR is per-second"
            ),
        )
    return CheckResult(
        passed=False,
        message=(
            f"FRR unit mismatch: frr={frr_dec} * 86400 / candle.close={close_dec} "
            f"= ratio={ratio:.3f} (expected in [{FRR_RATIO_LOW}, {FRR_RATIO_HIGH}])"
        ),
        failures=[f"ratio out of range: {ratio:.3f}"],
    )


# ---------- 4. continuity ----------

async def check_continuity(
    session: AsyncSession, specs: list[SeriesSpec],
) -> CheckResult:
    """Max consecutive gap rule.
    - Candles: < 60 days. The threshold is loose to accommodate sparse
      aggregations (e.g. p30) and observed Bitfinex-side quiet periods
      up to 47 days; resume-from-DB middle-skip bugs would create
      multi-month contiguous gaps and still trip the 60-day threshold.
    - funding_stats: < 1 day."""
    failures: list[str] = []

    for spec in specs:
        if spec.kind == "candles":
            if spec.timeframe is None or spec.period_agg is None:
                raise ValueError(
                    f"candles SeriesSpec missing timeframe/period_agg: {spec!r}"
                )
            stmt = select(FundingCandleRow.mts).where(
                FundingCandleRow.symbol == spec.symbol,
                FundingCandleRow.timeframe == spec.timeframe,
                FundingCandleRow.period_agg == spec.period_agg,
            ).order_by(FundingCandleRow.mts.asc())
            mts_list = [r[0] for r in (await session.execute(stmt)).all()]
            if len(mts_list) < 2:
                continue   # row_count check covers empty series
            max_gap = max(
                mts_list[i + 1] - mts_list[i] for i in range(len(mts_list) - 1)
            )
            if max_gap > CANDLE_MAX_GAP_MS:
                failures.append(
                    f"{spec.label()}: max_gap = {max_gap / 3_600_000:.1f}h "
                    f"(threshold {CANDLE_MAX_GAP_MS / 3_600_000:.0f}h)"
                )
        else:
            stmt2 = select(FundingStatRow.mts).where(
                FundingStatRow.symbol == spec.symbol,
            ).order_by(FundingStatRow.mts.asc())
            mts_list = [r[0] for r in (await session.execute(stmt2)).all()]
            if len(mts_list) < 2:
                continue
            max_gap = max(
                mts_list[i + 1] - mts_list[i] for i in range(len(mts_list) - 1)
            )
            if max_gap > FUNDING_STATS_MAX_GAP_MS:
                failures.append(
                    f"{spec.label()}: max_gap = {max_gap / 3_600_000:.1f}h "
                    f"(threshold {FUNDING_STATS_MAX_GAP_MS / 3_600_000:.0f}h)"
                )

    return CheckResult(
        passed=not failures,
        message=f"continuity: {len(specs) - len(failures)}/{len(specs)} series within threshold",
        failures=failures,
    )
