"""Account command gate ordering and fail-closed fault contracts."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from bfx_funding_bot.core.db import Base
from bfx_funding_bot.modules.accounts.tables import ExchangeAccount
from bfx_funding_bot.modules.execution.audit.tables import ExecutionDecisionRow
from bfx_funding_bot.modules.execution.boot_recovery import (
    LocalClaim,
    compute_recovery_actions,
)
from bfx_funding_bot.modules.execution.bus import DomainEventBus
from bfx_funding_bot.modules.execution.command_gate import (
    AccountCommandGate,
    CommandGateBlocked,
    DatabaseOpenUncertaintyReader,
)
from bfx_funding_bot.modules.execution.contracts import (
    ExecutionPolicy,
    GuardResult,
    ReadyToSubmit,
)
from bfx_funding_bot.modules.execution.event_store.persister import EventStorePersister
from bfx_funding_bot.modules.execution.event_store.store import PostgresEventStore
from bfx_funding_bot.modules.execution.event_store.tables import EventLogRow, PositionStateRow
from bfx_funding_bot.modules.execution.events import (
    ReservationClaimed,
    ReservationFailed,
    ReservationIntent,
    ReservationUnknown,
)
from bfx_funding_bot.modules.execution.protocols import (
    AccountContext,
    Credentials,
    SubmittedOrder,
)
from bfx_funding_bot.modules.execution.registry_offers import RegistryState
from bfx_funding_bot.modules.execution.submit_outcomes import (
    SubmitAcknowledged,
    SubmitNotSent,
    SubmitOutcomeUnknown,
    SubmitRejected,
)
from bfx_funding_bot.modules.execution.uncertainty_tables import (
    ExecutionUncertaintyRow,
    SubmissionAttemptRow,
)
from bfx_funding_bot.modules.marketfeed.schemas import DecisionOutcome, DecisionPayload

ACCOUNT_ID = UUID("3f19d046-5030-494c-9a0a-9573bb890c1f")
ENVIRONMENT = "ci"
SYMBOL = "fUST"


def _ready(*, decision_id: str = "decision-1", symbol: str = SYMBOL) -> ReadyToSubmit:
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
        market_snapshot_id="snapshot-1",
        model_version=None,
        evidence={},
        safety=GuardResult(allowed=True, guard_name="test"),
    )


def _context() -> AccountContext:
    return AccountContext(
        account_id=str(ACCOUNT_ID),
        credentials=Credentials(api_key="never-persist-key", api_secret="never-persist-secret"),
        allocation_cap_usdt=Decimal("100"),
    )


@dataclass
class _FakeUncertaintyReader:
    open_scopes: set[tuple[UUID, str, str]]
    error: Exception | None = None

    async def has_open(
        self,
        *,
        exchange_account_id: UUID,
        deployment_environment: str,
        symbol: str,
    ) -> bool:
        if self.error is not None:
            raise self.error
        return (exchange_account_id, deployment_environment, symbol) in self.open_scopes


class _FakePersister:
    """Fake serialized writer: commits calls and enforces one attempt per decision."""

    def __init__(self, reader: _FakeUncertaintyReader) -> None:
        self.reader = reader
        self.txns: list[tuple[object, ...]] = []
        self.attempts: dict[str, object] = {}
        self.fail_on_call: int | None = None
        self.unknown_started: asyncio.Event | None = None
        self.allow_unknown_commit: asyncio.Event | None = None

    async def persist(self, *events: object) -> list[bool]:
        call_number = len(self.txns) + 1
        if self.fail_on_call == call_number:
            raise RuntimeError(f"persistence failed on call {call_number}")
        for event in events:
            if isinstance(event, ReservationIntent):
                attempt = event.submission_attempt
                assert attempt is not None
                if event.execution_decision_id in self.attempts:
                    raise ValueError("one submission attempt per execution decision")
                self.attempts[event.execution_decision_id or ""] = attempt
            elif isinstance(event, ReservationUnknown):
                if self.unknown_started is not None:
                    self.unknown_started.set()
                if self.allow_unknown_commit is not None:
                    await self.allow_unknown_commit.wait()
                self.reader.open_scopes.add((ACCOUNT_ID, ENVIRONMENT, event.symbol))
        self.txns.append(events)
        return [True] * len(events)


class _FakeVenue:
    def __init__(self, outcome: object | None = None, *, crash: bool = False) -> None:
        self.outcome = outcome or SubmitAcknowledged("venue-1")
        self.crash = crash
        self.calls = 0
        self.events_seen_at_call: list[tuple[object, ...]] | None = None
        self.persister: _FakePersister | None = None

    async def submit(
        self, ready: ReadyToSubmit, ctx: AccountContext, *, cid: int | None = None,
        reservation_ref=None,
    ) -> SubmittedOrder:
        self.calls += 1
        if self.persister is not None:
            self.events_seen_at_call = list(self.persister.txns)
        if self.crash:
            raise RuntimeError("process crash after durable intent")
        return SubmittedOrder(
            cid=cid or 0,
            venue_offer_id=None,
            outcome=self.outcome,  # type: ignore[arg-type]
            reservation_ref=reservation_ref,
        )


def _gate(
    venue: _FakeVenue,
    reader: _FakeUncertaintyReader,
    persister: _FakePersister,
    *,
    bus: DomainEventBus | None = None,
) -> AccountCommandGate:
    venue.persister = persister
    return AccountCommandGate(
        venue,
        bus=bus or DomainEventBus(),
        persister=persister,
        uncertainty_reader=reader,
        deployment_environment=ENVIRONMENT,
        is_simulated=False,
        clock=iter((100, 101, 102, 103, 104, 105)).__next__,
        date_provider=lambda: date(2026, 9, 3),
    )


@pytest.mark.asyncio
async def test_open_uncertainty_blocks_before_intent_and_venue_call() -> None:
    reader = _FakeUncertaintyReader({(ACCOUNT_ID, ENVIRONMENT, SYMBOL)})
    persister = _FakePersister(reader)
    venue = _FakeVenue()

    with pytest.raises(CommandGateBlocked, match="open execution uncertainty"):
        await _gate(venue, reader, persister).submit(_ready(), _context())

    assert venue.calls == 0
    assert persister.txns == []


@pytest.mark.asyncio
async def test_uncertainty_read_error_fails_closed_before_intent_and_venue_call() -> None:
    reader = _FakeUncertaintyReader(set(), error=RuntimeError("database unavailable"))
    persister = _FakePersister(reader)
    venue = _FakeVenue()

    with pytest.raises(CommandGateBlocked, match="uncertainty guard unavailable"):
        await _gate(venue, reader, persister).submit(_ready(), _context())

    assert venue.calls == 0
    assert persister.txns == []


@pytest.mark.asyncio
async def test_unknown_is_durable_and_blocks_next_command_without_publish_or_retry() -> None:
    reader = _FakeUncertaintyReader(set())
    persister = _FakePersister(reader)
    venue = _FakeVenue(SubmitOutcomeUnknown("transport_timeout", True))
    bus = DomainEventBus()
    published: list[object] = []

    async def capture(event: object) -> None:
        published.append(event)

    bus.subscribe(ReservationClaimed, capture)
    gate = _gate(venue, reader, persister, bus=bus)

    result = await gate.submit(_ready(), _context())
    assert result.outcome_kind.value == "unknown"
    assert venue.calls == 1
    assert venue.events_seen_at_call is not None
    assert [type(event) for event in venue.events_seen_at_call[0]] == [ReservationIntent]
    assert [type(event) for event in persister.txns[1]] == [ReservationUnknown]
    assert reader.open_scopes == {(ACCOUNT_ID, ENVIRONMENT, SYMBOL)}
    assert published == []

    with pytest.raises(CommandGateBlocked, match="open execution uncertainty"):
        await gate.submit(_ready(decision_id="decision-2"), _context())
    assert venue.calls == 1


@pytest.mark.asyncio
async def test_next_submit_waits_until_unknown_block_commit() -> None:
    reader = _FakeUncertaintyReader(set())
    persister = _FakePersister(reader)
    persister.unknown_started = asyncio.Event()
    persister.allow_unknown_commit = asyncio.Event()
    venue = _FakeVenue(SubmitOutcomeUnknown("response_lost", True))
    gate = _gate(venue, reader, persister)

    first = asyncio.create_task(gate.submit(_ready(), _context()))
    await persister.unknown_started.wait()
    second = asyncio.create_task(
        gate.submit(_ready(decision_id="decision-2"), _context())
    )
    await asyncio.sleep(0)
    assert venue.calls == 1

    persister.allow_unknown_commit.set()
    await first
    with pytest.raises(CommandGateBlocked, match="open execution uncertainty"):
        await second
    assert venue.calls == 1


@pytest.mark.asyncio
async def test_uncertainty_scope_does_not_block_another_symbol() -> None:
    reader = _FakeUncertaintyReader({(ACCOUNT_ID, ENVIRONMENT, "fUSD")})
    persister = _FakePersister(reader)
    venue = _FakeVenue(SubmitRejected("explicit_rejection"))

    await _gate(venue, reader, persister).submit(_ready(symbol="fUST"), _context())

    assert venue.calls == 1


@pytest.mark.asyncio
async def test_one_attempt_per_decision_prevents_resubmit() -> None:
    reader = _FakeUncertaintyReader(set())
    persister = _FakePersister(reader)
    venue = _FakeVenue(SubmitRejected("explicit_rejection"))
    gate = _gate(venue, reader, persister)
    ready = _ready()

    await gate.submit(ready, _context())
    with pytest.raises(ValueError, match="one submission attempt"):
        await gate.submit(ready, _context())

    assert venue.calls == 1
    assert len(persister.attempts) == 1


@pytest.mark.asyncio
async def test_crash_after_intent_leaves_pending_for_boot_unknown_without_retry() -> None:
    reader = _FakeUncertaintyReader(set())
    persister = _FakePersister(reader)
    venue = _FakeVenue(crash=True)
    gate = _gate(venue, reader, persister)
    ready = _ready()

    with pytest.raises(RuntimeError, match="process crash"):
        await gate.submit(ready, _context())

    intent = persister.txns[0][0]
    assert isinstance(intent, ReservationIntent)
    assert intent.submission_attempt is not None
    assert intent.submission_attempt.outcome_kind is None
    actions = compute_recovery_actions(
        venue_offers=[],
        local_claims=[
            LocalClaim(
                cid=intent.cid,
                venue_offer_id=None,
                state=RegistryState.PENDING,
                size_usdt=Decimal("12.5"),
                signal_correlation_id=intent.signal_correlation_id,
                occurred_at_ms=100,
                symbol=SYMBOL,
                reservation_ref=intent.reservation_ref,
            )
        ],
        account_id=str(ACCOUNT_ID),
        is_simulated=False,
        now_ms=1_000,
        grace_ms=100,
        configured_symbols=frozenset({SYMBOL}),
    )
    assert [type(event) for event in actions] == [ReservationUnknown]

    with pytest.raises(CommandGateBlocked, match="without durable outcome"):
        await gate.submit(ready, _context())
    assert venue.calls == 1


@pytest.mark.asyncio
async def test_intent_persistence_failure_blocks_venue_call() -> None:
    reader = _FakeUncertaintyReader(set())
    persister = _FakePersister(reader)
    persister.fail_on_call = 1
    venue = _FakeVenue()

    with pytest.raises(RuntimeError, match="persistence failed"):
        await _gate(venue, reader, persister).submit(_ready(), _context())

    assert venue.calls == 0


@pytest.mark.asyncio
async def test_outcome_persistence_failure_latches_scope_closed() -> None:
    reader = _FakeUncertaintyReader(set())
    persister = _FakePersister(reader)
    persister.fail_on_call = 2
    venue = _FakeVenue()
    gate = _gate(venue, reader, persister)

    with pytest.raises(RuntimeError, match="persistence failed"):
        await gate.submit(_ready(), _context())
    persister.fail_on_call = None

    with pytest.raises(CommandGateBlocked, match="outcome persistence failed"):
        await gate.submit(_ready(decision_id="decision-2"), _context())
    assert venue.calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("outcome", "event_type", "reason"),
    [
        (SubmitNotSent("bad_local_input"), ReservationFailed, "local_pre_transport"),
        (SubmitRejected("venue_rejected"), ReservationFailed, "venue_rejected"),
    ],
)
async def test_non_ack_outcomes_persist_one_terminal_event(outcome, event_type, reason) -> None:
    reader = _FakeUncertaintyReader(set())
    persister = _FakePersister(reader)
    venue = _FakeVenue(outcome)

    await _gate(venue, reader, persister).submit(_ready(), _context())

    assert len(persister.txns[1]) == 1
    assert isinstance(persister.txns[1][0], event_type)
    assert persister.txns[1][0].reason == reason


@pytest.mark.asyncio
async def test_serialized_writer_commits_unknown_attempt_event_and_block_atomically(
    sqlite_engine,
) -> None:
    async with sqlite_engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(sqlite_engine, expire_on_commit=False)
    ready = _ready(decision_id="decision-durable")
    async with session_factory() as session:
        session.add(ExchangeAccount(id=ACCOUNT_ID, venue="bitfinex", label="ci"))
        session.add(
            ExecutionDecisionRow(
                decision_id=ready.decision_id,
                account_id=str(ACCOUNT_ID),
                exchange_account_id=ACCOUNT_ID,
                deployment_environment=ENVIRONMENT,
                reconcile_id="reconcile-1",
                cell_id="cell-1",
                symbol=SYMBOL,
                signal_correlation_id=str(ready.decision.signal_correlation_id),
                outcome="ready",
                signal_rate=Decimal("0.0001"),
                applied_rate=Decimal("0.0001"),
                amount_usdt=Decimal("12.5"),
                duration_days=2,
                model_evidence={},
                safety_result={},
                execution_policy="book_guarded",
                service_version="test",
                config_hash="config",
                occurred_at_ms=99,
                recorded_at_ms=99,
            )
        )
        await session.commit()

    store = PostgresEventStore(deployment_environment=ENVIRONMENT)
    gate = AccountCommandGate(
        _FakeVenue(SubmitOutcomeUnknown("transport_timeout", True)),
        bus=DomainEventBus(),
        persister=EventStorePersister(store=store, session_factory=session_factory),
        uncertainty_reader=DatabaseOpenUncertaintyReader(session_factory),
        deployment_environment=ENVIRONMENT,
        is_simulated=False,
        clock=iter((100, 101)).__next__,
        date_provider=lambda: date(2026, 9, 3),
    )

    await gate.submit(ready, _context())

    async with session_factory() as session:
        events = (await session.execute(select(EventLogRow))).scalars().all()
        attempt = (await session.execute(select(SubmissionAttemptRow))).scalar_one()
        uncertainty = (
            await session.execute(select(ExecutionUncertaintyRow))
        ).scalar_one()
        position = (await session.execute(select(PositionStateRow))).scalar_one()
    assert [event.event_type for event in events] == [
        "RESERVATION_INTENT",
        "SUBMIT_OUTCOME_UNKNOWN",
    ]
    assert attempt.outcome_kind == "unknown"
    assert attempt.completed_at_ms == 101
    assert uncertainty.attempt_id == attempt.attempt_id
    assert uncertainty.intended_amount == Decimal("12.5")
    assert position.uncertain_amount == Decimal("12.5")
    assert "never-persist" not in str(attempt.normalized_payload)
