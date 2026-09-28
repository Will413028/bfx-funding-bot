"""Reproducible, non-authorizing S0-2a input bundle; no baseline placeholders."""

import json
from collections.abc import Mapping
from dataclasses import dataclass, fields, is_dataclass
from decimal import Decimal
from hashlib import sha256
from typing import Literal, Protocol
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from bfx_funding_bot.modules.execution.capital_shadow_port import BaselineResult
from bfx_funding_bot.modules.trading import (
    AcceptedCapitalBasis,
    AppliedPolicy,
    AttemptFact,
    CapitalReadContext,
    CapitalResult,
    CapitalScope,
    DifferenceClassification,
    UncertaintyFact,
)


def _canonical(value: object) -> object:
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError("non-finite Decimal")
        if value == 0:
            return "0"
        fixed = format(value, "f")
        return fixed.rstrip("0").rstrip(".") if "." in fixed else fixed
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, InputManifest):
        return _canonical(value.digest_payload())
    if is_dataclass(value) and not isinstance(value, type):
        return {field.name: _canonical(getattr(value, field.name)) for field in fields(value)}
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise TypeError("canonical mappings require string keys")
        return {key: _canonical(item) for key, item in value.items()}
    if isinstance(value, (set, frozenset)):
        return sorted((_canonical(item) for item in value), key=canonical_bytes)
    if isinstance(value, (tuple, list)):
        return [_canonical(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise TypeError(f"unsupported canonical value: {type(value).__name__}")


def canonical_bytes(value: object) -> bytes:
    """UTF-8 canonical JSON; sequence order is semantic, set order is not."""
    return json.dumps(
        _canonical(value),
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
        ensure_ascii=False,
    ).encode("utf-8")


def candidate_input_digest(value: object) -> str:
    return sha256(canonical_bytes(value)).hexdigest()


@dataclass(frozen=True, slots=True)
class ScanLimits:
    max_history_rows: int = 10_000
    max_payload_bytes: int = 16_000_000
    max_reference_lookups: int = 10_000

    def __post_init__(self) -> None:
        if any(
            type(value) is not int or value < 1
            for value in (
                self.max_history_rows,
                self.max_payload_bytes,
                self.max_reference_lookups,
            )
        ):
            raise ValueError("scan limits must be positive integers")


@dataclass(frozen=True, slots=True)
class CandidateInputs:
    scope: CapitalScope
    policy: AppliedPolicy
    accepted: AcceptedCapitalBasis
    attempts: tuple[AttemptFact, ...]
    uncertainties: tuple[UncertaintyFact, ...]
    read_context: CapitalReadContext


@dataclass(frozen=True, slots=True)
class InputManifest:
    """Candidate-only manifest, including immutable canonical raw evidence.

    JSON bytes retain missing versus null and every consumed raw payload. They
    cannot be mutated through a frozen dataclass's nested dict. Heads and scan
    details are also canonical bytes so no SQLAlchemy/mutable rows escape.
    A baseline bundle and comparison schema belong to S0-2b, not this digest.
    """

    source_revision: str
    scope: CapitalScope
    now_ms: int
    max_snapshot_age_ms: int
    limits: ScanLimits
    decimal_context: tuple[tuple[str, str], ...]
    heads_json: bytes
    evidence_json: bytes
    work_json: bytes
    evidence_digests: tuple[tuple[str, str], ...]
    normalized_facts_digest: str
    manifest_version: int = 1
    loader_version: int = 1
    kind: Literal["fold_comparison"] = "fold_comparison"

    def digest_payload(self) -> dict[str, object]:
        return {
            **{
                field.name: getattr(self, field.name)
                for field in fields(self)
                if not field.name.endswith("_json")
            },
            "heads": json.loads(self.heads_json),
            "evidence": json.loads(self.evidence_json),
            "work": json.loads(self.work_json),
        }


@dataclass(frozen=True, slots=True)
class LoadedInputs:
    inputs: CandidateInputs
    manifest: InputManifest
    candidate_input_digest: str
    status: Literal["loaded"] = "loaded"


@dataclass(frozen=True, slots=True)
class NotComparable:
    reason: str
    evidence: tuple[tuple[str, str], ...] = ()
    classification: DifferenceClassification = "input_evidence_gap"
    status: Literal["not_comparable"] = "not_comparable"


type LoadResult = LoadedInputs | NotComparable


class BaselineReader(Protocol):
    async def __call__(
        self, session: AsyncSession, *, scope: CapitalScope, now_ms: int,
        max_snapshot_age_ms: int,
    ) -> BaselineResult: ...


class CandidateReader(Protocol):
    async def load(
        self, session: AsyncSession, *, scope: CapitalScope, now_ms: int,
        max_snapshot_age_ms: int,
    ) -> LoadResult: ...


@dataclass(frozen=True, slots=True)
class FieldDifference:
    path: str
    candidate: object
    baseline: object


@dataclass(frozen=True, slots=True)
class ComparisonHeads:
    watermark: int | None = None
    projection_cursor: int | None = None


@dataclass(frozen=True, slots=True)
class ShadowComparison:
    kind: Literal["fold_comparison"]
    status: Literal["equal", "different", "not_comparable", "error"]
    candidate: CapitalResult | None
    baseline: CapitalResult | None
    differences: tuple[FieldDifference, ...]
    classifications: tuple[DifferenceClassification, ...]
    candidate_input_digest: str | None
    baseline_input_digest: None
    baseline_evidence_complete: Literal[False]
    baseline_observation_digest: str | None
    heads: ComparisonHeads
    reason: str | None = None
