"""Phase 4.3 executor-middleware integration: chain composition and the no-retry contract."""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from uuid import UUID, uuid4

import pytest

from bfx_funding_bot.core.errors import ExecutorTransientError
from bfx_funding_bot.core.health import HealthProbe
from bfx_funding_bot.modules.execution.command_gate import SubmitOutcomeLostError
from bfx_funding_bot.modules.execution.contracts import (
    ExecutionPolicy,
    GuardResult,
    ReadyToSubmit,
    ReservationRef,
)
from bfx_funding_bot.modules.execution.middleware import (
    HeartbeatMiddleware,
    ReservationEmittingMiddleware,
)
from bfx_funding_bot.modules.execution.protocols import (
    AccountContext,
    Credentials,
    SubmittedOrder,
)
from bfx_funding_bot.modules.execution.submit_outcomes import SubmitAcknowledged
from bfx_funding_bot.modules.strategy import DecisionOutcome, DecisionPayload
from tests.modules.execution.fake_boundary import (  # noqa: F401  (fixture)
    Recording,
    boundary_stubs,
    with_capital,
)

pytestmark = pytest.mark.usefixtures("boundary_stubs")

_ACCOUNT = UUID("550e8400-e29b-41d4-a716-446655440000")


class _AllowChain:
    async def evaluate(self, decision: DecisionPayload, ctx: AccountContext) -> GuardResult:
        return GuardResult(allowed=True, guard_name="test")


class _UncertaintyNeverOpen:
    async def has_open(self, session: object, scope: object, symbol: str) -> bool:
        return False

    async def list_open(self, session: object, scope: object, symbol: str | None = None) -> tuple[()]:
        return ()


class _AckInner:
    async def submit(
        self, ready: ReadyToSubmit, ctx: AccountContext, *, cid: int | None = None,
        reservation_ref: ReservationRef | None = None,
    ) -> SubmittedOrder:
        assert cid is not None
        assert reservation_ref is not None
        return SubmittedOrder(
            cid=cid,
            venue_offer_id="venue-xyz",
            outcome=SubmitAcknowledged("venue-xyz"),
            raw_response=None,
            reservation_ref=reservation_ref.bind_venue_offer("venue-xyz"),
        )


class _TransientInner:
    """Inner executor that always raises a transient error — used to prove the
    chain attempts a financial submit exactly once (no retry)."""

    def __init__(self) -> None:
        self.calls = 0

    async def submit(
        self, ready: ReadyToSubmit, ctx: AccountContext, *, cid: int | None = None,
        reservation_ref: object | None = None,
    ) -> SubmittedOrder:
        self.calls += 1
        raise ExecutorTransientError("network_blip")


def _decision() -> DecisionPayload:
    return DecisionPayload(
        decision_outcome=DecisionOutcome.POST,
        signal_correlation_id=uuid4(),
        offer_rate=0.0001,
        offer_amount_usdt=100.0,
        offer_duration_days=2,
    symbol="fUST")


def _ctx() -> AccountContext:
    return AccountContext(
        account_id=str(_ACCOUNT),
        credentials=Credentials(api_key="k", api_secret="s"),
        allocation_cap_usdt=Decimal("10000"),
    )


def _ready() -> ReadyToSubmit:
    return with_capital(ReadyToSubmit(
        decision=_decision(), decision_id="d-daemon-chain",
        policy=ExecutionPolicy.BOOK_GUARDED, market_snapshot_id="snapshot-daemon-chain",
        model_version=None, evidence={}, safety=GuardResult(allowed=True, guard_name="test"),
    ))


def _build_chain(
    inner: object | None = None,
) -> tuple[HeartbeatMiddleware, HealthProbe, Recording]:
    """Mirror the daemon's production executor chain (apps/bot.build_daemon).

    Keep this in lockstep with bot.py's wrapped_executor composition.
    """
    probe = HealthProbe()
    recording = Recording(environment="ci", account_id=_ACCOUNT)
    executor = HeartbeatMiddleware(
        ReservationEmittingMiddleware(
            inner or _AckInner(),  # type: ignore[arg-type]
            safety_evaluator=_AllowChain(),
            boundary=recording.boundary(),
            uncertainty_reader=_UncertaintyNeverOpen(),  # type: ignore[arg-type]
            managed_offers=recording.offers,
            clock=iter(range(100, 1000)).__next__,
            date_provider=lambda: date(2026, 9, 3),
        ),
        probe=probe,
    )
    return executor, probe, recording


@pytest.mark.asyncio
async def test_wired_chain_types() -> None:
    executor, _probe, _recording = _build_chain()
    assert isinstance(executor, HeartbeatMiddleware)
    inner1 = executor._inner  # type: ignore[attr-defined]
    assert isinstance(inner1, ReservationEmittingMiddleware)
    # No retry wrapper: the command gate wraps the executor directly (submit is a
    # once-only financial write — see test_chain_does_not_retry_submit_on_transient).
    inner2 = inner1.command_gate._inner  # type: ignore[attr-defined]
    assert isinstance(inner2, _AckInner)


@pytest.mark.asyncio
async def test_chain_does_not_retry_submit_on_transient() -> None:
    """Regression guard (real-money double-offer): a funding-offer submit is a
    financial write and must be attempted exactly ONCE.

    Bitfinex funding offers have no client cid dedup (only trading orders do),
    so any retry around submit risks a duplicate live offer. The executor chain
    must add no retry — a transient error propagates after a single attempt.
    """
    inner = _TransientInner()
    executor, _probe, _recording = _build_chain(inner=inner)

    # The gate turns the lost outcome into process fencing (a BaseException); the
    # chain still attempted the submit exactly once.
    with pytest.raises(SubmitOutcomeLostError) as raised:
        await executor.submit(_ready(), _ctx())
    assert isinstance(raised.value.__cause__, ExecutorTransientError)
    assert inner.calls == 1


@pytest.mark.asyncio
async def test_end_to_end_records_the_command_and_fires_the_heartbeat() -> None:
    """Happy path: one submit is journaled intent-then-outcome and the heartbeat fires."""
    executor, probe, recording = _build_chain()

    result = await executor.submit(_ready(), _ctx())

    assert result.outcome_kind.value == "acknowledged"
    assert [type(event).__name__ for event in recording.events] == [
        "ReservationIntent", "ReservationClaimed",
    ]
    assert probe.last_active_ts.get("executor") is not None
