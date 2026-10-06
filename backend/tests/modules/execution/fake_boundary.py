"""An in-memory ``CommandBoundary`` for command-gate contract tests.

Every command gate has a ``CommandBoundary`` (the persister-only path is gone), so
gate tests that do not need a database build one from these doubles. They record what
the gate asked the boundary to do, as ordered transactions, so a test can say "the
intent was durable before the venue saw data" or "an UNKNOWN opened the symbol's
uncertainty before the next command ran".

This is a test double, not an authority: what the ledger journal writes is covered by
``tests/integration/contracts/test_command_gate_ledger.py`` and
``tests/integration/test_capital_command_boundary.py``. The recording keeps exactly what
the gate handed the journal: the authorised ``CommandAttempt`` (``AttemptAuthorized``) and
the attempt's ``CommandOutcome`` (``OutcomeRecorded``).
"""
from __future__ import annotations

import asyncio
from collections.abc import Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest

from bfx_funding_bot.modules.execution import command_gate
from bfx_funding_bot.modules.execution.audit.tables import ExecutionDecisionRow
from bfx_funding_bot.modules.execution.command_boundary import (
    CommandBoundary,
    CommandFacts,
)
from bfx_funding_bot.modules.execution.contracts import ReadyToSubmit
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


@dataclass(frozen=True, slots=True)
class AttemptAuthorized:
    """The admission transaction: the attempt the journal authorised before any venue call."""

    scope: Scope
    attempt: CommandAttempt


@dataclass(frozen=True, slots=True)
class OutcomeRecorded:
    """The outcome transaction: the attempt's durable outcome, as the gate handed it over."""

    scope: Scope
    attempt: CommandAttempt
    outcome: CommandOutcome


Record = AttemptAuthorized | OutcomeRecorded


def label(record: Record) -> str:
    """``authorized`` for an admission, else the outcome kind (ack / unknown / rejected / not_sent)."""
    return "authorized" if isinstance(record, AttemptAuthorized) else record.outcome.kind


class Recording:
    """What the gate made durable, as the ordered journal transactions it asked for."""

    def __init__(self, *, environment: str, account_id: UUID,
                 on_record: Callable[[Record], None] | None = None) -> None:
        self.scope = Scope(account_id, environment)
        self.txns: list[tuple[Record, ...]] = []
        self.attempts: dict[str, CommandAttempt] = {}
        # What the effects were told after each outcome committed.
        self.outcomes: list[tuple[CommandFacts, CommandOutcome]] = []
        self.fail_on_call: int | None = None
        self.unknown_started: asyncio.Event | None = None
        self.allow_unknown_commit: asyncio.Event | None = None
        self._on_record = on_record
        self.offers = _Offers()

    @property
    def records(self) -> list[Record]:
        return [record for txn in self.txns for record in txn]

    @property
    def labels(self) -> list[str]:
        return [label(record) for record in self.records]

    def _commit(self, *records: Record) -> None:
        call_number = len(self.txns) + 1
        if self.fail_on_call == call_number:
            raise RuntimeError(f"persistence failed on call {call_number}")
        self.txns.append(records)
        if self._on_record is not None:
            for record in records:
                self._on_record(record)

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
        r._commit(AttemptAuthorized(scope, attempt))
        return Authorized(attempt.attempt_id, 1, "digest")

    async def record_outcome(self, scope: Scope, attempt_id: UUID, outcome: CommandOutcome) -> None:
        r = self._r
        (attempt,) = [a for a in r.attempts.values() if a.attempt_id == attempt_id]
        if outcome.kind == "unknown":
            if r.unknown_started is not None:
                r.unknown_started.set()
            if r.allow_unknown_commit is not None:
                await r.allow_unknown_commit.wait()
        r._commit(OutcomeRecorded(scope, attempt, outcome))

    async def read_back_outcome(self, scope: Scope, attempt_id: UUID) -> CommandOutcome | None:
        return None

    async def admit_cancel(self, *args: object, **kwargs: object) -> object:
        raise NotImplementedError("the gate contract tests only submit")


class _Effects:
    def __init__(self, recording: Recording) -> None:
        self._r = recording

    def new_event_id(self) -> UUID | None:
        return None

    async def outcome_recorded(self, facts: CommandFacts, outcome: CommandOutcome) -> None:
        self._r.outcomes.append((facts, outcome))
