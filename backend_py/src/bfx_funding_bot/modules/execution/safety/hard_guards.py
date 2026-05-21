"""Hard guards — L1, always-on in 4.2.

Order matters: cheap checks first (manual kill > auth health > heartbeat >
allocation cap). Chain short-circuits on first block.
"""
from __future__ import annotations

import os
from datetime import UTC, datetime
from decimal import Decimal
from typing import Protocol

from bfx_funding_bot.modules.execution.protocols import (
    AccountContext,
    GuardResult,
)
from bfx_funding_bot.modules.marketfeed.health_monitor import HealthProbe
from bfx_funding_bot.modules.marketfeed.schemas import (
    DecisionOutcome,
    DecisionPayload,
    HealthStatus,
    HealthTarget,
)


class ManualKillGuard:
    """Block all when env BFX_KILL_SWITCH=true. Always-on watchdog."""

    name = "manual_kill"
    is_calibrated = False

    async def evaluate(
        self, decision: DecisionPayload, ctx: AccountContext,
    ) -> GuardResult:
        flag = os.environ.get("BFX_KILL_SWITCH", "").lower() in ("true", "1", "yes")
        if flag:
            return GuardResult(
                allowed=False, guard_name=self.name,
                reason="BFX_KILL_SWITCH env flag set",
            )
        return GuardResult(allowed=True, guard_name=self.name)


class AuthHealthGuard:
    """Block when executor target probe state is DOWN.

    Allows when target never set (day-1 boot) or HEALTHY/DEGRADED.
    DEGRADED is a soft warn, not a block — chain still considers each request.
    """

    name = "auth_health"
    is_calibrated = False

    def __init__(self, *, probe: HealthProbe) -> None:
        self.probe = probe

    async def evaluate(
        self, decision: DecisionPayload, ctx: AccountContext,
    ) -> GuardResult:
        status = self.probe.current_status(HealthTarget.EXECUTOR)
        if status == HealthStatus.DOWN:
            return GuardResult(
                allowed=False, guard_name=self.name,
                reason="executor health DOWN",
            )
        return GuardResult(allowed=True, guard_name=self.name)


class HeartbeatGuard:
    """Block when any watched sub-task heartbeat is older than threshold.

    Day-1 / never-recorded sub-tasks allow by default (booting state).
    Threshold strictly greater (> threshold) blocks; exactly equal allows.
    """

    name = "heartbeat"
    is_calibrated = False

    def __init__(
        self, *, probe: HealthProbe, threshold_seconds: int,
        watched_sub_tasks: list[str],
    ) -> None:
        self.probe = probe
        self.threshold_seconds = threshold_seconds
        self.watched = watched_sub_tasks

    async def evaluate(
        self, decision: DecisionPayload, ctx: AccountContext,
    ) -> GuardResult:
        now = datetime.now(UTC)
        for sub_task in self.watched:
            last = self.probe.last_active_ts.get(sub_task)
            if last is None:
                continue
            # Truncate to integer seconds so "exactly at threshold" semantics
            # are deterministic — microsecond drift from datetime.now() between
            # heartbeat record and evaluate must not flip the boundary case.
            age = int((now - last).total_seconds())
            if age > self.threshold_seconds:
                return GuardResult(
                    allowed=False, guard_name=self.name,
                    reason=f"sub_task={sub_task} stale {age}s > {self.threshold_seconds}s",
                )
        return GuardResult(allowed=True, guard_name=self.name)


class _LedgerProtocol(Protocol):
    def current_exposure(self) -> Decimal: ...


class AllocationCapGuard:
    """Block POST decision when current_exposure + offer_amount > cap.

    SKIP decisions always allowed (no cap consumption). Exactly-at-cap
    allows; strictly over blocks (so cap=500, exposure=400, offer=100 → allowed
    at 500 = cap; cap=500, exposure=400, offer=101 → blocked at 501 > 500).
    """

    name = "allocation_cap"
    is_calibrated = False

    def __init__(self, *, ledger: _LedgerProtocol) -> None:
        self.ledger = ledger

    async def evaluate(
        self, decision: DecisionPayload, ctx: AccountContext,
    ) -> GuardResult:
        if decision.decision_outcome != DecisionOutcome.POST:
            return GuardResult(allowed=True, guard_name=self.name)
        if decision.offer_amount_usdt is None:
            return GuardResult(
                allowed=False, guard_name=self.name,
                reason="POST decision missing offer_amount_usdt",
            )
        exposure = self.ledger.current_exposure()
        offer = Decimal(str(decision.offer_amount_usdt))
        projected = exposure + offer
        if projected > ctx.allocation_cap_usdt:
            return GuardResult(
                allowed=False, guard_name=self.name,
                reason=(
                    f"exposure={exposure}+offer={offer}={projected} > "
                    f"cap={ctx.allocation_cap_usdt}"
                ),
            )
        return GuardResult(allowed=True, guard_name=self.name)
