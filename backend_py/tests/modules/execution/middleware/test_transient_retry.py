"""TransientRetryMiddleware — wraps inner.submit with transient_retry decorator."""
from __future__ import annotations

from decimal import Decimal
from typing import Any
from uuid import uuid4

import pytest

from bfx_funding_bot.core.errors import (
    ExecutorAuthError,
    ExecutorFatalError,
    ExecutorTransientError,
)
from bfx_funding_bot.modules.execution.middleware.transient_retry import (
    TransientRetryMiddleware,
)
from bfx_funding_bot.modules.execution.protocols import (
    AccountContext,
    Credentials,
    SubmittedOrder,
)
from bfx_funding_bot.modules.marketfeed.schemas import (
    DecisionOutcome,
    DecisionPayload,
)


def _decision() -> DecisionPayload:
    return DecisionPayload(
        decision_outcome=DecisionOutcome.POST,
        signal_correlation_id=uuid4(),
        offer_rate=0.0001, offer_amount_usdt=100.0, offer_duration_days=2,
    )


def _ctx() -> AccountContext:
    return AccountContext(
        account_id="default",
        credentials=Credentials(api_key="k", api_secret="s"),
        allocation_cap_usdt=Decimal("10000"),
    )


class _CountingInner:
    def __init__(self, behavior: list[Any]) -> None:
        self._behavior = behavior
        self.calls = 0

    async def submit(self, decision: DecisionPayload, ctx: AccountContext) -> SubmittedOrder:
        self.calls += 1
        result = self._behavior[self.calls - 1]
        if isinstance(result, BaseException):
            raise result
        return result


def _ok() -> SubmittedOrder:
    return SubmittedOrder(cid=1, venue_offer_id="x", status="filled", raw_response=None)


@pytest.mark.asyncio
async def test_no_retry_on_success() -> None:
    inner = _CountingInner([_ok()])
    mw = TransientRetryMiddleware(inner)
    result = await mw.submit(_decision(), _ctx())
    assert inner.calls == 1
    assert result.status == "filled"


@pytest.mark.asyncio
async def test_retries_on_transient_then_succeeds() -> None:
    inner = _CountingInner([
        ExecutorTransientError("network_blip"),
        ExecutorTransientError("network_blip"),
        _ok(),
    ])
    mw = TransientRetryMiddleware(inner)
    result = await mw.submit(_decision(), _ctx())
    assert inner.calls == 3
    assert result.status == "filled"


@pytest.mark.asyncio
async def test_fatal_propagates_first_attempt() -> None:
    inner = _CountingInner([ExecutorFatalError("venue_rejected_400")])
    mw = TransientRetryMiddleware(inner)
    with pytest.raises(ExecutorFatalError):
        await mw.submit(_decision(), _ctx())
    assert inner.calls == 1


@pytest.mark.asyncio
async def test_auth_propagates_first_attempt() -> None:
    inner = _CountingInner([ExecutorAuthError("auth_failed")])
    mw = TransientRetryMiddleware(inner)
    with pytest.raises(ExecutorAuthError):
        await mw.submit(_decision(), _ctx())
    assert inner.calls == 1


@pytest.mark.asyncio
async def test_exhausts_retries_after_n() -> None:
    inner = _CountingInner([ExecutorTransientError("blip")] * 5)  # exceed retry cap
    mw = TransientRetryMiddleware(inner)
    with pytest.raises(ExecutorTransientError):
        await mw.submit(_decision(), _ctx())
    assert inner.calls >= 2
