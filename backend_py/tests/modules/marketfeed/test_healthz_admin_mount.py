"""healthz.make_app — admin router mounting rules.

The smoke-test and trading-status feature sets mount independently; the token
gates both. A live canary must be able to expose trading-status even where no
smoke runner is wired, and no admin route may ever be reachable without a token.
"""
from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient

from bfx_funding_bot.modules.marketfeed.health_monitor import HealthProbe
from bfx_funding_bot.modules.marketfeed.healthz import make_app


class _FakeStatus:
    async def snapshot(self) -> dict[str, Any]:
        return {"halt": {"halted": False}}

    async def dry_run(self, **kwargs: Any) -> dict[str, Any]:
        return {"would_submit": True, "blocked_by": None, "guards": []}


def test_trading_status_mounts_without_a_smoke_runner() -> None:
    app = make_app(HealthProbe(), admin_token="secret", trading_status=_FakeStatus())
    resp = TestClient(app).get(
        "/admin/trading-status", headers={"Authorization": "Bearer secret"},
    )
    assert resp.status_code == 200


def test_no_admin_routes_without_a_token() -> None:
    """A trading-status endpoint served unauthenticated would publish live
    balances and positions."""
    app = make_app(HealthProbe(), admin_token=None, trading_status=_FakeStatus())
    assert TestClient(app).get("/admin/trading-status").status_code == 404


def test_healthz_still_served_when_no_admin_feature_is_wired() -> None:
    app = make_app(HealthProbe())
    client = TestClient(app)
    assert client.get("/admin/trading-status").status_code == 404
    # /healthz itself must not depend on any admin wiring.
    assert client.get("/healthz").status_code in (200, 503)
