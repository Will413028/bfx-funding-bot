"""Pick the web API's read models from the capital authority it booted under."""

from __future__ import annotations

from bfx_funding_bot.core.authority import Authority
from bfx_funding_bot.modules.api.deps import ReadModels
from bfx_funding_bot.modules.execution.legacy_operator_reads import (
    LegacyExecutionHistory,
    LegacyOperatorReads,
)
from bfx_funding_bot.modules.execution.operator_evidence import LegacyOperatorEvidence
from bfx_funding_bot.modules.execution.uncertainty_resolution import LegacyOperatorResolution
from bfx_funding_bot.modules.ledger.wiring import (
    build_execution_history,
    build_operator_evidence,
    build_operator_reads,
    build_operator_resolution,
)


def select_read_models(authority: Authority) -> ReadModels:
    """Both authorities are covered; ``apps/authority_support.py`` decides which may boot."""
    if authority == "ledger":
        # The history continues below the switch in the frozen legacy event log (plan Q4).
        return ReadModels(
            build_operator_reads(), build_operator_evidence(), build_operator_resolution(),
            build_execution_history(LegacyExecutionHistory()),
        )
    evidence = LegacyOperatorEvidence()
    return ReadModels(LegacyOperatorReads(), evidence, LegacyOperatorResolution(evidence),
                      LegacyExecutionHistory())
