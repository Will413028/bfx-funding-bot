from dataclasses import dataclass
from decimal import Decimal

import pytest

from bfx_funding_bot.modules.lending.tracking.artifact import FillModelArtifact
from bfx_funding_bot.modules.lending.tracking.model import FillModelEvidence, FillRateModel


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
    artifact_hash: str


def _artifact() -> FillModelArtifact:
    return FillModelArtifact(
        symbol="fUSD", period_agg="p2", horizon_h=4, source="candle",
        model_version="g13-candle-v1", schema_version=1, artifact_hash="artifact-v1",
        training_start_ms=0, training_end_ms=10_000, cutoff_ms=10_000,
        sample_count=200, confidence_min_samples=30,
    )


def _rows():
    # period p2, horizon 4: fill_prob 1.0 @ 0bps, 0.5 @ 100bps, 0.0 @ 200bps
    return [
        _Row("candle", "fUSD", "p2", 4, 0, 1.0, 100, 1000, "artifact-v1"),
        _Row("candle", "fUSD", "p2", 4, 100, 0.5, 100, 5000, "artifact-v1"),
        _Row("candle", "fUSD", "p2", 4, 200, 0.0, 5, None, "artifact-v1"),
    ]


def test_interpolates_between_buckets():
    m = FillRateModel.from_rows(_rows(), artifact=_artifact())
    # offer 50 bps above ref → halfway between 0bps(1.0) and 100bps(0.5) → 0.75
    est = m.estimate_fill(
        reference_rate=Decimal("0.0003"),
        offer_rate=Decimal("0.0003") * Decimal("1.005"),  # +50 bps
        period_agg="p2", horizon_h=4,
    )
    assert isinstance(est, FillModelEvidence)
    assert est.fill_prob == Decimal("0.75")


def test_clamps_below_lowest_bucket():
    m = FillRateModel.from_rows(_rows(), artifact=_artifact())
    est = m.estimate_fill(
        reference_rate=Decimal("0.0003"),
        offer_rate=Decimal("0.0003") * Decimal("0.90"),  # -1000 bps, below grid
        period_agg="p2", horizon_h=4,
    )
    assert est is not None and est.fill_prob == Decimal("1.0")


def test_low_confidence_when_endpoint_sparse():
    m = FillRateModel.from_rows(_rows(), artifact=_artifact())
    # near 200 bps bucket (n_samples=5 < MIN_SAMPLES)
    est = m.estimate_fill(
        reference_rate=Decimal("0.0003"),
        offer_rate=Decimal("0.0003") * Decimal("1.015"),  # +150 bps (between 100 & 200)
        period_agg="p2", horizon_h=4,
    )
    assert est.__class__.__name__ == "FillModelUnavailable"
    assert est.reason == "low_confidence"


def test_unknown_key_returns_none():
    m = FillRateModel.from_rows(_rows(), artifact=_artifact())
    result = m.estimate_fill(
        reference_rate=Decimal("0.0003"), offer_rate=Decimal("0.0003"),
        period_agg="p30", horizon_h=4,
    )
    assert result.__class__.__name__ == "FillModelUnavailable"
    assert result.reason == "scope_mismatch"


def test_bad_reference_raises():
    m = FillRateModel.from_rows(_rows(), artifact=_artifact())
    with pytest.raises(ValueError):
        m.estimate_fill(reference_rate=Decimal("0"), offer_rate=Decimal("0.0003"),
                        period_agg="p2", horizon_h=4)
