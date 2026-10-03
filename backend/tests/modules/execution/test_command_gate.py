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
    SubmitOutcomeLostError,
)
from bfx_funding_bot.modules.execution.contracts import (
    ExecutionPolicy,
    GuardResult,
    ReadyToSubmit,
    ReservationRef,
)
from bfx_funding_bot.modules.execution.event_store.persister import (
    EventStorePersister,
    NoopEventPersister,
)
from bfx_funding_bot.modules.execution.event_store.store import PostgresEventStore
from bfx_funding_bot.modules.execution.event_store.tables import (
    EventLogRow,
    OfferClaimRow,
    PositionStateRow,
)
from bfx_funding_bot.modules.execution.event_store.writer import ProjectionWriteError
from bfx_funding_bot.modules.execution.events import (
    ReservationClaimed,
    ReservationFailed,
    ReservationIntent,
    ReservationUnknown,
)
from bfx_funding_bot.modules.execution.legacy_ports import LegacyUncertaintyReader
from bfx_funding_bot.modules.execution.middleware.reservation_emitting import (
    ReservationEmittingMiddleware,
)
from bfx_funding_bot.modules.execution.protocols import (
    AccountContext,
    Credentials,
    SubmittedOrder,
)
from bfx_funding_bot.modules.execution.registry_offers import RegistryState
from bfx_funding_bot.modules.execution.submit_outcomes import (
    SubmissionAttemptPayload,
    SubmitAcknowledged,
    SubmitNotSent,
    SubmitOutcomeUnknown,
    SubmitRejected,
)
from bfx_funding_bot.modules.execution.uncertainty_tables import (
    ExecutionUncertaintyRow,
    SubmissionAttemptRow,
)
from bfx_funding_bot.modules.ledger import Scope
from bfx_funding_bot.modules.strategy import DecisionOutcome, DecisionPayload

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

    async def has_open(self, session: object, scope: Scope, symbol: str) -> bool:
        if self.error is not None:
            raise self.error
        return (scope.exchange_account_id, scope.deployment_environment, symbol) in self.open_scopes

    async def list_open(self, session: object, scope: Scope, symbol: str | None = None) -> tuple[()]:
        return ()


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


class _BlockingAckVenue(_FakeVenue):
    def __init__(self) -> None:
        super().__init__(SubmitAcknowledged("venue-1"))
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def submit(
        self, ready: ReadyToSubmit, ctx: AccountContext, *, cid: int | None = None,
        reservation_ref=None,
    ) -> SubmittedOrder:
        self.started.set()
        await self.release.wait()
        return await super().submit(
            ready,
            ctx,
            cid=cid,
            reservation_ref=reservation_ref,
        )


class _MismatchedCidVenue(_FakeVenue):
    async def submit(
        self, ready: ReadyToSubmit, ctx: AccountContext, *, cid: int | None = None,
        reservation_ref=None,
    ) -> SubmittedOrder:
        self.calls += 1
        assert cid is not None
        return SubmittedOrder(
            cid=cid + 1,
            venue_offer_id="venue-untrusted",
            outcome=SubmitAcknowledged("venue-untrusted"),
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
        safety_evaluator=_FakeSafetyEvaluator(
            [GuardResult(allowed=True, guard_name="<chain>")]
        ),
        deployment_environment=ENVIRONMENT,
        is_simulated=True,
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
async def test_waiting_submit_rechecks_authoritative_safety_inside_account_lock() -> None:
    """Removing the locked evaluator would persist a second intent and call venue."""
    reader = _FakeUncertaintyReader(set())
    persister = _FakePersister(reader)
    venue = _BlockingAckVenue()
    safety = _FakeSafetyEvaluator(
        [
            GuardResult(allowed=True, guard_name="<chain>"),
            GuardResult(
                allowed=False,
                guard_name="kill_switch",
                reason="halted while waiting for account lock",
            ),
        ]
    )
    gate = AccountCommandGate(
        venue,
        bus=DomainEventBus(),
        persister=persister,
        uncertainty_reader=reader,
        safety_evaluator=safety,
        deployment_environment=ENVIRONMENT,
        is_simulated=True,
        clock=iter((100, 101, 102, 103)).__next__,
        date_provider=lambda: date(2026, 9, 3),
    )

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
    assert safety.calls == 2
    assert len(persister.attempts) == 1


def test_live_middleware_rejects_a_missing_command_boundary() -> None:
    """A live noop/wrapper must never silently select the plain submit path."""
    with pytest.raises(ValueError, match="command boundary"):
        ReservationEmittingMiddleware(
            _FakeVenue(),
            bus=DomainEventBus(),
            persister=NoopEventPersister(),
            is_simulated=False,
        )

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

    with pytest.raises(SubmitOutcomeLostError, match="submit ended without durable outcome"):
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
    # No retry in this process: it exits (SubmitOutcomeLostError); the restarted
    # daemon's recovery turns the PENDING into the UNKNOWN above.
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
async def test_outcome_persistence_failure_ends_the_process() -> None:
    """Process fencing (lending envelope D3): no in-memory latch that only a
    restart clears -- the gate raises a BaseException no ``except Exception``
    keeps alive, and the restarted daemon quarantines the symbol from the
    durable PENDING intent."""
    reader = _FakeUncertaintyReader(set())
    persister = _FakePersister(reader)
    persister.fail_on_call = 2
    venue = _FakeVenue()
    gate = _gate(venue, reader, persister)

    with pytest.raises(SubmitOutcomeLostError, match="outcome persistence failed") as lost:
        await gate.submit(_ready(), _context())
    assert not isinstance(lost.value, Exception)
    assert isinstance(lost.value.__cause__, RuntimeError)
    assert venue.calls == 1


@pytest.mark.asyncio
async def test_a_submit_that_raises_mid_transport_ends_the_process() -> None:
    reader = _FakeUncertaintyReader(set())
    persister = _FakePersister(reader)
    venue = _FakeVenue()

    async def explode(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise RuntimeError("socket reset")

    venue.submit = explode  # type: ignore[method-assign]
    with pytest.raises(SubmitOutcomeLostError, match="submit ended without durable outcome"):
        await _gate(venue, reader, persister).submit(_ready(), _context())


@pytest.mark.asyncio
async def test_submit_cancelled_before_transport_closes_intent_as_not_sent() -> None:
    """The executor was cancelled while waiting for the per-key venue gate:
    nothing was sent, so the intent closes as NOT_SENT (no UNKNOWN quarantine
    for recovery to invent) and the cancellation still propagates."""
    from bfx_funding_bot.modules.execution.submit_outcomes import SubmitCancelledNotSent

    reader = _FakeUncertaintyReader(set())
    persister = _FakePersister(reader)
    venue = _FakeVenue()

    async def cancelled_waiting(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise SubmitCancelledNotSent("cancelled_waiting_for_venue_gate")

    venue.submit = cancelled_waiting  # type: ignore[method-assign]
    with pytest.raises(asyncio.CancelledError):
        await _gate(venue, reader, persister).submit(_ready(), _context())

    assert [type(event) for event in persister.txns[1]] == [ReservationFailed]
    assert persister.txns[1][0].reason == "local_pre_transport"
    assert not reader.open_scopes


@pytest.mark.asyncio
async def test_executor_cid_mismatch_becomes_durable_unknown_and_blocks_scope() -> None:
    """Trusting a mismatched result CID would falsely claim another command's ACK."""
    reader = _FakeUncertaintyReader(set())
    persister = _FakePersister(reader)
    venue = _MismatchedCidVenue()
    gate = _gate(venue, reader, persister)

    result = await gate.submit(_ready(), _context())

    assert result.outcome_kind.value == "unknown"
    assert [type(event) for event in persister.txns[1]] == [ReservationUnknown]
    with pytest.raises(CommandGateBlocked, match="open execution uncertainty"):
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
        uncertainty_reader=LegacyUncertaintyReader(session_factory),
        safety_evaluator=_FakeSafetyEvaluator(
            [GuardResult(allowed=True, guard_name="<chain>")]
        ),
        deployment_environment=ENVIRONMENT,
        is_simulated=True,
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
    stored_attempt_id = UUID(events[0].payload["submission_attempt"]["attempt_id"])
    assert stored_attempt_id == attempt.attempt_id
    assert uncertainty.attempt_id == attempt.attempt_id
    assert uncertainty.intended_amount == Decimal("12.5")
    assert position.uncertain_amount == Decimal("12.5")
    assert "never-persist" not in str(attempt.normalized_payload)


@pytest.mark.asyncio
async def test_full_rebuild_replays_attempt_and_uncertainty_with_stable_identity(
    sqlite_engine,
) -> None:
    """Leaving either projection intact or regenerating IDs breaks second replay."""
    async with sqlite_engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(sqlite_engine, expire_on_commit=False)
    ready = _ready(decision_id="decision-rebuild")
    async with session_factory() as session:
        session.add(ExchangeAccount(id=ACCOUNT_ID, venue="bitfinex", label="ci"))
        session.add(
            ExecutionDecisionRow(
                decision_id=ready.decision_id,
                account_id=str(ACCOUNT_ID),
                exchange_account_id=ACCOUNT_ID,
                deployment_environment=ENVIRONMENT,
                reconcile_id="reconcile-rebuild",
                cell_id="cell-rebuild",
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
        uncertainty_reader=LegacyUncertaintyReader(session_factory),
        safety_evaluator=_FakeSafetyEvaluator(
            [GuardResult(allowed=True, guard_name="<chain>")]
        ),
        deployment_environment=ENVIRONMENT,
        is_simulated=True,
        clock=iter((100, 101)).__next__,
        date_provider=lambda: date(2026, 9, 3),
    )
    await gate.submit(ready, _context())

    async with session_factory() as session:
        original_attempt = (await session.execute(select(SubmissionAttemptRow))).scalar_one()
        original_uncertainty = (
            await session.execute(select(ExecutionUncertaintyRow))
        ).scalar_one()
        expected = (
            original_attempt.attempt_id,
            original_uncertainty.uncertainty_id,
            original_uncertainty.correlation_key,
        )
        await store.rebuild_snapshot_from_log(
            session,
            account_id=str(ACCOUNT_ID),
            deployment_environment=ENVIRONMENT,
        )
        await session.commit()

    async with session_factory() as session:
        rebuilt_attempt = (await session.execute(select(SubmissionAttemptRow))).scalar_one()
        rebuilt_uncertainty = (
            await session.execute(select(ExecutionUncertaintyRow))
        ).scalar_one()
        first_rebuild = (
            rebuilt_attempt.attempt_id,
            rebuilt_uncertainty.uncertainty_id,
            rebuilt_uncertainty.correlation_key,
        )
        await store.rebuild_snapshot_from_log(
            session,
            account_id=str(ACCOUNT_ID),
            deployment_environment=ENVIRONMENT,
        )
        await session.commit()

    async with session_factory() as session:
        second_attempt = (await session.execute(select(SubmissionAttemptRow))).scalar_one()
        second_uncertainty = (
            await session.execute(select(ExecutionUncertaintyRow))
        ).scalar_one()
    second_rebuild = (
        second_attempt.attempt_id,
        second_uncertainty.uncertainty_id,
        second_uncertainty.correlation_key,
    )
    assert first_rebuild == second_rebuild == expected


@pytest.mark.asyncio
async def test_persisted_crash_recovery_closes_pending_attempt_as_unknown(
    sqlite_engine,
) -> None:
    """Recovery must update the durable attempt and block a fresh gate, never retry."""
    async with sqlite_engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(sqlite_engine, expire_on_commit=False)
    ready = _ready(decision_id="decision-crash-persisted")
    async with session_factory() as session:
        session.add(ExchangeAccount(id=ACCOUNT_ID, venue="bitfinex", label="ci"))
        session.add(
            ExecutionDecisionRow(
                decision_id=ready.decision_id,
                account_id=str(ACCOUNT_ID),
                exchange_account_id=ACCOUNT_ID,
                deployment_environment=ENVIRONMENT,
                reconcile_id="reconcile-crash",
                cell_id="cell-crash",
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
    persister = EventStorePersister(store=store, session_factory=session_factory)
    crashing_venue = _FakeVenue(crash=True)
    gate = AccountCommandGate(
        crashing_venue,
        bus=DomainEventBus(),
        persister=persister,
        uncertainty_reader=LegacyUncertaintyReader(session_factory),
        safety_evaluator=_FakeSafetyEvaluator(
            [GuardResult(allowed=True, guard_name="<chain>")]
        ),
        deployment_environment=ENVIRONMENT,
        is_simulated=True,
        clock=iter((100, 101)).__next__,
        date_provider=lambda: date(2026, 9, 3),
    )
    with pytest.raises(SubmitOutcomeLostError, match="submit ended without durable outcome"):
        await gate.submit(ready, _context())

    async with session_factory() as session:
        pending = (await session.execute(select(OfferClaimRow))).scalar_one()
        attempt = (await session.execute(select(SubmissionAttemptRow))).scalar_one()
    assert pending.state == RegistryState.PENDING.value
    assert attempt.outcome_kind is None
    actions = compute_recovery_actions(
        venue_offers=[],
        local_claims=[
            LocalClaim(
                cid=pending.cid,
                venue_offer_id=None,
                state=RegistryState.PENDING,
                size_usdt=Decimal(str(pending.size_usdt)),
                signal_correlation_id=UUID(pending.signal_correlation_id),
                occurred_at_ms=pending.occurred_at_ms,
                symbol=pending.symbol,
                reservation_ref=ReservationRef(
                    execution_decision_id=ready.decision_id,
                    cid=pending.cid,
                    signal_correlation_id=UUID(pending.signal_correlation_id),
                ),
            )
        ],
        account_id=str(ACCOUNT_ID),
        is_simulated=False,
        now_ms=1_000,
        grace_ms=100,
        configured_symbols=frozenset({SYMBOL}),
    )
    await persister.persist(*actions)

    async with session_factory() as session:
        recovered_attempt = (
            await session.execute(select(SubmissionAttemptRow))
        ).scalar_one()
        uncertainty = (
            await session.execute(select(ExecutionUncertaintyRow))
        ).scalar_one()
    assert recovered_attempt.outcome_kind == "unknown"
    assert uncertainty.attempt_id == recovered_attempt.attempt_id

    fresh_venue = _FakeVenue()
    fresh_gate = AccountCommandGate(
        fresh_venue,
        bus=DomainEventBus(),
        persister=persister,
        uncertainty_reader=LegacyUncertaintyReader(session_factory),
        safety_evaluator=_FakeSafetyEvaluator(
            [GuardResult(allowed=True, guard_name="<chain>")]
        ),
        deployment_environment=ENVIRONMENT,
        is_simulated=True,
    )
    with pytest.raises(CommandGateBlocked, match="open execution uncertainty"):
        await fresh_gate.submit(_ready(decision_id="decision-after-crash"), _context())
    assert crashing_venue.calls == 1
    assert fresh_venue.calls == 0


@pytest.mark.asyncio
async def test_registered_legacy_pending_without_attempt_recovers_to_unknown(
    sqlite_engine,
) -> None:
    """Historical pending intents cannot be rejected or retried for lacking attempt rows."""
    async with sqlite_engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(sqlite_engine, expire_on_commit=False)
    decision_id = "decision-legacy-pending"
    signal_id = uuid4()
    async with session_factory() as session:
        session.add(ExchangeAccount(id=ACCOUNT_ID, venue="bitfinex", label="ci"))
        session.add(
            ExecutionDecisionRow(
                decision_id=decision_id,
                account_id=str(ACCOUNT_ID),
                exchange_account_id=ACCOUNT_ID,
                deployment_environment=ENVIRONMENT,
                reconcile_id="reconcile-legacy",
                cell_id="cell-legacy",
                symbol=SYMBOL,
                signal_correlation_id=str(signal_id),
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
    persister = EventStorePersister(store=store, session_factory=session_factory)
    intent = ReservationIntent(
        symbol=SYMBOL,
        cid=77,
        signal_correlation_id=signal_id,
        account_id=str(ACCOUNT_ID),
        is_simulated=False,
        execution_decision_id=decision_id,
        amount=Decimal("12.5"),
        occurred_at_ms=100,
    )
    await persister.persist(intent)
    actions = compute_recovery_actions(
        venue_offers=[],
        local_claims=[
            LocalClaim(
                cid=77,
                venue_offer_id=None,
                state=RegistryState.PENDING,
                size_usdt=Decimal("12.5"),
                signal_correlation_id=signal_id,
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
    await persister.persist(*actions)

    async with session_factory() as session:
        assert (await session.execute(select(SubmissionAttemptRow))).scalar_one_or_none() is None
        uncertainty = (
            await session.execute(select(ExecutionUncertaintyRow))
        ).scalar_one()
        claim = (await session.execute(select(OfferClaimRow))).scalar_one()
    assert uncertainty.attempt_id is None
    assert uncertainty.evidence["payload_sha256"] is None
    assert uncertainty.correlation_key.startswith("legacy_submit_unknown:")
    assert claim.state == RegistryState.UNKNOWN.value
    legacy_identity = (
        uncertainty.uncertainty_id,
        uncertainty.correlation_key,
    )

    async with session_factory() as session:
        await store.rebuild_snapshot_from_log(
            session,
            account_id=str(ACCOUNT_ID),
            deployment_environment=ENVIRONMENT,
        )
        await session.commit()
    async with session_factory() as session:
        rebuilt = (await session.execute(select(ExecutionUncertaintyRow))).scalar_one()
    assert (rebuilt.uncertainty_id, rebuilt.correlation_key) == legacy_identity


@pytest.mark.asyncio
@pytest.mark.parametrize("mismatched_field", ["account", "environment", "symbol"])
async def test_attempt_rejects_cross_scope_execution_decision(
    sqlite_engine,
    mismatched_field: str,
) -> None:
    """Removing any decision-scope comparison would cross-link tenant audit state."""
    async with sqlite_engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(sqlite_engine, expire_on_commit=False)
    other_account = UUID("24c4f9b8-c0a1-40bf-8f2e-21ad11cc4a14")
    decision_account = other_account if mismatched_field == "account" else ACCOUNT_ID
    decision_environment = "prod" if mismatched_field == "environment" else ENVIRONMENT
    decision_symbol = "fUSD" if mismatched_field == "symbol" else SYMBOL
    decision_id = f"decision-cross-{mismatched_field}"
    signal_id = uuid4()
    async with session_factory() as session:
        session.add_all(
            [
                ExchangeAccount(id=ACCOUNT_ID, venue="bitfinex", label="ci"),
                ExchangeAccount(id=other_account, venue="bitfinex", label="other"),
            ]
        )
        session.add(
            ExecutionDecisionRow(
                decision_id=decision_id,
                account_id=str(decision_account),
                exchange_account_id=decision_account,
                deployment_environment=decision_environment,
                reconcile_id=f"reconcile-{mismatched_field}",
                cell_id="cell-cross-scope",
                symbol=decision_symbol,
                signal_correlation_id=str(signal_id),
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
    attempt = SubmissionAttemptPayload(
        execution_decision_id=decision_id,
        account_id=ACCOUNT_ID,
        environment=ENVIRONMENT,
        symbol=SYMBOL,
        cid=88,
        normalized_payload={"symbol": SYMBOL, "amount": "12.5"},
        started_at_ms=100,
    )
    intent = ReservationIntent(
        symbol=SYMBOL,
        cid=88,
        signal_correlation_id=signal_id,
        account_id=str(ACCOUNT_ID),
        is_simulated=False,
        execution_decision_id=decision_id,
        submission_attempt=attempt,
        amount=Decimal("12.5"),
        occurred_at_ms=100,
    )

    with pytest.raises(ProjectionWriteError, match="execution decision scope"):
        await EventStorePersister(
            store=PostgresEventStore(deployment_environment=ENVIRONMENT),
            session_factory=session_factory,
        ).persist(intent)

    async with session_factory() as session:
        assert (await session.execute(select(SubmissionAttemptRow))).scalar_one_or_none() is None
