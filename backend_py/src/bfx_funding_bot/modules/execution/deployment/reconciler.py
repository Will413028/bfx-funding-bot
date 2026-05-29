"""DeploymentReconciler — the single writer for venue submits.

Called by PeriodicReconcile after each successful reconcile (ledger holds fresh
venue truth). Reads active standing quotes + global exposure, allocates the gap
toward target (= account cap), applies the full safety chain, and submits. The
signal layer no longer submits (single-writer; spec 2026-05-29).
"""
from __future__ import annotations

import logging
from collections.abc import Callable
from decimal import Decimal
from typing import Protocol

from bfx_funding_bot.modules.execution.deployment.sizing import (
    allocate_gap,
    effective_min_usdt,
)
from bfx_funding_bot.modules.execution.deployment.standing_quote import StandingQuoteStore
from bfx_funding_bot.modules.execution.deployment.tracker import CellDeploymentTracker
from bfx_funding_bot.modules.execution.protocols import (
    AccountContext,
    ExecutorPort,
    GuardResult,
)
from bfx_funding_bot.modules.marketfeed.config import CellConfig
from bfx_funding_bot.modules.marketfeed.schemas import DecisionOutcome, DecisionPayload

log = logging.getLogger(__name__)


class _LedgerProtocol(Protocol):
    def current_exposure(self) -> Decimal: ...


class _SafetyChainProtocol(Protocol):
    async def evaluate(
        self, decision: DecisionPayload, ctx: AccountContext,
    ) -> GuardResult: ...


class DeploymentReconciler:
    def __init__(
        self,
        *,
        store: StandingQuoteStore,
        tracker: CellDeploymentTracker,
        ledger: _LedgerProtocol,
        safety_chain: _SafetyChainProtocol,
        executor: ExecutorPort,
        account_ctx: AccountContext,
        cells: list[CellConfig],
        venue_floor_usd: Decimal,
        min_offer_buffer_pct: Decimal,
        concentration_pct: Decimal,
        clock: Callable[[], int],
    ) -> None:
        self._store = store
        self._tracker = tracker
        self._ledger = ledger
        self._safety = safety_chain
        self._executor = executor
        self._ctx = account_ctx
        self._cells = cells
        self._min_fill = effective_min_usdt(venue_floor_usd, min_offer_buffer_pct)
        self._concentration_pct = concentration_pct
        self._clock = clock

    async def deploy(self) -> None:
        now = self._clock()
        e_total = self._ledger.current_exposure()
        # Keep per-cell intent consistent with the authoritative venue truth.
        self._tracker.reconcile_to_total(e_total)

        active = [c.cell_id for c in self._cells
                  if self._store.get_active(c.cell_id, now_ms=now) is not None]

        # Fills are pre-computed from this single pre-loop snapshot; the per-cell
        # concentration cap is enforced inside allocate_gap, not incrementally as
        # we record each submit below. Correct within a tick (sum of fills <= gap,
        # each <= per-cell cap); cross-tick drift is corrected by reconcile_to_total.
        fills = allocate_gap(
            target=self._ctx.allocation_cap_usdt,
            current_exposure=e_total,
            deployed=self._tracker.snapshot(),
            active_cells=active,
            concentration_pct=self._concentration_pct,
            min_fill=self._min_fill,
        )
        if not fills:
            return

        for cell_id, amount in fills.items():
            quote = self._store.get_active(cell_id, now_ms=now)
            if quote is None:  # defensive: TTL could lapse between checks
                continue
            decision = DecisionPayload(
                decision_outcome=DecisionOutcome.POST,
                signal_correlation_id=quote.signal_correlation_id,
                offer_rate=quote.rate,
                offer_amount_usdt=float(amount),
                offer_duration_days=quote.period_days,
            )
            guard = await self._safety.evaluate(decision, self._ctx)
            if not guard.allowed:
                log.info(
                    "deployment_skip cell=%s amount=%s guard=%s reason=%s",
                    cell_id, amount, guard.guard_name, guard.reason,
                )
                continue
            try:
                await self._executor.submit(decision, self._ctx)
            except Exception:
                log.exception("deployment_submit_failed cell=%s amount=%s", cell_id, amount)
                continue
            self._tracker.record_deploy(cell_id, amount)
            log.info("deployment_submitted cell=%s amount=%s", cell_id, amount)
