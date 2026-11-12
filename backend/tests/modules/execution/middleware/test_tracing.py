"""TracingSubmitMiddleware / TracedReconcileRecovery / instrument_ws_dispatcher: transparent, fail-open span wrappers."""
from __future__ import annotations

from decimal import Decimal
from typing import Any
from uuid import uuid4

import pytest
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode

from bfx_funding_bot.external.bitfinex.submit_outcomes import SubmitAcknowledged
from bfx_funding_bot.modules.execution.contracts import (
    ExecutionPolicy,
    GuardResult,
    ReadyToSubmit,
)
from bfx_funding_bot.modules.execution.middleware import (
    TracedReconcileRecovery,
    TracingSubmitMiddleware,
    instrument_ws_dispatcher,
)
from bfx_funding_bot.modules.execution.protocols import (
    AccountContext,
    Credentials,
    SubmittedOrder,
)
from bfx_funding_bot.modules.execution.ws_dispatcher import BitfinexLiveWSDispatcher
from bfx_funding_bot.modules.ledger import CycleResult, Scope
from bfx_funding_bot.modules.observability.resource import (
    DeploymentEnvironment,
    EventResource,
)
from bfx_funding_bot.modules.observability.tracing import (
    DEFAULT_OTLP_ENDPOINT,
    DaemonTracing,
)
from bfx_funding_bot.modules.strategy import DecisionOutcome, DecisionPayload


def _resource() -> EventResource:
    return EventResource(
        deployment_environment=DeploymentEnvironment.PROD,
        service_version="v-test-123",
        host_name=None,
    )


def _enabled_tracing(exporter: InMemorySpanExporter) -> DaemonTracing:
    return DaemonTracing(
        enabled=True,
        endpoint=DEFAULT_OTLP_ENDPOINT,
        event_resource=_resource(),
        exporter=exporter,
    )


_SCOPE = Scope(uuid4())


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
        decision_id="d-trace",
        policy=ExecutionPolicy.BOOK_GUARDED,
        market_snapshot_id="snapshot-trace",
        model_version=None,
        evidence={},
        safety=GuardResult(allowed=True, guard_name="test"),
    )


class _StubExecutor:
    def __init__(self, order: SubmittedOrder | None = None, exc: Exception | None = None) -> None:
        self.order = order
        self.exc = exc
        self.readies: list[ReadyToSubmit] = []

    async def submit(
        self, ready: ReadyToSubmit, ctx: AccountContext,
    ) -> SubmittedOrder:
        self.readies.append(ready)
        if self.exc is not None:
            raise self.exc
        assert self.order is not None
        return self.order


def _reconcile_result() -> CycleResult:
    return CycleResult("accepted")


async def test_submit_middleware_passthrough_and_span() -> None:
    exporter = InMemorySpanExporter()
    t = _enabled_tracing(exporter)
    order = SubmittedOrder(outcome=SubmitAcknowledged("x"))
    inner = _StubExecutor(order=order)
    mw = TracingSubmitMiddleware(inner, tracing=t)
    ready = _ready()
    got = await mw.submit(ready, _ctx())
    assert got is order                 # byte-identical passthrough
    assert inner.readies == [ready]      # immutable boundary object is not rebuilt
    (span,) = exporter.get_finished_spans()
    assert span.name == "executor.submit"
    assert span.attributes is not None
    assert span.attributes["bfx.symbol"] == "fUST"
    assert span.attributes["bfx.execution_decision_id"] == "d-trace"
    assert set(span.attributes) == {
        "bfx.symbol", "bfx.execution_decision_id", "bfx.submit.status",
    }
    assert span.attributes["bfx.submit.status"] == "submitted"
    t.shutdown()


async def test_submit_middleware_exception_passthrough() -> None:
    exporter = InMemorySpanExporter()
    t = _enabled_tracing(exporter)
    boom = RuntimeError("venue down")
    mw = TracingSubmitMiddleware(_StubExecutor(exc=boom), tracing=t)
    with pytest.raises(RuntimeError) as exc_info:
        await mw.submit(_ready(), _ctx())
    assert exc_info.value is boom       # the SAME exception object, unchanged
    (span,) = exporter.get_finished_spans()
    assert span.status.status_code is StatusCode.ERROR
    t.shutdown()


async def test_submit_middleware_transparent_when_disabled() -> None:
    t = DaemonTracing(enabled=False, endpoint=DEFAULT_OTLP_ENDPOINT, event_resource=None)
    order = SubmittedOrder(outcome=SubmitAcknowledged("x"))
    inner = _StubExecutor(order=order)
    mw = TracingSubmitMiddleware(inner, tracing=t)
    got = await mw.submit(_ready(), _ctx())
    assert got is order


async def test_reconcile_wrapper_passthrough_and_span() -> None:
    exporter = InMemorySpanExporter()
    t = _enabled_tracing(exporter)
    result = _reconcile_result()

    class _StubRecovery:
        async def run(self, scope: Scope) -> CycleResult:
            return result

    wrapper = TracedReconcileRecovery(_StubRecovery(), tracing=t)
    got = await wrapper.run(_SCOPE)
    assert got is result
    (span,) = exporter.get_finished_spans()
    assert span.name == "reconcile.tick"
    assert span.attributes is not None
    assert span.attributes["bfx.reconcile.result"] == "ok"
    t.shutdown()


async def test_reconcile_wrapper_exception_passthrough() -> None:
    exporter = InMemorySpanExporter()
    t = _enabled_tracing(exporter)
    boom = ConnectionError("venue fetch failed")

    class _BoomRecovery:
        async def run(self, scope: Scope) -> CycleResult:
            raise boom

    wrapper = TracedReconcileRecovery(_BoomRecovery(), tracing=t)
    with pytest.raises(ConnectionError) as exc_info:
        await wrapper.run(_SCOPE)
    assert exc_info.value is boom
    (span,) = exporter.get_finished_spans()
    assert span.status.status_code is StatusCode.ERROR
    t.shutdown()


class _StubWSClient:
    def events(self) -> Any:  # pragma: no cover - never iterated in these tests
        raise NotImplementedError


class _StubHintSink:
    """A venue hint sink that fails on every hint when built with ``exc``."""

    def __init__(self, exc: Exception | None = None) -> None:
        self.exc = exc

    async def _hint(self) -> None:
        if self.exc is not None:
            raise self.exc

    async def offer_closed(self, hint: Any) -> None:
        await self._hint()

    async def credit_closed(self, hint: Any) -> None:
        await self._hint()


class _StubSink:
    async def emit(self, event: dict[str, Any]) -> None:  # pragma: no cover
        raise NotImplementedError


def _dispatcher(sink: _StubHintSink | None = None) -> BitfinexLiveWSDispatcher:
    return BitfinexLiveWSDispatcher(
        ws_client=_StubWSClient(), event_sink=_StubSink(),
        venue_hint_sink=sink or _StubHintSink())


async def test_ws_dispatcher_instrumented_process_spans() -> None:
    exporter = InMemorySpanExporter()
    t = _enabled_tracing(exporter)
    dispatcher = _dispatcher()
    instrument_ws_dispatcher(dispatcher, tracing=t)
    await dispatcher._process(object())  # unknown event type → no hint
    (span,) = exporter.get_finished_spans()
    assert span.name == "ws_dispatcher.process"
    assert span.attributes is not None
    assert span.attributes["bfx.ws.event_type"] == "object"
    t.shutdown()


async def test_ws_dispatcher_instrumented_process_exception_passthrough() -> None:
    from bfx_funding_bot.external.bitfinex.auth_ws import FocEvent

    exporter = InMemorySpanExporter()
    t = _enabled_tracing(exporter)
    boom = RuntimeError("hint sink broken")
    dispatcher = _dispatcher(_StubHintSink(exc=boom))
    instrument_ws_dispatcher(dispatcher, tracing=t)
    with pytest.raises(RuntimeError) as exc_info:
        # A closing offer is handed to the sink, whose failure passes through the span.
        await dispatcher._process(FocEvent(
            venue_offer_id="42", symbol="fUST", mts_create=1000, mts_update=2000,
            amount=Decimal("100"), status="EXECUTED", rate=0.0005,
            period_days=2, raw_seq=17,
        ))
    assert exc_info.value is boom
    t.shutdown()


def test_instrument_ws_dispatcher_fail_open_on_odd_object() -> None:
    """A dispatcher without a _process attribute is skipped, never a raise."""
    t = DaemonTracing(enabled=False, endpoint=DEFAULT_OTLP_ENDPOINT, event_resource=None)
    instrument_ws_dispatcher(object(), tracing=t)  # must not raise


@pytest.mark.asyncio
async def test_cycle_wrapper_passes_scope_and_result_identity():
    from unittest.mock import AsyncMock

    from bfx_funding_bot.modules.ledger import CycleResult, Scope

    scope = Scope(uuid4())
    result = CycleResult("fenced")
    sink = AsyncMock()
    sink.run.return_value = result
    tracing = _enabled_tracing(InMemorySpanExporter())
    wrapper = TracedReconcileRecovery(sink, tracing=tracing)
    assert await wrapper.run(scope) is result
    sink.run.assert_awaited_once_with(scope)
    tracing.shutdown()
