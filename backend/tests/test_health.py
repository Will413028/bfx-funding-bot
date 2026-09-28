from fastapi.testclient import TestClient

from bfx_funding_bot.apps.webapi import app


def test_health_returns_200() -> None:
    with TestClient(app) as client:
        resp = client.get("/health")
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok"}
