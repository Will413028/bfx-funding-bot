"""Real-Postgres integration: the daemon's fail-closed WriterLockGuard.

The guard's authoritative per-submit gate is `WriterLock.verify_held()` against
a live advisory-lock session. When the lock is lost (connection dropped /
released), the guard must refuse the real-money submit (fail closed).

The refresh/re-acquire-after-transient-loss path is covered by
`tests/integration/test_writer_lock.py::test_refresh_reacquires_after_transient_loss`
on the WriterLock itself — not duplicated here.
"""
from __future__ import annotations

from uuid import uuid4

import pytest

from bfx_funding_bot.core.writer_lock import WriterLock, derive_lock_key
from bfx_funding_bot.modules.execution.safety.hard_guards import WriterLockGuard

# Reuse the tiny decision/ctx builders from the unit guard tests.
from tests.modules.execution.safety.test_hard_guards import _ctx, _post

pytestmark = pytest.mark.integration


def _url(pg_engine) -> str:
    return pg_engine.url.render_as_string(hide_password=False)


@pytest.mark.asyncio
async def test_guard_fails_closed_when_lock_lost(pg_engine) -> None:
    lock = WriterLock(
        database_url=_url(pg_engine),
        key=derive_lock_key(f"d-{uuid4().hex[:8]}", "prod"),
    )
    await lock.acquire()
    guard = WriterLockGuard(lock=lock)

    # held -> allowed
    ok = await guard.evaluate(_post(), _ctx())
    assert ok.allowed is True

    # lose the lock (release the underlying session) -> guard now fails closed
    await lock.release()
    blocked = await guard.evaluate(_post(), _ctx())
    assert blocked.allowed is False
    assert blocked.guard_name == "writer_lock"
