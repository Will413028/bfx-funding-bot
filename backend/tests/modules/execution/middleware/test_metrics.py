"""MetricsSubmitMiddleware / TimedReconcileRecovery: transparent, fail-open timing wrappers."""
from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

import pytest

from bfx_funding_bot.external.bitfinex.submit_outcomes import (
    SubmitAcknowledged,
    SubmitOutcomeUnknown,
)
from bfx_funding_bot.modules.execution.contracts import (
    ExecutionPolicy,
    GuardResult,
    ReadyToSubmit,
)
from bfx_funding_bot.modules.execution.middleware import (
    MetricsSubmitMiddleware,
    TimedReconcileRecovery,
)
from bfx_funding_bot.modules.execution.protocols import (
    AccountContext,
    Credentials,
    SubmittedOrder,
)
from bfx_funding_bot.modules.ledger import CycleResult, Scope
from bfx_funding_bot.modules.observability.metrics import (
    DaemonMetrics,
)
from bfx_funding_bot.modules.strategy import DecisionOutcome, DecisionPayload

_SCOPE = Scope(UUID("00000000-0000-0000-0000-00000000a001"))


def _ctx() -> AccountContext:
    return AccountContext(
        account_id="default",
        credentials=Credentials(api_key="k", api_secret="s"),
    )


def _decision() -> DecisionPayload:
    return DecisionPayload(
        decision_outcome=DecisionOutcome.POST,
        signal_correlation_id=uuid4(),
        offer_rate=0.0005,
        offer_amount_usdt=150.0,
        offer_duration_days=2,
        symbol="fUST",
    )


def _ready() -> ReadyToSubmit:
    return ReadyToSubmit(
        decision=_decision(),
        decision_id="d-metrics",
        policy=ExecutionPolicy.BOOK_GUARDED,
        market_snapshot_id="snapshot-metrics",
        model_version=None,
        evidence={},
        safety=GuardResult(allowed=True, guard_name="test"),
    )


class _StubExecutor:
    def __init__(self, order: SubmittedOrder | None = None,
                 exc: Exception | None = None) -> None:
        self._order = order
        self._exc = exc
        self.readies: list[ReadyToSubmit] = []

    async def submit(
        self, ready: ReadyToSubmit, ctx: AccountContext,
    ) -> SubmittedOrder:
        self.readies.append(ready)
        if self._exc is not None:
            raise self._exc
        assert self._order is not None
        return self._order


@pytest.mark.asyncio
async def test_submit_middleware_passes_result_and_records() -> None:
    m = DaemonMetrics()
    order = SubmittedOrder(outcome=SubmitAcknowledged("paper_x"))
    inner = _StubExecutor(order=order)
    mw = MetricsSubmitMiddleware(inner, metrics=m)
    ready = _ready()
    got = await mw.submit(ready, _ctx())
    assert got is order                       # byte-identical passthrough
    assert inner.readies == [ready]            # immutable boundary object is not rebuilt
    assert m.registry.get_sample_value(
        "bfx_executor_submits_total", {"status": "submitted"},
    ) == 1.0
    assert m.registry.get_sample_value("bfx_executor_submit_duration_seconds_count") == 1.0


@pytest.mark.asyncio
async def test_submit_middleware_reraises_and_counts_exception() -> None:
    m = DaemonMetrics()
    boom = ValueError("venue said no")
    mw = MetricsSubmitMiddleware(_StubExecutor(exc=boom), metrics=m)
    with pytest.raises(ValueError) as ei:
        await mw.submit(_ready(), _ctx())
    assert ei.value is boom                   # exception object unchanged
    assert m.registry.get_sample_value(
        "bfx_executor_submits_total", {"status": "exception"},
    ) == 1.0


@pytest.mark.asyncio
async def test_submit_middleware_keeps_unknown_as_first_class_metric() -> None:
    m = DaemonMetrics()
    order = SubmittedOrder(
        venue_offer_id=None,
        outcome=SubmitOutcomeUnknown("timeout", True),
    )
    mw = MetricsSubmitMiddleware(_StubExecutor(order=order), metrics=m)

    await mw.submit(_ready(), _ctx())

    assert m.registry.get_sample_value(
        "bfx_executor_submits_total", {"status": "unknown"},
    ) == 1.0


@pytest.mark.asyncio
async def test_submit_middleware_fail_open_when_metrics_broken() -> None:
    m = DaemonMetrics()

    def _boom(**kwargs: Any) -> None:
        raise RuntimeError("metrics down")

    m.observe_submit = _boom  # type: ignore[method-assign]
    order = SubmittedOrder(outcome=SubmitAcknowledged("x"))
    mw = MetricsSubmitMiddleware(_StubExecutor(order=order), metrics=m)
    got = await mw.submit(_ready(), _ctx())   # must NOT raise
    assert got is order


def _reconcile_result() -> CycleResult:
    return CycleResult("accepted")


class _StubRecovery:
    def __init__(self, result: CycleResult | None = None,
                 exc: Exception | None = None) -> None:
        self._result = result
        self._exc = exc

    async def run(self, scope: Scope) -> CycleResult:
        assert scope == _SCOPE
        if self._exc is not None:
            raise self._exc
        assert self._result is not None
        return self._result


@pytest.mark.asyncio
async def test_timed_recovery_passes_result_through() -> None:
    m = DaemonMetrics()
    result = _reconcile_result()
    wrapped = TimedReconcileRecovery(_StubRecovery(result=result), metrics=m)
    got = await wrapped.run(_SCOPE)
    assert got is result
    assert m.registry.get_sample_value(
        "bfx_reconcile_ticks_total", {"result": "ok"},
    ) == 1.0
    assert m.registry.get_sample_value("bfx_reconcile_tick_duration_seconds_count") == 1.0


@pytest.mark.asyncio
async def test_timed_recovery_reraises_and_counts_error() -> None:
    m = DaemonMetrics()
    boom = ConnectionError("venue unreachable")
    wrapped = TimedReconcileRecovery(_StubRecovery(exc=boom), metrics=m)
    with pytest.raises(ConnectionError) as ei:
        await wrapped.run(_SCOPE)
    assert ei.value is boom
    assert m.registry.get_sample_value(
        "bfx_reconcile_ticks_total", {"result": "error"},
    ) == 1.0


@pytest.mark.asyncio
async def test_cycle_wrapper_passes_scope_and_result_identity():
    from unittest.mock import AsyncMock

    from bfx_funding_bot.modules.ledger import CycleResult, Scope

    scope = Scope(uuid4())
    result = CycleResult("fenced")
    sink = AsyncMock()
    sink.run.return_value = result
    metrics = DaemonMetrics()
    wrapper = TimedReconcileRecovery(sink, metrics=metrics)
    assert await wrapper.run(scope) is result
    sink.run.assert_awaited_once_with(scope)
