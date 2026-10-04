"""GET /metrics on the healthz HTTP server — Prometheus text exposition.

Additive to /healthz: the route is mounted only when a DaemonMetrics is
provided (back-compat default keeps the pre-metrics app shape), and /healthz
semantics are untouched either way.
"""
from __future__ import annotations

from datetime import UTC, datetime

from fastapi.testclient import TestClient

from bfx_funding_bot.core.health import HealthProbe
from bfx_funding_bot.modules.marketfeed.healthz import make_app
from bfx_funding_bot.modules.observability.metrics import DaemonMetrics


def _fresh_probe() -> HealthProbe:
    probe = HealthProbe()
    probe.last_active_ts["ws"] = datetime.now(UTC)
    return probe


def test_metrics_endpoint_returns_prometheus_text() -> None:
    probe = _fresh_probe()
    metrics = DaemonMetrics()
    metrics.register_probe(probe)
    metrics.set_daemon_info(
        service_version="abc1234", deployment_environment="ci", phase="shadow",
    )
    metrics.observe_operational_event({"event_type": "signal", "level": "info"})

    client = TestClient(make_app(probe, metrics=metrics))
    resp = client.get("/metrics")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/plain")
    body = resp.text
    assert 'bfx_operational_events_total{event_type="signal",level="info"} 1.0' in body
    assert 'bfx_subtask_heartbeat_age_seconds{sub_task="ws"}' in body
    assert 'bfx_daemon_info{' in body


def test_metrics_endpoint_absent_without_metrics() -> None:
    """Default make_app(probe) keeps the pre-metrics surface: no /metrics."""
    client = TestClient(make_app(_fresh_probe()))
    assert client.get("/metrics").status_code == 404


def test_healthz_unchanged_with_metrics_mounted() -> None:
    probe = _fresh_probe()
    client = TestClient(make_app(probe, metrics=DaemonMetrics()))
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"
