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

CANDLE_INTERVAL_MS = 3_600_000          # 1h candles
MIN_CONTINUITY = Decimal("0.95")        # 95% threshold for candles
FRR_RATIO_LOW = Decimal("0.1")
FRR_RATIO_HIGH = Decimal("10")
FRR_SECONDS_PER_DAY = Decimal("86400")
ROUND_TRIP_TOLERANCE = Decimal("1e-15")


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

    candle_specs = [s for s in specs if s.kind == "candles"]
    fs_specs = [s for s in specs if s.kind == "funding_stats"]

    if candle_specs:
        spec = candle_specs[0]
        assert spec.timeframe is not None and spec.period_agg is not None
        # Get newest mts in DB for this series
        stmt = select(func.max(FundingCandleRow.mts)).where(
            FundingCandleRow.symbol == spec.symbol,
            FundingCandleRow.timeframe == spec.timeframe,
            FundingCandleRow.period_agg == spec.period_agg,
        )
        max_mts = (await session.execute(stmt)).scalar_one_or_none()
        if max_mts is not None:
            fetched = await client.get_funding_candles(
                symbol=spec.symbol,
                timeframe=spec.timeframe,
                period_agg=spec.period_agg,
                start=0, end=max_mts, limit=sample_size,
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
        stmt2 = select(func.max(FundingStatRow.mts)).where(
            FundingStatRow.symbol == spec.symbol,
        )
        max_mts2 = (await session.execute(stmt2)).scalar_one_or_none()
        if max_mts2 is not None:
            fetched_fs = await client.get_funding_stats(
                symbol=spec.symbol, end=max_mts2, limit=sample_size,
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

    return CheckResult(
        passed=not failures,
        message=f"round-trip exact-match: {sampled} series sampled",
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
    """For candles: rows / expected_rows >= 0.95.
    For funding_stats: max consecutive gap < 1 day."""
    failures: list[str] = []

    for spec in specs:
        if spec.kind == "candles":
            assert spec.timeframe is not None and spec.period_agg is not None
            stmt = select(
                func.count(),
                func.min(FundingCandleRow.mts),
                func.max(FundingCandleRow.mts),
            ).where(
                FundingCandleRow.symbol == spec.symbol,
                FundingCandleRow.timeframe == spec.timeframe,
                FundingCandleRow.period_agg == spec.period_agg,
            )
            n, min_mts, max_mts = (await session.execute(stmt)).one()
            if n == 0 or min_mts is None or max_mts is None:
                continue   # row_count check covers this
            expected = (max_mts - min_mts) // CANDLE_INTERVAL_MS + 1
            if expected == 0:
                continue
            ratio = Decimal(n) / Decimal(expected)
            if ratio < MIN_CONTINUITY:
                failures.append(
                    f"{spec.label()}: {ratio:.2%} (rows={n}, expected≈{expected})"
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
            one_day_ms = 24 * 60 * 60 * 1000
            if max_gap > one_day_ms:
                failures.append(
                    f"{spec.label()}: max_gap = {max_gap / 3_600_000:.1f}h "
                    f"(threshold 24h)"
                )

    return CheckResult(
        passed=not failures,
        message=f"continuity: {len(specs) - len(failures)}/{len(specs)} series within threshold",
        failures=failures,
    )
