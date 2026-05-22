"""Admin router — POST /admin/smoke-test endpoint tests."""
from __future__ import annotations

from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient

from bfx_funding_bot.modules.admin.router import build_router
from bfx_funding_bot.modules.admin.smoke_runner import SmokeResult


class _FakeSmokeRunner:
    def __init__(
        self,
        l2_result: SmokeResult | None = None,
        l3_result: SmokeResult | None = None,
    ) -> None:
        self.l2_result = l2_result or SmokeResult(
            status="pass", level="L2", checks={}, duration_ms=10,
        )
        self.l3_result = l3_result or SmokeResult(
            status="pass", level="L3", checks={}, duration_ms=20,
        )
        self.l2_calls = 0
        self.l3_calls = 0

    async def run_l2(self) -> SmokeResult:
        self.l2_calls += 1
        return self.l2_result

    async def run_l3(self) -> SmokeResult:
        self.l3_calls += 1
        return self.l3_result


def _app(runner: Any, token: str) -> FastAPI:
    app = FastAPI()
    app.include_router(build_router(smoke_runner=runner, admin_token=token))
    return app


def test_missing_authorization_returns_401() -> None:
    runner = _FakeSmokeRunner()
    client = TestClient(_app(runner, "secret"))
    resp = client.post("/admin/smoke-test")
    assert resp.status_code == 401
    assert resp.json() == {"error": "missing_auth"}
    assert runner.l3_calls == 0


def test_wrong_token_returns_403() -> None:
    runner = _FakeSmokeRunner()
    client = TestClient(_app(runner, "secret"))
    resp = client.post(
        "/admin/smoke-test", headers={"Authorization": "Bearer wrong"},
    )
    assert resp.status_code == 403
    assert resp.json() == {"error": "unauthorized"}


def test_correct_token_runs_l3_by_default() -> None:
    runner = _FakeSmokeRunner()
    client = TestClient(_app(runner, "secret"))
    resp = client.post(
        "/admin/smoke-test", headers={"Authorization": "Bearer secret"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "pass"
    assert body["level"] == "L3"
    assert runner.l3_calls == 1
    assert runner.l2_calls == 0


def test_level_l2_query_param_runs_l2() -> None:
    runner = _FakeSmokeRunner()
    client = TestClient(_app(runner, "secret"))
    resp = client.post(
        "/admin/smoke-test?level=L2",
        headers={"Authorization": "Bearer secret"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["level"] == "L2"
    assert runner.l2_calls == 1
    assert runner.l3_calls == 0


def test_lock_held_returns_409() -> None:
    """Manually hold _SMOKE_LOCK to simulate concurrent smoke."""
    import asyncio

    from bfx_funding_bot.modules.admin import smoke_runner as sr_mod
    runner = _FakeSmokeRunner()
    client = TestClient(_app(runner, "secret"))

    async def hold_lock_briefly() -> None:
        async with sr_mod._SMOKE_LOCK:
            await asyncio.sleep(0.5)

    loop = asyncio.new_event_loop()
    task = loop.create_task(hold_lock_briefly())
    # Drive the loop just enough to acquire the lock
    loop.run_until_complete(asyncio.sleep(0.05))
    try:
        resp = client.post(
            "/admin/smoke-test", headers={"Authorization": "Bearer secret"},
        )
        assert resp.status_code == 409
        assert resp.json() == {"error": "smoke_already_running"}
    finally:
        loop.run_until_complete(task)
        loop.close()


def test_smoke_fail_returns_200_with_status_fail() -> None:
    fail_result = SmokeResult(
        status="fail", level="L2", checks={"events_count": 1},
        duration_ms=5, error="expected 2 events, got 1",
    )
    runner = _FakeSmokeRunner(l2_result=fail_result, l3_result=fail_result)
    client = TestClient(_app(runner, "secret"))
    resp = client.post(
        "/admin/smoke-test", headers={"Authorization": "Bearer secret"},
    )
    assert resp.status_code == 200  # endpoint worked; smoke result in body
    body = resp.json()
    assert body["status"] == "fail"
    assert body["error"]
