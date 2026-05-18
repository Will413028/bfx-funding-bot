from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

from bfx_funding_bot.modules.marketfeed.health_monitor import (
    HealthMonitor,
    HealthProbe,
)
from bfx_funding_bot.modules.marketfeed.schemas import (
    HealthStatus,
    HealthTarget,
    Phase,
)


async def test_state_change_emits_immediately():
    axiom = AsyncMock()
    probe = HealthProbe()
    monitor = HealthMonitor(phase=Phase.PAPER, axiom=axiom, probe=probe,
                            heartbeat_interval_s=10.0)
    await monitor.start()
    probe.update(HealthTarget.BITFINEX_WS, HealthStatus.HEALTHY,
                 last_msg_age_ms=100, reconnect_count_last_hour=0)
    await asyncio.sleep(0.1)
    probe.update(HealthTarget.BITFINEX_WS, HealthStatus.DEGRADED,
                 last_msg_age_ms=400_000, reconnect_count_last_hour=1,
                 error_message="hb_timeout")
    await asyncio.sleep(0.2)
    await monitor.stop()

    statuses = [
        call.args[0]["payload"]["status"]
        for call in axiom.emit.call_args_list
    ]
    assert HealthStatus.HEALTHY.value in statuses
    assert HealthStatus.DEGRADED.value in statuses


async def test_heartbeat_emits_periodically():
    axiom = AsyncMock()
    probe = HealthProbe()
    probe.update(HealthTarget.BITFINEX_WS, HealthStatus.HEALTHY,
                 last_msg_age_ms=100, reconnect_count_last_hour=0)
    monitor = HealthMonitor(phase=Phase.PAPER, axiom=axiom, probe=probe,
                            heartbeat_interval_s=0.1)
    await monitor.start()
    await asyncio.sleep(0.35)
    await monitor.stop()

    assert axiom.emit.call_count >= 2  # multiple heartbeats fired
