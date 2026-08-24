"""Immutable provenance for a learned empirical fill-rate model."""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Literal


@dataclass(frozen=True)
class FillModelArtifact:
    symbol: str
    period_agg: str
    horizon_h: int
    source: str
    model_version: str
    schema_version: int
    artifact_hash: str
    training_start_ms: int
    training_end_ms: int
    cutoff_ms: int
    sample_count: int
    confidence_min_samples: int
    metadata: Mapping[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class FillModelEvidence:
    fill_prob: Decimal
    expected_ttf_ms: int | None
    n_samples: int
    symbol: str
    period_agg: str
    horizon_h: int
    model_version: str
    artifact_hash: str
    cutoff_ms: int


@dataclass(frozen=True)
class FillModelUnavailable:
    reason: Literal["missing", "low_confidence", "scope_mismatch", "unversioned"]
