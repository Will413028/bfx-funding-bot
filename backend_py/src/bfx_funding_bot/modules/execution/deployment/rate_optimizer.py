"""Pure, evidence-gated lending-rate candidate selection.

This module deliberately has no dependency on the executor or execution
contracts.  Its local no-recommendation value therefore cannot accidentally be
submitted as an execution decision.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from types import MappingProxyType
from typing import Literal

CandidateSource = Literal["signal", "maker", "taker"]
OptimizerNoRecommendationReason = Literal["no_eligible_candidate"]
FillModelUnavailableReason = Literal["missing", "low_confidence"]


@dataclass(frozen=True, slots=True)
class FillModelEvidence:
    """High-confidence fill estimate for a single exact-period decision."""

    model_version: str
    artifact_hash: str
    fill_probability: Decimal
    expected_ttf_ms: int | None
    n_samples: int
    symbol: str
    period_agg: str
    horizon_h: int
    cutoff_ms: int
    low_confidence: bool = False

    @property
    def fill_prob(self) -> Decimal:
        """Compatibility spelling used at the pre-optimizer gate seam."""
        return self.fill_probability


@dataclass(frozen=True, slots=True)
class FillModelUnavailable:
    """Typed provider outcome; it is never interpreted as usable evidence."""

    reason: FillModelUnavailableReason


@dataclass(frozen=True, slots=True)
class RateCandidate:
    rate: Decimal
    source: CandidateSource
    fill_evidence: FillModelEvidence | None
    book_evidence: Mapping[str, object]

    def __post_init__(self) -> None:
        if self.source not in {"signal", "maker", "taker"}:
            raise ValueError(f"unsupported candidate source {self.source!r}")
        object.__setattr__(self, "book_evidence", _freeze_mapping(self.book_evidence))


@dataclass(frozen=True, slots=True)
class OptimizationResult:
    selected: RateCandidate
    candidates: tuple[RateCandidate, ...]
    scores: Mapping[str, Decimal]
    model_version: str
    artifact_hash: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "candidates", tuple(self.candidates))
        object.__setattr__(self, "scores", MappingProxyType(dict(self.scores)))


@dataclass(frozen=True, slots=True)
class OptimizerNoRecommendation:
    """Optimizer-local outcome, intentionally distinct from execution NoRecommendation."""

    reason: OptimizerNoRecommendationReason
    candidates: tuple[RateCandidate, ...]


class RateOptimizer:
    """Select the deterministic maximum expected-net-rate candidate."""

    def select(
        self,
        signal_rate: Decimal,
        *,
        maker: RateCandidate | None,
        taker: RateCandidate | None,
        fill_evidence: FillModelEvidence,
        fee_rate: Decimal,
    ) -> OptimizationResult | OptimizerNoRecommendation:
        _validate_fee_rate(fee_rate)
        candidates = [
            RateCandidate(
                rate=signal_rate,
                source="signal",
                fill_evidence=fill_evidence,
                book_evidence={},
            ),
        ]
        candidates.extend(candidate for candidate in (maker, taker) if candidate is not None)

        eligible = tuple(
            candidate
            for candidate in candidates
            if _is_eligible(candidate, signal_rate)
        )
        if not eligible:
            return OptimizerNoRecommendation(
                reason="no_eligible_candidate",
                candidates=(),
            )

        scores: dict[str, Decimal] = {
            candidate.source: _score(
                candidate.rate,
                candidate.fill_evidence or fill_evidence,
                fee_rate,
            )
            for candidate in eligible
        }
        selected = max(
            eligible,
            key=lambda candidate: (
                scores[candidate.source],
                (candidate.fill_evidence or fill_evidence).fill_probability,
                -candidate.rate,
            ),
        )
        return OptimizationResult(
            selected=selected,
            candidates=eligible,
            scores=scores,
            model_version=fill_evidence.model_version,
            artifact_hash=fill_evidence.artifact_hash,
        )


def _is_eligible(candidate: RateCandidate, signal_rate: Decimal) -> bool:
    return (
        candidate.rate.is_finite()
        and signal_rate.is_finite()
        and candidate.rate > 0
        and signal_rate > 0
        and candidate.rate >= signal_rate
    )


def _score(rate: Decimal, evidence: FillModelEvidence, fee_rate: Decimal) -> Decimal:
    return rate * evidence.fill_probability * (Decimal("1") - fee_rate)


def _validate_fee_rate(fee_rate: Decimal) -> None:
    if not fee_rate.is_finite() or fee_rate < 0 or fee_rate > 1:
        raise ValueError("fee_rate must be a finite decimal between zero and one")


def _freeze_mapping(value: Mapping[str, object]) -> Mapping[str, object]:
    return MappingProxyType({key: _freeze_value(item) for key, item in value.items()})


def _freeze_value(value: object) -> object:
    if isinstance(value, Mapping):
        return _freeze_mapping(value)
    if isinstance(value, list | tuple):
        return tuple(_freeze_value(item) for item in value)
    if isinstance(value, set | frozenset):
        return frozenset(_freeze_value(item) for item in value)
    return value
