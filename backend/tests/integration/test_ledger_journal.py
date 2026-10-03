"""S1-2a journal on an S1-1 clone.

Mutation checks, each applied alone and reverted before the next run:

* Remove ``bump_locked`` from ``record_resolution``: the clock assertion in
  ``test_journal_writes_and_first_write_errors`` fails.
* Return success when ``stored is not None`` in ``record_outcome``: the
  identical-outcome ``OutcomeAlreadyRecorded`` assertion fails.
* Remove ``lock_scope`` from ``begin_query``: the concurrent query test races
  on the scope's max revision (second transaction either conflicts or returns 1).

For each mutation, edit the named function in ``modules/ledger/_internal``, run
``backend/.venv/bin/python -m pytest -q -p no:cacheprovider
tests/integration/test_ledger_journal.py`` from ``backend/`` against a fresh
clone, confirm failure, then revert that single edit before the next mutation.
"""

from __future__ import annotations

import asyncio
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from bfx_funding_bot.modules.ledger import (
    Attempt,
    Outcome,
    OutcomeAlreadyRecorded,
    Quarantine,
    QuarantineMember,
    QuarantineMemberConflict,
    QueryAdmissionRefused,
    Resolution,
    ResolutionAction,
    ResolutionAlreadyRecorded,
    ResolutionRejected,
    Scope,
)
from bfx_funding_bot.modules.ledger._internal.journal import record_attempt
from bfx_funding_bot.modules.ledger.tables import CapitalCommandClockRow
from bfx_funding_bot.modules.ledger.wiring import build_ledger_journal

from .test_ledger_schema_roles import (
    _A,
    _B,
    _D2,
    _O,
    _P,
    _observation_sql,
    _query_sql,
    ledger_db,  # noqa: F401 - dependency of seeded_fixture
)
from .test_ledger_schema_roles import (
    ledger_db as ledger_db_fixture,  # noqa: F401 - pytest fixture re-export
)
from .test_ledger_schema_roles import (
    seeded as seeded_fixture,  # noqa: F401 - pytest fixture re-export
)

pytestmark = pytest.mark.integration
SCOPE = Scope(UUID(_A), "ci")
OBSERVATION_ID = UUID(_O)
JOURNAL = build_ledger_journal()


def _engine(sync_engine):
    return create_async_engine(
        sync_engine.url.render_as_string(hide_password=False).replace("+psycopg", "+asyncpg")
    )


def _attempt(*, started_at_ms: int = 0) -> Attempt:
    return Attempt(
        uuid4(),
        _D2,
        "fUST",
        "cell",
        {"z": 1, "a": "值", "amount": "1"},
        UUID(_B),
        UUID(_P),
        {"authorized": True},
        started_at_ms,
    )


def _resolution(
    *,
    attempt_id: UUID | None = None,
    quarantine_id: UUID | None = None,
    observation_id: UUID = OBSERVATION_ID,
    symbol: str = "fUST",
    action: ResolutionAction = "not_accepted",
) -> Resolution:
    return Resolution(
        uuid4(),
        symbol,
        action,
        None,
        observation_id,
        "operator",
        "test",
        5,
        "verified",
        {},
        attempt_id,
        quarantine_id,
    )


@pytest.mark.asyncio
async def test_clock_transaction_and_scope_queries(ledger_db_fixture) -> None:  # noqa: F811
    engine = _engine(ledger_db_fixture)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    with ledger_db_fixture.begin() as conn:
        conn.execute(
            text("INSERT INTO exchange_accounts(id,venue,label) VALUES (:id,'bitfinex','ledger')"),
            {"id": _A},
        )
    try:
        async with factory() as session:
            async with session.begin():
                assert await JOURNAL.bump_clock(session, SCOPE) == 1
            async with session.begin():
                assert await JOURNAL.bump_clock(session, SCOPE) == 2
                await session.rollback()
            async with session.begin():
                assert await JOURNAL.bump_clock(session, SCOPE) == 2
                first = await JOURNAL.begin_query(session, SCOPE, 10)
                second = await JOURNAL.begin_query(session, SCOPE, 11)
                assert (first.query_revision, second.query_revision) == (1, 2)
                assert first.start_revision == second.start_revision == 2
            async with session.begin():
                other = await JOURNAL.begin_query(session, Scope(UUID(_A), "other"), 12)
                assert (other.query_revision, other.start_revision) == (1, 0)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_begin_query_refuses_pending_attempt(seeded_fixture) -> None:  # noqa: F811
    engine = _engine(seeded_fixture)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    attempt = _attempt()
    try:
        async with factory.begin() as session:
            await record_attempt(session, SCOPE, attempt)
        async with factory.begin() as session:
            with pytest.raises(QueryAdmissionRefused):
                await JOURNAL.begin_query(session, SCOPE, 10)
            assert await JOURNAL.begin_query(session, Scope(SCOPE.exchange_account_id, "other"), 10)
        async with factory.begin() as session:
            await JOURNAL.record_outcome(
                session, SCOPE, Outcome(attempt.attempt_id, "unknown", None, None, 11, {})
            )
            assert await JOURNAL.begin_query(session, SCOPE, 12)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_concurrent_scope_lock_serializes_query_revision(ledger_db_fixture) -> None:  # noqa: F811
    engine = _engine(ledger_db_fixture)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    with ledger_db_fixture.begin() as conn:
        conn.execute(
            text("INSERT INTO exchange_accounts(id,venue,label) VALUES (:id,'bitfinex','ledger')"),
            {"id": _A},
        )
    entered = asyncio.Event()
    release = asyncio.Event()

    async def first() -> int:
        async with factory.begin() as session:
            handle = await JOURNAL.begin_query(session, SCOPE, 1)
            entered.set()
            await release.wait()
            return handle.query_revision

    async def second() -> int:
        await entered.wait()
        async with factory.begin() as session:
            return (await JOURNAL.begin_query(session, SCOPE, 2)).query_revision

    try:
        task1 = asyncio.create_task(first())
        await entered.wait()
        task2 = asyncio.create_task(second())
        await asyncio.sleep(0.1)
        assert not task2.done()
        release.set()
        assert await task1 == 1
        assert await task2 == 2
    finally:
        release.set()
        await engine.dispose()


@pytest.mark.asyncio
async def test_concurrent_clock_bumps_serialize(ledger_db_fixture) -> None:  # noqa: F811
    engine = _engine(ledger_db_fixture)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    with ledger_db_fixture.begin() as conn:
        conn.execute(
            text("INSERT INTO exchange_accounts(id,venue,label) VALUES (:id,'bitfinex','ledger')"),
            {"id": _A},
        )
    entered = asyncio.Event()
    release = asyncio.Event()

    async def first() -> int:
        async with factory.begin() as session:
            revision = await JOURNAL.bump_clock(session, SCOPE)
            entered.set()
            await release.wait()
            return revision

    async def second() -> int:
        await entered.wait()
        async with factory.begin() as session:
            return await JOURNAL.bump_clock(session, SCOPE)

    try:
        task1 = asyncio.create_task(first())
        await entered.wait()
        task2 = asyncio.create_task(second())
        await asyncio.sleep(0.1)
        assert not task2.done()
        release.set()
        assert await task1 == 1
        assert await task2 == 2
    finally:
        release.set()
        await engine.dispose()


@pytest.mark.asyncio
async def test_journal_writes_and_first_write_errors(seeded_fixture) -> None:  # noqa: F811
    engine = _engine(seeded_fixture)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    attempt = _attempt()
    # Recorded before the seeded observation's query began (G2: the UNKNOWN is the opening).
    outcome = Outcome(attempt.attempt_id, "unknown", None, None, 0, {})
    quarantine = Quarantine(uuid4(), "fUST", Decimal("1"), 0, {})
    member = QuarantineMember(quarantine.quarantine_id, "offer", "offer-1", UUID(_O), Decimal("1"))
    try:
        async with factory.begin() as session:
            recorded = await record_attempt(session, SCOPE, attempt)
            assert recorded.attempt_seq == 2
            assert await JOURNAL.read_back_outcome(session, attempt.attempt_id) is None
            await JOURNAL.record_outcome(session, SCOPE, outcome)
            assert await JOURNAL.read_back_outcome(session, attempt.attempt_id) == outcome
            with pytest.raises(OutcomeAlreadyRecorded) as exc:
                await JOURNAL.record_outcome(session, SCOPE, outcome)
            assert exc.value.stored == outcome
            await JOURNAL.open_quarantine(session, SCOPE, quarantine)
            await JOURNAL.add_quarantine_member(session, SCOPE, member)
            with pytest.raises(QuarantineMemberConflict) as member_exc:
                await JOURNAL.add_quarantine_member(session, SCOPE, member)
            assert member_exc.value.stored == member
            resolution = _resolution(attempt_id=attempt.attempt_id)
            await JOURNAL.record_resolution(session, SCOPE, resolution)
            with pytest.raises(ResolutionAlreadyRecorded) as resolution_exc:
                await JOURNAL.record_resolution(session, SCOPE, resolution)
            assert resolution_exc.value.stored.id == resolution.id
            assert (
                await session.scalar(
                    select(CapitalCommandClockRow.revision).where(
                        CapitalCommandClockRow.exchange_account_id == SCOPE.exchange_account_id,
                        CapitalCommandClockRow.deployment_environment
                        == SCOPE.deployment_environment,
                    )
                )
                == 5
            )
        async with factory.begin() as session:
            assert await JOURNAL.read_back_outcome(session, attempt.attempt_id) == outcome
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_resolution_structural_rejections(seeded_fixture) -> None:  # noqa: F811
    engine = _engine(seeded_fixture)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    attempt = _attempt(started_at_ms=2)
    try:
        async with factory.begin() as session:
            await record_attempt(session, SCOPE, attempt)
            await JOURNAL.record_outcome(
                session, SCOPE, Outcome(attempt.attempt_id, "ack", "offer", None, 3, {})
            )
            with pytest.raises(ResolutionRejected, match="UNKNOWN"):
                await JOURNAL.record_resolution(
                    session, SCOPE, _resolution(attempt_id=attempt.attempt_id)
                )
            with pytest.raises(ResolutionRejected, match="quarantines only"):
                await JOURNAL.record_resolution(
                    session, SCOPE, _resolution(attempt_id=attempt.attempt_id, action="manual")
                )
            quarantine = Quarantine(uuid4(), "fUST", Decimal("1"), 0, {})
            await JOURNAL.open_quarantine(session, SCOPE, quarantine)
            with pytest.raises(ResolutionRejected, match="scope"):
                await JOURNAL.record_resolution(
                    session,
                    Scope(SCOPE.exchange_account_id, "other"),
                    _resolution(quarantine_id=quarantine.quarantine_id),
                )
            with pytest.raises(ResolutionRejected, match="scope"):
                await JOURNAL.record_resolution(
                    session,
                    SCOPE,
                    _resolution(quarantine_id=quarantine.quarantine_id, symbol="fEUR"),
                )
            with pytest.raises(ResolutionRejected, match="does not exist"):
                await JOURNAL.record_resolution(
                    session,
                    SCOPE,
                    _resolution(quarantine_id=quarantine.quarantine_id, observation_id=uuid4()),
                )
            await JOURNAL.record_resolution(
                session, SCOPE, _resolution(quarantine_id=quarantine.quarantine_id)
            )
            with pytest.raises(ResolutionAlreadyRecorded):
                await JOURNAL.record_resolution(
                    session, SCOPE, _resolution(quarantine_id=quarantine.quarantine_id)
                )
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_resolution_rejects_stale_and_incomplete_observations(seeded_fixture) -> None:  # noqa: F811
    with seeded_fixture.begin() as conn:
        query_id, observation_id = uuid4(), uuid4()
        conn.exec_driver_sql(_query_sql(str(query_id), 2, started_at_ms=1))
        conn.exec_driver_sql(
            _observation_sql(str(observation_id), str(query_id), accepted=False, complete=False)
        )
    engine = _engine(seeded_fixture)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory.begin() as session:
            quarantine = Quarantine(uuid4(), "fUST", Decimal("1"), 2, {})
            await JOURNAL.open_quarantine(session, SCOPE, quarantine)
            with pytest.raises(ResolutionRejected, match="stale"):
                await JOURNAL.record_resolution(
                    session, SCOPE, _resolution(quarantine_id=quarantine.quarantine_id)
                )
            with pytest.raises(ResolutionRejected, match="incomplete"):
                await JOURNAL.record_resolution(
                    session,
                    SCOPE,
                    _resolution(
                        quarantine_id=quarantine.quarantine_id, observation_id=observation_id
                    ),
                )
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_clock_bump_waits_for_scope_lock_holder(ledger_db_fixture) -> None:  # noqa: F811
    """A bump must not commit while another txn holds the scope lock.

    Acceptance reads the clock under the scope lock; a bump that bypassed the
    lock could commit in between and let a stale revision be accepted. The row
    lock of the UPDATE alone does not give this (the lock holder never updates
    the clock row).
    """
    engine = _engine(ledger_db_fixture)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    with ledger_db_fixture.begin() as conn:
        conn.execute(
            text("INSERT INTO exchange_accounts(id,venue,label) VALUES (:id,'bitfinex','ledger')"),
            {"id": _A},
        )
    entered = asyncio.Event()
    release = asyncio.Event()

    async def holder() -> None:
        async with factory.begin() as session:
            await JOURNAL.begin_query(session, SCOPE, 1)
            entered.set()
            await release.wait()

    async def bumper() -> int:
        async with factory.begin() as session:
            return await JOURNAL.bump_clock(session, SCOPE)

    try:
        task1 = asyncio.create_task(holder())
        await entered.wait()
        task2 = asyncio.create_task(bumper())
        await asyncio.sleep(0.2)
        assert not task2.done()
        release.set()
        await task1
        assert await task2 == 1
    finally:
        release.set()
        await engine.dispose()
