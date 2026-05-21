"""Hard guards — L1, always-on in 4.2.

Order matters: cheap checks first (manual kill > auth health > heartbeat >
allocation cap). Chain short-circuits on first block.
"""
from __future__ import annotations

import os

from bfx_funding_bot.modules.execution.protocols import (
    AccountContext,
    GuardResult,
)
from bfx_funding_bot.modules.marketfeed.health_monitor import HealthProbe
from bfx_funding_bot.modules.marketfeed.schemas import (
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
