"""healthz.make_app — admin router mounting rules.

The admin routes (trading-status, dry-evaluate, halt) mount only with a token and a
status service, and no admin route may ever be reachable without a token.
"""
from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient

from bfx_funding_bot.core.health import HealthProbe
from bfx_funding_bot.modules.marketfeed.healthz import make_app


class _FakeStatus:
    async def snapshot(self) -> dict[str, Any]:
        return {"halt": {"halted": False}}

    async def dry_run(self, **kwargs: Any) -> dict[str, Any]:
        return {"would_submit": True, "blocked_by": None, "guards": []}


def test_trading_status_mounts_with_a_token() -> None:
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


def test_healthz_is_unaffected_by_the_admin_router() -> None:
    """Regression: mounting the admin router doesn't break /healthz."""
    client = TestClient(make_app(HealthProbe(), admin_token="secret", trading_status=_FakeStatus()))
    resp = client.get("/healthz")
    assert resp.status_code == 503
    assert resp.json()["status"] == "starting"


def test_the_read_token_reaches_the_router() -> None:
    app = make_app(HealthProbe(), admin_token="secret", read_token="read-only",
                   trading_status=_FakeStatus())
    resp = TestClient(app).get(
        "/admin/trading-status", headers={"Authorization": "Bearer read-only"},
    )
    assert resp.status_code == 200


async def test_the_daemon_passes_its_read_token_to_the_server(monkeypatch) -> None:
    """The bot → Daemon → run_healthz_server link: a dropped kwarg would leave the web API
    locked out while every router test stays green."""
    import asyncio
    from types import SimpleNamespace

    from bfx_funding_bot.modules.marketfeed import daemon as daemon_module

    seen: dict[str, Any] = {}

    async def fake_server(**kwargs: Any) -> None:
        seen.update(kwargs)

    monkeypatch.setattr(daemon_module, "run_healthz_server", fake_server)
    fake = SimpleNamespace(
        probe=HealthProbe(), healthz_host="127.0.0.1", healthz_port=0,
        _stop_event=asyncio.Event(), admin_token="secret", admin_read_token="read-only",
        metrics=None, trading_status=None, trading_readiness=None)
    await daemon_module.Daemon._healthz_server_loop(fake)
    assert (seen["admin_token"], seen["read_token"]) == ("secret", "read-only")


def test_a_read_token_equal_to_the_admin_token_is_dropped() -> None:
    from bfx_funding_bot.apps.bot import admin_tokens

    assert admin_tokens({"BFX_ADMIN_TOKEN": "a", "BFX_ADMIN_READ_TOKEN": "r"}) == ("a", "r")
    assert admin_tokens({"BFX_ADMIN_TOKEN": "a", "BFX_ADMIN_READ_TOKEN": "a"}) == ("a", None)
    assert admin_tokens({"BFX_ADMIN_TOKEN": " ", "BFX_ADMIN_READ_TOKEN": ""}) == (None, None)
