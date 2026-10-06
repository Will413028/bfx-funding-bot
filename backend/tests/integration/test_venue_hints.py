"""Zero-write ledger hints on PostgreSQL.

Mutation: a ledger sink INSERT into event_log must fail
test_ledger_hint_leaves_all_table_counts_unchanged.
"""

import pytest
from sqlalchemy import func, select

from bfx_funding_bot.core.db import Base
from bfx_funding_bot.modules.execution.event_store import (
    tables as event_store_tables,  # noqa: F401 - register the frozen legacy tables
)
from bfx_funding_bot.modules.ledger import (
    tables as ledger_tables,  # noqa: F401 - register all ledger tables
)
from bfx_funding_bot.modules.ledger.wiring import build_venue_hint_sink
from tests.modules.ledger.test_venue_hints import KINDS, SCOPE, send

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", KINDS)
async def test_ledger_hint_leaves_all_table_counts_unchanged(pg_session_factory, kind):
    async def counts():
        async with pg_session_factory() as session:
            return {
                table.name: await session.scalar(select(func.count()).select_from(table))
                for table in Base.metadata.sorted_tables
            }

    before = await counts()
    assert "event_log" in before
    assert "submission_attempt_journal" in before
    now = [0.0]
    requested = []
    published = []

    class Bus:
        async def publish(self, event):
            # Every publication observes exactly the original row counts too.
            assert await counts() == before
            published.append(event)

    sink = build_venue_hint_sink(
        scope=SCOPE, request_resync=requested.append, bus=Bus(), monotonic=lambda: now[0],
    )
    await send(sink, kind)
    now[0] = 4.999
    await send(sink, kind)
    assert len(requested) == 1
    assert len(published) == 2
    assert await counts() == before
