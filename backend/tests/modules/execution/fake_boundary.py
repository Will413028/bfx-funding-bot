"""An in-memory ``CommandBoundary`` for command-gate contract tests.

Every command gate has a ``CommandBoundary`` (the persister-only path is gone), so
gate tests that do not need a database build one from these doubles. They record what
the gate asked the boundary to do, as ordered transactions, so a test can say "the
intent was durable before the venue saw data" or "an UNKNOWN opened the symbol's
uncertainty before the next command ran".

This is a test double, not an authority: what the ledger journal writes is covered by
``tests/integration/contracts/test_command_gate_ledger.py`` and
``tests/integration/test_capital_command_boundary.py``. The recording names outcomes with
the domain events (``ReservationClaimed`` and friends) only as a readable trace.
"""
from __future__ import annotations

import asyncio
from collections.abc import Callable
from contextlib import asynccontextmanager
from dataclasses import replace
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest

from bfx_funding_bot.external.bitfinex.cid import generate_cid
from bfx_funding_bot.modules.execution import command_gate
from bfx_funding_bot.modules.execution.audit.tables import ExecutionDecisionRow
from bfx_funding_bot.modules.execution.command_boundary import (
    CommandBoundary,
    CommandFacts,
)
from bfx_funding_bot.modules.execution.contracts import ReadyToSubmit, ReservationRef
from bfx_funding_bot.modules.execution.events import (
    OrderFilled,
    ReservationClaimed,
    ReservationFailed,
    ReservationIntent,
    ReservationUnknown,
)
from bfx_funding_bot.modules.execution.submit_outcomes import SubmissionAttemptPayload
from bfx_funding_bot.modules.ledger import (
    Authorized,
    CommandAttempt,
    CommandOutcome,
    LockedCommandGuard,
    Scope,
)

_READIES: dict[str, ReadyToSubmit] = {}


def with_capital(ready: ReadyToSubmit) -> ReadyToSubmit:
    """Attach the capital view a boundary gate demands and remember the ready."""
    view = SimpleNamespace(
        applied=SimpleNamespace(revision=1, digest="digest", revision_id=uuid4()),
        basis_token="1",
    )
    ready = replace(ready, capital_view=view)  # type: ignore[arg-type]
    _READIES[ready.decision_id] = ready
    return ready


@pytest.fixture
def boundary_stubs(monkeypatch: pytest.MonkeyPatch) -> None:
    """Isolate the gate from market evidence checks and the amount fingerprint.

    Those have their own tests (funding rules, managed offers); the contracts tested
    here are ordering, fencing and outcome recording.
    """
    monkeypatch.setattr(command_gate, "validate_amount", lambda *args, **kwargs: None)
    monkeypatch.setattr(command_gate, "fingerprint_of", lambda amount: 1)
    monkeypatch.setattr(ReadyToSubmit, "book_valid_at", lambda self, now_ms: True)


class _Session:
    def __init__(self, sessions: _Sessions) -> None:
        self._sessions = sessions

    async def get(self, table: object, key: str, **kwargs: object) -> object:
        assert table is ExecutionDecisionRow
        ready = _READIES[key]
        decision = ready.decision
        return SimpleNamespace(
            outcome="ready", applied_rate=decision.offer_rate,
            duration_days=decision.offer_duration_days, cell_id="cell",
            signal_correlation_id=str(decision.signal_correlation_id),
        )

    async def scalar(self, query: object) -> str:
        return "active"


class _Sessions:
    """Session factory whose sessions answer the gate's two reads (decision, account)."""

    @asynccontextmanager
    async def begin(self) -> Any:
        yield _Session(self)

    @asynccontextmanager
    async def __call__(self) -> Any:
        yield _Session(self)


class _Offers:
    async def fingerprints_in_use(self, session: object, scope: Scope, symbol: str) -> frozenset[int]:
        return frozenset()


class Recording:
    """What the gate made durable, as the ordered transactions it asked for."""

    def __init__(self, *, environment: str, account_id: UUID,
                 on_event: Callable[[object], None] | None = None) -> None:
        self.scope = Scope(account_id, environment)
        self.txns: list[tuple[object, ...]] = []
        self.attempts: dict[str, CommandAttempt] = {}
        self.outcomes: list[tuple[CommandFacts, CommandOutcome]] = []
        self.fail_on_call: int | None = None
        self.unknown_started: asyncio.Event | None = None
        self.allow_unknown_commit: asyncio.Event | None = None
        self._on_event = on_event
        self.offers = _Offers()

    @property
    def events(self) -> list[object]:
        return [event for txn in self.txns for event in txn]

    def _commit(self, *events: object) -> None:
        call_number = len(self.txns) + 1
        if self.fail_on_call == call_number:
            raise RuntimeError(f"persistence failed on call {call_number}")
        self.txns.append(events)
        if self._on_event is not None:
            for event in events:
                self._on_event(event)

    def boundary(self) -> CommandBoundary:
        return CommandBoundary(
            self.scope, _Sessions(),  # type: ignore[arg-type]
            _Journal(self), _Effects(self),
        )


class _Journal:
    def __init__(self, recording: Recording) -> None:
        self._r = recording

    async def authorize(
        self, session: object, scope: Scope, attempt: CommandAttempt, basis_token: str,
        *, now_ms: int, locked_guard: LockedCommandGuard,
    ) -> Authorized:
        r = self._r
        await locked_guard(session)  # type: ignore[arg-type]
        if attempt.execution_decision_id in r.attempts:
            raise ValueError("one submission attempt per execution decision")
        r.attempts[attempt.execution_decision_id] = attempt
        ready = _READIES[attempt.execution_decision_id]
        decision = ready.decision
        assert attempt.command_date is not None
        cid = generate_cid(decision.signal_correlation_id, attempt.command_date)
        r._commit(ReservationIntent(
            cid=cid, size_usdt=attempt.amount,
            signal_correlation_id=decision.signal_correlation_id,
            account_id=str(scope.exchange_account_id), is_simulated=False,
            occurred_at_ms=attempt.started_at_ms, symbol=attempt.symbol,
            execution_decision_id=attempt.execution_decision_id,
            reservation_ref=ReservationRef(
                attempt.execution_decision_id, cid, decision.signal_correlation_id),
            submission_attempt=SubmissionAttemptPayload(
                attempt_id=attempt.attempt_id,
                execution_decision_id=attempt.execution_decision_id,
                account_id=scope.exchange_account_id,
                environment=scope.deployment_environment, symbol=attempt.symbol, cid=cid,
                normalized_payload=attempt.normalized_payload,
                started_at_ms=attempt.started_at_ms,
            ),
        ))
        return Authorized(attempt.attempt_id, 1, "digest")

    async def record_outcome(self, scope: Scope, attempt_id: UUID, outcome: CommandOutcome) -> None:
        return None

    async def read_back_outcome(self, scope: Scope, attempt_id: UUID) -> CommandOutcome | None:
        return None

    async def admit_cancel(self, *args: object, **kwargs: object) -> object:
        raise NotImplementedError("the gate contract tests only submit")


def _fields(facts: CommandFacts, outcome: CommandOutcome) -> dict[str, object]:
    return {
        "cid": facts.reference.cid, "size_usdt": facts.amount,
        "signal_correlation_id": facts.signal_correlation_id,
        "account_id": str(facts.scope.exchange_account_id),
        "is_simulated": facts.is_simulated, "occurred_at_ms": outcome.completed_at_ms,
        "symbol": facts.symbol, "reservation_ref": facts.reference,
    }


def terminal_event(
    facts: CommandFacts, outcome: CommandOutcome,
) -> ReservationClaimed | ReservationFailed | ReservationUnknown:
    """The recording's own name for a terminal outcome (a readable trace, not a journal)."""
    fields = _fields(facts, outcome)
    if outcome.event_id is not None:
        fields["event_id"] = outcome.event_id
    if outcome.kind == "ack":
        return ReservationClaimed(**fields, venue_offer_id=outcome.venue_offer_id or "")  # type: ignore[arg-type]
    if outcome.kind == "unknown":
        return ReservationUnknown(**fields, reason=outcome.reason or "submit_outcome_unknown")  # type: ignore[arg-type]
    return ReservationFailed(**fields, reason=outcome.reason or "submit_rejected")  # type: ignore[arg-type]


def _filled_event(facts: CommandFacts, outcome: CommandOutcome) -> OrderFilled:
    return OrderFilled(
        **_fields(facts, outcome),  # type: ignore[arg-type]
        venue_offer_id=outcome.venue_offer_id or "", credit_id=None,
        fill_rate=float(facts.offer_rate or 0),
    )


class _Effects:
    def __init__(self, recording: Recording) -> None:
        self._r = recording

    def new_event_id(self) -> UUID | None:
        return None

    async def outcome_recorded(self, facts: CommandFacts, outcome: CommandOutcome) -> None:
        r = self._r
        r.outcomes.append((facts, outcome))
        event = terminal_event(facts, outcome)
        if isinstance(event, ReservationUnknown):
            if r.unknown_started is not None:
                r.unknown_started.set()
            if r.allow_unknown_commit is not None:
                await r.allow_unknown_commit.wait()
            r._commit(event)
            return
        filled: OrderFilled | None = None
        if isinstance(event, ReservationClaimed) and facts.filled:
            filled = _filled_event(facts, outcome)
        if filled is None:
            r._commit(event)
        else:
            r._commit(event, filled)
