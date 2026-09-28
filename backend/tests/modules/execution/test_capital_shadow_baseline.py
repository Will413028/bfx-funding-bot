"""Cursor proof precedes every legacy baseline read."""

from contextlib import nullcontext
from types import SimpleNamespace
from uuid import UUID

import pytest

from bfx_funding_bot.modules.execution.capital_shadow_baseline import read_baseline
from bfx_funding_bot.modules.execution.capital_shadow_port import BaselineNotComparable
from bfx_funding_bot.modules.trading import CapitalScope

SCOPE = CapitalScope(UUID(int=1), "ci", "fUST", "a30")


class CursorSession:
    new = ()
    dirty = ()
    deleted = ()
    no_autoflush = nullcontext()

    def __init__(self, cursor):
        self.cursor = cursor
        self.get_calls = 0

    def in_transaction(self):
        return True

    async def scalar(self, statement):
        return 5

    async def get(self, model, key, **kwargs):
        self.get_calls += 1
        assert self.get_calls == 1, "policy read occurred before cursor gate"
        return None if self.cursor is None else SimpleNamespace(last_event_seq=self.cursor)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("cursor", "reason"),
    [(4, "projection_cursor_lag"), (None, "projection_integrity"), (6, "projection_integrity")],
)
async def test_cursor_gate_prevents_baseline_read(cursor, reason):
    session = CursorSession(cursor)
    result = await read_baseline(
        session, scope=SCOPE, now_ms=2000, max_snapshot_age_ms=10000,
    )
    assert isinstance(result, BaselineNotComparable)
    assert (result.reason, result.projection_cursor, result.watermark) == (reason, cursor, 5)
    assert session.get_calls == 1
