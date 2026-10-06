"""The web API's read models: the ledger's, with the execution history continuing below
the switch into the frozen legacy event log (plan Q4)."""

from __future__ import annotations

from bfx_funding_bot.modules.api.deps import ReadModels
from bfx_funding_bot.modules.execution.archived_execution_history import (
    ArchivedExecutionHistory,
)
from bfx_funding_bot.modules.ledger.wiring import (
    build_execution_history,
    build_operator_evidence,
    build_operator_reads,
    build_operator_resolution,
)


def select_read_models() -> ReadModels:
    """The web API boots only on the ``ledger`` authority (``apps/authority_support.py``)."""
    return ReadModels(
        build_operator_reads(), build_operator_evidence(), build_operator_resolution(),
        build_execution_history(ArchivedExecutionHistory()),
    )
