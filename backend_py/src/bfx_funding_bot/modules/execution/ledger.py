"""PaperPositionLedger — in-memory per-symbol counters loaded from PostgreSQL snapshot.

PostgreSQL event_log is SoT. On daemon startup, from_snapshot reads
position_state (reserved / realized) to rebuild in-memory per-symbol
dicts. Subscribe to DomainEventBus for live updates.

reserved: open reservations (offer placed, no match yet). AllocationCap
uses reserved+realized+uncertain for pre-trade reservation check. An uncertain
symbol is additionally command-gated until explicit resolution.
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

from bfx_funding_bot.modules.accounts.exchange_accounts import account_scope_clause
from bfx_funding_bot.modules.execution.events import (
    OrderFilled,
    PositionReconciled,
    ReservationClaimed,
    ReservationReleased,
    ReservationUnknown,
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
        # A post-transport UNKNOWN is pessimistic capital.  Keep it separate
        # from venue-observed offered/lent buckets so a later full-account
        # resolver can clear it without fabricating a claim.
        self._uncertain: dict[str, Decimal] = {}
        self.replay_floor_hit_count = 0
        self._processed_fills: set[tuple[str, int | None]] = set()
        self._processed_releases: set[tuple[str, int | None]] = set()
        self._processed_unknowns: set[tuple[int, str, str]] = set()

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
                    account_scope_clause(
                        session,
                        account_id=account_id,
                        exchange_account_column=PositionStateRow.exchange_account_id,
                        legacy_account_column=PositionStateRow.account_id,
                    ),
                    PositionStateRow.deployment_environment == deployment_environment,
                )
            )
        ).scalars().all()
        for row in rows:
            # Canonical v3 buckets are authoritative.  The fallback only
            # serves pre-observation SQLite fixtures whose additive migration
            # columns are still zero while legacy values are seeded directly.
            offered = Decimal(str(row.offered_amount))
            lent = Decimal(str(row.lent_amount))
            if offered == 0 and Decimal(str(row.reserved)) != 0:
                offered = Decimal(str(row.reserved))
            if lent == 0 and Decimal(str(row.realized)) != 0:
                lent = Decimal(str(row.realized))
            ledger._reserved[row.symbol] = offered
            ledger._realized[row.symbol] = lent
            raw_uncertain = getattr(row, "uncertain_amount", Decimal("0"))
            ledger._uncertain[row.symbol] = Decimal(str(raw_uncertain or 0))
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

    async def on_reservation_unknown(self, event: ReservationUnknown) -> None:
        """Open a durable symbol-level submit uncertainty gate.

        UNKNOWN is intentionally not sent through the normal claim bus: it must
        not look like a venue acknowledgement.  The middleware and boot
        recovery call this explicit projection hook after persisting the event.
        Deduplication makes repeated delivery safe.
        """
        if event.account_id != self.account_id:
            return
        amount = event.amount
        assert amount is not None  # invariant: _resolve_amount guarantees this
        key = (event.cid, event.symbol, str(event.signal_correlation_id))
        if key in self._processed_unknowns:
            log.debug("ledger_dedup unknown %s", key)
            return
        self._processed_unknowns.add(key)
        self._uncertain[event.symbol] = (
            self._uncertain.get(event.symbol, Decimal("0")) + amount
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
        """For AllocationCapGuard: reserved + realized + uncertain for THIS
        symbol (native units; never cross-symbol).

        ``uncertain`` is pessimistic capital: the request may already exist at
        the venue, so it counts toward exposure even before reconciliation can
        assign a venue offer id.  The deployment reconciler additionally checks
        ``is_uncertain`` and blocks the whole symbol rather than filling a
        residual gap.
        """
        return (
            self._reserved.get(symbol, Decimal("0"))
            + self._realized.get(symbol, Decimal("0"))
            + self._uncertain.get(symbol, Decimal("0"))
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

    def uncertain_exposure(self, symbol: str) -> Decimal:
        """Pessimistic post-transport amount awaiting explicit resolution."""
        return self._uncertain.get(symbol, Decimal("0"))

    def is_uncertain(self, symbol: str) -> bool:
        """Return whether new submits for this symbol must remain blocked."""
        return self.uncertain_exposure(symbol) > 0

    def clear_uncertainty(self, symbol: str, amount: Decimal) -> None:
        """Release an explicitly resolved UNKNOWN amount.

        Resolution is deliberately an operator/reconcile action, never an
        automatic consequence of a partial snapshot.  Future matching logic
        calls this method only after it has durable evidence.
        """
        if amount < 0:
            raise ValueError("uncertainty amount must be non-negative")
        remaining = self.uncertain_exposure(symbol) - amount
        if remaining < 0:
            raise ValueError("cannot clear more uncertainty than is open")
        self._uncertain[symbol] = remaining

    def available_balance(self, symbol: str) -> Decimal:
        """Funding-wallet available balance from the last reconcile (in-memory;
        not persisted). 0 until the first reconcile populates it — fail-closed
        (the reconciler deploys nothing on unknown funds). Read by the
        DeploymentReconciler balance clamp and BuyingPowerGuard.

        `symbol` is required — never sum across currencies implicitly.
        """
        return self._available.get(symbol, Decimal("0"))

    def total_exposure_all_symbols(self) -> Decimal:
        """Explicit, non-hot-path cross-symbol total (reserved + realized +
        uncertain, summed over every symbol bucket).

        This is the sanctioned replacement for the removed implicit no-arg
        getter sum: when you genuinely want a total across all currencies, call
        this by name. Hot-path guards must read a single symbol's getter.
        """
        return (
            sum(self._reserved.values(), Decimal("0"))
            + sum(self._realized.values(), Decimal("0"))
            + sum(self._uncertain.values(), Decimal("0"))
        )
