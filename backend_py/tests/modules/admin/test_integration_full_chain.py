"""Full chain integration: SmokeRunner end-to-end with real bus + middleware
+ prod_ledger. Verifies (1) prod ledger untouched, (2) smoke passes via PG path.

Note: NOT marked pytest.mark.integration — runs without network
(no network calls / pure in-process). Lives in tests/modules/admin/ for
default test run inclusion.
"""
from __future__ import annotations

from typing import Any

from bfx_funding_bot.modules.admin.smoke_runner import (
    SmokeRunner,
)
from bfx_funding_bot.modules.execution.bus import DomainEventBus
from bfx_funding_bot.modules.execution.event_store.persister import NoopEventPersister
from bfx_funding_bot.modules.execution.events import (
    OrderFilled,
    ReservationClaimed,
    ReservationReleased,
)
from bfx_funding_bot.modules.execution.ledger import PaperPositionLedger
from bfx_funding_bot.modules.execution.middleware import (
    ReservationEmittingMiddleware,
)
from bfx_funding_bot.modules.execution.paper import EchoPaperExecutor
from bfx_funding_bot.modules.marketfeed.schemas import (
    Phase,
    StrategyName,
)


class _FakeEventSink:
    def __init__(self) -> None:
        self.emits: list[dict[str, Any]] = []

    async def emit(self, event: dict[str, Any]) -> None:
        self.emits.append(event)


class _StubEventLogQuery:
    async def query_order_events(
        self, account_id: str, since: Any,
    ) -> list[dict[str, Any]]:
        return []


async def test_smoke_does_not_touch_prod_ledger() -> None:
    """Invariant I1: smoke pollutes 0 prod state.

    Focuses on prod-ledger isolation; EchoPaperExecutor emit behavior is
    covered separately in test_paper.py.
    """
    bus = DomainEventBus()
    fake_sink = _FakeEventSink()
    prod_ledger = PaperPositionLedger(account_id="default")
    bus.subscribe(ReservationClaimed, prod_ledger.on_reservation_claimed)
    bus.subscribe(OrderFilled, prod_ledger.on_order_filled)
    bus.subscribe(ReservationReleased, prod_ledger.on_reservation_released)

    paper = EchoPaperExecutor(
        event_sink=fake_sink, phase=Phase.PAPER,
        strategy=StrategyName.RATE_PERCENTILE, cell="bfx_USDT",
    )
    wrapped = ReservationEmittingMiddleware(paper, bus=bus, persister=NoopEventPersister())
    runner = SmokeRunner(
        executor=wrapped, bus=bus,
        pg_query=_StubEventLogQuery(),
        phase=Phase.PAPER,
        strategy=StrategyName.RATE_PERCENTILE,
        cell="bfx_USDT",
    )

    # Invariant: smoke touches 0 prod state — assert the whole-ledger
    # cross-symbol total is unchanged (explicit helper; no per-symbol intent).
    before_total = prod_ledger.total_exposure_all_symbols()

    result = await runner.run_l2()

    assert result.status == "pass"
    assert prod_ledger.total_exposure_all_symbols() == before_total
    assert prod_ledger.replay_floor_hit_count == 0


