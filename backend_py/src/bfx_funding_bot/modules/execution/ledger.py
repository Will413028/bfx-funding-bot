"""PaperPositionLedger — in-memory dual counter loaded from PostgreSQL snapshot.

PostgreSQL event_log is SoT. On daemon startup, from_snapshot reads
position_state (reserved_usdt / realized_usdt) to rebuild in-memory
counters. Subscribe to DomainEventBus for live updates.

reserved: open reservations (offer placed, no match yet). AllocationCap
uses reserved+realized for pre-trade reservation check.
realized: matched credits (actual exposure earning APR). L2 guards
(DrawdownGuard / DivergenceRateGuard, Phase 4.4) use realized only.

floor-at-0 on RELEASE without prior CLAIM: edge case where CLAIMED
event not yet reflected in snapshot. Tracked via replay_floor_hit_count;
pre-prod CI expects 0.
"""
from __future__ import annotations

import logging
from decimal import Decimal
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

from bfx_funding_bot.modules.execution.events import (
    OrderFilled,
    PositionReconciled,
    ReservationClaimed,
    ReservationReleased,
)

log = logging.getLogger(__name__)


class PaperPositionLedger:
    def __init__(self, account_id: str) -> None:
        self.account_id = account_id
        self._reserved = Decimal("0")
        self._realized = Decimal("0")
        self._available = Decimal("0")
        self.replay_floor_hit_count = 0
        self._processed_fills: set[tuple[str, int | None]] = set()
        self._processed_releases: set[tuple[str, int | None]] = set()

    # ---------- cold-start loader (PostgreSQL snapshot) ----------

    @classmethod
    async def from_snapshot(
        cls,
        session: AsyncSession,
        *,
        account_id: str,
        deployment_environment: str,
    ) -> PaperPositionLedger:
        """Load ledger counters from the position_state snapshot table (no replay)."""
        from sqlalchemy import select

        from bfx_funding_bot.modules.execution.event_store.tables import PositionStateRow

        ledger = cls(account_id=account_id)
        row = (
            await session.execute(
                select(PositionStateRow).where(
                    PositionStateRow.account_id == account_id,
                    PositionStateRow.deployment_environment == deployment_environment,
                )
            )
        ).scalar_one_or_none()
        if row is not None:
            ledger._reserved = Decimal(str(row.reserved_usdt))
            ledger._realized = Decimal(str(row.realized_usdt))
        return ledger

    # ---------- live update handlers (DomainEventBus subscribers) ----------

    async def on_reservation_claimed(self, event: ReservationClaimed) -> None:
        if event.account_id != self.account_id:
            return
        self._reserved += event.size_usdt

    async def on_order_filled(self, event: OrderFilled) -> None:
        if event.account_id != self.account_id:
            return
        key = (event.venue_offer_id, event.venue_seq)
        if key in self._processed_fills:
            log.debug("ledger_dedup filled %s", key)
            return
        self._processed_fills.add(key)
        delta = min(self._reserved, event.size_usdt)
        self._reserved -= delta
        if delta < event.size_usdt:
            self.replay_floor_hit_count += 1
            log.warning(
                "order_filled_without_claim cid=%d offer=%s expected=%.2f applied=%.2f",
                event.cid, event.venue_offer_id,
                float(event.size_usdt), float(delta),
            )
        self._realized += event.size_usdt

    async def on_reservation_released(self, event: ReservationReleased) -> None:
        if event.account_id != self.account_id:
            return
        key = (event.venue_offer_id, event.venue_seq)
        if key in self._processed_releases:
            log.debug("ledger_dedup released %s", key)
            return
        self._processed_releases.add(key)
        delta = min(self._reserved, event.size_usdt)
        self._reserved -= delta
        if delta < event.size_usdt:
            self.replay_floor_hit_count += 1
            log.warning(
                "reservation_release_without_claim cid=%d offer=%s expected=%.2f applied=%.2f reason=%s",
                event.cid, event.venue_offer_id,
                float(event.size_usdt), float(delta), event.reason,
            )

    async def on_position_reconciled(self, event: PositionReconciled) -> None:
        """Absolute set from venue snapshot — NOT a delta.

        Overwrites reserved/realized with the authoritative venue values.
        Called after each reconcile tick (boot + periodic). The next WS delta
        that arrives will temporarily diverge; the next reconcile corrects it.
        """
        if event.account_id != self.account_id:
            return
        self._reserved = event.reserved_usdt
        self._realized = event.realized_usdt
        self._available = event.available_usdt

    # ---------- public getters ----------

    def current_exposure(self) -> Decimal:
        """For AllocationCapGuard: reserved + realized = capital committed at venue."""
        return self._reserved + self._realized

    def reserved_exposure(self) -> Decimal:
        """Pending open-offer capital only (placed but not yet matched).

        Used by CellDeploymentTracker.reconcile_to_total to rescale per-cell
        intent to the reserved total — NOT to current_exposure. Realized credits
        are committed and unattributable to any specific cell; including them in
        the rescale factor would inflate per-cell intent past cap_per_cell.
        """
        return self._reserved

    def realized_exposure(self) -> Decimal:
        """For L2 guards (DrawdownGuard etc., Phase 4.4): matched credits only."""
        return self._realized

    def available_balance(self) -> Decimal:
        """Funding-wallet available balance from the last reconcile (in-memory;
        not persisted). 0 until the first reconcile populates it — fail-closed
        (the reconciler deploys nothing on unknown funds). Read by the
        DeploymentReconciler balance clamp and BuyingPowerGuard."""
        return self._available


