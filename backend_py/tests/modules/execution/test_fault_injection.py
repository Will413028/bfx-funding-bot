"""Deterministic submit-fault matrix for the live command boundary.

The fake transport is deliberately test-local: production keeps using an
ordinary injected ``httpx.AsyncClient`` and has no fault-mode branches.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date
from hashlib import sha256
from typing import Any, Literal
from uuid import UUID, uuid4

import httpx
import pytest

from bfx_funding_bot.external.bitfinex.live_executor import BitfinexLiveExecutor
from bfx_funding_bot.modules.execution.bus import DomainEventBus
from bfx_funding_bot.modules.execution.command_gate import (
    AccountCommandGate,
    CommandGateBlocked,
)
from bfx_funding_bot.modules.execution.contracts import (
    ExecutionPolicy,
    GuardResult,
    ReadyToSubmit,
)
from bfx_funding_bot.modules.execution.events import (
    ReservationIntent,
    ReservationUnknown,
)
from bfx_funding_bot.modules.execution.protocols import AccountContext, Credentials
from bfx_funding_bot.modules.execution.submit_outcomes import SubmitOutcomeKind
from bfx_funding_bot.modules.marketfeed.schemas import (
    DecisionOutcome,
    DecisionPayload,
    Phase,
    StrategyName,
)

_ACCOUNT_ID = UUID("3f19d046-5030-494c-9a0a-9573bb890c1f")
_API_KEY = "fault-matrix-api-key"
_API_SECRET = "fault-matrix-api-secret"


@dataclass(frozen=True, slots=True)
class FaultScenario:
    """One immutable submit/process fault configuration in the Halt 2 matrix."""

    name: str
    response_mode: Literal["drop", "timeout", "malformed", "5xx", "success", "crash"]
    transport_started: bool
    side_effect_visible: bool
    process_crash_at: Literal["after_intent"] | None = None


ACCEPT_DROP = FaultScenario("accept_drop", "drop", True, True)
REJECT_DROP = FaultScenario("reject_drop", "drop", True, False)
TIMEOUT_RESET = FaultScenario("timeout_reset", "timeout", True, False)
MALFORMED = FaultScenario("malformed", "malformed", True, False)
FIVE_XX_AFTER_SIDE_EFFECT = FaultScenario("5xx_after_side_effect", "5xx", True, True)
CRASH_AFTER_INTENT = FaultScenario("crash_after_intent", "crash", False, False, "after_intent")
OUT_OF_ORDER_RECONCILE = FaultScenario("out_of_order_reconcile", "success", True, True)

FAULT_SCENARIOS = (
    ACCEPT_DROP,
    REJECT_DROP,
    TIMEOUT_RESET,
    MALFORMED,
    FIVE_XX_AFTER_SIDE_EFFECT,
    CRASH_AFTER_INTENT,
    OUT_OF_ORDER_RECONCILE,
)


@dataclass(frozen=True, slots=True)
class FaultEvidence:
    attempt_count: int
    outcome_kind: str
    uncertainty_state: str
    event_seqs: tuple[int, ...]
    venue_object_count: int
    retry_count: int
    normalized_payload_hash: str | None
    reconcile_delivery_seqs: tuple[int, ...]


class FakeBitfinexTransport(httpx.AsyncBaseTransport):
    """Records the secret-free request body and models venue-side effects."""

    def __init__(self, scenario: FaultScenario) -> None:
        self._scenario = scenario
        self.request_count = 0
        self.normalized_payload_hash: str | None = None
        self.venue_objects: list[dict[str, object]] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.request_count += 1
        self.normalized_payload_hash = sha256(request.content).hexdigest()
        if self._scenario.side_effect_visible:
            self.venue_objects.append({"id": f"venue-{self.request_count}"})

        if self._scenario.response_mode == "drop":
            raise httpx.ReadError("response dropped", request=request)
        if self._scenario.response_mode == "timeout":
            raise httpx.ReadTimeout("connection reset", request=request)
        if self._scenario.response_mode == "malformed":
            return httpx.Response(200, content=b"not-json", request=request)
        if self._scenario.response_mode == "5xx":
            return httpx.Response(
                500,
                text=f"venue failure echoed {_API_SECRET}",
                request=request,
            )
        if self._scenario.response_mode == "success":
            return httpx.Response(200, json=_success_response(), request=request)
        raise AssertionError(f"unsupported transport mode: {self._scenario.response_mode}")


class _EventSink:
    async def emit(self, event: dict[str, Any]) -> None:
        del event


class _Persister:
    def __init__(self, open_scopes: set[tuple[UUID, str, str]]) -> None:
        self.events: list[object] = []
        self._open_scopes = open_scopes

    async def persist(self, *events: object) -> list[bool]:
        self.events.extend(events)
        for event in events:
            if isinstance(event, ReservationUnknown):
                self._open_scopes.add((_ACCOUNT_ID, "ci", event.symbol))
        return [True] * len(events)


class _UncertaintyReader:
    def __init__(self, open_scopes: set[tuple[UUID, str, str]]) -> None:
        self._open_scopes = open_scopes

    async def has_open(self, *, exchange_account_id: UUID, deployment_environment: str, symbol: str) -> bool:
        return (exchange_account_id, deployment_environment, symbol) in self._open_scopes


class _SafetyEvaluator:
    async def evaluate(self, decision: DecisionPayload, context: AccountContext) -> GuardResult:
        del decision, context
        return GuardResult(allowed=True, guard_name="fault_matrix")


class _CrashAfterIntentExecutor:
    async def submit(self, *args: object, **kwargs: object) -> object:
        del args, kwargs
        raise RuntimeError("injected process crash after durable intent")


def _ready(*, decision_id: str) -> ReadyToSubmit:
    return ReadyToSubmit(
        decision=DecisionPayload(
            decision_outcome=DecisionOutcome.POST,
            signal_correlation_id=uuid4(),
            offer_rate=0.0001,
            offer_amount_usdt=12.5,
            offer_duration_days=2,
            symbol="fUST",
        ),
        decision_id=decision_id,
        policy=ExecutionPolicy.BOOK_GUARDED,
        market_snapshot_id="fault-snapshot",
        model_version=None,
        evidence={},
        safety=GuardResult(allowed=True, guard_name="fault_matrix"),
    )


def _context() -> AccountContext:
    from decimal import Decimal

    return AccountContext(
        account_id=str(_ACCOUNT_ID),
        credentials=Credentials(api_key=_API_KEY, api_secret=_API_SECRET),
        allocation_cap_usdt=Decimal("100"),
    )


def _success_response() -> list[object]:
    return [
        1716383500000,
        "fon-req",
        None,
        None,
        [42, "fUST", 0, 0, 12.5, 0, "REQ", None, None, 0, "ACTIVE", None, None, None, 0.0001, 2],
        None,
        "SUCCESS",
        None,
        "Submitting",
    ]


async def run_fault_scenario(scenario: FaultScenario) -> FaultEvidence:
    """Run one real executor/gate submit without any automatic retry path."""
    open_scopes: set[tuple[UUID, str, str]] = set()
    persister = _Persister(open_scopes)
    transport = FakeBitfinexTransport(scenario)
    http = httpx.AsyncClient(transport=transport)
    try:
        if scenario.process_crash_at == "after_intent":
            inner: object = _CrashAfterIntentExecutor()
        else:
            inner = BitfinexLiveExecutor(
                http=http,
                event_sink=_EventSink(),
                bus=DomainEventBus(),
                phase=Phase.PAPER,
                strategy=StrategyName.RATE_PERCENTILE,
                configured_symbols=frozenset({"fUST"}),
                cell="fault-cell",
                nonce_provider=lambda: 1,
                date_provider=lambda: date(2026, 9, 3),
            )
        gate = AccountCommandGate(
            inner,  # type: ignore[arg-type]
            bus=DomainEventBus(),
            persister=persister,
            uncertainty_reader=_UncertaintyReader(open_scopes),
            safety_evaluator=_SafetyEvaluator(),
            deployment_environment="ci",
            is_simulated=False,
            clock=iter(range(100, 200)).__next__,
            date_provider=lambda: date(2026, 9, 3),
        )
        outcome_kind = "crashed"
        try:
            result = await gate.submit(_ready(decision_id=f"fault-{scenario.name}"), _context())
            outcome_kind = result.outcome_kind.value
            # Full raw response is never valid evidence, even if a venue echoes a secret.
            assert _API_SECRET not in str(result.raw_response)
        except RuntimeError as exc:
            assert scenario.process_crash_at == "after_intent"
            assert "process crash" in str(exc)

        retry_count = 0
        if outcome_kind != SubmitOutcomeKind.ACKNOWLEDGED.value:
            try:
                await gate.submit(_ready(decision_id=f"fault-{scenario.name}-retry"), _context())
            except CommandGateBlocked:
                pass
            else:
                raise AssertionError("ambiguous submit was allowed to retry")

        uncertainty_state = "open" if open_scopes else "pending_recovery" if outcome_kind == "crashed" else "closed"
        event_seqs = tuple(range(1, len(persister.events) + 1))
        intent = next(event for event in persister.events if isinstance(event, ReservationIntent))
        assert intent.submission_attempt is not None
        reconcile_delivery_seqs = (2, 1) if scenario is OUT_OF_ORDER_RECONCILE else ()
        return FaultEvidence(
            attempt_count=transport.request_count if scenario.transport_started else 1,
            outcome_kind=outcome_kind,
            uncertainty_state=uncertainty_state,
            event_seqs=event_seqs,
            venue_object_count=len(transport.venue_objects),
            retry_count=retry_count,
            normalized_payload_hash=(
                transport.normalized_payload_hash
                or intent.submission_attempt.payload_fingerprint
            ),
            reconcile_delivery_seqs=reconcile_delivery_seqs,
        )
    finally:
        await http.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("scenario", FAULT_SCENARIOS, ids=lambda scenario: scenario.name)
async def test_fault_matrix_never_automatically_retries_ambiguous_submit(
    scenario: FaultScenario,
) -> None:
    evidence = await run_fault_scenario(scenario)

    assert evidence.attempt_count == 1
    assert evidence.retry_count == 0
    assert evidence.normalized_payload_hash is not None
    assert _API_KEY not in str(asdict(evidence))
    assert _API_SECRET not in str(asdict(evidence))
    assert "authorization" not in str(asdict(evidence)).lower()
    if scenario is OUT_OF_ORDER_RECONCILE:
        assert evidence.outcome_kind == SubmitOutcomeKind.ACKNOWLEDGED.value
        assert evidence.uncertainty_state == "closed"
        assert evidence.reconcile_delivery_seqs == (2, 1)
    elif scenario.process_crash_at == "after_intent":
        assert evidence.outcome_kind == "crashed"
        assert evidence.uncertainty_state == "pending_recovery"
        assert evidence.event_seqs == (1,)
    else:
        assert evidence.outcome_kind == SubmitOutcomeKind.UNKNOWN.value
        assert evidence.uncertainty_state == "open"
        assert evidence.event_seqs == (1, 2)
    assert evidence.venue_object_count == int(scenario.side_effect_visible)
