"""The ledger's read side: what the control plane may hold (ADR 2026-10-19 D6).

Nothing here writes, takes the scope lock or bumps the clock. The writers in ``_internal``
import from here, never the reverse: ``control-plane-exposure`` lists this package as a
source, so neither it nor the web API may reach ``_internal``, ``wiring`` or the lock.
These builders are how ``apps`` composes the read ports; they carry no access rule
(ADR 2026-10-19 D3'': nothing here reads env, opens a connection or starts a task).
"""

from __future__ import annotations

from bfx_funding_bot.modules.ledger import (
    ExecutionHistory,
    OperatorEvidence,
    OperatorReads,
    PolicyReader,
    Scope,
)
from bfx_funding_bot.modules.ledger.read import (
    execution_history,
    match_evidence,
    operator_evidence,
    operator_reads,
    policy,
)


def build_operator_reads() -> OperatorReads:
    """The ledger operator read model."""
    return operator_reads.LedgerOperatorReads()


def build_operator_evidence() -> OperatorEvidence:
    """Operator evidence with the attempt match preview, which reads granted columns only
    (any role may run it); the worker's authority is ``wiring.build_operator_resolution().apply``."""
    return operator_evidence.LedgerOperatorEvidence(
        operator_reads.LedgerOperatorReads(), match_evidence.preview_match
    )


def build_execution_history(archive: ExecutionHistory) -> ExecutionHistory:
    """The ledger's execution history: the journal above the switch, ``archive`` (the frozen
    legacy event log) below it, behind one cursor (plan Q4)."""
    return execution_history.LedgerExecutionHistory(archive)


def build_policy_reader(scope: Scope) -> PolicyReader:
    """The applied capital policy of ``scope``, read only."""
    return policy.LedgerPolicyReader(scope)


__all__ = [
    "build_execution_history",
    "build_operator_evidence",
    "build_operator_reads",
    "build_policy_reader",
]
