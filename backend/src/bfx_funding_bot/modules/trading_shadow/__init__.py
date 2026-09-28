"""Immutable candidate inputs. Importing the facade performs no I/O."""

from bfx_funding_bot.modules.trading_shadow.contracts import (
    BaselineReader,
    CandidateInputs,
    CandidateReader,
    ComparisonHeads,
    FieldDifference,
    InputManifest,
    LoadedInputs,
    LoadResult,
    NotComparable,
    ScanLimits,
    ShadowComparison,
    candidate_input_digest,
    canonical_bytes,
)

__all__ = [
    "BaselineReader",
    "CandidateInputs",
    "CandidateReader",
    "ComparisonHeads",
    "FieldDifference",
    "InputManifest",
    "LoadResult",
    "LoadedInputs",
    "NotComparable",
    "ScanLimits",
    "ShadowComparison",
    "candidate_input_digest",
    "canonical_bytes",
]
