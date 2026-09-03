"""Deterministic submit-fault matrix for the live command boundary.

The fake transport is deliberately test-local: production keeps using an
ordinary injected ``httpx.AsyncClient`` and has no fault-mode branches.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date
from decimal import Decimal
from hashlib import sha256
from typing import Any, Literal
from uuid import UUID, uuid4

import httpx
import pytest

from bfx_funding_bot.external.bitfinex.auth_rest import (
    ActiveFundingOffer,
    FundingOfferHistoryCoverage,
)
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
    ReservationRef,
)
from bfx_funding_bot.modules.execution.event_store.projector import (
    InvalidVenueOfferTransition,
    apply_offer_transition,
)
from bfx_funding_bot.modules.execution.events import (
    ReservationFailed,
    ReservationIntent,
    ReservationUnknown,
)
from bfx_funding_bot.modules.execution.protocols import (
    AccountContext,
    Credentials,
    SubmittedOrder,
)
from bfx_funding_bot.modules.execution.submit_outcomes import (
    SubmitOutcomeKind,
    SubmitOutcomeUnknown,
    SubmitRejected,
)
from bfx_funding_bot.modules.execution.unknown_matching import (
    UnknownSubmitAttempt,
    match_unknown_attempt,
)
from bfx_funding_bot.modules.marketfeed.schemas import (
    DecisionOutcome,
    DecisionPayload,
    Phase,
    StrategyName,
)

_ACCOUNT_ID = UUID("3f19d046-5030-494c-9a0a-9573bb890c1f")
_ADJACENT_ACCOUNT_ID = UUID("28b31e79-83ce-4b32-a6b7-03d78043ce68")
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
OUT_OF_ORDER_RECONCILE = FaultScenario("out_of_order_reconcile", "drop", True, True)

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
    durable_intent_count: int
    transport_request_count: int
    outcome_kind: str
    uncertainty_state: str
    event_seqs: tuple[int, ...]
    venue_object_count: int
    retry_count: int
    normalized_payload_hash: str | None
    reconcile_delivery_seqs: tuple[int, ...]
    reconcile_final_status: str | None
    unknown_exposure_usdt: Decimal | None
    unknown_scope: tuple[UUID, str, str] | None
    adjacent_scope_allowed: bool
    uncertainty_reader_calls: tuple[tuple[UUID, str, str], ...]


@dataclass(frozen=True, slots=True)
class MultipleCandidateEvidence:
    durable_intent_count: int
    transport_request_count: int
    retry_count: int
    match_kind: str
    candidate_ids: tuple[str, ...]
    unknown_exposure_usdt: Decimal
    candidate_exposure_usdt: Decimal
    unknown_scope: tuple[UUID, str, str]


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
    def __init__(self, open_scopes: set[tuple[UUID, str, str]], environment: str) -> None:
        self.events: list[object] = []
        self._open_scopes = open_scopes
        self._environment = environment

    async def persist(self, *events: object) -> list[bool]:
        self.events.extend(events)
        for event in events:
            if isinstance(event, ReservationUnknown):
                self._open_scopes.add((UUID(event.account_id), self._environment, event.symbol))
        return [True] * len(events)


class _UncertaintyReader:
    def __init__(self, open_scopes: set[tuple[UUID, str, str]]) -> None:
        self._open_scopes = open_scopes
        self.calls: list[tuple[UUID, str, str]] = []

    async def has_open(self, *, exchange_account_id: UUID, deployment_environment: str, symbol: str) -> bool:
        self.calls.append((exchange_account_id, deployment_environment, symbol))
        return (exchange_account_id, deployment_environment, symbol) in self._open_scopes


class _SafetyEvaluator:
    async def evaluate(self, decision: DecisionPayload, context: AccountContext) -> GuardResult:
        del decision, context
        return GuardResult(allowed=True, guard_name="fault_matrix")


class _CrashAfterIntentExecutor:
    async def submit(self, *args: object, **kwargs: object) -> object:
        del args, kwargs
        raise RuntimeError("injected process crash after durable intent")


class _TypedOutcomeExecutor:
    def __init__(self, outcome: SubmitOutcomeUnknown | SubmitRejected) -> None:
        self.outcome = outcome
        self.calls = 0

    async def submit(
        self,
        ready: ReadyToSubmit,
        context: AccountContext,
        *,
        cid: int | None = None,
        reservation_ref: ReservationRef | None = None,
    ) -> SubmittedOrder:
        del ready, context
        self.calls += 1
        return SubmittedOrder(
            cid=cid or 0,
            venue_offer_id=None,
            outcome=self.outcome,
            reservation_ref=reservation_ref,
        )


def _ready(*, decision_id: str, symbol: str = "fUST") -> ReadyToSubmit:
    return ReadyToSubmit(
        decision=DecisionPayload(
            decision_outcome=DecisionOutcome.POST,
            signal_correlation_id=uuid4(),
            offer_rate=0.0001,
            offer_amount_usdt=12.5,
            offer_duration_days=2,
            symbol=symbol,
        ),
        decision_id=decision_id,
        policy=ExecutionPolicy.BOOK_GUARDED,
        market_snapshot_id="fault-snapshot",
        model_version=None,
        evidence={},
        safety=GuardResult(allowed=True, guard_name="fault_matrix"),
    )


def _context(account_id: UUID = _ACCOUNT_ID) -> AccountContext:
    return AccountContext(
        account_id=str(account_id),
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


def _unknown_attempt() -> UnknownSubmitAttempt:
    ready = _ready(decision_id="candidate-ambiguity")
    return UnknownSubmitAttempt(
        attempt_id=UUID("22222222-2222-2222-2222-222222222222"),
        execution_decision_id=ready.decision_id,
        account_id=str(_ACCOUNT_ID),
        symbol="fUST",
        cid=7,
        amount=Decimal("12.5"),
        rate=Decimal("0.0001"),
        period_days=2,
        offer_type="LIMIT",
        flags=0,
        started_at_ms=1_000,
        signal_correlation_id=ready.decision.signal_correlation_id,
        reservation_ref=ReservationRef(
            execution_decision_id=ready.decision_id,
            cid=7,
            signal_correlation_id=ready.decision.signal_correlation_id,
        ),
    )


def _matching_offer(venue_offer_id: str, *, mts_created: int = 1_000) -> ActiveFundingOffer:
    return ActiveFundingOffer(
        venue_offer_id=venue_offer_id,
        symbol="fUST",
        amount=Decimal("12.5"),
        amount_original=Decimal("12.5"),
        rate=0.0001,
        rate_decimal=Decimal("0.0001"),
        period_days=2,
        mts_created=mts_created,
        status="ACTIVE",
        offer_type="LIMIT",
        flags=0,
    )


def _deliver_out_of_order_reconcile() -> tuple[tuple[int, ...], str]:
    """Deliver two real projection observations in reverse event order."""
    deliveries: list[int] = []
    projected = apply_offer_transition(
        None,
        venue_offer_id="venue-1",
        symbol="fUST",
        amount_original=Decimal("12.5"),
        amount_remaining=Decimal("12.5"),
        rate=Decimal("0.0001"),
        period_days=2,
        mts_created=101,
        mts_updated=102,
        status="active",
        event_seq=2,
        offer_type="LIMIT",
        flags={"raw": 0},
    )
    deliveries.append(2)
    with pytest.raises(InvalidVenueOfferTransition, match="precedes last seen"):
        apply_offer_transition(
            projected,
            status="cancelled",
            event_seq=1,
            mts_updated=101,
            amount_remaining=Decimal("0"),
        )
    deliveries.append(1)
    return tuple(deliveries), projected.status


async def run_multiple_candidate_reconcile() -> MultipleCandidateEvidence:
    """Run UNKNOWN persistence, two-candidate reconcile, then the blocked retry."""
    environment = "ci"
    open_scopes: set[tuple[UUID, str, str]] = set()
    persister = _Persister(open_scopes, environment)
    reader = _UncertaintyReader(open_scopes)
    transport = FakeBitfinexTransport(ACCEPT_DROP)
    async with httpx.AsyncClient(transport=transport) as http:
        executor = BitfinexLiveExecutor(
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
            executor,
            bus=DomainEventBus(),
            persister=persister,
            uncertainty_reader=reader,
            safety_evaluator=_SafetyEvaluator(),
            deployment_environment=environment,
            is_simulated=False,
            clock=iter(range(100, 200)).__next__,
            date_provider=lambda: date(2026, 9, 3),
        )
        ready = _ready(decision_id="multiple-candidate")
        result = await gate.submit(ready, _context())
        assert result.outcome_kind is SubmitOutcomeKind.UNKNOWN
        intent = next(event for event in persister.events if isinstance(event, ReservationIntent))
        unknown = next(event for event in persister.events if isinstance(event, ReservationUnknown))
        assert intent.submission_attempt is not None
        assert intent.reservation_ref is not None
        payload = intent.submission_attempt.normalized_payload
        attempt = UnknownSubmitAttempt(
            attempt_id=intent.submission_attempt.attempt_id,
            execution_decision_id=intent.submission_attempt.execution_decision_id,
            account_id=str(intent.submission_attempt.account_id),
            symbol=intent.submission_attempt.symbol,
            cid=intent.submission_attempt.cid,
            amount=Decimal(str(payload["amount"])),
            rate=Decimal(str(payload["rate"])),
            period_days=int(payload["period"]),
            offer_type=str(payload["type"]),
            flags=payload["flags"],
            started_at_ms=intent.submission_attempt.started_at_ms,
            signal_correlation_id=intent.signal_correlation_id,
            reservation_ref=intent.reservation_ref,
        )
        candidates = (
            _matching_offer("venue-a", mts_created=attempt.started_at_ms),
            _matching_offer("venue-b", mts_created=attempt.started_at_ms),
        )
        matched = match_unknown_attempt(
            attempt,
            candidates,
            (),
            FundingOfferHistoryCoverage(
                requested_start_ms=attempt.started_at_ms,
                requested_end_ms=attempt.started_at_ms,
                oldest_mts_created=attempt.started_at_ms,
                newest_mts_created=attempt.started_at_ms,
                pages=1,
                complete=True,
            ),
        )
        request_count_before_retry = transport.request_count
        with pytest.raises(CommandGateBlocked, match="open execution uncertainty"):
            await gate.submit(_ready(decision_id="multiple-candidate-retry"), _context())
        retry_count = transport.request_count - request_count_before_retry
        return MultipleCandidateEvidence(
            durable_intent_count=sum(
                isinstance(event, ReservationIntent) for event in persister.events
            ),
            transport_request_count=transport.request_count,
            retry_count=retry_count,
            match_kind=matched.kind,
            candidate_ids=tuple(candidate.venue_offer_id for candidate in matched.candidates),
            unknown_exposure_usdt=unknown.size_usdt,
            candidate_exposure_usdt=sum(candidate.amount for candidate in matched.candidates),
            unknown_scope=(UUID(unknown.account_id), environment, unknown.symbol),
        )


async def run_fault_scenario(scenario: FaultScenario) -> FaultEvidence:
    """Run one real executor/gate submit without any automatic retry path."""
    open_scopes: set[tuple[UUID, str, str]] = set()
    environment = "ci"
    persister = _Persister(open_scopes, environment)
    uncertainty_reader = _UncertaintyReader(open_scopes)
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
            uncertainty_reader=uncertainty_reader,
            safety_evaluator=_SafetyEvaluator(),
            deployment_environment="ci",
            is_simulated=False,
            clock=iter(range(100, 200)).__next__,
            date_provider=lambda: date(2026, 9, 3),
        )
        outcome_kind = "crashed"
        ready = _ready(decision_id=f"fault-{scenario.name}")
        context = _context()
        try:
            result = await gate.submit(ready, context)
            outcome_kind = result.outcome_kind.value
            # Full raw response is never valid evidence, even if a venue echoes a secret.
            assert _API_SECRET not in str(result.raw_response)
        except RuntimeError as exc:
            assert scenario.process_crash_at == "after_intent"
            assert "process crash" in str(exc)

        transport_count_before_retry_check = transport.request_count
        if outcome_kind != SubmitOutcomeKind.ACKNOWLEDGED.value:
            try:
                await gate.submit(_ready(decision_id=f"fault-{scenario.name}-retry"), _context())
            except CommandGateBlocked:
                pass
            else:
                raise AssertionError("ambiguous submit was allowed to retry")

        retry_count = transport.request_count - transport_count_before_retry_check
        unknown_events = tuple(
            event for event in persister.events if isinstance(event, ReservationUnknown)
        )
        unknown_event = unknown_events[0] if unknown_events else None
        if unknown_event is not None:
            assert unknown_event.size_usdt == Decimal("12.5")
            assert unknown_event.account_id == str(_ACCOUNT_ID)
            assert unknown_event.symbol == "fUST"
            assert (_ACCOUNT_ID, environment, "fUST") in open_scopes
            await gate.check(_ready(decision_id=f"fault-{scenario.name}-other-symbol", symbol="fUSD"), context)
            await gate.check(_ready(decision_id=f"fault-{scenario.name}-other-account"), _context(_ADJACENT_ACCOUNT_ID))
            other_environment_gate = AccountCommandGate(
                inner,  # type: ignore[arg-type]
                bus=DomainEventBus(),
                persister=persister,
                uncertainty_reader=uncertainty_reader,
                safety_evaluator=_SafetyEvaluator(),
                deployment_environment="staging",
                is_simulated=False,
                clock=iter(range(300, 400)).__next__,
                date_provider=lambda: date(2026, 9, 3),
            )
            await other_environment_gate.check(
                _ready(decision_id=f"fault-{scenario.name}-other-environment"),
                context,
            )
            adjacent_scope_allowed = True
        else:
            adjacent_scope_allowed = False
        uncertainty_state = "open" if open_scopes else "pending_recovery" if outcome_kind == "crashed" else "closed"
        event_seqs = tuple(range(1, len(persister.events) + 1))
        intent = next(event for event in persister.events if isinstance(event, ReservationIntent))
        assert intent.submission_attempt is not None
        reconcile_delivery_seqs: tuple[int, ...] = ()
        reconcile_final_status: str | None = None
        if scenario is OUT_OF_ORDER_RECONCILE:
            reconcile_delivery_seqs, reconcile_final_status = _deliver_out_of_order_reconcile()
        return FaultEvidence(
            durable_intent_count=sum(
                isinstance(event, ReservationIntent) for event in persister.events
            ),
            transport_request_count=transport.request_count,
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
            reconcile_final_status=reconcile_final_status,
            unknown_exposure_usdt=(unknown_event.size_usdt if unknown_event else None),
            unknown_scope=(
                (UUID(unknown_event.account_id), environment, unknown_event.symbol)
                if unknown_event is not None
                else None
            ),
            adjacent_scope_allowed=adjacent_scope_allowed,
            uncertainty_reader_calls=tuple(uncertainty_reader.calls),
        )
    finally:
        await http.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("scenario", FAULT_SCENARIOS, ids=lambda scenario: scenario.name)
async def test_fault_matrix_never_automatically_retries_ambiguous_submit(
    scenario: FaultScenario,
) -> None:
    evidence = await run_fault_scenario(scenario)

    assert evidence.durable_intent_count == 1
    assert evidence.transport_request_count == int(scenario.transport_started)
    assert evidence.retry_count == 0
    assert evidence.normalized_payload_hash is not None
    assert _API_KEY not in str(asdict(evidence))
    assert _API_SECRET not in str(asdict(evidence))
    assert "authorization" not in str(asdict(evidence)).lower()
    if scenario is OUT_OF_ORDER_RECONCILE:
        assert evidence.outcome_kind == SubmitOutcomeKind.UNKNOWN.value
        assert evidence.uncertainty_state == "open"
        assert evidence.reconcile_delivery_seqs == (2, 1)
        assert evidence.reconcile_final_status == "active"
    elif scenario.process_crash_at == "after_intent":
        assert evidence.outcome_kind == "crashed"
        assert evidence.uncertainty_state == "pending_recovery"
        assert evidence.event_seqs == (1,)
    else:
        assert evidence.outcome_kind == SubmitOutcomeKind.UNKNOWN.value
        assert evidence.uncertainty_state == "open"
        assert evidence.event_seqs == (1, 2)
        assert evidence.unknown_exposure_usdt == Decimal("12.5")
        assert evidence.unknown_scope == (_ACCOUNT_ID, "ci", "fUST")
        assert evidence.adjacent_scope_allowed is True
        assert (_ACCOUNT_ID, "ci", "fUST") in evidence.uncertainty_reader_calls
        assert (_ACCOUNT_ID, "ci", "fUSD") in evidence.uncertainty_reader_calls
        assert (_ADJACENT_ACCOUNT_ID, "ci", "fUST") in evidence.uncertainty_reader_calls
        assert (_ACCOUNT_ID, "staging", "fUST") in evidence.uncertainty_reader_calls
    assert evidence.venue_object_count == int(scenario.side_effect_visible)


@pytest.mark.asyncio
async def test_multiple_candidate_reconcile_stays_unknown_and_preserves_exposure() -> None:
    """One durable UNKNOWN remains blocked when reconcile finds two matches."""
    evidence = await run_multiple_candidate_reconcile()

    assert evidence.match_kind == "multiple_match"
    assert evidence.candidate_ids == ("venue-a", "venue-b")
    assert evidence.durable_intent_count == 1
    assert evidence.transport_request_count == 1
    assert evidence.retry_count == 0
    assert evidence.unknown_exposure_usdt == Decimal("12.5")
    assert evidence.candidate_exposure_usdt == Decimal("25.0")
    assert evidence.unknown_scope == (_ACCOUNT_ID, "ci", "fUST")


def test_out_of_order_reconcile_projection_rejects_stale_delivery() -> None:
    """The real projector retains the newer observed offer, never reopening it."""
    newer = apply_offer_transition(
        None,
        venue_offer_id="venue-1",
        symbol="fUST",
        amount_original=Decimal("12.5"),
        amount_remaining=Decimal("12.5"),
        rate=Decimal("0.0001"),
        period_days=2,
        mts_created=101,
        mts_updated=102,
        status="active",
        event_seq=2,
        offer_type="LIMIT",
        flags={"raw": 0},
    )

    with pytest.raises(InvalidVenueOfferTransition, match="precedes last seen"):
        apply_offer_transition(
            newer,
            status="cancelled",
            event_seq=1,
            mts_updated=101,
            amount_remaining=Decimal("0"),
        )

    assert newer.status == "active"
    assert newer.last_seen_event_seq == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("outcome", "event_type"),
    [
        (SubmitOutcomeUnknown(f"{_API_KEY} Authorization: Bearer {_API_SECRET}", True), ReservationUnknown),
        (SubmitRejected(f"{_API_KEY} Authorization: Bearer {_API_SECRET}"), ReservationFailed),
    ],
)
async def test_durable_outcome_reason_redacts_credentials_and_authorization(
    outcome: SubmitOutcomeUnknown | SubmitRejected,
    event_type: type[ReservationUnknown] | type[ReservationFailed],
) -> None:
    open_scopes: set[tuple[UUID, str, str]] = set()
    persister = _Persister(open_scopes, "ci")
    executor = _TypedOutcomeExecutor(outcome)
    gate = AccountCommandGate(
        executor,
        bus=DomainEventBus(),
        persister=persister,
        uncertainty_reader=_UncertaintyReader(open_scopes),
        safety_evaluator=_SafetyEvaluator(),
        deployment_environment="ci",
        is_simulated=False,
        clock=iter(range(100, 200)).__next__,
        date_provider=lambda: date(2026, 9, 3),
    )

    await gate.submit(_ready(decision_id=f"redaction-{outcome.kind.value}"), _context())

    persisted = persister.events[-1]
    assert isinstance(persisted, event_type)
    assert _API_KEY not in persisted.reason
    assert _API_SECRET not in persisted.reason
    assert "authorization=[redacted]" in persisted.reason.lower()
    assert len(persisted.reason) <= 256
    assert executor.calls == 1
