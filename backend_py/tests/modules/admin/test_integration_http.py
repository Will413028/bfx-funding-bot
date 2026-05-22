"""HTTP integration: build_router + healthz.make_app + real SmokeRunner."""
from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi.testclient import TestClient

from bfx_funding_bot.modules.admin.smoke_runner import SmokeRunner
from bfx_funding_bot.modules.execution.axiom_sink import AxiomEventSink
from bfx_funding_bot.modules.execution.bus import DomainEventBus
from bfx_funding_bot.modules.execution.events import (
    OrderFilled,
    ReservationClaimed,
)
from bfx_funding_bot.modules.execution.middleware import (
    ReservationEmittingMiddleware,
)
from bfx_funding_bot.modules.execution.paper import EchoPaperExecutor
from bfx_funding_bot.modules.marketfeed.health_monitor import HealthProbe
from bfx_funding_bot.modules.marketfeed.healthz import make_app
from bfx_funding_bot.modules.marketfeed.schemas import (
    EventType,
    Phase,
    StrategyName,
)


class _FakeAxiomClient:
    def __init__(self) -> None:
        self.emits: list[dict[str, Any]] = []

    async def emit(self, event: dict[str, Any]) -> None:
        self.emits.append(event)


class _FakeAxiomQuery:
    """Returns one ReservationClaimed + one ORDER_FILL row immediately."""

    async def query_order_events(
        self, account_id: str, since: datetime,
    ) -> list[dict[str, Any]]:
        return [
            {
                "_time": datetime.now().isoformat(),
                "event_type": EventType.RESERVATION_CLAIMED.value,
                "account_id": account_id,
                "payload": {"size_usdt": 1.0},
            },
            {
                "_time": datetime.now().isoformat(),
                "event_type": EventType.ORDER_FILL.value,
                "account_id": account_id,
                "payload": {"fill_size_usdt": 1.0},
            },
        ]


def _build_app(token: str = "secret") -> tuple[Any, SmokeRunner]:
    bus = DomainEventBus()
    axiom = _FakeAxiomClient()
    sink = AxiomEventSink(
        axiom_client=axiom, phase=Phase.PAPER,
        strategy=StrategyName.RATE_PERCENTILE, cell="bfx_USDT",
    )
    bus.subscribe(ReservationClaimed, sink.on_reservation_claimed)
    bus.subscribe(OrderFilled, sink.on_order_filled)

    paper = EchoPaperExecutor(
        axiom=axiom, phase=Phase.PAPER,
        strategy=StrategyName.RATE_PERCENTILE, cell="bfx_USDT",
    )
    wrapped = ReservationEmittingMiddleware(paper, bus=bus)
    runner = SmokeRunner(
        executor=wrapped, bus=bus, axiom_client=axiom,
        axiom_query=_FakeAxiomQuery(),
        phase=Phase.PAPER,
        strategy=StrategyName.RATE_PERCENTILE,
        cell="bfx_USDT",
    )
    probe = HealthProbe()
    app = make_app(probe, smoke_runner=runner, admin_token=token)
    return app, runner


def test_http_smoke_l3_pass_end_to_end() -> None:
    app, _ = _build_app()
    client = TestClient(app)
    resp = client.post(
        "/admin/smoke-test", headers={"Authorization": "Bearer secret"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "pass"
    assert body["level"] == "L3"
    assert body["checks"]["l2_passed"] is True
    assert body["checks"]["axiom_events_seen"] >= 2


def test_http_smoke_l2_query_param() -> None:
    app, _ = _build_app()
    client = TestClient(app)
    resp = client.post(
        "/admin/smoke-test?level=L2",
        headers={"Authorization": "Bearer secret"},
    )
    assert resp.status_code == 200
    assert resp.json()["level"] == "L2"


def test_http_no_token_means_admin_router_not_mounted() -> None:
    """make_app(admin_token=None) -> POST /admin/smoke-test should 404."""
    bus = DomainEventBus()
    axiom = _FakeAxiomClient()
    paper = EchoPaperExecutor(
        axiom=axiom, phase=Phase.PAPER,
        strategy=StrategyName.RATE_PERCENTILE, cell="bfx_USDT",
    )
    wrapped = ReservationEmittingMiddleware(paper, bus=bus)
    runner = SmokeRunner(
        executor=wrapped, bus=bus, axiom_client=axiom,
        axiom_query=_FakeAxiomQuery(),
        phase=Phase.PAPER,
        strategy=StrategyName.RATE_PERCENTILE,
        cell="bfx_USDT",
    )
    probe = HealthProbe()
    app = make_app(probe, smoke_runner=runner, admin_token=None)
    client = TestClient(app)
    resp = client.post(
        "/admin/smoke-test", headers={"Authorization": "Bearer anything"},
    )
    assert resp.status_code == 404


def test_http_healthz_still_works_alongside_admin() -> None:
    """Regression: mounting admin router doesn't break /healthz."""
    app, _ = _build_app()
    client = TestClient(app)
    # No sub-tasks registered -> 503 starting
    resp = client.get("/healthz")
    assert resp.status_code == 503
    assert resp.json()["status"] == "starting"
