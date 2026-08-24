"""Persistence for G13 fill_rate_stats."""
from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from bfx_funding_bot.modules.lending.tracking.artifact import FillModelArtifact
from bfx_funding_bot.modules.lending.tracking.fill_rate import BucketStat
from bfx_funding_bot.modules.lending.tracking.tables import (
    FillRateModelArtifactRow,
    FillRateStatsRow,
)

_PK = ["source", "symbol", "period_agg", "horizon_h", "spread_bucket_bps"]
_ARTIFACT_FIELDS = (
    "artifact_hash",
    "source",
    "symbol",
    "period_agg",
    "horizon_h",
    "model_version",
    "schema_version",
    "training_start_ms",
    "training_end_ms",
    "cutoff_ms",
    "sample_count",
    "confidence_min_samples",
    "metadata_json",
)


def _artifact_values(artifact: FillModelArtifact) -> dict[str, object]:
    return {
        "artifact_hash": artifact.artifact_hash,
        "source": artifact.source,
        "symbol": artifact.symbol,
        "period_agg": artifact.period_agg,
        "horizon_h": artifact.horizon_h,
        "model_version": artifact.model_version,
        "schema_version": artifact.schema_version,
        "training_start_ms": artifact.training_start_ms,
        "training_end_ms": artifact.training_end_ms,
        "cutoff_ms": artifact.cutoff_ms,
        "sample_count": artifact.sample_count,
        "confidence_min_samples": artifact.confidence_min_samples,
        "metadata_json": artifact.metadata_for_storage(),
    }


async def ensure_fill_model_artifact(
    session: AsyncSession,
    *,
    artifact: FillModelArtifact,
    created_at: datetime,
) -> None:
    """Insert an artifact once, rejecting any hash/provenance collision."""
    expected = _artifact_values(artifact)
    result = await session.execute(
        select(FillRateModelArtifactRow).where(
            FillRateModelArtifactRow.artifact_hash == artifact.artifact_hash,
        )
    )
    existing = result.scalar_one_or_none()
    if existing is None:
        session.add(FillRateModelArtifactRow(
            **expected,
            created_at=created_at.astimezone(UTC),
        ))
        return

    mismatches = [
        field
        for field in _ARTIFACT_FIELDS
        if getattr(existing, field) != expected[field]
    ]
    if mismatches:
        fields = ", ".join(mismatches)
        raise ValueError(
            f"artifact hash {artifact.artifact_hash!r} conflicts with existing "
            f"artifact fields: {fields}"
        )


async def upsert_fill_rate_stats(
    session: AsyncSession,
    *,
    source: str,
    symbol: str,
    period_agg: str,
    stats: list[BucketStat],
    candle_range_start_ms: int,
    candle_range_end_ms: int,
    learned_at: datetime,
    artifact: FillModelArtifact,
) -> None:
    """Upsert stats by composite PK. SQLITE-ONLY (uses the sqlite ON CONFLICT
    dialect) — exercised by the unit tests. Production persistence goes through
    scripts/learn_fill_rate.py's dialect-agnostic delete-then-insert, NOT this.
    Do not call against Postgres without switching to a pg/dialect-neutral upsert.
    """
    if not stats:
        return
    if (
        artifact.source != source
        or artifact.symbol != symbol
        or artifact.period_agg != period_agg
        or any(stat.horizon_h != artifact.horizon_h for stat in stats)
    ):
        raise ValueError("artifact scope must match every persisted fill-rate stat")
    values = [
        {
            "source": source,
            "symbol": symbol,
            "period_agg": period_agg,
            "horizon_h": s.horizon_h,
            "spread_bucket_bps": s.spread_bucket_bps,
            "fill_prob": float(s.fill_prob),
            "n_samples": s.n_samples,
            "ttf_p50_ms": s.ttf_p50_ms,
            "ttf_p90_ms": s.ttf_p90_ms,
            "mean_ttf_ms": s.mean_ttf_ms,
            "artifact_hash": artifact.artifact_hash,
            "learned_at": learned_at,
            "candle_range_start_ms": candle_range_start_ms,
            "candle_range_end_ms": candle_range_end_ms,
        }
        for s in stats
    ]
    await ensure_fill_model_artifact(
        session,
        artifact=artifact,
        created_at=learned_at,
    )
    stmt = sqlite_insert(FillRateStatsRow).values(values)
    update_cols = {
        c: getattr(stmt.excluded, c)
        for c in (
            "fill_prob", "n_samples", "ttf_p50_ms", "ttf_p90_ms", "mean_ttf_ms",
            "artifact_hash", "learned_at", "candle_range_start_ms", "candle_range_end_ms",
        )
    }
    stmt = stmt.on_conflict_do_update(index_elements=_PK, set_=update_cols)
    await session.execute(stmt)


async def load_fill_rate_stats(
    session: AsyncSession, *, source: str, symbol: str
) -> list[FillRateStatsRow]:
    stmt = select(FillRateStatsRow).where(
        FillRateStatsRow.source == source,
        FillRateStatsRow.symbol == symbol,
    )
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def load_fill_model_artifact(
    session: AsyncSession,
    *,
    artifact_hash: str,
) -> FillModelArtifact | None:
    result = await session.execute(
        select(FillRateModelArtifactRow).where(
            FillRateModelArtifactRow.artifact_hash == artifact_hash,
        )
    )
    row = result.scalar_one_or_none()
    if row is None:
        return None
    return FillModelArtifact(
        symbol=row.symbol,
        period_agg=row.period_agg,
        horizon_h=row.horizon_h,
        source=row.source,
        model_version=row.model_version,
        schema_version=row.schema_version,
        artifact_hash=row.artifact_hash,
        training_start_ms=row.training_start_ms,
        training_end_ms=row.training_end_ms,
        cutoff_ms=row.cutoff_ms,
        sample_count=row.sample_count,
        confidence_min_samples=row.confidence_min_samples,
        metadata=row.metadata_json,
    )
