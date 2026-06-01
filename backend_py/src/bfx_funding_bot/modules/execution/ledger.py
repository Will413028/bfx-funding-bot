"""PaperPositionLedger — in-memory per-symbol counters loaded from PostgreSQL snapshot.

PostgreSQL event_log is SoT. On daemon startup, from_snapshot reads
position_state (reserved / realized) to rebuild in-memory per-symbol
dicts. Subscribe to DomainEventBus for live updates.

reserved: open reservations (offer placed, no match yet). AllocationCap
uses reserved+realized for pre-trade reservation check.
realized: matched credits (actual exposure earning APR). L2 guards
(DrawdownGuard / DivergenceRateGuard, Phase 4.4) use realized only.

Counters are keyed by symbol (e.g. "fUST", "fUSD"). Each symbol bucket
is isolated; every read getter requires an explicit symbol. For a
cross-symbol total use the explicit total_exposure_all_symbols() helper —
never an implicit no-arg getter (that backdoor was removed so a guard /
reconciler that forgets a symbol fails loudly instead of silently summing
across currencies on the money path).

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
        # Per-symbol dicts keyed by offer currency (e.g. "fUST", "fUSD").
        # Missing key → 0; use .get(sym, Decimal("0")) everywhere.
        self._reserved: dict[str, Decimal] = {}
        self._realized: dict[str, Decimal] = {}
        self._available: dict[str, Decimal] = {}
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
        """Load per-symbol ledger counters from the position_state snapshot
        table (no replay). Loads ALL rows for (account, env) — one per symbol —
        into the per-symbol dicts. `available` is never persisted (in-memory,
        populated by the first reconcile), so it stays empty here."""
        from sqlalchemy import select

        from bfx_funding_bot.modules.execution.event_store.tables import PositionStateRow

        ledger = cls(account_id=account_id)
        rows = (
            await session.execute(
                select(PositionStateRow).where(
                    PositionStateRow.account_id == account_id,
                    PositionStateRow.deployment_environment == deployment_environment,
                )
            )
        ).scalars().all()
        for row in rows:
            ledger._reserved[row.symbol] = Decimal(str(row.reserved))
            ledger._realized[row.symbol] = Decimal(str(row.realized))
        return ledger

    # ---------- live update handlers (DomainEventBus subscribers) ----------

    async def on_reservation_claimed(self, event: ReservationClaimed) -> None:
        if event.account_id != self.account_id:
            return
        amount = event.amount
        assert amount is not None  # invariant: _resolve_amount guarantees this
        self._reserved[event.symbol] = self._reserved.get(event.symbol, Decimal("0")) + amount

    async def on_order_filled(self, event: OrderFilled) -> None:
        if event.account_id != self.account_id:
            return
        key = (event.venue_offer_id, event.venue_seq)
        if key in self._processed_fills:
            log.debug("ledger_dedup filled %s", key)
            return
        self._processed_fills.add(key)
        amount = event.amount
        assert amount is not None  # invariant: _resolve_amount guarantees this
        reserved = self._reserved.get(event.symbol, Decimal("0"))
        delta = min(reserved, amount)
        self._reserved[event.symbol] = reserved - delta
        if delta < amount:
            self.replay_floor_hit_count += 1
            log.warning(
                "order_filled_without_claim cid=%d offer=%s symbol=%s expected=%.2f applied=%.2f",
                event.cid, event.venue_offer_id, event.symbol,
                float(amount), float(delta),
            )
        self._realized[event.symbol] = (
            self._realized.get(event.symbol, Decimal("0")) + amount
        )

    async def on_reservation_released(self, event: ReservationReleased) -> None:
        if event.account_id != self.account_id:
            return
        key = (event.venue_offer_id, event.venue_seq)
        if key in self._processed_releases:
            log.debug("ledger_dedup released %s", key)
            return
        self._processed_releases.add(key)
        amount = event.amount
        assert amount is not None  # invariant: _resolve_amount guarantees this
        reserved = self._reserved.get(event.symbol, Decimal("0"))
        delta = min(reserved, amount)
        self._reserved[event.symbol] = reserved - delta
        if delta < amount:
            self.replay_floor_hit_count += 1
            log.warning(
                "reservation_release_without_claim cid=%d offer=%s symbol=%s expected=%.2f applied=%.2f reason=%s",
                event.cid, event.venue_offer_id, event.symbol,
                float(amount), float(delta), event.reason,
            )

    async def on_position_reconciled(self, event: PositionReconciled) -> None:
        """Absolute set from venue snapshot — NOT a delta.

        Overwrites THIS symbol's reserved/realized/available with the
        authoritative venue values. Called once per configured symbol after
        each reconcile tick (boot + periodic). Other symbols' buckets are left
        intact; each carries its own PositionReconciled. The next WS delta that
        arrives will temporarily diverge; the next reconcile corrects it.
        """
        if event.account_id != self.account_id:
            return
        reserved = event.reserved
        realized = event.realized
        available = event.available
        assert reserved is not None and realized is not None and available is not None
        self._reserved[event.symbol] = reserved
        self._realized[event.symbol] = realized
        self._available[event.symbol] = available

    # ---------- public getters ----------

    def current_exposure(self, symbol: str) -> Decimal:
        """For AllocationCapGuard: reserved + realized for THIS symbol (native
        units; never cross-symbol). `symbol` is required — for a cross-symbol
        total use total_exposure_all_symbols()."""
        return self._reserved.get(symbol, Decimal("0")) + self._realized.get(
            symbol, Decimal("0")
        )

    def reserved_exposure(self, symbol: str) -> Decimal:
        """Pending open-offer capital only (placed but not yet matched).

        Used by CellDeploymentTracker.reconcile_to_total to rescale per-cell
        intent to the reserved total — NOT to current_exposure. Realized credits
        are committed and unattributable to any specific cell; including them in
        the rescale factor would inflate per-cell intent past cap_per_cell.

        `symbol` is required — never sum across currencies implicitly.
        """
        return self._reserved.get(symbol, Decimal("0"))

    def realized_exposure(self, symbol: str) -> Decimal:
        """Matched credits only, for this symbol.

        For L2 guards (DrawdownGuard etc., Phase 4.4): matched credits only.
        `symbol` is required — never sum across currencies implicitly.
        """
        return self._realized.get(symbol, Decimal("0"))

    def available_balance(self, symbol: str) -> Decimal:
        """Funding-wallet available balance from the last reconcile (in-memory;
        not persisted). 0 until the first reconcile populates it — fail-closed
        (the reconciler deploys nothing on unknown funds). Read by the
        DeploymentReconciler balance clamp and BuyingPowerGuard.

        `symbol` is required — never sum across currencies implicitly.
        """
        return self._available.get(symbol, Decimal("0"))

    def total_exposure_all_symbols(self) -> Decimal:
        """Explicit, non-hot-path cross-symbol total (reserved + realized,
        summed over every symbol bucket).

        This is the sanctioned replacement for the removed implicit no-arg
        getter sum: when you genuinely want a total across all currencies, call
        this by name. Hot-path guards must read a single symbol's getter.
        """
        return sum(self._reserved.values(), Decimal("0")) + sum(
            self._realized.values(), Decimal("0")
        )


