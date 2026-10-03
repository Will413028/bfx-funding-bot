"""Pick the web API's read models from the capital authority it booted under."""

from __future__ import annotations

from bfx_funding_bot.core.authority import Authority
from bfx_funding_bot.modules.api.deps import ReadModels
from bfx_funding_bot.modules.execution.legacy_operator_reads import LegacyOperatorReads
from bfx_funding_bot.modules.execution.operator_evidence import LegacyOperatorEvidence
from bfx_funding_bot.modules.execution.uncertainty_resolution import LegacyOperatorResolution
from bfx_funding_bot.modules.ledger.wiring import (
    build_operator_evidence,
    build_operator_reads,
    build_operator_resolution,
)


def select_read_models(authority: Authority) -> ReadModels:
    """Both authorities are covered; ``SUPPORTED_AUTHORITIES`` decides which may boot."""
    if authority == "ledger":
        return ReadModels(
            build_operator_reads(), build_operator_evidence(), build_operator_resolution()
        )
    evidence = LegacyOperatorEvidence()
    return ReadModels(LegacyOperatorReads(), evidence, LegacyOperatorResolution(evidence))
