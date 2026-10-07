"""Deterministic simulated submit-fault matrix using the real HTTP adapter.

The fake transport is deliberately test-local: production keeps using an
ordinary injected ``httpx.AsyncClient`` and has no fault-mode branches.
The command boundary and uncertainty fixtures are in-memory doubles
(``fake_boundary``). Actual policy-backed SQLite/PG command faults live in
test_capital_command_boundary.
"""
from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass
from decimal import Decimal
from hashlib import sha256
from typing import Any, Literal
from uuid import UUID, uuid4

import httpx
import pytest

from bfx_funding_bot.core.telemetry import Phase
from bfx_funding_bot.external.bitfinex.live_executor import BitfinexLiveExecutor
from bfx_funding_bot.external.bitfinex.nonce import AuthRequestGate
from bfx_funding_bot.modules.execution.bus import DomainEventBus
from bfx_funding_bot.modules.execution.command_gate import (
    AccountCommandGate,
    CommandGateBlocked,
    SubmitOutcomeLostError,
)
from bfx_funding_bot.modules.execution.contracts import (
    ExecutionPolicy,
    GuardResult,
    ReadyToSubmit,
    ReservationRef,
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
from bfx_funding_bot.modules.ledger import (
    Coverage,
    MatchEvidence,
    Offer,
    Scope,
    UnknownTerms,
    match_unknown,
)
from bfx_funding_bot.modules.strategy import DecisionOutcome, DecisionPayload, StrategyName
from tests.modules.execution.fake_boundary import (  # noqa: F401  (fixture)
    AttemptAuthorized,
    OutcomeRecorded,
    Record,
    Recording,
    boundary_stubs,
    with_capital,
)

pytestmark = pytest.mark.usefixtures("boundary_stubs")

_ACCOUNT_ID = UUID("3f19d046-5030-494c-9a0a-9573bb890c1f")
_ADJACENT_ACCOUNT_ID = UUID("28b31e79-83ce-4b32-a6b7-03d78043ce68")
_API_KEY = "fault-matrix-api-key"
_API_SECRET = "fault-matrix-api-secret"


@dataclass(frozen=True, slots=True)
class FaultScenario:
    """One immutable submit/process fault configuration in the Halt 2 matrix."""

    name: str
    response_mode: Literal[
        "drop", "timeout", "malformed", "malformed_after_side_effect",
        "5xx", "success", "crash",
    ]
    transport_started: bool
    side_effect_visible: bool
    process_crash_at: Literal["after_intent", "during_send"] | None = None


class InjectedProcessCrash(BaseException):
    """Test-only hard process boundary; production has no fault-mode branch."""


ACCEPT_DROP = FaultScenario("accept_drop", "drop", True, True)
REJECT_DROP = FaultScenario("reject_drop", "drop", True, False)
TIMEOUT_RESET = FaultScenario("timeout_reset", "timeout", True, False)
MALFORMED = FaultScenario("malformed", "malformed", True, False)
FIVE_XX_AFTER_SIDE_EFFECT = FaultScenario("5xx_after_side_effect", "5xx", True, True)
MALFORMED_AFTER_SIDE_EFFECT = FaultScenario(
    "malformed_after_side_effect", "malformed_after_side_effect", True, True,
)
CRASH_AFTER_INTENT = FaultScenario("crash_after_intent", "crash", False, False, "after_intent")
CRASH_AFTER_SIDE_EFFECT = FaultScenario(
    "crash_after_side_effect", "crash", True, True, "during_send",
)

FAULT_SCENARIOS = (
    ACCEPT_DROP,
    REJECT_DROP,
    TIMEOUT_RESET,
    MALFORMED,
    MALFORMED_AFTER_SIDE_EFFECT,
    FIVE_XX_AFTER_SIDE_EFFECT,
    CRASH_AFTER_INTENT,
    CRASH_AFTER_SIDE_EFFECT,
)


@dataclass(frozen=True, slots=True)
class FaultEvidence:
    durable_intent_count: int
    transport_request_count: int
    outcome_kind: str
    uncertainty_state: str
    journal_labels: tuple[str, ...]
    venue_object_count: int
    retry_count: int
    normalized_payload_hash: str | None
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
        if self._scenario.response_mode == "malformed_after_side_effect":
            return httpx.Response(200, content=b"not-json", request=request)
        if self._scenario.response_mode == "5xx":
            return httpx.Response(
                500,
                text=f"venue failure echoed {_API_SECRET}",
                request=request,
            )
        if self._scenario.response_mode == "success":
            return httpx.Response(200, json=_success_response(), request=request)
        if self._scenario.response_mode == "crash":
            raise InjectedProcessCrash("injected process crash during venue send")
        raise AssertionError(f"unsupported transport mode: {self._scenario.response_mode}")


class _EventSink:
    async def emit(self, event: dict[str, Any]) -> None:
        del event


def _recording(open_scopes: set[tuple[UUID, str, str]], environment: str) -> Recording:
    """A boundary whose committed UNKNOWN opens the symbol's uncertainty scope."""
    def opened(record: Record) -> None:
        if isinstance(record, OutcomeRecorded) and record.outcome.kind == "unknown":
            open_scopes.add((record.scope.exchange_account_id, environment, record.attempt.symbol))

    return Recording(environment=environment, account_id=_ACCOUNT_ID, on_record=opened)


def _authorized(recording: Recording) -> list[AttemptAuthorized]:
    return [record for record in recording.records if isinstance(record, AttemptAuthorized)]


def _unknowns(recording: Recording) -> list[OutcomeRecorded]:
    return [
        record for record in recording.records
        if isinstance(record, OutcomeRecorded) and record.outcome.kind == "unknown"
    ]


class _UncertaintyReader:
    def __init__(self, open_scopes: set[tuple[UUID, str, str]]) -> None:
        self._open_scopes = open_scopes
        self.calls: list[tuple[UUID, str, str]] = []

    async def has_open(self, session: object, scope: Scope, symbol: str) -> bool:
        key = (scope.exchange_account_id, scope.deployment_environment, symbol)
        self.calls.append(key)
        return key in self._open_scopes

    async def list_open(self, session: object, scope: Scope, symbol: str | None = None) -> tuple[()]:
        return ()


class _SafetyEvaluator:
    """The chain's UncertaintyGuard: an open scope for this account and symbol refuses."""

    def __init__(self, reader: _UncertaintyReader, environment: str) -> None:
        self._reader = reader
        self._environment = environment

    async def evaluate(self, decision: DecisionPayload, context: AccountContext) -> GuardResult:
        scope = Scope(UUID(context.account_id), self._environment)
        if await self._reader.has_open(None, scope, decision.symbol):
            return GuardResult(False, "uncertainty", reason="open execution uncertainty")
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
        reservation_ref: ReservationRef,
    ) -> SubmittedOrder:
        del ready, context
        self.calls += 1
        return SubmittedOrder(
            venue_offer_id=None,
            outcome=self.outcome,
            reservation_ref=reservation_ref,
        )


def _ready(*, decision_id: str, symbol: str = "fUST") -> ReadyToSubmit:
    from tests.external.bitfinex.test_funding_rules import evidence
    return with_capital(ReadyToSubmit(
        decision=DecisionPayload(
            decision_outcome=DecisionOutcome.POST,
            signal_correlation_id=uuid4(),
            offer_rate=0.0001,
            offer_amount_usdt=150.0,
            offer_duration_days=2,
            symbol=symbol,
        ),
        decision_id=decision_id,
        policy=ExecutionPolicy.BOOK_GUARDED,
        market_snapshot_id="fault-snapshot",
        model_version=None,
        evidence={},
        safety=GuardResult(allowed=True, guard_name="fault_matrix"),
        funding_amount_evidence=evidence(symbol=symbol, now=int(time.time() * 1000)),
    ))


def _context(account_id: UUID = _ACCOUNT_ID) -> AccountContext:
    return AccountContext(
        account_id=str(account_id),
        credentials=Credentials(api_key=_API_KEY, api_secret=_API_SECRET),
        allocation_cap_usdt=Decimal("1000"),
    )


def _success_response() -> list[object]:
    return [
        1716383500000,
        "fon-req",
        None,
        None,
        [42, "fUST", 0, 0, 150.0, 0, "REQ", None, None, 0, "ACTIVE", None, None, None, 0.0001, 2],
        None,
        "SUCCESS",
        "Submitting",
    ]


def _payload_digest(payload: dict[str, Any]) -> str:
    return sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _matching_offer(venue_offer_id: str, terms: UnknownTerms) -> Offer:
    """An active venue offer carrying exactly the UNKNOWN attempt's terms."""
    return Offer(
        venue_offer_id, terms.symbol, terms.amount, terms.amount, terms.rate, True,
        terms.period_days, terms.offer_type, terms.flags, "active",
        terms.started_at_ms, terms.started_at_ms,
    )


def _complete_evidence(terms: UnknownTerms, offers: tuple[Offer, ...]) -> MatchEvidence:
    """One observation, begun after the UNKNOWN, whose offer history covers the submit."""
    observed_at = terms.unknown_recorded_at_ms + 1
    return MatchEvidence(
        UUID("33333333-3333-3333-3333-333333333333"), observed_at, observed_at + 1,
        Coverage(
            wallets_complete=True, offers_complete=True, credits_complete=True,
            loans_complete=True, offer_history_complete=True, credit_history_complete=True,
            wallet_pages=1, offer_pages=1, credit_pages=1, loan_pages=1,
            offer_history_pages=1, credit_history_pages=1,
            history_requested_start_ms=terms.started_at_ms,
            history_requested_end_ms=observed_at + 1,
            history_oldest_mts_created=terms.started_at_ms,
            history_newest_mts_created=terms.started_at_ms,
            trades_complete=True, history_symbols=frozenset({terms.symbol}),
        ),
        offers, (),
    )


async def run_multiple_candidate_reconcile() -> MultipleCandidateEvidence:
    """Run UNKNOWN persistence, two-candidate reconcile, then the blocked retry."""
    environment = "ci"
    open_scopes: set[tuple[UUID, str, str]] = set()
    recording = _recording(open_scopes, environment)
    reader = _UncertaintyReader(open_scopes)
    transport = FakeBitfinexTransport(ACCEPT_DROP)
    async with httpx.AsyncClient(transport=transport) as http:
        executor = BitfinexLiveExecutor(
            http=http,
            event_sink=_EventSink(),
            bus=DomainEventBus(),
            phase=Phase.SHADOW,
            strategy=StrategyName.RATE_PERCENTILE,
            configured_symbols=frozenset({"fUST"}),
            cell="fault-cell",
            auth_gate=AuthRequestGate(lambda: 1),
        )
        gate = AccountCommandGate(
            executor,
            uncertainty_reader=reader,
            safety_evaluator=_SafetyEvaluator(reader, environment),
            deployment_environment=environment,
            boundary=recording.boundary(),
            managed_offers=recording.offers,
            clock=iter(range(100, 200)).__next__,
        )
        ready = _ready(decision_id="multiple-candidate")
        result = await gate.submit(ready, _context())
        assert result.outcome_kind is SubmitOutcomeKind.UNKNOWN
        (unknown,) = _unknowns(recording)
        attempt = unknown.attempt
        payload = attempt.normalized_payload
        terms = UnknownTerms(
            attempt.attempt_id, attempt.symbol,
            Decimal(str(payload["amount"])), Decimal(str(payload["rate"])),
            int(payload["period"]), str(payload["type"]), payload["flags"],
            attempt.started_at_ms, unknown.outcome.completed_at_ms,
        )
        candidates = {
            offer.venue_offer_id: offer
            for offer in (_matching_offer("venue-a", terms), _matching_offer("venue-b", terms))
        }
        matched = match_unknown(terms, _complete_evidence(terms, tuple(candidates.values())))
        request_count_before_retry = transport.request_count
        with pytest.raises(CommandGateBlocked, match="open execution uncertainty"):
            await gate.submit(_ready(decision_id="multiple-candidate-retry"), _context())
        retry_count = transport.request_count - request_count_before_retry
        return MultipleCandidateEvidence(
            durable_intent_count=len(_authorized(recording)),
            transport_request_count=transport.request_count,
            retry_count=retry_count,
            match_kind=matched.kind,
            candidate_ids=matched.candidate_venue_offer_ids,
            unknown_exposure_usdt=attempt.amount,
            candidate_exposure_usdt=sum(
                candidates[venue_offer_id].amount_remaining
                for venue_offer_id in matched.candidate_venue_offer_ids
            ),
            unknown_scope=(unknown.scope.exchange_account_id, environment, attempt.symbol),
        )


async def run_fault_scenario(scenario: FaultScenario) -> FaultEvidence:
    """Run one real executor/gate submit without any automatic retry path."""
    open_scopes: set[tuple[UUID, str, str]] = set()
    environment = "ci"
    recording = _recording(open_scopes, environment)
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
                phase=Phase.SHADOW,
                strategy=StrategyName.RATE_PERCENTILE,
                configured_symbols=frozenset({"fUST"}),
                cell="fault-cell",
                auth_gate=AuthRequestGate(lambda: 1),
            )
        gate = AccountCommandGate(
            inner,  # type: ignore[arg-type]
            uncertainty_reader=uncertainty_reader,
            safety_evaluator=_SafetyEvaluator(uncertainty_reader, "ci"),
            deployment_environment="ci",
            boundary=recording.boundary(),
            managed_offers=recording.offers,
            clock=iter(range(100, 200)).__next__,
        )
        outcome_kind = "crashed"
        ready = _ready(decision_id=f"fault-{scenario.name}")
        context = _context()
        try:
            result = await gate.submit(ready, context)
            outcome_kind = result.outcome_kind.value
            # Full raw response is never valid evidence, even if a venue echoes a secret.
            assert _API_SECRET not in str(result.raw_response)
        except BaseException as exc:
            assert scenario.process_crash_at in {"after_intent", "during_send"}
            # The gate turns it into process fencing; the crash is the cause.
            assert isinstance(exc, SubmitOutcomeLostError)
            assert "process crash" in str(exc.__cause__)

        transport_count_before_retry_check = transport.request_count
        # A crashed submit ends the process, so there is no in-process retry to
        # refuse; recovery on restart opens the UNKNOWN (asserted below).
        if outcome_kind not in {SubmitOutcomeKind.ACKNOWLEDGED.value, "crashed"}:
            try:
                await gate.submit(_ready(decision_id=f"fault-{scenario.name}-retry"), _context())
            except CommandGateBlocked:
                pass
            else:
                raise AssertionError("ambiguous submit was allowed to retry")

        retry_count = transport.request_count - transport_count_before_retry_check
        unknowns = _unknowns(recording)
        unknown = unknowns[0] if unknowns else None
        if unknown is not None:
            assert unknown.attempt.amount == Decimal("150")
            assert unknown.scope.exchange_account_id == _ACCOUNT_ID
            assert unknown.attempt.symbol == "fUST"
            assert (_ACCOUNT_ID, environment, "fUST") in open_scopes
            await gate.check(_ready(decision_id=f"fault-{scenario.name}-other-symbol", symbol="fUSD"), context)
            await gate.check(_ready(decision_id=f"fault-{scenario.name}-other-account"), _context(_ADJACENT_ACCOUNT_ID))
            other_environment_gate = AccountCommandGate(
                inner,  # type: ignore[arg-type]
                uncertainty_reader=uncertainty_reader,
                safety_evaluator=_SafetyEvaluator(uncertainty_reader, "staging"),
                deployment_environment="staging",
                boundary=_recording(open_scopes, "staging").boundary(),
                managed_offers=recording.offers,
                clock=iter(range(300, 400)).__next__,
            )
            await other_environment_gate.check(
                _ready(decision_id=f"fault-{scenario.name}-other-environment"),
                context,
            )
            adjacent_scope_allowed = True
        else:
            adjacent_scope_allowed = False
        uncertainty_state = "open" if open_scopes else "pending_recovery" if outcome_kind == "crashed" else "closed"
        authorized = _authorized(recording)
        return FaultEvidence(
            durable_intent_count=len(authorized),
            transport_request_count=transport.request_count,
            outcome_kind=outcome_kind,
            uncertainty_state=uncertainty_state,
            journal_labels=tuple(recording.labels),
            venue_object_count=len(transport.venue_objects),
            retry_count=retry_count,
            normalized_payload_hash=(
                transport.normalized_payload_hash
                or _payload_digest(authorized[0].attempt.normalized_payload)
            ),
            unknown_exposure_usdt=(unknown.attempt.amount if unknown is not None else None),
            unknown_scope=(
                (unknown.scope.exchange_account_id, environment, unknown.attempt.symbol)
                if unknown is not None
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
    if scenario.process_crash_at in {"after_intent", "during_send"}:
        assert evidence.outcome_kind == "crashed"
        assert evidence.uncertainty_state == "pending_recovery"
        assert evidence.journal_labels == ("authorized",)
    else:
        assert evidence.outcome_kind == SubmitOutcomeKind.UNKNOWN.value
        assert evidence.uncertainty_state == "open"
        assert evidence.journal_labels == ("authorized", "unknown")
        assert evidence.unknown_exposure_usdt == Decimal("150")
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
    assert evidence.unknown_exposure_usdt == Decimal("150")
    assert evidence.candidate_exposure_usdt == Decimal("300")
    assert evidence.unknown_scope == (_ACCOUNT_ID, "ci", "fUST")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("outcome", "kind"),
    [
        (SubmitOutcomeUnknown(f"{_API_KEY} Authorization: Bearer {_API_SECRET}", True), "unknown"),
        (SubmitRejected(f"{_API_KEY} Authorization: Bearer {_API_SECRET}"), "rejected"),
    ],
)
async def test_durable_outcome_reason_redacts_credentials_and_authorization(
    outcome: SubmitOutcomeUnknown | SubmitRejected,
    kind: str,
) -> None:
    open_scopes: set[tuple[UUID, str, str]] = set()
    recording = _recording(open_scopes, "ci")
    executor = _TypedOutcomeExecutor(outcome)
    reader = _UncertaintyReader(open_scopes)
    gate = AccountCommandGate(
        executor,
        uncertainty_reader=reader,
        safety_evaluator=_SafetyEvaluator(reader, "ci"),
        deployment_environment="ci",
        boundary=recording.boundary(),
        managed_offers=recording.offers,
        clock=iter(range(100, 200)).__next__,
    )

    await gate.submit(_ready(decision_id=f"redaction-{outcome.kind.value}"), _context())

    persisted = recording.records[-1]
    assert isinstance(persisted, OutcomeRecorded)
    assert persisted.outcome.kind == kind
    reason = persisted.outcome.reason
    assert reason is not None
    assert _API_KEY not in reason
    assert _API_SECRET not in reason
    assert "authorization=[redacted]" in reason.lower()
    assert len(reason) <= 256
    assert executor.calls == 1
