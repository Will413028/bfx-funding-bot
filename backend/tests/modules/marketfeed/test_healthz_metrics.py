"""GET /metrics on the healthz HTTP server — Prometheus text exposition.

Additive to /healthz: the route is mounted only when a DaemonMetrics is
provided (back-compat default keeps the pre-metrics app shape), and /healthz
semantics are untouched either way.
"""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from bfx_funding_bot.core.health import HealthProbe
from bfx_funding_bot.modules.marketfeed.healthz import make_app
from bfx_funding_bot.modules.observability import metrics as metrics_module
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


class _HaltSource:
    def __init__(self, verdict: bool | None = None, *, fail: bool = False,
                 delay_s: float = 0.0) -> None:
        self.verdict, self.fail, self.delay_s, self.calls = verdict, fail, delay_s, 0

    async def halted(self) -> bool | None:
        self.calls += 1
        if self.delay_s:
            await asyncio.sleep(self.delay_s)
        if self.fail:
            raise RuntimeError("trading state read failed")
        return self.verdict

    def metric_scope(self) -> dict[str, str]:
        return {"exchange_account_id": "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee",
                "deployment_environment": "ci"}


_HALTED_SERIES = ('bfx_trading_halted{deployment_environment="ci",'
                  'exchange_account_id="aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"}')


def _scrape(source: _HaltSource) -> str:
    client = TestClient(make_app(
        _fresh_probe(), metrics=DaemonMetrics(),
        trading_status=source,  # type: ignore[arg-type]
    ))
    resp = client.get("/metrics")
    assert resp.status_code == 200
    return resp.text


@pytest.mark.parametrize(("verdict", "value"), [(True, "1.0"), (False, "0.0")])
def test_each_scrape_reports_the_current_halt_verdict(verdict: bool, value: str) -> None:
    source = _HaltSource(verdict)
    assert f"{_HALTED_SERIES} {value}" in _scrape(source)
    assert source.calls == 1


def test_halt_gauge_is_absent_when_no_kill_guard_is_installed() -> None:
    assert "bfx_trading_halted{" not in _scrape(_HaltSource(None))


def test_a_failed_halt_read_still_serves_the_scrape() -> None:
    assert "bfx_trading_halted{" not in _scrape(_HaltSource(fail=True))


def test_a_hung_halt_read_is_cut_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(metrics_module, "_HALT_REFRESH_TIMEOUT_S", 0.05)
    assert "bfx_trading_halted{" not in _scrape(_HaltSource(True, delay_s=5.0))


def test_a_failed_read_keeps_the_last_verdict() -> None:
    metrics = DaemonMetrics()
    asyncio.run(metrics.refresh_trading_halted(_HaltSource(True)))
    asyncio.run(metrics.refresh_trading_halted(_HaltSource(fail=True)))
    assert f"{_HALTED_SERIES} 1.0" in metrics.render().decode()
