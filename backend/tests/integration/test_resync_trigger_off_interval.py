"""Integration: a resync trigger (as auth_ws fires on reconnect / seq-gap) makes
PeriodicReconcile run a ledger cycle OFF the interval -- far sooner than the timer.
The real ledger cycle over a fake venue, on migrated PostgreSQL.
"""
from __future__ import annotations

import pytest

from bfx_funding_bot.modules.execution.periodic_reconcile import PeriodicReconcile
from bfx_funding_bot.modules.execution.resync_channel import ResyncChannel
from bfx_funding_bot.modules.ledger import OfferHistory
from tests.async_wait import running, until, yield_loop

from .test_ledger_capital_reader import book  # noqa: F401 - fixture re-export
from .test_ledger_schema_roles import ledger_db  # noqa: F401 - fixture re-export
from .test_ledger_unknown_resolver_pg import CYCLE_1, CYCLE_2, SCOPE, Clock, FakeVenue, cycle
from .test_reconcile_converges_without_ws import (
    CountingSink,
    FakeProbe,
    cell_exposure,
    placed_and_live,
)

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


async def test_resync_request_reconciles_off_interval(book) -> None:  # noqa: F811
    clock = Clock()
    live = await placed_and_live(book, clock)
    gone = FakeVenue(clock, past=(OfferHistory(live, "canceled", CYCLE_1 + 5_000),))
    sink = CountingSink(cycle(book, gone, clock))
    clock.now = CYCLE_2
    # Huge interval: only an off-interval resync can produce a second cycle.
    reconcile = PeriodicReconcile(
        resync=ResyncChannel(), recovery=sink, scope=SCOPE, probe=FakeProbe(),
        interval_s=3600.0, max_consecutive_failures=3, min_resync_interval_s=0.0,
    )
    async with running(reconcile.run_loop):
        # Tick 1 (loop start) only; the hour-long interval means nothing else can tick
        # before the trigger.
        await until(lambda: len(sink.results) == 1, what="tick 1")
        await yield_loop(50)
        assert len(sink.results) == 1
        assert await cell_exposure(book, clock) == 0
        reconcile.resync.request("reconnect")  # the trigger under test
        await until(lambda: len(sink.results) >= 2, what="the off-interval cycle")
    assert all(result.decision == "accepted" for result in sink.results)
