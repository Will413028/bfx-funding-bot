"""Unit tests for /healthz endpoint logic — independent of HTTP server.

FastAPI TestClient drives the route handler against a HealthProbe with
synthesized last_active_ts values. Confirms the 200/503 branching contract
Koyeb / k8s liveness probes depend on.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient

from bfx_funding_bot.modules.marketfeed.health_monitor import HealthProbe
from bfx_funding_bot.modules.marketfeed.healthz import make_app


def _probe_with(
    fresh: list[str] | None = None,
    stale: list[str] | None = None,
) -> HealthProbe:
    """Build a probe with the named sub-tasks at synthetic ages.

    fresh: heartbeat NOW (well within any threshold).
    stale: heartbeat 10h ago (exceeds every SUB_TASK_THRESHOLDS entry).
    """
    probe = HealthProbe()
    now = datetime.now(UTC)
    for t in fresh or []:
        probe.last_active_ts[t] = now
    for t in stale or []:
        probe.last_active_ts[t] = now - timedelta(hours=10)
    return probe


def test_healthz_returns_200_when_all_sub_tasks_fresh() -> None:
    probe = _probe_with(fresh=["ws", "scheduler", "axiom", "candle_writer"])
    client = TestClient(make_app(probe))
    resp = client.get("/healthz")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["tasks"] == 4


def test_healthz_returns_503_when_any_sub_task_stale() -> None:
    probe = _probe_with(
        fresh=["scheduler", "axiom", "candle_writer"],
        stale=["ws"],
    )
    client = TestClient(make_app(probe))
    resp = client.get("/healthz")
    assert resp.status_code == 503
    body = resp.json()
    assert body["status"] == "degraded"
    assert len(body["stale"]) == 1
    assert body["stale"][0]["task"] == "ws"
    assert body["stale"][0]["age_s"] > body["stale"][0]["threshold_s"]


def test_healthz_returns_503_when_multiple_sub_tasks_stale() -> None:
    probe = _probe_with(
        fresh=["scheduler"],
        stale=["ws", "axiom"],
    )
    client = TestClient(make_app(probe))
    resp = client.get("/healthz")
    assert resp.status_code == 503
    body = resp.json()
    stale_tasks = sorted(s["task"] for s in body["stale"])
    assert stale_tasks == ["axiom", "ws"]


def test_healthz_returns_503_when_no_sub_tasks_registered() -> None:
    """Startup: warmup may not have completed yet → probe.last_active_ts empty.
    Treated as "not ready" — Koyeb should not route traffic / mark healthy."""
    probe = HealthProbe()
    client = TestClient(make_app(probe))
    resp = client.get("/healthz")
    assert resp.status_code == 503
    body = resp.json()
    assert body["status"] == "starting"
    assert body["reason"] == "no_sub_tasks_registered_yet"


def test_healthz_uses_per_task_threshold_from_sub_task_thresholds() -> None:
    """candle_writer has 65*60s threshold (1h+); 30min stale should be FRESH
    for candle_writer but DEGRADED for axiom (60s threshold)."""
    probe = HealthProbe()
    now = datetime.now(UTC)
    probe.last_active_ts["candle_writer"] = now - timedelta(minutes=30)  # ok for 1h+ threshold
    probe.last_active_ts["axiom"] = now - timedelta(minutes=30)          # bad for 60s threshold
    probe.last_active_ts["scheduler"] = now  # fresh

    client = TestClient(make_app(probe))
    resp = client.get("/healthz")
    assert resp.status_code == 503
    stale_tasks = [s["task"] for s in resp.json()["stale"]]
    assert "axiom" in stale_tasks
    assert "candle_writer" not in stale_tasks
