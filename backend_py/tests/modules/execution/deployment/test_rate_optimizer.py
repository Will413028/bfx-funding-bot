from decimal import Decimal

import pytest

from bfx_funding_bot.modules.execution.deployment.rate_optimizer import (
    OptimizerNoRecommendation,
    RateCandidate,
    RateOptimizer,
)
from bfx_funding_bot.modules.lending.tracking.artifact import FillModelEvidence


def _evidence(
    *,
    fill_probability: str = "0.95",
    model_version: str = "fill-v1",
    artifact_hash: str = "sha256:model",
) -> FillModelEvidence:
    return FillModelEvidence(
        model_version=model_version,
        artifact_hash=artifact_hash,
        fill_prob=Decimal(fill_probability),
        expected_ttf_ms=500,
        n_samples=100,
        symbol="fUST",
        period_agg="p2",
        horizon_h=1,
        cutoff_ms=1_000,
    )


def _candidate(
    rate: str,
    source: str,
    *,
    fill_probability: str | None = None,
    book_evidence: dict[str, object] | None = None,
) -> RateCandidate:
    return RateCandidate(
        rate=Decimal(rate),
        source=source,  # type: ignore[arg-type]
        fill_evidence=(
            _evidence(fill_probability=fill_probability)
            if fill_probability is not None
            else None
        ),
        book_evidence=book_evidence or {"snapshot_id": "book-1", "period_days": 2},
    )


def test_selects_highest_expected_net_rate() -> None:
    """A lower probability exact-period quote must lose to a higher expected net rate."""
    result = RateOptimizer().select(
        signal_rate=Decimal("0.00020"),
        maker=_candidate("0.00021", "maker", fill_probability="0.95"),
        taker=_candidate("0.00020", "taker", fill_probability="0.90"),
        fill_evidence=_evidence(),
        fee_rate=Decimal("0.15"),
    )

    assert result.selected.source == "maker"
    assert result.scores["maker"] == Decimal("0.000169575")
    assert result.scores["taker"] == Decimal("0.00015300")


def test_excludes_exact_period_quote_below_signal_floor() -> None:
    """A stale or adverse exact-period quote cannot lower the signal floor."""
    result = RateOptimizer().select(
        signal_rate=Decimal("0.00020"),
        maker=_candidate("0.00019", "maker"),
        taker=None,
        fill_evidence=_evidence(),
        fee_rate=Decimal("0"),
    )

    assert result.selected.source == "signal"
    assert tuple(candidate.source for candidate in result.candidates) == ("signal",)


def test_breaks_equal_score_ties_by_fill_probability_then_lower_quote_rate() -> None:
    """Equal net return is ordered deterministically without rate-only preference."""
    result = RateOptimizer().select(
        signal_rate=Decimal("0.00020"),
        maker=_candidate("0.00020", "maker", fill_probability="0.50"),
        taker=_candidate("0.00025", "taker", fill_probability="0.40"),
        fill_evidence=_evidence(fill_probability="0.10"),
        fee_rate=Decimal("0"),
    )

    assert result.selected.source == "maker"


def test_copies_candidate_provenance_before_returning_frozen_result() -> None:
    """Caller mutation cannot rewrite an optimizer result already selected for audit."""
    evidence = {"snapshot_id": "book-1", "levels": ["0.00021"]}
    candidate = _candidate("0.00021", "maker", book_evidence=evidence)

    result = RateOptimizer().select(
        signal_rate=Decimal("0.00020"),
        maker=candidate,
        taker=None,
        fill_evidence=_evidence(),
        fee_rate=Decimal("0"),
    )
    evidence["snapshot_id"] = "rewritten"
    evidence["levels"].append("0.99999")

    assert result.selected.book_evidence == {
        "snapshot_id": "book-1", "levels": ("0.00021",),
    }


def test_returns_optimizer_local_no_recommendation_for_no_eligible_candidates() -> None:
    """Optimizer-local outcomes never fabricate an execution decision id."""
    result = RateOptimizer().select(
        signal_rate=Decimal("0"),
        maker=_candidate("-0.00001", "maker"),
        taker=None,
        fill_evidence=_evidence(),
        fee_rate=Decimal("0"),
    )

    assert isinstance(result, OptimizerNoRecommendation)
    assert result.reason == "no_eligible_candidate"


def test_accepts_task7_canonical_fill_model_evidence() -> None:
    """The optimizer consumes the canonical Task 7 evidence contract."""
    evidence = _evidence()

    result = RateOptimizer().select(
        signal_rate=Decimal("0.00020"),
        maker=_candidate("0.00021", "maker"),
        taker=None,
        fill_evidence=evidence,
        fee_rate=Decimal("0.15"),
    )

    assert result.model_version == evidence.model_version
    assert result.artifact_hash == evidence.artifact_hash
    assert result.scores["signal"] == Decimal("0.0001615")
    assert result.selected.source == "maker"


def test_rejects_unsupported_mutable_provenance() -> None:
    """Frozen audit values must not retain aliases to unsupported mutables."""
    with pytest.raises(TypeError, match=r"immutable|JSON-safe|unsupported"):
        RateCandidate(
            rate=Decimal("0.00021"),
            source="maker",
            fill_evidence=None,
            book_evidence={"raw": bytearray(b"mutable")},
        )
