"""Immutable provenance for a learned empirical fill-rate model."""
from __future__ import annotations

import json
import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from types import MappingProxyType
from typing import Literal, cast


def _freeze_metadata(value: object) -> object:
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise TypeError("unsupported non-finite fill model metadata value")
        return value
    if isinstance(value, Mapping):
        frozen: dict[str, object] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise TypeError("fill model metadata keys must be strings")
            frozen[key] = _freeze_metadata(item)
        return MappingProxyType(frozen)
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_metadata(item) for item in value)
    if isinstance(value, (set, frozenset)):
        return frozenset(_freeze_metadata(item) for item in value)
    raise TypeError(
        f"unsupported fill model metadata value: {type(value).__name__}"
    )


def _json_sort_key(value: object) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def _thaw_metadata(value: object) -> object:
    if isinstance(value, Mapping):
        return {key: _thaw_metadata(value[key]) for key in sorted(value)}
    if isinstance(value, tuple):
        return [_thaw_metadata(item) for item in value]
    if isinstance(value, frozenset):
        items = [_thaw_metadata(item) for item in value]
        return sorted(items, key=_json_sort_key)
    return value


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

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "metadata",
            cast(Mapping[str, object], _freeze_metadata(self.metadata)),
        )

    def metadata_for_storage(self) -> dict[str, object]:
        """Return a detached JSON-compatible copy for persistence."""
        return cast(dict[str, object], _thaw_metadata(self.metadata))


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
