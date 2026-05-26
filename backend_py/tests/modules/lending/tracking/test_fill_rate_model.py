from dataclasses import dataclass
from decimal import Decimal

import pytest

from bfx_funding_bot.modules.lending.tracking.model import FillRateModel


@dataclass
class _Row:
    period_agg: str
    horizon_h: int
    spread_bucket_bps: int
    fill_prob: float
    n_samples: int
    mean_ttf_ms: int | None


def _rows():
    # period p2, horizon 4: fill_prob 1.0 @ 0bps, 0.5 @ 100bps, 0.0 @ 200bps
    return [
        _Row("p2", 4, 0, 1.0, 100, 1000),
        _Row("p2", 4, 100, 0.5, 100, 5000),
        _Row("p2", 4, 200, 0.0, 5, None),
    ]


def test_interpolates_between_buckets():
    m = FillRateModel.from_rows(_rows())
    # offer 50 bps above ref → halfway between 0bps(1.0) and 100bps(0.5) → 0.75
    est = m.estimate_fill(
        reference_rate=Decimal("0.0003"),
        offer_rate=Decimal("0.0003") * Decimal("1.005"),  # +50 bps
        period_agg="p2", horizon_h=4,
    )
    assert est is not None
    assert est.fill_prob == Decimal("0.75")
    assert est.low_confidence is False


def test_clamps_below_lowest_bucket():
    m = FillRateModel.from_rows(_rows())
    est = m.estimate_fill(
        reference_rate=Decimal("0.0003"),
        offer_rate=Decimal("0.0003") * Decimal("0.90"),  # -1000 bps, below grid
        period_agg="p2", horizon_h=4,
    )
    assert est is not None and est.fill_prob == Decimal("1.0")


def test_low_confidence_when_endpoint_sparse():
    m = FillRateModel.from_rows(_rows())
    # near 200 bps bucket (n_samples=5 < MIN_SAMPLES)
    est = m.estimate_fill(
        reference_rate=Decimal("0.0003"),
        offer_rate=Decimal("0.0003") * Decimal("1.015"),  # +150 bps (between 100 & 200)
        period_agg="p2", horizon_h=4,
    )
    assert est is not None and est.low_confidence is True


def test_unknown_key_returns_none():
    m = FillRateModel.from_rows(_rows())
    assert m.estimate_fill(
        reference_rate=Decimal("0.0003"), offer_rate=Decimal("0.0003"),
        period_agg="p30", horizon_h=4,
    ) is None


def test_bad_reference_raises():
    m = FillRateModel.from_rows(_rows())
    with pytest.raises(ValueError):
        m.estimate_fill(reference_rate=Decimal("0"), offer_rate=Decimal("0.0003"),
                        period_agg="p2", horizon_h=4)
