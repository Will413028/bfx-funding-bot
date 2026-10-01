"""Daemon boot uses the scoped cycle port.

How a non-accepted ledger cycle decision gates boot is decided in S1-3e (a boot
refusal on an in-grace attempt would crash-loop); legacy refuses by raising.
"""
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from bfx_funding_bot.modules.ledger import CycleResult, Scope
from bfx_funding_bot.modules.marketfeed.daemon import Daemon


@pytest.mark.asyncio
async def test_daemon_boot_passes_scope_to_observation_port():
    scope = Scope(uuid4(), "ci")
    sink = AsyncMock()
    sink.run.return_value = CycleResult("accepted", uuid4())
    daemon = SimpleNamespace(boot_recovery=sink, observation_scope=scope, protection=None)
    await Daemon._run_boot_recovery(daemon)
    sink.run.assert_awaited_once_with(scope)
