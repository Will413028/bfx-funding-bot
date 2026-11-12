"""The web API's read models: the ledger's, with the execution history continuing below
the switch into the frozen legacy event log (plan Q4).

Built from ``ledger.read`` only: the control plane holds no writing port
(``control-plane-exposure``, ADR 2026-10-19 D6).
"""

from __future__ import annotations

from bfx_funding_bot.modules.api.deps import ReadModels
from bfx_funding_bot.modules.execution.archived_execution_history import (
    ArchivedExecutionHistory,
)
from bfx_funding_bot.modules.ledger.read import (
    build_execution_history,
    build_operator_evidence,
    build_operator_reads,
    build_policy_reader,
)


def build_read_models(deployment_environment: str | None) -> ReadModels:
    """The web API boots only on the ``ledger`` authority (``apps/authority_support.py``).

    ``deployment_environment`` is what the lifespan read (None without a valid value);
    only the archived event log's read uses it.
    """
    return ReadModels(
        build_operator_reads(), build_operator_evidence(),
        build_execution_history(ArchivedExecutionHistory(deployment_environment)),
        build_policy_reader,
    )
