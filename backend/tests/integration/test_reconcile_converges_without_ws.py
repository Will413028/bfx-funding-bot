"""Integration: the periodic reconcile converges the ledger with the WS stream DEAD.

Regression guard for a real-money incident (2026-05-26 / spec 2026-05-27): an offer was
placed and charged against capital, then it left the venue, and NO WS event was ever
processed for it. The bot stayed pinned at its allocation cap and placed nothing more.

The runtime ``PeriodicReconcile`` backbone is the fix: with no WS event at all, the
periodic ledger observation cycle must see the offer gone and accept a basis that no
longer charges it. Wired end to end on migrated PostgreSQL: the real ledger cycle
(``build_observation_sink``) over a fake venue, driven by the real ``PeriodicReconcile``
loop, with no WS dispatcher.

Mutation: make the fake venue keep reporting the offer live after it was canceled; the
cell exposure stays 150 and the test fails (it is not vacuously passing).
"""
from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest

from bfx_funding_bot.modules.execution.periodic_reconcile import PeriodicReconcile
from bfx_funding_bot.modules.execution.resync_channel import ResyncChannel
from bfx_funding_bot.modules.ledger import CycleResult, ObservationSink, OfferHistory, Scope
from tests.async_wait import running, until

from .test_ledger_capital_reader import (
    Book,
    _view,
    book,  # noqa: F401 - fixture re-export
)
from .test_ledger_schema_roles import ledger_db  # noqa: F401 - fixture re-export
from .test_ledger_unknown_resolver_pg import (
    CYCLE_1,
    CYCLE_2,
    SCOPE,
    Clock,
    FakeVenue,
    cycle,
    run_cycle,
    start,
    venue_offer,
)

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

VOI = "777"
SIZE = "150"
PLACED_AT = 950_000


class FakeProbe:
    """No-op probe (heartbeat + health updates), like the unit tests use."""

    def record_heartbeat(self, _sub_task: str) -> None:
        pass

    def update(self, _target: Any, _status: Any, **_fields: object) -> None:
        pass


class CountingSink:
    """The real ledger sink, recording each completed cycle."""

    def __init__(self, inner: ObservationSink) -> None:
        self._inner = inner
        self.results: list[CycleResult] = []

    async def run(self, scope: Scope) -> CycleResult:
        result = await self._inner.run(scope)
        self.results.append(result)
        return result


async def cell_exposure(book_: Book, clock: Clock) -> Decimal:
    read = await book_.read(now=clock.now, max_age=10**9)
    return Decimal(_view(read).snapshot.cell_exposure)


async def placed_and_live(book_: Book, clock: Clock) -> Any:
    """A placed offer of 150 the venue reports live: the cell is charged 150."""
    await start(book_)
    await book_.attempt(SIZE, outcome="ack", venue_offer_id=VOI,
                        started_at_ms=PLACED_AT, completed_at_ms=PLACED_AT + 100)
    live = venue_offer(VOI, amount=SIZE, created=PLACED_AT + 50)
    assert (await run_cycle(book_, FakeVenue(clock, offers=(live,)), clock, CYCLE_1)).decision \
        == "accepted"
    # Precondition: the cell is charged, else the convergence below proves nothing.
    assert await cell_exposure(book_, clock) == Decimal(SIZE)
    return live


async def test_periodic_reconcile_converges_the_ledger_with_ws_dead(book) -> None:  # noqa: F811
    clock = Clock()
    live = await placed_and_live(book, clock)
    # The offer left the venue (canceled); no WS event says so.
    gone = FakeVenue(clock, past=(OfferHistory(live, "canceled", CYCLE_1 + 5_000),))
    sink = CountingSink(cycle(book, gone, clock))
    clock.now = CYCLE_2
    reconcile = PeriodicReconcile(
        resync=ResyncChannel(), recovery=sink, scope=SCOPE, probe=FakeProbe(), interval_s=0.01,
        max_consecutive_failures=3,
    )
    async with running(reconcile.run_loop):
        await until(lambda: any(r.decision == "accepted" for r in sink.results),
                    what="an accepted periodic cycle")
    # Released purely by the periodic cycle: the cell is no longer charged.
    assert await cell_exposure(book, clock) == Decimal("0")
    assert gone.windows  # the venue was observed by the loop, not by the test
