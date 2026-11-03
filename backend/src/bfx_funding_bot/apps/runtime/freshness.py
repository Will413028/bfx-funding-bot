"""Dependency freshness shared by the market data and account halves of the daemon."""
from __future__ import annotations

from bfx_funding_bot.core.health import HealthProbe
from bfx_funding_bot.modules.execution.readiness import TradingReadiness


def dependency_answered(probe: HealthProbe, readiness: TradingReadiness | None,
                        dependency: str) -> None:
    """A dependency answered: beat its freshness and clear it in /readyz now,
    not at the next staleness scan (which only ever marks it stale)."""
    probe.record_heartbeat(dependency)
    if readiness is not None:
        readiness.clear_dependency(dependency)
