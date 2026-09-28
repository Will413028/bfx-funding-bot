"""Immutable candidate inputs. Importing the facade performs no I/O."""

from bfx_funding_bot.modules.trading_shadow.contracts import (
    CandidateInputs,
    InputManifest,
    LoadedInputs,
    LoadResult,
    NotComparable,
    ScanLimits,
    candidate_input_digest,
    canonical_bytes,
)

__all__ = [
    "CandidateInputs",
    "InputManifest",
    "LoadResult",
    "LoadedInputs",
    "NotComparable",
    "ScanLimits",
    "candidate_input_digest",
    "canonical_bytes",
]
