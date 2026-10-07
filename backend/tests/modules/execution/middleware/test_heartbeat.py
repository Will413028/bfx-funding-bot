"""HeartbeatMiddleware — record_heartbeat("executor") outcome-independent (I1)."""
from __future__ import annotations

from decimal import Decimal
from uuid import uuid4

import pytest

from bfx_funding_bot.core.errors import ExecutorTransientError
from bfx_funding_bot.core.health import HealthProbe
from bfx_funding_bot.modules.execution.contracts import (
    ExecutionPolicy,
    GuardResult,
    ReadyToSubmit,
)
from bfx_funding_bot.modules.execution.middleware.heartbeat import HeartbeatMiddleware
from bfx_funding_bot.modules.execution.protocols import (
    AccountContext,
    Credentials,
    SubmittedOrder,
)
from bfx_funding_bot.modules.execution.submit_outcomes import SubmitAcknowledged
from bfx_funding_bot.modules.strategy import DecisionOutcome, DecisionPayload


def _decision() -> DecisionPayload:
    return DecisionPayload(
        decision_outcome=DecisionOutcome.POST, signal_correlation_id=uuid4(),
        offer_rate=0.0001, offer_amount_usdt=100.0, offer_duration_days=2,
    symbol="fUST")


def _ready() -> ReadyToSubmit:
    return ReadyToSubmit(
        decision=_decision(),
        decision_id="d-heartbeat",
        policy=ExecutionPolicy.BOOK_GUARDED,
        market_snapshot_id="snapshot-heartbeat",
        model_version=None,
        evidence={},
        safety=GuardResult(allowed=True, guard_name="test"),
    )


def _ctx() -> AccountContext:
    return AccountContext(
        account_id="default",
        credentials=Credentials(api_key="k", api_secret="s"),
        allocation_cap_usdt=Decimal("10000"),
    )


class _InnerOk:
    async def submit(self, ready: ReadyToSubmit, ctx: AccountContext) -> SubmittedOrder:
        return SubmittedOrder(outcome=SubmitAcknowledged("x"))


class _InnerRaises:
    async def submit(self, ready: ReadyToSubmit, ctx: AccountContext) -> SubmittedOrder:
        raise ExecutorTransientError("network_blip")


@pytest.mark.asyncio
async def test_heartbeat_fires_on_inner_success() -> None:
    probe = HealthProbe()
    mw = HeartbeatMiddleware(_InnerOk(), probe=probe)
    await mw.submit(_ready(), _ctx())
    assert probe.last_active_ts.get("executor") is not None


@pytest.mark.asyncio
async def test_heartbeat_fires_on_inner_failure() -> None:
    probe = HealthProbe()
    mw = HeartbeatMiddleware(_InnerRaises(), probe=probe)
    with pytest.raises(ExecutorTransientError):
        await mw.submit(_ready(), _ctx())
    # I1 invariant: heartbeat outcome-independent (try/finally)
    assert probe.last_active_ts.get("executor") is not None


@pytest.mark.asyncio
async def test_heartbeat_record_failure_does_not_break_submit() -> None:
    """Probe internal failure shouldn't kill submit path."""
    class _BrokenProbe:
        def record_heartbeat(self, target: str) -> None:
            raise RuntimeError("probe broken")
    probe = _BrokenProbe()
    mw = HeartbeatMiddleware(_InnerOk(), probe=probe)  # type: ignore[arg-type]
    result = await mw.submit(_ready(), _ctx())
    assert result.status == "submitted"
