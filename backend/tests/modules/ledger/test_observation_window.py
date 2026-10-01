"""Observation anchors preserve the margin and the explicit first-observation case."""

from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from bfx_funding_bot.modules.ledger import LedgerReadUnbounded, ObservationWindow, Scope
from bfx_funding_bot.modules.ledger._internal import reads
from bfx_funding_bot.modules.ledger.tables import SubmissionAttemptJournalRow


@pytest.mark.parametrize(
    ("attempt", "previous", "start"),
    [
        (None, None, None),
        (100_000, None, 40_000),
        (None, 150_000, 90_000),
        (100_000, 150_000, 40_000),
        (200_000, 150_000, 90_000),
        (0, None, -60_000),  # an anchor at zero is not absent; no invented clamp
    ],
)
def test_window_anchor_minimum_and_margin(attempt, previous, start) -> None:
    assert reads._window_from_anchors(attempt, previous) == ObservationWindow(
        attempt, previous, start
    )


@pytest.mark.asyncio
async def test_no_basis_still_reads_the_earliest_attempt(monkeypatch) -> None:
    monkeypatch.setattr(reads, "unresolved_quarantines", AsyncMock(return_value=[]))
    monkeypatch.setattr(reads, "attempts_by_id", AsyncMock(return_value=[]))
    session = AsyncMock()
    scope = Scope(uuid4(), "ci")
    monkeypatch.setattr(reads, "previous_basis", AsyncMock(return_value=None))
    tail = AsyncMock(
        return_value=[
            SubmissionAttemptJournalRow(started_at_ms=200_000),
            SubmissionAttemptJournalRow(started_at_ms=100_000),
        ]
    )
    monkeypatch.setattr(reads, "tail_attempts", tail)
    assert await reads.observation_window(session, scope) == ObservationWindow(
        100_000, None, 40_000
    )
    tail.assert_awaited_once_with(
        session, scope.exchange_account_id, "ci", 0, limit=reads.MAX_TAIL_ATTEMPTS + 1
    )


@pytest.mark.asyncio
async def test_no_basis_tail_cap_fails_closed(monkeypatch) -> None:
    monkeypatch.setattr(reads, "previous_basis", AsyncMock(return_value=None))
    monkeypatch.setattr(reads, "MAX_TAIL_ATTEMPTS", 1)
    monkeypatch.setattr(
        reads,
        "tail_attempts",
        AsyncMock(return_value=[SubmissionAttemptJournalRow(), SubmissionAttemptJournalRow()]),
    )
    with pytest.raises(LedgerReadUnbounded):
        await reads.observation_window(AsyncMock(), Scope(uuid4(), "ci"))


@pytest.mark.asyncio
async def test_window_includes_only_unresolved_r6_source_attempts(monkeypatch) -> None:
    from types import SimpleNamespace

    scope = Scope(uuid4(), "ci")
    source = uuid4()
    basis = SimpleNamespace(observation_id=uuid4())
    monkeypatch.setattr(reads, "_candidates", AsyncMock(
        return_value=reads._Candidates(basis, (), {}, {}),
    ))
    session = AsyncMock()
    session.scalar.return_value = 300_000
    unresolved = AsyncMock(return_value=[
        SimpleNamespace(source_attempt_id=None), SimpleNamespace(source_attempt_id=source),
    ])
    sources = AsyncMock(return_value=[SubmissionAttemptJournalRow(started_at_ms=100_000)])
    monkeypatch.setattr(reads, "unresolved_quarantines", unresolved)
    monkeypatch.setattr(reads, "attempts_by_id", sources)
    assert await reads.observation_window(session, scope) == ObservationWindow(
        100_000, 300_000, 40_000,
    )
    unresolved.assert_awaited_once_with(session, scope, basis)
    sources.assert_awaited_once_with(session, [source])
