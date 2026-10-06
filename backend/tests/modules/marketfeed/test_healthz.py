"""Unit tests for /healthz endpoint logic — independent of HTTP server.

FastAPI TestClient drives the route handler against a HealthProbe with
synthesized last_active_ts values. Confirms the 200/503 branching contract
Koyeb / k8s liveness probes depend on.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient

from bfx_funding_bot.core.health import HealthProbe
from bfx_funding_bot.modules.execution.contracts import BlockReason
from bfx_funding_bot.modules.marketfeed.healthz import make_app
from bfx_funding_bot.modules.marketfeed.readiness import TradingReadiness


def _probe_with(
    fresh: list[str] | None = None,
    stale: list[str] | None = None,
) -> HealthProbe:
    """Build a probe with the named sub-tasks at synthetic ages.

    fresh: heartbeat NOW (well within any threshold).
    stale: heartbeat 10h ago (exceeds every liveness threshold).
    """
    probe = HealthProbe()
    now = datetime.now(UTC)
    for t in fresh or []:
        probe.last_active_ts[t] = now
    for t in stale or []:
        probe.last_active_ts[t] = now - timedelta(hours=10)
    return probe


def test_healthz_returns_200_when_all_sub_tasks_fresh() -> None:
    probe = _probe_with(fresh=["ws", "scheduler", "candle_writer", "periodic_reconcile"])
    client = TestClient(make_app(probe))
    resp = client.get("/healthz")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["tasks"] == 4


def test_healthz_returns_503_when_any_sub_task_stale() -> None:
    probe = _probe_with(
        fresh=["scheduler", "candle_writer"],
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
        stale=["ws", "periodic_reconcile"],
    )
    client = TestClient(make_app(probe))
    resp = client.get("/healthz")
    assert resp.status_code == 503
    body = resp.json()
    stale_tasks = sorted(s["task"] for s in body["stale"])
    assert stale_tasks == ["periodic_reconcile", "ws"]


def test_healthz_returns_503_when_no_liveness_sub_tasks_registered() -> None:
    """Startup: only reactive activity recorded (e.g. boot-smoke bumped
    executor) but no liveness loop ticked yet → not ready, do not mark healthy."""
    probe = HealthProbe()
    probe.last_active_ts["executor"] = datetime.now(UTC)  # activity only
    client = TestClient(make_app(probe))
    resp = client.get("/healthz")
    assert resp.status_code == 503
    body = resp.json()
    assert body["status"] == "starting"
    assert body["reason"] == "no_liveness_sub_tasks_registered_yet"


def test_healthz_uses_per_task_threshold() -> None:
    """candle_writer has 65*60s threshold (1h+); 30min stale is FRESH for it
    but a 90s-threshold task (ws) at 30min is stale."""
    probe = HealthProbe()
    now = datetime.now(UTC)
    probe.last_active_ts["candle_writer"] = now - timedelta(minutes=30)  # ok for 1h+ threshold
    probe.last_active_ts["ws"] = now - timedelta(minutes=30)             # stale for 90s threshold
    probe.last_active_ts["scheduler"] = now                              # fresh
    client = TestClient(make_app(probe))
    resp = client.get("/healthz")
    assert resp.status_code == 503
    stale_tasks = [s["task"] for s in resp.json()["stale"]]
    assert "ws" in stale_tasks
    assert "candle_writer" not in stale_tasks


def test_healthz_ignores_reactive_executor_and_safety_chain() -> None:
    """Regression for the 2026-05-26 canary idle-restart loop: executor/
    safety_chain are reactive activity, not liveness. Even 10h stale they must
    NOT cause /healthz 503 as long as a liveness task is fresh."""
    probe = HealthProbe()
    now = datetime.now(UTC)
    probe.last_active_ts["ws"] = now                               # fresh liveness
    probe.last_active_ts["scheduler"] = now                        # fresh liveness
    probe.last_active_ts["executor"] = now - timedelta(hours=10)   # stale activity
    probe.last_active_ts["safety_chain"] = now - timedelta(hours=10)
    client = TestClient(make_app(probe))
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json()["tasks"] == 2  # only liveness tasks counted


def test_readyz_returns_503_for_business_blocked_state_without_changing_healthz() -> None:
    probe = _probe_with(fresh=["ws"])
    readiness = TradingReadiness()
    readiness.set_blocked(BlockReason.FILL_MODEL_MISSING, "fill_model")
    client = TestClient(make_app(probe, readiness=readiness))

    readyz = client.get("/readyz")
    healthz = client.get("/healthz")

    assert readyz.status_code == 503
    assert readyz.json() == {
        "trading_ready": False,
        "reason": "fill_model_missing",
    }
    assert healthz.status_code == 200


def test_readyz_returns_200_when_trading_is_ready() -> None:
    readiness = TradingReadiness()
    readiness.set_ready()
    client = TestClient(make_app(_probe_with(fresh=["ws"]), readiness=readiness))

    response = client.get("/readyz")

    assert response.status_code == 200
    assert response.json() == {"trading_ready": True, "reason": None}
