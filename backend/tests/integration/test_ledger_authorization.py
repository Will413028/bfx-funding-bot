"""S1-3b2 CAS and boot closure on a migrated PostgreSQL clone.

Mutations 1-4: test_authorize_refusal_has_no_writes (latest/clock/policy/early INSERT).
Mutation 8: test_close_dangling_grace_scope_idempotence_and_admission.
Mutation 9: that test's exact UNKNOWN outcome assertions.

Parity: legacy test_capital_repository.py:664-692,904-951;
test_capital_command_boundary.py:309-335; test_boot_recovery.py:78-96.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker

from bfx_funding_bot.modules.ledger import (
    Authorized,
    AuthorizeRefused,
    Outcome,
    Quarantine,
    QueryAdmissionRefused,
    encode_basis_token,
)
from bfx_funding_bot.modules.ledger._internal.journal import record_attempt
from bfx_funding_bot.modules.ledger.tables import (
    CapitalCommandClockRow,
    SubmissionAttemptJournalRow,
    TransportOutcomeJournalRow,
)

from .test_ledger_journal import JOURNAL, SCOPE, _attempt, _engine
from .test_ledger_schema_roles import (
    _A,
    _B,
    _P,
    _QID,
    _T,
    ledger_db,  # noqa: F401 - fixture dependency
    seeded,  # noqa: F401 - fixture dependency
)

pytestmark = pytest.mark.integration
TOKEN = encode_basis_token(UUID(_QID), 0)


@pytest_asyncio.fixture
async def factory(seeded):  # noqa: F811
    with seeded.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO capital_policy_heads(exchange_account_id, deployment_environment, "
                "symbol, revision_id, revision) VALUES (:a, 'ci', 'fUST', :p, 1)"
            ),
            {"a": _A, "p": _P},
        )
    engine = _engine(seeded)
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


async def _fresh_attempt(session, *, started_at_ms: int = 0):
    """An attempt on its own decision row (one attempt per decision is UNIQUE)."""
    decision_id = f"auth-decision-{uuid4()}"
    await session.execute(
        text(
            "INSERT INTO execution_decisions(decision_id, account_id, "
            "exchange_account_id, deployment_environment, reconcile_id, cell_id, "
            "symbol, signal_correlation_id, outcome, signal_rate, amount_usdt, "
            "duration_days, model_evidence, safety_result, execution_policy, "
            "service_version, config_hash, occurred_at_ms, recorded_at_ms) VALUES "
            "(:d, 'account', :a, 'ci', 'r', 'cell', 'fUST', :d, 'submitted', 0, "
            "1, 2, '{}', '{}', 'policy', 'test', 'hash', 0, 0)"
        ),
        {"d": decision_id, "a": _A},
    )
    return replace(_attempt(started_at_ms=started_at_ms), execution_decision_id=decision_id)


async def _state(session) -> tuple[int, int, int]:
    return (
        await session.scalar(select(func.count()).select_from(SubmissionAttemptJournalRow)),
        await session.scalar(select(func.count()).select_from(TransportOutcomeJournalRow)),
        await session.scalar(select(CapitalCommandClockRow.revision)),
    )


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ("latest", "capital_snapshot_changed"),
        ("clock", "capital_snapshot_changed"),
        ("policy", "capital_policy_revision_changed"),
        ("missing_policy", "capital_policy_revision_changed"),
        ("basis", "capital_snapshot_changed"),
        ("pending", "query_pending"),
        ("malformed", "capital_snapshot_changed"),
        ("scope", "capital_snapshot_changed"),
    ],
)
@pytest.mark.asyncio
async def test_authorize_refusal_has_no_writes(factory, change: str, reason: str) -> None:
    attempt, token = _attempt(), TOKEN
    async with factory.begin() as session:
        if change in {"latest", "pending"}:
            handle = await JOURNAL.begin_query(session, SCOPE, 10)
            if change == "pending":
                token = encode_basis_token(handle.query_id, handle.start_revision)
        elif change == "clock":
            await JOURNAL.bump_clock(session, SCOPE)
        elif change == "policy":
            newer = uuid4()
            await session.execute(
                text(
                    "INSERT INTO capital_policy_revisions(id, exchange_account_id, "
                    "deployment_environment, symbol, revision, schema_version, policy, digest, "
                    "source) VALUES (:id, :a, 'ci', 'fUST', 2, 1, '{}', 'p', '{}')"
                ),
                {"id": newer, "a": _A},
            )
            await session.execute(
                text(
                    "UPDATE capital_policy_heads SET revision_id=:p, revision=2 WHERE "
                    "exchange_account_id=:a AND deployment_environment='ci' AND symbol='fUST'"
                ),
                {"p": newer, "a": _A},
            )
            assert (await _state(session))[2] == 0  # policy does not move the command clock
        elif change == "missing_policy":
            await session.execute(text("DELETE FROM capital_policy_heads"))
        elif change == "basis":
            attempt = replace(attempt, basis_id=uuid4())
        elif change == "malformed":
            token = "1"
    scope = replace(SCOPE, deployment_environment="other") if change == "scope" else SCOPE
    async with factory.begin() as session:
        before = await _state(session)
        result = await JOURNAL.authorize_attempt(session, scope, attempt, token, now_ms=10)
        assert result == AuthorizeRefused(reason)
        await session.flush()
        assert await session.get(SubmissionAttemptJournalRow, attempt.attempt_id) is None
        assert await _state(session) == before
    async with factory.begin() as session:
        assert await _state(session) == before  # refusal commits no new facts


@pytest.mark.asyncio
async def test_authorize_success_rollback_and_re_read_clock(factory) -> None:
    attempt = _attempt()
    async with factory() as session, session.begin():
        assert isinstance(
            await JOURNAL.authorize_attempt(session, SCOPE, attempt, TOKEN, now_ms=10), Authorized
        )
        assert (await _state(session))[2] == 1
        await session.rollback()
    async with factory.begin() as session:
        assert (await _state(session)) == (1, 1, 0)
        first = await JOURNAL.authorize_attempt(session, SCOPE, attempt, TOKEN, now_ms=10)
        assert isinstance(first, Authorized) and first.attempt_seq == 2
        assert len(first.payload_sha256) == 64
        second = await _fresh_attempt(session)
        assert await JOURNAL.authorize_attempt(
            session, SCOPE, second, TOKEN, now_ms=10
        ) == AuthorizeRefused("capital_snapshot_changed")
        # DB-only read of the same basis with its attempt tail has a new clock token.
        refreshed = encode_basis_token(UUID(_QID), 1)
        assert isinstance(
            await JOURNAL.authorize_attempt(session, SCOPE, second, refreshed, now_ms=10),
            Authorized,
        )
        assert (await _state(session)) == (3, 1, 2)
        assert attempt.basis_id == UUID(_B)


@pytest.mark.asyncio
async def test_contending_authorizations_wait_then_refuse_stale_clock(factory) -> None:
    entered = asyncio.Event()
    pid = None

    async def contender():
        nonlocal pid
        async with factory.begin() as session:
            pid = await session.scalar(text("SELECT pg_backend_pid()"))
            entered.set()
            return await JOURNAL.authorize_attempt(session, SCOPE, _attempt(), TOKEN, now_ms=10)

    async with factory.begin() as first:
        assert isinstance(
            await JOURNAL.authorize_attempt(first, SCOPE, _attempt(), TOKEN, now_ms=10), Authorized
        )
        waiter = asyncio.create_task(contender())
        await entered.wait()
        async with factory() as observer:
            for _ in range(100):
                locked = await observer.scalar(
                    text("SELECT wait_event_type='Lock' FROM pg_stat_activity WHERE pid=:pid"),
                    {"pid": pid},
                )
                await observer.rollback()
                if locked:
                    break
                await asyncio.sleep(0.01)
            else:
                pytest.fail("authorization never waited for the scope lock")
        assert not waiter.done()
    assert await asyncio.wait_for(waiter, 5) == AuthorizeRefused("capital_snapshot_changed")


@pytest.mark.asyncio
async def test_close_dangling_grace_scope_idempotence_and_admission(factory) -> None:
    async with factory.begin() as session:
        old, boundary, recent = [
            await _fresh_attempt(session, started_at_ms=t) for t in (0, 80_000, 80_001)
        ]
        for attempt in (old, boundary, recent):
            await record_attempt(session, SCOPE, attempt)
        before = await _state(session)
        assert (
            await JOURNAL.close_dangling(
                session, replace(SCOPE, deployment_environment="other"), now_ms=200_000
            )
            == ()
        )
        assert await _state(session) == before
    async with factory.begin() as session:
        closed = await JOURNAL.close_dangling(session, SCOPE, now_ms=200_000)
        assert closed == (old.attempt_id, boundary.attempt_id)
        for attempt_id in closed:
            assert await JOURNAL.read_back_outcome(session, attempt_id) == Outcome(
                attempt_id, "unknown", None, "unresolved_at_boot", 200_000, {}
            )
        assert await JOURNAL.read_back_outcome(session, recent.attempt_id) is None
        assert (await _state(session))[2] == before[2] + 2
        after = await _state(session)
        assert await JOURNAL.close_dangling(session, SCOPE, now_ms=200_000) == ()
        assert await _state(session) == after
        with pytest.raises(QueryAdmissionRefused):
            await JOURNAL.begin_query(session, SCOPE, 200_000)
        assert await JOURNAL.close_dangling(session, SCOPE, now_ms=200_000, grace_ms=119_999) == (
            recent.attempt_id,
        )
        assert await JOURNAL.begin_query(session, SCOPE, 200_001)


@pytest.mark.asyncio
async def test_close_dangling_rolls_back_outcome_and_clock(factory) -> None:
    attempt = _attempt()
    async with factory.begin() as session:
        await record_attempt(session, SCOPE, attempt)
        before = await _state(session)
    async with factory.begin() as session:
        assert await JOURNAL.close_dangling(session, SCOPE, now_ms=120_000) == (attempt.attempt_id,)
        await session.rollback()
    async with factory.begin() as session:
        assert await _state(session) == before
        assert await JOURNAL.read_back_outcome(session, attempt.attempt_id) is None


@pytest.mark.parametrize("field", ["account", "environment", "symbol", "missing"])
@pytest.mark.asyncio
async def test_source_quarantine_refuses_mismatch_before_bump(factory, field: str) -> None:
    from decimal import Decimal

    scope = replace(SCOPE, exchange_account_id=uuid4()) if field == "account" else SCOPE
    if field == "environment":
        scope = replace(SCOPE, deployment_environment="other")
    opening = Quarantine(
        uuid4(),
        "fUSD" if field == "symbol" else "fUST",
        Decimal("1"),
        10,
        {},
        source_attempt_id=uuid4() if field == "missing" else UUID(_T),
    )
    async with factory.begin() as session:
        before = await _state(session)
        with pytest.raises(ValueError, match="quarantine source attempt scope mismatch"):
            await JOURNAL.open_quarantine(session, scope, opening)
        assert await _state(session) == before
        assert await session.scalar(select(func.count()).select_from(CapitalCommandClockRow)) == 1
