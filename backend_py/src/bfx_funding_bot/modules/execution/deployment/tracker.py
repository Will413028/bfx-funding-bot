"""CellDeploymentTracker — in-memory per-cell deployment intent.

Avoids the venue credit->cell attribution problem (the known fcn-mapping pain):
per-cell amounts come from (a) our own successful submits, and (b) proportional
rescaling to the global venue truth after each reconcile. The global allocation
cap is still enforced against authoritative venue exposure; this tracker only
informs the per-cell concentration limit (best-effort).
"""
from __future__ import annotations

import logging
from decimal import Decimal

log = logging.getLogger(__name__)


class CellDeploymentTracker:
    def __init__(self) -> None:
        self._deployed: dict[str, Decimal] = {}

    def deployed(self, cell_id: str) -> Decimal:
        return self._deployed.get(cell_id, Decimal("0"))

    def snapshot(self) -> dict[str, Decimal]:
        return dict(self._deployed)

    def record_deploy(self, cell_id: str, amount: Decimal) -> None:
        self._deployed[cell_id] = self.deployed(cell_id) + amount

    def reconcile_to_total(
        self,
        reserved_total: Decimal,
        *,
        cap_per_cell: Decimal | None = None,
    ) -> None:
        """Rescale per-cell intent to match the *reserved* (pending open-offer) total.

        IMPORTANT: pass ledger.reserved_exposure() here, NOT current_exposure().
        Realized credits are committed to the venue and cannot be attributed to
        any specific cell. Including them in the rescale factor (reserved+realized
        / sum) would produce factor > 1, inflating per-cell intent past
        cap_per_cell and causing allocate_gap to compute negative headroom —
        silently starving that cell until the next boot.

        When our offer fills: reserved drops (offer consumed), so the cell's
        tracked intent shrinks proportionally — correct behaviour.

        S=0 (no recorded intent yet, e.g. post-restart with pre-existing credits)
        -> no-op; per-cell stays 0 and the concentration limit is best-effort
        until intent rebuilds via record_deploy calls.

        cap_per_cell: optional hard clamp applied after rescaling (I2 defense-in-
        depth). Any cell whose rescaled intent exceeds cap_per_cell is clamped
        with a warning so concentration drift is immediately visible in logs.
        """
        s = sum(self._deployed.values(), Decimal("0"))
        if s <= 0:
            return
        factor = reserved_total / s
        rescaled = {c: v * factor for c, v in self._deployed.items()}
        if cap_per_cell is not None:
            clamped: dict[str, Decimal] = {}
            for c, v in rescaled.items():
                if v > cap_per_cell:
                    log.warning(
                        "cell_deployment_clamped cell=%s from=%s to=%s",
                        c, v, cap_per_cell,
                    )
                    clamped[c] = cap_per_cell
                else:
                    clamped[c] = v
            self._deployed = clamped
        else:
            self._deployed = rescaled
