"""CellDeploymentTracker — in-memory per-cell sum of the amounts the reconciler submitted.

``record_deploy`` adds each acknowledged submit. Capital authorization does not read it: the
per-cell limit comes from the ledger's ``CapitalAvailable`` budget.
"""
from __future__ import annotations

from decimal import Decimal


class CellDeploymentTracker:
    def __init__(self) -> None:
        self._deployed: dict[str, Decimal] = {}

    def deployed(self, cell_id: str) -> Decimal:
        return self._deployed.get(cell_id, Decimal("0"))

    def snapshot(self) -> dict[str, Decimal]:
        return dict(self._deployed)

    def record_deploy(self, cell_id: str, amount: Decimal) -> None:
        self._deployed[cell_id] = self.deployed(cell_id) + amount

