"""Legacy adapter preserves result identity and BootRecovery's invocation."""
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from bfx_funding_bot.modules.execution.observation_sink import LegacyObservationSink
from bfx_funding_bot.modules.execution.reconcile_result import ReconcileResult
from bfx_funding_bot.modules.ledger import CycleResult, Scope


@pytest.mark.asyncio
async def test_legacy_adapter_keeps_result_and_calls_run_without_arguments():
    result = ReconcileResult(1, 2, 3, snapshot_event_seq=42)
    recovery = AsyncMock()
    recovery.run.return_value = result
    scope = Scope(uuid4(), "ci")
    adapter = LegacyObservationSink(recovery, scope)
    cycle = await adapter.run(scope)
    assert isinstance(cycle, CycleResult) and cycle.decision == "accepted"
    assert cycle.legacy is result
    recovery.run.assert_awaited_once_with()
    with pytest.raises(ValueError, match="scope mismatch"):
        await adapter.run(Scope(scope.exchange_account_id, "other"))
    assert recovery.run.await_count == 1


@pytest.mark.asyncio
async def test_legacy_adapter_propagates_boot_refusal():
    scope = Scope(uuid4(), "ci")
    recovery = AsyncMock()
    error = RuntimeError("boot refused")
    recovery.run.side_effect = error
    with pytest.raises(RuntimeError) as caught:
        await LegacyObservationSink(recovery, scope).run(scope)
    assert caught.value is error
