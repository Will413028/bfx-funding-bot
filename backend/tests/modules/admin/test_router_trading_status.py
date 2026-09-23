"""Admin router — GET /admin/trading-status and POST /admin/dry-evaluate.

Both endpoints sit behind the same static Bearer token as /admin/smoke-test.
Auth is retested here rather than assumed: these expose real-money position and
balance figures, and /dry-evaluate runs the guard chain on demand.
"""
from __future__ import annotations

from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient

from bfx_funding_bot.modules.admin.router import build_router


class _FakeStatus:
    def __init__(self) -> None:
        self.snapshot_calls = 0
        self.dry_runs: list[dict[str, Any]] = []

    async def snapshot(self) -> dict[str, Any]:
        self.snapshot_calls += 1
        return {"halt": {"halted": True, "reason": "kill switch", "guard_installed": True}}

    async def dry_run(
        self, *, symbol: str | None = None, amount: float | None = None,
        rate: float | None = None, period_days: int | None = None,
    ) -> dict[str, Any]:
        self.dry_runs.append(
            {"symbol": symbol, "amount": amount, "rate": rate, "period_days": period_days},
        )
        return {"would_submit": False, "blocked_by": "manual_kill", "guards": []}


class _RaisingStatus(_FakeStatus):
    async def dry_run(self, **kwargs: Any) -> dict[str, Any]:
        raise ValueError("symbol 'fBTC' is not configured")


def _app(status: Any, token: str = "secret") -> FastAPI:
    app = FastAPI()
    app.include_router(build_router(
        smoke_runner=None, admin_token=token, trading_status=status,
    ))
    return app


# ------------------------------------------------------------------ auth


def test_trading_status_requires_authorization() -> None:
    status = _FakeStatus()
    resp = TestClient(_app(status)).get("/admin/trading-status")
    assert resp.status_code == 401
    assert status.snapshot_calls == 0


def test_trading_status_rejects_a_wrong_token() -> None:
    status = _FakeStatus()
    resp = TestClient(_app(status)).get(
        "/admin/trading-status", headers={"Authorization": "Bearer wrong"},
    )
    assert resp.status_code == 403
    assert status.snapshot_calls == 0


def test_dry_evaluate_requires_authorization() -> None:
    status = _FakeStatus()
    resp = TestClient(_app(status)).post("/admin/dry-evaluate")
    assert resp.status_code == 401
    assert status.dry_runs == []


# --------------------------------------------------------------- happy path


def test_trading_status_returns_the_snapshot() -> None:
    status = _FakeStatus()
    resp = TestClient(_app(status)).get(
        "/admin/trading-status", headers={"Authorization": "Bearer secret"},
    )
    assert resp.status_code == 200
    assert resp.json()["halt"]["halted"] is True
    assert status.snapshot_calls == 1


def test_dry_evaluate_runs_the_probe_with_defaults() -> None:
    status = _FakeStatus()
    resp = TestClient(_app(status)).post(
        "/admin/dry-evaluate", headers={"Authorization": "Bearer secret"},
    )
    assert resp.status_code == 200
    assert resp.json()["blocked_by"] == "manual_kill"
    assert status.dry_runs == [
        {"symbol": None, "amount": None, "rate": None, "period_days": None},
    ]


def test_dry_evaluate_passes_through_query_overrides() -> None:
    status = _FakeStatus()
    resp = TestClient(_app(status)).post(
        "/admin/dry-evaluate?symbol=fUST&amount=250&rate=0.0002&period_days=7",
        headers={"Authorization": "Bearer secret"},
    )
    assert resp.status_code == 200
    assert status.dry_runs == [
        {"symbol": "fUST", "amount": 250.0, "rate": 0.0002, "period_days": 7},
    ]


def test_dry_evaluate_returns_400_for_an_unconfigured_symbol() -> None:
    resp = TestClient(_app(_RaisingStatus())).post(
        "/admin/dry-evaluate?symbol=fBTC", headers={"Authorization": "Bearer secret"},
    )
    assert resp.status_code == 400
    assert "fBTC" in resp.json()["error"]


# ------------------------------------------------------- optional mounting


def test_endpoints_are_absent_when_no_status_service_is_wired() -> None:
    """Paper/shadow builds without the service must not 500 — the routes simply
    do not exist there."""
    app = FastAPI()
    app.include_router(build_router(
        smoke_runner=None, admin_token="secret", trading_status=None,
    ))
    client = TestClient(app)
    assert client.get(
        "/admin/trading-status", headers={"Authorization": "Bearer secret"},
    ).status_code == 404
    assert client.post(
        "/admin/dry-evaluate", headers={"Authorization": "Bearer secret"},
    ).status_code == 404


def test_smoke_test_route_is_absent_when_no_runner_is_wired() -> None:
    """The router now serves two independent feature sets; wiring only the
    status service must not silently expose a broken smoke-test route."""
    client = TestClient(_app(_FakeStatus()))
    assert client.post(
        "/admin/smoke-test", headers={"Authorization": "Bearer secret"},
    ).status_code == 404


# ------------------------------------------------------- halt / resume (P2)


class _HaltableStatus(_FakeStatus):
    def __init__(self) -> None:
        super().__init__()
        self.halts: list[dict[str, Any]] = []
        self.resumes: list[dict[str, Any]] = []

    async def halt(self, *, reason: str, actor: str, renew: bool = False) -> dict[str, Any]:
        self.halts.append({"reason": reason, "actor": actor, "renew": renew})
        return {"halted": True, "reason": reason, "actor": actor}

    async def resume(self, *, reason: str, actor: str) -> dict[str, Any]:
        self.resumes.append({"reason": reason, "actor": actor})
        return {"halted": False, "reason": reason, "actor": actor}


class _UnconfiguredHaltStatus(_FakeStatus):
    async def halt(self, **kwargs: Any) -> dict[str, Any]:
        raise ValueError("persisted halt is not configured for this daemon")

    async def resume(self, **kwargs: Any) -> dict[str, Any]:
        raise ValueError("persisted halt is not configured for this daemon")


def test_halt_requires_authorization() -> None:
    status = _HaltableStatus()
    resp = TestClient(_app(status)).post("/admin/halt?reason=x")
    assert resp.status_code == 401
    assert status.halts == []


def test_halt_records_the_reason_and_actor() -> None:
    status = _HaltableStatus()
    resp = TestClient(_app(status)).post(
        "/admin/halt?reason=candle+distortion&actor=will",
        headers={"Authorization": "Bearer secret"},
    )
    assert resp.status_code == 200
    # renew defaults off: an ordinary halt must never advance the epoch.
    assert status.halts == [
        {"reason": "candle distortion", "actor": "will", "renew": False}
    ]


def test_halt_requires_a_reason() -> None:
    """An unexplained halt is the thing nobody can safely undo later."""
    status = _HaltableStatus()
    resp = TestClient(_app(status)).post(
        "/admin/halt", headers={"Authorization": "Bearer secret"},
    )
    assert resp.status_code == 422
    assert status.halts == []


def test_resume_refuses_without_explicit_confirmation() -> None:
    """Resuming restarts REAL-MONEY lending. It must not be one stray curl."""
    status = _HaltableStatus()
    resp = TestClient(_app(status)).post(
        "/admin/resume?reason=verified", headers={"Authorization": "Bearer secret"},
    )
    assert resp.status_code == 400
    assert "confirm" in resp.json()["error"]
    assert status.resumes == []


def test_resume_proceeds_with_confirmation() -> None:
    status = _HaltableStatus()
    resp = TestClient(_app(status)).post(
        "/admin/resume?reason=L4+v2+passed&actor=will&confirm=true",
        headers={"Authorization": "Bearer secret"},
    )
    assert resp.status_code == 200
    assert resp.json()["halted"] is False
    assert status.resumes == [{"reason": "L4 v2 passed", "actor": "will"}]


def test_halt_returns_400_when_no_store_is_configured() -> None:
    resp = TestClient(_app(_UnconfiguredHaltStatus())).post(
        "/admin/halt?reason=x", headers={"Authorization": "Bearer secret"},
    )
    assert resp.status_code == 400
    assert "not configured" in resp.json()["error"]


def test_actor_defaults_to_something_identifiable() -> None:
    status = _HaltableStatus()
    TestClient(_app(status)).post(
        "/admin/halt?reason=x", headers={"Authorization": "Bearer secret"},
    )
    assert status.halts[0]["actor"]  # non-empty
