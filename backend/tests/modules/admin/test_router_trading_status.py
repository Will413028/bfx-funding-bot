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


# ---------------------------------------------------------- halt


class _HaltableStatus(_FakeStatus):
    def __init__(self, *, cancel_all_complete: bool = True) -> None:
        super().__init__()
        self.halts: list[dict[str, Any]] = []
        self.cancel_all_complete = cancel_all_complete

    async def halt(self, *, reason: str, actor: str) -> dict[str, Any]:
        self.halts.append({"reason": reason, "actor": actor})
        return {"halted": True, "state": "HALTED", "reason": reason, "actor": actor,
                "cancel_all_complete": self.cancel_all_complete}


class _UnconfiguredHaltStatus(_FakeStatus):
    async def halt(self, **kwargs: Any) -> dict[str, Any]:
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
    assert status.halts == [{"reason": "candle distortion", "actor": "will"}]


def test_halt_reports_502_while_the_venue_cancel_all_is_incomplete() -> None:
    """HALTED is in force either way; a non-2xx makes an unfinished kill loud."""
    status = _HaltableStatus(cancel_all_complete=False)
    resp = TestClient(_app(status)).post(
        "/admin/halt?reason=venue+incident", headers={"Authorization": "Bearer secret"},
    )
    assert resp.status_code == 502
    assert resp.json()["state"] == "HALTED"
    assert resp.json()["cancel_all_complete"] is False


def test_the_pause_endpoint_is_retired() -> None:
    """REDUCING is gone (lending envelope D4): the everyday stop is the policy's
    enabled flag, the emergency stop is /admin/halt."""
    resp = TestClient(_app(_HaltableStatus())).post(
        "/admin/pause?reason=x", headers={"Authorization": "Bearer secret"})
    assert resp.status_code in (404, 405)


def test_halt_no_longer_renews_a_canary_epoch() -> None:
    """The release ceremony's epoch is not a trading decision; this endpoint
    only changes the trading state and ignores the old parameter."""
    status = _HaltableStatus()
    resp = TestClient(_app(status)).post(
        "/admin/halt?reason=x&renew=true", headers={"Authorization": "Bearer secret"},
    )
    assert resp.status_code == 200
    assert status.halts == [{"reason": "x", "actor": "admin-api"}]


def test_halt_requires_a_reason() -> None:
    """An unexplained halt is the thing nobody can safely undo later."""
    status = _HaltableStatus()
    resp = TestClient(_app(status)).post(
        "/admin/halt", headers={"Authorization": "Bearer secret"},
    )
    assert resp.status_code == 422
    assert status.halts == []


def test_the_static_token_cannot_resume() -> None:
    """Resume needs the operator's TOTP (the web API's trading-control request).
    The static admin token only reduces exposure: halt."""
    resp = TestClient(_app(_HaltableStatus())).post(
        "/admin/resume?reason=x&confirm=true", headers={"Authorization": "Bearer secret"},
    )
    assert resp.status_code in (404, 405)


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
