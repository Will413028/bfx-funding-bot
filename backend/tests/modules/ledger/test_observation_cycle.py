"""Cycle ordering and transaction fences without DB or venue I/O."""
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from sqlalchemy import text

from bfx_funding_bot.modules.ledger import (
    RUNTIME_GRACE_MS,
    Acceptance,
    CycleResult,
    ObservationWindow,
    QueryAdmissionRefused,
    QueryHandle,
    Scope,
)
from bfx_funding_bot.modules.ledger._internal import clock, journal, resolver
from bfx_funding_bot.modules.ledger.wiring import build_observation_sink

SCOPE = Scope(uuid4(), "ci")


class Factory:
    def __init__(self):
        self.active = None
        self.commits = 0
        self.events = []

    @asynccontextmanager
    async def begin(self):
        assert self.active is None
        self.active = object()
        self.events.append("begin")
        try:
            yield self.active
        except BaseException:
            self.events.append("rollback")
            raise
        else:
            self.commits += 1
            self.events.append("commit")
        finally:
            self.active = None


def cycle(monkeypatch, *, decision="accepted", refuse=False, grace=120_000, resolved=(), settle=120_000):
    factory = Factory()
    handle = QueryHandle(uuid4(), 1, 0, 300_000)
    window = ObservationWindow(100_000, 200_000, 40_000)
    observation_id = uuid4() if decision == "accepted" else None

    async def lock(session, scope):
        assert session is factory.active and scope == SCOPE
        factory.events.append("lock")

    async def dangling(session, scope, *, now_ms, grace_ms):
        assert session is factory.active and scope == SCOPE
        assert now_ms == 300_000 and grace_ms == grace
        factory.events.append("dangling")
        return ()

    async def get_window(session, scope):
        assert session is factory.active
        factory.events.append("window")
        return window

    async def begin(session, scope, started):
        assert factory.events[-2:] == ["dangling", "window"]
        assert session is factory.active and started == 300_000
        factory.events.append("query")
        if refuse:
            raise QueryAdmissionRefused("young attempt")
        return handle

    first, confirmation = object(), object()

    async def observe(scope, started, anchors):
        assert factory.active is None
        assert factory.commits == 1 and factory.events[-1] == "commit"
        assert (scope, started, anchors) == (SCOPE, handle.started_at_ms, window)
        factory.events.append("observe")
        return first, confirmation, 300_002

    async def accept(session, scope, query, a, b, started):
        assert session is factory.active and factory.commits == 1
        assert factory.events[-2:] == ["begin", "lock"]
        assert (scope, query, a, b, started) == (SCOPE, handle, first, confirmation, 300_002)
        factory.events.append("accept")
        return Acceptance(decision, observation_id, None, None)

    async def resolve(session, scope, accepted_id, *, now_ms, settle_ms):
        assert session is factory.active and scope == SCOPE
        assert (accepted_id, now_ms, settle_ms) == (observation_id, 300_000, settle)
        factory.events.append("resolve")
        return tuple(SimpleNamespace(id=item) for item in resolved)

    monkeypatch.setattr(clock, "lock_scope", lock)
    monkeypatch.setattr(resolver, "resolve_unknowns", resolve)
    sink = build_observation_sink(
        factory, SimpleNamespace(observe=observe),
        journal_port=SimpleNamespace(close_dangling=dangling),
        observations=SimpleNamespace(observation_window=get_window, begin_query=begin, accept=accept),
        now_ms=lambda: 300_000, grace_ms=grace, settle_ms=settle,
    )
    return sink, factory, observation_id


@pytest.mark.asyncio
async def test_dangling_before_query_committed_before_io_accept_in_txn2(monkeypatch):
    sink, factory, observation_id = cycle(monkeypatch)
    assert await sink.run(SCOPE) == CycleResult("accepted", observation_id)
    assert factory.events == [
        "begin", "lock", "dangling", "window", "query", "commit", "observe",
        "begin", "lock", "accept", "resolve", "commit",
    ]
    assert factory.active is None and factory.commits == 2


@pytest.mark.asyncio
async def test_resolutions_are_returned_for_post_commit_notices(monkeypatch):
    ids = (uuid4(), uuid4())
    sink, _, observation_id = cycle(monkeypatch, resolved=ids, settle=7_000)
    assert await sink.run(SCOPE) == CycleResult("accepted", observation_id, ids)


@pytest.mark.parametrize("decision", ["fenced", "incomplete_or_unequal"])
@pytest.mark.asyncio
async def test_acceptance_refusals_commit_and_return(monkeypatch, decision):
    sink, factory, _ = cycle(monkeypatch, decision=decision)
    assert await sink.run(SCOPE) == CycleResult(decision)
    assert factory.commits == 2
    assert "resolve" not in factory.events  # only an accepted observation resolves


@pytest.mark.asyncio
async def test_query_refusal_commits_dangling_without_observe(monkeypatch):
    sink, factory, _ = cycle(monkeypatch, refuse=True)
    assert await sink.run(SCOPE) == CycleResult("query_admission_refused")
    assert factory.events == ["begin", "lock", "dangling", "window", "query", "commit"]


@pytest.mark.asyncio
async def test_cycle_passes_configured_dangling_grace(monkeypatch):
    sink, _, _ = cycle(monkeypatch, grace=99_000)
    await sink.run(SCOPE)


@pytest.mark.asyncio
async def test_close_dangling_respects_grace_boundary(sqlite_session, monkeypatch):
    await sqlite_session.execute(text(
        "CREATE TABLE submission_attempt_journal (attempt_id VARCHAR(32), attempt_seq INT, "
        "exchange_account_id VARCHAR(32), deployment_environment TEXT, started_at_ms INT)"
    ))
    await sqlite_session.execute(text(
        "CREATE TABLE transport_outcome_journal (attempt_id VARCHAR(32))"
    ))
    older, boundary, young = uuid4(), uuid4(), uuid4()
    for seq, (attempt_id, started) in enumerate([
        (older, 179_999), (boundary, 180_000), (young, 180_001),
    ]):
        await sqlite_session.execute(text(
            "INSERT INTO submission_attempt_journal VALUES (:id, :seq, :account, 'ci', :start)"
        ), {"id": attempt_id.hex, "seq": seq, "account": SCOPE.exchange_account_id.hex, "start": started})
    monkeypatch.setattr(journal, "lock_scope", AsyncMock())
    record = AsyncMock()
    monkeypatch.setattr(journal, "record_outcome", record)
    assert await journal.close_dangling(sqlite_session, SCOPE, now_ms=300_000, grace_ms=RUNTIME_GRACE_MS) == (older, boundary)
    assert [call.args[2].kind for call in record.await_args_list] == ["unknown", "unknown"]


@pytest.mark.asyncio
async def test_venue_failure_keeps_query_committed_without_open_transaction(monkeypatch):
    sink, factory, _ = cycle(monkeypatch)
    error = ConnectionError("venue unavailable")
    sink._venue.observe = AsyncMock(side_effect=error)
    with pytest.raises(ConnectionError) as caught:
        await sink.run(SCOPE)
    assert caught.value is error
    assert factory.active is None and factory.commits == 1
    assert factory.events[-1] == "commit"


@pytest.mark.asyncio
async def test_accept_failure_rolls_back_only_txn2(monkeypatch):
    sink, factory, _ = cycle(monkeypatch)
    sink._observations.accept = AsyncMock(side_effect=RuntimeError("DB write failed"))
    with pytest.raises(RuntimeError, match="DB write failed"):
        await sink.run(SCOPE)
    assert factory.active is None and factory.commits == 1
    assert factory.events[-1] == "rollback"
