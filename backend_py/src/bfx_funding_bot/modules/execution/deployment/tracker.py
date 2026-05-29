"""CellDeploymentTracker — in-memory per-cell deployment intent.

Avoids the venue credit->cell attribution problem (the known fcn-mapping pain):
per-cell amounts come from (a) our own successful submits, and (b) proportional
rescaling to the global venue truth after each reconcile. The global allocation
cap is still enforced against authoritative venue exposure; this tracker only
informs the per-cell concentration limit (best-effort).
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

    def reconcile_to_total(self, e_total: Decimal) -> None:
        """Rescale per-cell intent so it sums to the venue truth e_total.

        S=0 (no recorded intent yet, e.g. post-restart with pre-existing credits)
        -> no-op; per-cell stays 0 and the concentration limit is best-effort
        until intent rebuilds.
        """
        s = sum(self._deployed.values(), Decimal("0"))
        if s <= 0:
            return
        factor = e_total / s
        self._deployed = {c: v * factor for c, v in self._deployed.items()}
