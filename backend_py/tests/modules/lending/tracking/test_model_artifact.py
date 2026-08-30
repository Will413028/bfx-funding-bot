from dataclasses import dataclass
from decimal import Decimal

from bfx_funding_bot.modules.lending.tracking.artifact import FillModelArtifact
from bfx_funding_bot.modules.lending.tracking.model import (
    FillModelUnavailable,
    FillRateModel,
)


@dataclass
class _Row:
    source: str
    symbol: str
    period_agg: str
    horizon_h: int
    spread_bucket_bps: int
    fill_prob: float
    n_samples: int
    mean_ttf_ms: int | None
    artifact_hash: str | None


def _artifact() -> FillModelArtifact:
    return FillModelArtifact(
        symbol="fUSD",
        period_agg="p2",
        horizon_h=4,
        source="candle",
        model_version="g13-candle-v1",
        schema_version=1,
        artifact_hash="artifact-v1",
        training_start_ms=0,
        training_end_ms=10_000,
        cutoff_ms=10_000,
        sample_count=100,
        confidence_min_samples=30,
    )


def _rows_without_artifact_hash() -> list[_Row]:
    return [
        _Row("candle", "fUSD", "p2", 4, 0, 1.0, 100, 1_000, None),
        _Row("candle", "fUSD", "p2", 4, 100, 0.5, 100, 5_000, None),
    ]


def test_unversioned_model_is_unavailable() -> None:
    model = FillRateModel.from_rows(_rows_without_artifact_hash(), artifact=None)

    result = model.estimate_fill(
        reference_rate=Decimal("0.00020"),
        offer_rate=Decimal("0.00021"),
        period_agg="p2",
        horizon_h=4,
    )

    assert isinstance(result, FillModelUnavailable)
    assert result.reason == "unversioned"


def test_artifact_scope_mismatch_is_unavailable() -> None:
    rows = [
        _Row("candle", "fUSD", "p30", 4, 0, 1.0, 100, 1_000, "artifact-v1"),
    ]
    model = FillRateModel.from_rows(rows, artifact=_artifact())

    result = model.estimate_fill(
        reference_rate=Decimal("0.00020"),
        offer_rate=Decimal("0.00020"),
        period_agg="p2",
        horizon_h=4,
    )

    assert isinstance(result, FillModelUnavailable)
    assert result.reason == "scope_mismatch"


def test_empty_artifact_model_exposes_missing_readiness() -> None:
    model = FillRateModel.from_rows([], artifact=_artifact())

    assert model.unavailable_reason == "missing"
