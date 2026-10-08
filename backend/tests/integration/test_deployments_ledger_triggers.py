"""The deployments ledger's triggers on a migrated database: append-only, one release per attempt.

bfx-deploy appends a ``started`` row before it changes containers and a terminal row after;
``deployments_attempt_pairing`` keeps the pair one release, ``deployments_append_only`` and
``deployments_no_truncate`` keep both rows as written.

Mutation checks (one at a time; revert after each):

* ``DROP TRIGGER deployments_attempt_pairing ON deployments``: the restart and the swapped
  release both insert.
* ``DROP TRIGGER deployments_append_only ON deployments``: the UPDATE and the DELETE succeed.
"""
from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

pytestmark = pytest.mark.integration

_REVISION = "a" * 40
_BACKEND = "sha256:" + "b" * 64
_FRONTEND = "sha256:" + "c" * 64


async def _append(session, attempt, outcome: str, *, revision: str = _REVISION,
                  backend: str = _BACKEND) -> None:
    finished = None if outcome == "started" else datetime(2026, 10, 9, 0, 1, tzinfo=UTC)
    await session.execute(text(
        "INSERT INTO deployments (attempt_id, started_at, finished_at, source_revision, "
        "backend_digest, frontend_digest, migrations_applied, outcome, detail) VALUES "
        "(:attempt, :started, :finished, :revision, "
        ":backend, :frontend, false, :outcome, '')"),
        {"attempt": attempt, "started": datetime(2026, 10, 9, tzinfo=UTC), "finished": finished,
         "revision": revision, "backend": backend, "frontend": _FRONTEND, "outcome": outcome})


async def _refused(factory, statement, match: str) -> None:
    with pytest.raises(DBAPIError, match=match):
        async with factory.begin() as session:
            await statement(session)


@pytest.mark.asyncio
async def test_an_attempt_records_one_release_from_start_to_end(migrated_db) -> None:
    factory, _ = migrated_db
    attempt = uuid4()
    async with factory.begin() as session:
        await _append(session, attempt, "started")
        await _append(session, attempt, "deployed")

    await _refused(factory, lambda s: _append(s, attempt, "started"), "already recorded")
    other = uuid4()
    async with factory.begin() as session:
        await _append(session, other, "started")
    for changed in ({"revision": "d" * 40}, {"backend": "sha256:" + "e" * 64}):
        await _refused(factory, lambda s, c=changed: _append(s, other, "failed", **c),
                       "finished as a different release")
    # An attempt that stopped before changing containers has only its terminal row.
    async with factory.begin() as session:
        await _append(session, uuid4(), "failed")
        await _append(session, other, "rolled_back")


@pytest.mark.asyncio
async def test_recorded_attempts_are_never_rewritten(migrated_db) -> None:
    factory, _ = migrated_db
    async with factory.begin() as session:
        await _append(session, uuid4(), "failed")
    for statement in ("UPDATE deployments SET detail = 'rewritten'", "DELETE FROM deployments",
                      "TRUNCATE deployments"):
        await _refused(factory, lambda s, q=statement: s.execute(text(q)), "append-only")
    async with factory() as session:
        assert (await session.execute(text("SELECT detail FROM deployments"))).scalar_one() == ""
