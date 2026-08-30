"""Pure, evidence-gated lending-rate candidate selection.

This module deliberately has no dependency on the executor or execution
contracts.  Its local no-recommendation value therefore cannot accidentally be
submitted as an execution decision.  Fill evidence is imported from the
lending-tracking contract so the optimizer and fill model cannot drift apart.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from math import isfinite
from types import MappingProxyType
from typing import Literal

from bfx_funding_bot.modules.lending.tracking.artifact import (
    FillModelEvidence,
    FillModelUnavailable,  # noqa: F401 - canonical compatibility re-export
)

CandidateSource = Literal["signal", "maker", "taker"]
OptimizerNoRecommendationReason = Literal[
    "no_eligible_candidate",
    "evidence_scope_mismatch",
    "optimizer_error",
]


@dataclass(frozen=True, slots=True)
class RateCandidate:
    rate: Decimal
    source: CandidateSource
    fill_evidence: FillModelEvidence | None
    book_evidence: Mapping[str, object]

    def __post_init__(self) -> None:
        if self.source not in {"signal", "maker", "taker"}:
            raise ValueError(f"unsupported candidate source {self.source!r}")
        if self.fill_evidence is not None:
            if not isinstance(self.fill_evidence, FillModelEvidence):
                raise TypeError("fill_evidence must use the canonical FillModelEvidence")
            _validate_fill_evidence(self.fill_evidence)
        object.__setattr__(self, "book_evidence", _freeze_mapping(self.book_evidence))


@dataclass(frozen=True, slots=True)
class OptimizationResult:
    selected: RateCandidate
    candidates: tuple[RateCandidate, ...]
    scores: Mapping[str, Decimal]
    model_version: str
    artifact_hash: str

    def __post_init__(self) -> None:
        if not isinstance(self.selected, RateCandidate):
            raise TypeError("selected must be a RateCandidate")
        object.__setattr__(self, "candidates", tuple(self.candidates))
        if not all(isinstance(candidate, RateCandidate) for candidate in self.candidates):
            raise TypeError("candidates must contain only RateCandidate values")
        object.__setattr__(self, "scores", _freeze_scores(self.scores))
        if not isinstance(self.model_version, str) or not isinstance(self.artifact_hash, str):
            raise TypeError("optimizer provenance must use string model and artifact identifiers")


@dataclass(frozen=True, slots=True)
class OptimizerNoRecommendation:
    """Optimizer-local outcome, intentionally distinct from execution NoRecommendation."""

    reason: OptimizerNoRecommendationReason
    candidates: tuple[RateCandidate, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "candidates", tuple(self.candidates))
        if not all(isinstance(candidate, RateCandidate) for candidate in self.candidates):
            raise TypeError("candidates must contain only RateCandidate values")


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
        if not isinstance(fill_evidence, FillModelEvidence):
            raise TypeError("fill_evidence must use the canonical FillModelEvidence")
        _validate_fill_evidence(fill_evidence)
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

        for candidate in (maker, taker):
            if (
                candidate is not None
                and candidate.fill_evidence is not None
                and not _same_evidence_scope(candidate.fill_evidence, fill_evidence)
            ):
                return OptimizerNoRecommendation(
                    reason="evidence_scope_mismatch",
                    candidates=(),
                )

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
                (candidate.fill_evidence or fill_evidence).fill_prob,
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
    return rate * evidence.fill_prob * (Decimal("1") - fee_rate)


def _validate_fee_rate(fee_rate: Decimal) -> None:
    if not fee_rate.is_finite() or fee_rate < 0 or fee_rate > 1:
        raise ValueError("fee_rate must be a finite decimal between zero and one")


def _freeze_mapping(value: Mapping[str, object]) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise TypeError("book evidence must be a mapping")
    frozen: dict[str, object] = {}
    for key, item in value.items():
        if not isinstance(key, str):
            raise TypeError("book evidence keys must be strings")
        frozen[key] = _freeze_value(item)
    return MappingProxyType(frozen)


def _freeze_value(value: object) -> object:
    if value is None or isinstance(value, bool | int | str):
        return value
    if isinstance(value, float):
        if not isfinite(value):
            raise TypeError("book evidence floats must be finite")
        return value
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise TypeError("book evidence decimals must be finite")
        return value
    if isinstance(value, Mapping):
        return _freeze_mapping(value)
    if isinstance(value, list | tuple):
        return tuple(_freeze_value(item) for item in value)
    raise TypeError(
        f"unsupported mutable or non-JSON-safe evidence value: {type(value).__name__}",
    )


def _freeze_scores(scores: Mapping[str, Decimal]) -> Mapping[str, Decimal]:
    frozen: dict[str, Decimal] = {}
    for source, score in scores.items():
        if not isinstance(source, str) or not isinstance(score, Decimal):
            raise TypeError("optimizer scores must map string sources to Decimal values")
        if not score.is_finite():
            raise TypeError("optimizer scores must be finite")
        frozen[source] = score
    return MappingProxyType(frozen)


def _validate_fill_evidence(evidence: FillModelEvidence) -> None:
    """Reject malformed canonical values before they can retain mutable aliases."""
    if not isinstance(evidence.fill_prob, Decimal):
        raise TypeError("canonical fill evidence fill_prob must be a Decimal")
    if (
        not evidence.fill_prob.is_finite()
        or evidence.fill_prob < Decimal("0")
        or evidence.fill_prob > Decimal("1")
    ):
        raise TypeError("canonical fill evidence fill_prob must be finite between zero and one")
    if evidence.expected_ttf_ms is not None and (
        isinstance(evidence.expected_ttf_ms, bool)
        or not isinstance(evidence.expected_ttf_ms, int)
        or evidence.expected_ttf_ms < 0
    ):
        raise TypeError("canonical fill evidence expected_ttf_ms must be a non-negative integer")
    if (
        isinstance(evidence.n_samples, bool)
        or not isinstance(evidence.n_samples, int)
        or evidence.n_samples < 0
    ):
        raise TypeError("canonical fill evidence n_samples must be a non-negative integer")
    if not isinstance(evidence.symbol, str) or not evidence.symbol:
        raise TypeError("canonical fill evidence symbol must be a non-empty string")
    if not isinstance(evidence.period_agg, str) or not evidence.period_agg:
        raise TypeError("canonical fill evidence period_agg must be a non-empty string")
    if (
        isinstance(evidence.horizon_h, bool)
        or not isinstance(evidence.horizon_h, int)
        or evidence.horizon_h <= 0
    ):
        raise TypeError("canonical fill evidence horizon_h must be a positive integer")
    if not isinstance(evidence.model_version, str) or not evidence.model_version:
        raise TypeError("canonical fill evidence model_version must be a non-empty string")
    if not isinstance(evidence.artifact_hash, str) or not evidence.artifact_hash:
        raise TypeError("canonical fill evidence artifact_hash must be a non-empty string")
    if (
        isinstance(evidence.cutoff_ms, bool)
        or not isinstance(evidence.cutoff_ms, int)
        or evidence.cutoff_ms < 0
    ):
        raise TypeError("canonical fill evidence cutoff_ms must be a non-negative integer")


def _same_evidence_scope(
    candidate: FillModelEvidence,
    global_evidence: FillModelEvidence,
) -> bool:
    return (
        candidate.symbol == global_evidence.symbol
        and candidate.period_agg == global_evidence.period_agg
        and candidate.horizon_h == global_evidence.horizon_h
        and candidate.model_version == global_evidence.model_version
        and candidate.artifact_hash == global_evidence.artifact_hash
    )
