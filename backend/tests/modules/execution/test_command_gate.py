"""Account command gate ordering and fail-closed fault contracts.

The gate always runs over a ``CommandBoundary``. The contract tests below use the
in-memory boundary of ``fake_boundary``, which records the journal transactions the gate
asked for (the authorised attempt, then its outcome).
"""
from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
from uuid import UUID, uuid4

import pytest

from bfx_funding_bot.external.bitfinex.credentials import HasCredentials
from bfx_funding_bot.external.bitfinex.order_port import OfferRequest
from bfx_funding_bot.external.bitfinex.submit_outcomes import (
    SubmitAcknowledged,
    SubmitNotSent,
    SubmitOutcome,
    SubmitOutcomeUnknown,
    SubmitRejected,
)
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
from bfx_funding_bot.modules.execution.middleware.reservation_emitting import (
    ReservationEmittingMiddleware,
)
from bfx_funding_bot.modules.execution.protocols import (
    AccountContext,
    Credentials,
)
from bfx_funding_bot.modules.ledger import Scope
from bfx_funding_bot.modules.strategy import DecisionOutcome, DecisionPayload
from tests.modules.execution.fake_boundary import (  # noqa: F401  (fixture)
    AttemptAuthorized,
    OutcomeRecorded,
    Record,
    Recording,
    boundary_stubs,
    label,
    with_capital,
)

ACCOUNT_ID = UUID("3f19d046-5030-494c-9a0a-9573bb890c1f")
ENVIRONMENT = "ci"
SYMBOL = "fUST"

pytestmark = pytest.mark.usefixtures("boundary_stubs")


def _ready(*, decision_id: str = "decision-1", symbol: str = SYMBOL) -> ReadyToSubmit:
    return with_capital(ReadyToSubmit(
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
        market_snapshot_id="snapshot-1",
        model_version=None,
        evidence={},
        safety=GuardResult(allowed=True, guard_name="test"),
    ))


def _context() -> AccountContext:
    return AccountContext(
        account_id=str(ACCOUNT_ID),
        credentials=Credentials(api_key="never-persist-key", api_secret="never-persist-secret"),
    )


@dataclass
class _FakeUncertaintyReader:
    open_scopes: set[tuple[UUID, str, str]]
    error: Exception | None = None

    async def has_open(self, session: object, scope: Scope, symbol: str) -> bool:
        if self.error is not None:
            raise self.error
        return (scope.exchange_account_id, scope.deployment_environment, symbol) in self.open_scopes

    async def list_open(self, session: object, scope: Scope, symbol: str | None = None) -> tuple[()]:
        return ()


class _FakeVenue:
    """The venue adapter behind the gate: records what it was handed, returns an outcome."""

    def __init__(self, outcome: SubmitOutcome | None = None, *, crash: bool = False) -> None:
        self.outcome: SubmitOutcome = outcome or SubmitAcknowledged("venue-1")
        self.crash = crash
        self.calls = 0
        self.offers: list[OfferRequest] = []
        self.pre_send: Callable[[], bool] | None = None
        self.txns_seen_at_call: list[tuple[Record, ...]] | None = None
        self.recording: Recording | None = None

    async def submit(
        self, offer: OfferRequest, account: HasCredentials, *,
        pre_send: Callable[[], bool] | None = None,
    ) -> SubmitOutcome:
        self.calls += 1
        self.offers.append(offer)
        self.pre_send = pre_send
        if self.recording is not None:
            self.txns_seen_at_call = list(self.recording.txns)
        if self.crash:
            raise RuntimeError("process crash after durable intent")
        return self.outcome

    async def cancel(self, *args: object, **kwargs: object) -> object:
        raise NotImplementedError("the submit contract tests never cancel")


@dataclass
class _FakeSafetyEvaluator:
    results: list[GuardResult]
    calls: int = 0

    async def evaluate(
        self, decision: DecisionPayload, context: AccountContext,
    ) -> GuardResult:
        del decision, context
        result = self.results[min(self.calls, len(self.results) - 1)]
        self.calls += 1
        return result


@dataclass
class _UncertaintyAwareEvaluator:
    """What the real chain's UncertaintyGuard does: an open scope refuses the command."""

    reader: _FakeUncertaintyReader

    async def evaluate(self, decision: DecisionPayload, context: AccountContext) -> GuardResult:
        if await self.reader.has_open(None, Scope(ACCOUNT_ID, ENVIRONMENT), decision.symbol):
            return GuardResult(False, "uncertainty", reason="open execution uncertainty")
        return GuardResult(True, "uncertainty")


class _BlockingAckVenue(_FakeVenue):
    def __init__(self) -> None:
        super().__init__(SubmitAcknowledged("venue-1"))
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def submit(
        self, offer: OfferRequest, account: HasCredentials, *,
        pre_send: Callable[[], bool] | None = None,
    ) -> SubmitOutcome:
        self.started.set()
        await self.release.wait()
        return await super().submit(offer, account, pre_send=pre_send)


def _recording(reader: _FakeUncertaintyReader) -> Recording:
    """A boundary whose committed UNKNOWN opens the reader's scope, as the real one does."""
    def opened(record: Record) -> None:
        if isinstance(record, OutcomeRecorded) and record.outcome.kind == "unknown":
            reader.open_scopes.add((ACCOUNT_ID, ENVIRONMENT, record.attempt.symbol))

    return Recording(environment=ENVIRONMENT, account_id=ACCOUNT_ID, on_record=opened)


def _gate(
    venue: _FakeVenue,
    reader: _FakeUncertaintyReader,
    recording: Recording,
    *,
    safety: object | None = None,
    clock_values: tuple[int, ...] = (100, 101, 102, 103, 104, 105, 106, 107),
) -> AccountCommandGate:
    venue.recording = recording
    return AccountCommandGate(
        venue,
        bus=DomainEventBus(),
        uncertainty_reader=reader,
        safety_evaluator=safety or _UncertaintyAwareEvaluator(reader),  # type: ignore[arg-type]
        deployment_environment=ENVIRONMENT,
        boundary=recording.boundary(),
        managed_offers=recording.offers,
        clock=iter(clock_values).__next__,
    )


# ---------------------------------------------------------------- construction


def test_a_gate_cannot_be_built_without_a_command_boundary() -> None:
    """There is no persister-only command path: the boundary is a required argument."""
    reader = _FakeUncertaintyReader(set())
    recording = _recording(reader)
    with pytest.raises(TypeError, match="boundary"):
        AccountCommandGate(  # type: ignore[call-arg]
            _FakeVenue(),
            bus=DomainEventBus(),
            uncertainty_reader=reader,
            safety_evaluator=_UncertaintyAwareEvaluator(reader),
            deployment_environment=ENVIRONMENT,
            managed_offers=recording.offers,
        )


def test_a_gate_cannot_be_built_without_managed_offer_reads() -> None:
    reader = _FakeUncertaintyReader(set())
    with pytest.raises(TypeError, match="managed_offers"):
        AccountCommandGate(  # type: ignore[call-arg]
            _FakeVenue(),
            bus=DomainEventBus(),
            uncertainty_reader=reader,
            safety_evaluator=_UncertaintyAwareEvaluator(reader),
            deployment_environment=ENVIRONMENT,
            boundary=_recording(reader).boundary(),
        )


def test_the_middleware_cannot_be_built_without_a_command_boundary() -> None:
    reader = _FakeUncertaintyReader(set())
    recording = _recording(reader)
    with pytest.raises(TypeError, match="boundary"):
        ReservationEmittingMiddleware(  # type: ignore[call-arg]
            _FakeVenue(),
            bus=DomainEventBus(),
            safety_evaluator=_UncertaintyAwareEvaluator(reader),
            uncertainty_reader=reader,
            managed_offers=recording.offers,
        )


@pytest.mark.asyncio
async def test_the_middleware_submits_through_its_command_gate() -> None:
    reader = _FakeUncertaintyReader(set())
    recording = _recording(reader)
    venue = _FakeVenue()
    middleware = ReservationEmittingMiddleware(
        venue,
        bus=DomainEventBus(),
        safety_evaluator=_UncertaintyAwareEvaluator(reader),
        boundary=recording.boundary(),
        uncertainty_reader=reader,
        managed_offers=recording.offers,
        clock=iter(range(100, 110)).__next__,
    )
    assert isinstance(middleware.command_gate, AccountCommandGate)
    await middleware.submit(_ready(), _context())
    assert venue.calls == 1
    assert recording.labels == ["authorized", "ack"]


# ------------------------------------------------------------- uncertainty scope


@pytest.mark.asyncio
async def test_open_uncertainty_blocks_before_intent_and_venue_call() -> None:
    reader = _FakeUncertaintyReader({(ACCOUNT_ID, ENVIRONMENT, SYMBOL)})
    recording = _recording(reader)
    venue = _FakeVenue()

    with pytest.raises(CommandGateBlocked, match="open execution uncertainty"):
        await _gate(venue, reader, recording).submit(_ready(), _context())

    assert venue.calls == 0
    assert recording.txns == []


@pytest.mark.asyncio
async def test_check_fails_closed_when_the_uncertainty_read_errors() -> None:
    reader = _FakeUncertaintyReader(set(), error=RuntimeError("database unavailable"))
    gate = _gate(_FakeVenue(), reader, _recording(reader))

    with pytest.raises(CommandGateBlocked, match="uncertainty guard unavailable"):
        await gate.check(_ready(), _context())


@pytest.mark.asyncio
async def test_check_scopes_uncertainty_to_the_symbol() -> None:
    reader = _FakeUncertaintyReader({(ACCOUNT_ID, ENVIRONMENT, "fUSD")})
    gate = _gate(_FakeVenue(), reader, _recording(reader))

    await gate.check(_ready(symbol="fUST"), _context())
    with pytest.raises(CommandGateBlocked, match="open execution uncertainty"):
        await gate.check(_ready(symbol="fUSD"), _context())


@pytest.mark.asyncio
async def test_uncertainty_scope_does_not_block_another_symbol() -> None:
    reader = _FakeUncertaintyReader({(ACCOUNT_ID, ENVIRONMENT, "fUSD")})
    recording = _recording(reader)
    venue = _FakeVenue(SubmitRejected("explicit_rejection"))

    await _gate(venue, reader, recording).submit(_ready(symbol="fUST"), _context())

    assert venue.calls == 1


# --------------------------------------------------------------------- ordering


@pytest.mark.asyncio
async def test_the_intent_is_durable_before_the_venue_sees_data() -> None:
    reader = _FakeUncertaintyReader(set())
    recording = _recording(reader)
    venue = _FakeVenue()

    await _gate(venue, reader, recording).submit(_ready(), _context())

    assert venue.txns_seen_at_call is not None
    assert [label(record) for txn in venue.txns_seen_at_call for record in txn] == ["authorized"]
    assert recording.labels == ["authorized", "ack"]


@pytest.mark.asyncio
async def test_unknown_keeps_the_venue_error_as_durable_evidence() -> None:
    """The log line ages out; the journal is where an unlisted refusal is read later."""
    reader = _FakeUncertaintyReader(set())
    recording = _recording(reader)
    venue = _FakeVenue(SubmitOutcomeUnknown(
        "http_5xx", True, venue_error_code=10001,
        venue_error_message="Invalid offer: never-persist-secret is not a rate",
    ))

    await _gate(venue, reader, recording).submit(_ready(), _context())

    [record] = recording.txns[1]
    assert isinstance(record, OutcomeRecorded)
    assert record.outcome.reason == "http_5xx"
    assert record.outcome.evidence == {
        "venue_error_code": 10001,
        "venue_error_message": "Invalid offer: [redacted] is not a rate",
    }


@pytest.mark.asyncio
async def test_unknown_without_a_venue_answer_records_no_venue_evidence() -> None:
    reader = _FakeUncertaintyReader(set())
    recording = _recording(reader)
    venue = _FakeVenue(SubmitOutcomeUnknown("timeout", True))

    await _gate(venue, reader, recording).submit(_ready(), _context())

    [record] = recording.txns[1]
    assert isinstance(record, OutcomeRecorded)
    assert record.outcome.evidence == {}


@pytest.mark.asyncio
async def test_unknown_is_durable_and_blocks_next_command_without_retry() -> None:
    reader = _FakeUncertaintyReader(set())
    recording = _recording(reader)
    venue = _FakeVenue(SubmitOutcomeUnknown("transport_timeout", True))
    gate = _gate(venue, reader, recording)

    result = await gate.submit(_ready(), _context())
    assert result.outcome_kind.value == "unknown"
    assert venue.calls == 1
    assert venue.txns_seen_at_call is not None
    assert [label(record) for record in venue.txns_seen_at_call[0]] == ["authorized"]
    assert [label(record) for record in recording.txns[1]] == ["unknown"]
    assert reader.open_scopes == {(ACCOUNT_ID, ENVIRONMENT, SYMBOL)}

    with pytest.raises(CommandGateBlocked, match="open execution uncertainty"):
        await gate.submit(_ready(decision_id="decision-2"), _context())
    assert venue.calls == 1


@pytest.mark.asyncio
async def test_next_submit_waits_until_unknown_block_commit() -> None:
    reader = _FakeUncertaintyReader(set())
    recording = _recording(reader)
    recording.unknown_started = asyncio.Event()
    recording.allow_unknown_commit = asyncio.Event()
    venue = _FakeVenue(SubmitOutcomeUnknown("response_lost", True))
    gate = _gate(venue, reader, recording)

    first = asyncio.create_task(gate.submit(_ready(), _context()))
    await recording.unknown_started.wait()
    second = asyncio.create_task(
        gate.submit(_ready(decision_id="decision-2"), _context())
    )
    await asyncio.sleep(0)
    assert venue.calls == 1

    recording.allow_unknown_commit.set()
    await first
    with pytest.raises(CommandGateBlocked, match="open execution uncertainty"):
        await second
    assert venue.calls == 1


@pytest.mark.asyncio
async def test_waiting_submit_rechecks_authoritative_safety_inside_account_lock() -> None:
    """Removing the locked evaluator would persist a second intent and call venue."""
    reader = _FakeUncertaintyReader(set())
    recording = _recording(reader)
    venue = _BlockingAckVenue()
    allow = GuardResult(allowed=True, guard_name="<chain>")
    safety = _FakeSafetyEvaluator(
        [
            allow,  # first submit, inside its admission transaction
            allow,  # first submit, again after commit
            GuardResult(
                allowed=False,
                guard_name="kill_switch",
                reason="halted while waiting for account lock",
            ),
        ]
    )
    gate = _gate(venue, reader, recording, safety=safety)

    first = asyncio.create_task(gate.submit(_ready(), _context()))
    await venue.started.wait()
    second = asyncio.create_task(
        gate.submit(_ready(decision_id="decision-2"), _context())
    )
    await asyncio.sleep(0)
    venue.release.set()
    await first

    with pytest.raises(CommandGateBlocked, match="halted while waiting"):
        await second
    assert venue.calls == 1
    assert safety.calls == 3
    assert len(recording.attempts) == 1


# ------------------------------------------------------------------ fault fencing


@pytest.mark.asyncio
async def test_one_attempt_per_decision_prevents_resubmit() -> None:
    reader = _FakeUncertaintyReader(set())
    recording = _recording(reader)
    venue = _FakeVenue(SubmitRejected("explicit_rejection"))
    gate = _gate(venue, reader, recording)
    ready = _ready()

    await gate.submit(ready, _context())
    with pytest.raises(ValueError, match="one submission attempt"):
        await gate.submit(ready, _context())

    assert venue.calls == 1
    assert len(recording.attempts) == 1


@pytest.mark.asyncio
async def test_crash_after_intent_leaves_the_attempt_open_without_retry() -> None:
    reader = _FakeUncertaintyReader(set())
    recording = _recording(reader)
    venue = _FakeVenue(crash=True)
    gate = _gate(venue, reader, recording)
    ready = _ready()

    with pytest.raises(SubmitOutcomeLostError, match="submit ended without durable outcome"):
        await gate.submit(ready, _context())

    (intent,) = recording.txns[0]
    assert isinstance(intent, AttemptAuthorized)
    assert intent.attempt.execution_decision_id == ready.decision_id
    assert recording.txns == [(intent,)]  # no outcome transaction
    assert recording.outcomes == []
    # No retry in this process: it exits (SubmitOutcomeLostError); the restarted daemon's
    # observation cycle finds the started attempt without an outcome and holds it UNKNOWN.
    assert venue.calls == 1


@pytest.mark.asyncio
async def test_intent_persistence_failure_blocks_venue_call() -> None:
    reader = _FakeUncertaintyReader(set())
    recording = _recording(reader)
    recording.fail_on_call = 1
    venue = _FakeVenue()

    with pytest.raises(RuntimeError, match="persistence failed"):
        await _gate(venue, reader, recording).submit(_ready(), _context())

    assert venue.calls == 0


@pytest.mark.asyncio
async def test_outcome_persistence_failure_ends_the_process() -> None:
    """Process fencing (lending envelope D3): no in-memory latch that only a
    restart clears -- the gate raises a BaseException no ``except Exception``
    keeps alive, and the restarted daemon quarantines the symbol from the
    durable PENDING intent."""
    reader = _FakeUncertaintyReader(set())
    recording = _recording(reader)
    recording.fail_on_call = 2
    venue = _FakeVenue()
    gate = _gate(venue, reader, recording)

    with pytest.raises(SubmitOutcomeLostError, match="outcome persistence failed") as lost:
        await gate.submit(_ready(), _context())
    assert not isinstance(lost.value, Exception)
    assert isinstance(lost.value.__cause__, RuntimeError)
    assert venue.calls == 1


@pytest.mark.asyncio
async def test_a_submit_that_raises_mid_transport_ends_the_process() -> None:
    reader = _FakeUncertaintyReader(set())
    recording = _recording(reader)
    venue = _FakeVenue()

    async def explode(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise RuntimeError("socket reset")

    venue.submit = explode  # type: ignore[method-assign]
    with pytest.raises(SubmitOutcomeLostError, match="submit ended without durable outcome"):
        await _gate(venue, reader, recording).submit(_ready(), _context())


@pytest.mark.asyncio
async def test_submit_cancelled_before_transport_closes_intent_as_not_sent() -> None:
    """The executor was cancelled while waiting for the per-key venue gate:
    nothing was sent, so the intent closes as NOT_SENT (no UNKNOWN quarantine
    for recovery to invent) and the cancellation still propagates."""
    from bfx_funding_bot.external.bitfinex.submit_outcomes import SubmitCancelledNotSent

    reader = _FakeUncertaintyReader(set())
    recording = _recording(reader)
    venue = _FakeVenue()

    async def cancelled_waiting(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise SubmitCancelledNotSent("cancelled_waiting_for_venue_gate")

    venue.submit = cancelled_waiting  # type: ignore[method-assign]
    with pytest.raises(asyncio.CancelledError):
        await _gate(venue, reader, recording).submit(_ready(), _context())

    (closed,) = recording.txns[1]
    assert isinstance(closed, OutcomeRecorded)
    assert (closed.outcome.kind, closed.outcome.reason) == ("not_sent", "local_pre_transport")
    assert [outcome.kind for _, outcome in recording.outcomes] == ["not_sent"]
    assert not reader.open_scopes


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("outcome", "kind", "reason"),
    [
        (SubmitNotSent("bad_local_input"), "not_sent", "local_pre_transport"),
        (SubmitRejected("venue_rejected"), "rejected", "venue_rejected"),
    ],
)
async def test_non_ack_outcomes_persist_one_terminal_outcome(outcome, kind, reason) -> None:
    reader = _FakeUncertaintyReader(set())
    recording = _recording(reader)
    venue = _FakeVenue(outcome)

    await _gate(venue, reader, recording).submit(_ready(), _context())

    (terminal,) = recording.txns[1]
    assert isinstance(terminal, OutcomeRecorded)
    assert (terminal.outcome.kind, terminal.outcome.reason) == (kind, reason)
    assert [(recorded.kind, recorded.reason) for _, recorded in recording.outcomes] == [
        (kind, reason)
    ]


@pytest.mark.asyncio
async def test_every_outcome_fact_is_marked_not_simulated() -> None:
    """The gate carries the constant False: no simulated command path remains."""
    reader = _FakeUncertaintyReader(set())
    recording = _recording(reader)

    await _gate(_FakeVenue(), reader, recording).submit(_ready(), _context())

    assert [facts.is_simulated for facts, _ in recording.outcomes] == [False]


# ------------------------------------------------- R4: the gate owns the venue request


@pytest.mark.asyncio
async def test_the_gate_hands_the_venue_an_offer_request_built_from_the_decision() -> None:
    reader = _FakeUncertaintyReader(set())
    recording = _recording(reader)
    venue = _FakeVenue()
    ready = _ready()

    await _gate(venue, reader, recording).submit(ready, _context())

    decision = ready.decision
    assert venue.offers == [OfferRequest(
        symbol=SYMBOL, amount=Decimal("12.5"), rate=Decimal("0.0001"), period_days=2,
        funding_amount_evidence=ready.funding_amount_evidence,
    )]
    assert (decision.offer_amount_usdt, decision.offer_rate) == (Decimal("12.5"), Decimal("0.0001"))


@pytest.mark.asyncio
async def test_the_venue_pre_send_is_the_decisions_book_validity_at_send_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``pre_send`` replaces ``AccountContext.before_submit_transport``: it is the
    decision's own book check, evaluated when the adapter calls it."""
    reader = _FakeUncertaintyReader(set())
    venue = _FakeVenue()
    gate = _gate(venue, reader, _recording(reader), clock_values=tuple(range(100, 120)))
    await gate.submit(_ready(), _context())
    assert venue.pre_send is not None

    seen: list[int] = []

    def book_valid_at(self: ReadyToSubmit, now_ms: int) -> bool:
        seen.append(now_ms)
        return False

    monkeypatch.setattr(ReadyToSubmit, "book_valid_at", book_valid_at)
    assert venue.pre_send() is False
    assert len(seen) == 1


@pytest.mark.asyncio
async def test_an_acknowledgement_binds_the_gates_own_reference_to_the_venue_offer() -> None:
    reader = _FakeUncertaintyReader(set())
    ready = _ready()

    result = await _gate(_FakeVenue(SubmitAcknowledged("42")), reader, _recording(reader)).submit(
        ready, _context())

    reference = ReservationRef(execution_decision_id=ready.decision_id,
                               signal_correlation_id=ready.decision.signal_correlation_id)
    assert result.venue_offer_id == "42"
    assert result.reservation_ref == reference.bind_venue_offer("42")


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", [
    SubmitOutcomeUnknown("http_5xx", True, http_status=500),
    SubmitRejected("venue_rejected"),
    SubmitNotSent("symbol_not_configured"),
], ids=["unknown", "rejected", "not_sent"])
async def test_any_other_outcome_keeps_the_gates_reference_unbound(outcome: SubmitOutcome) -> None:
    reader = _FakeUncertaintyReader(set())
    ready = _ready()

    result = await _gate(_FakeVenue(outcome), reader, _recording(reader)).submit(ready, _context())

    assert result.outcome is outcome
    assert result.venue_offer_id is None
    assert result.reservation_ref == ReservationRef(
        execution_decision_id=ready.decision_id,
        signal_correlation_id=ready.decision.signal_correlation_id,
    )


def _account_goes_inactive_after_commit(monkeypatch: pytest.MonkeyPatch) -> None:
    from tests.modules.execution import fake_boundary

    answers = iter(["active", "disabled"])

    async def scalar(self: object, query: object) -> str:
        return next(answers)

    monkeypatch.setattr(fake_boundary._Session, "scalar", scalar)


@pytest.mark.asyncio
@pytest.mark.parametrize(("path", "reason"), [
    ("halted_after_commit", "halted after commit"),
    ("account_inactive_after_commit", "account_inactive"),
    ("book_expired", "decision_book_invalid_or_expired"),
    ("amount_invalid", "amount below the funding minimum"),
    ("rate_not_positive", "invalid_submit_payload"),
    ("period_not_positive", "invalid_submit_payload"),
])
async def test_every_gate_not_sent_path_never_calls_the_venue(
    path: str, reason: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """(a1): a refusal after the intent committed closes it NOT_SENT and the venue
    adapter is never called (so no HTTP can have been sent)."""
    from bfx_funding_bot.modules.execution import command_gate

    reader = _FakeUncertaintyReader(set())
    recording = _recording(reader)
    venue = _FakeVenue()
    allow = GuardResult(allowed=True, guard_name="<chain>")
    safety: object | None = None
    ready = _ready()
    if path == "halted_after_commit":
        safety = _FakeSafetyEvaluator([allow, GuardResult(False, "kill_switch",
                                                          reason="halted after commit")])
    elif path == "account_inactive_after_commit":
        _account_goes_inactive_after_commit(monkeypatch)
    elif path == "book_expired":
        monkeypatch.setattr(ReadyToSubmit, "book_valid_at", lambda self, now_ms: False)
    elif path == "amount_invalid":
        def refuse(*args: object, **kwargs: object) -> None:
            raise ValueError("amount below the funding minimum")
        monkeypatch.setattr(command_gate, "validate_amount", refuse)
    else:
        field = "offer_rate" if path == "rate_not_positive" else "offer_duration_days"
        decision = ready.decision.model_copy(update={field: Decimal("0") if field == "offer_rate"
                                                     else 0})
        ready = with_capital(ReadyToSubmit(
            decision=decision, decision_id="decision-bad", policy=ready.policy,
            market_snapshot_id=ready.market_snapshot_id, model_version=None,
            evidence={}, safety=ready.safety,
        ))

    result = await _gate(venue, reader, recording, safety=safety).submit(ready, _context())

    assert venue.calls == 0
    assert isinstance(result.outcome, SubmitNotSent)
    assert result.outcome.reason == reason
    assert recording.labels == ["authorized", "not_sent"]
    assert not reader.open_scopes


@pytest.mark.asyncio
async def test_a_decision_without_a_symbol_is_refused_before_any_intent() -> None:
    """(a1) symbol missing: refused even earlier than the post-commit NOT_SENT paths --
    the attempt payload cannot be built, so nothing is journaled and the venue is
    never called (the gate's own ``symbol_missing`` check is the second line)."""
    reader = _FakeUncertaintyReader(set())
    recording = _recording(reader)
    venue = _FakeVenue()

    with pytest.raises(ValueError, match="symbol must be non-empty"):
        await _gate(venue, reader, recording).submit(_ready(symbol=""), _context())

    assert venue.calls == 0
    assert recording.txns == []
