"""HTTP integration: build_router + healthz.make_app + real SmokeRunner."""
from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi.testclient import TestClient

from bfx_funding_bot.modules.admin.smoke_runner import SmokeRunner
from bfx_funding_bot.modules.execution.bus import DomainEventBus
from bfx_funding_bot.modules.execution.event_store.persister import NoopEventPersister
from bfx_funding_bot.modules.execution.middleware import (
    ReservationEmittingMiddleware,
)
from bfx_funding_bot.modules.execution.paper import EchoPaperExecutor
from bfx_funding_bot.modules.marketfeed.health_monitor import HealthProbe
from bfx_funding_bot.modules.marketfeed.healthz import make_app
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
    """Returns RESERVATION_CLAIMED + ORDER_FILL rows immediately (UPPERCASE — PG style)."""

    async def query_order_events(
        self, account_id: str, since: datetime,
    ) -> list[dict[str, Any]]:
        return [
            {
                "event_type": "RESERVATION_CLAIMED",  # UPPERCASE — matches PG event_log
                "account_id": account_id,
                "occurred_at_ms": 1716374400000,
            },
            {
                "event_type": "ORDER_FILL",  # UPPERCASE — matches PG event_log
                "account_id": account_id,
                "occurred_at_ms": 1716374400001,
            },
        ]


def _build_app(token: str = "secret") -> tuple[Any, SmokeRunner]:
    bus = DomainEventBus()
    fake_sink = _FakeEventSink()

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
    probe = HealthProbe()
    app = make_app(probe, smoke_runner=runner, admin_token=token)
    return app, runner


def test_http_smoke_l3_reports_gated_submit_path() -> None:
    app, _ = _build_app()
    client = TestClient(app)
    resp = client.post(
        "/admin/smoke-test", headers={"Authorization": "Bearer secret"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "fail"
    assert body["level"] == "L2"
    assert "audited ReadyToSubmit" in body["error"]


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
    fake_sink = _FakeEventSink()
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
