"""Every bot /admin route refuses a missing or wrong Bearer token before doing any work.

The routes are enumerated from the built router, not listed by hand, so a route added
later is covered without editing this file. Required query parameters are filled in so a
422 from validation cannot stand in for the auth answer.

Mutation check: add an /admin route that skips ``_auth_error`` — it answers 200 here.
"""
from __future__ import annotations

from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.routing import APIRoute, iter_route_contexts
from fastapi.testclient import TestClient

from bfx_funding_bot.modules.admin.router import build_router

TOKEN = "admin-token-for-tests"


class _RecordingStatus:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def snapshot(self) -> dict[str, Any]:
        self.calls.append("snapshot")
        return {}

    async def dry_run(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append("dry_run")
        return {}

    async def halt(self, *, reason: str, actor: str) -> dict[str, Any]:
        self.calls.append("halt")
        return {"cancel_all_complete": True}


def _routes() -> list[tuple[str, str, dict[str, str]]]:
    app = FastAPI()
    app.include_router(build_router(admin_token=TOKEN, trading_status=_RecordingStatus()))
    routes = []
    for context in iter_route_contexts(app.routes):
        route = context.original_route
        if not isinstance(route, APIRoute):
            continue
        required = {param.alias: "x" for param in route.dependant.query_params if param.field_info.is_required()}
        routes.extend((method, route.path, required) for method in sorted(route.methods))
    return routes


ROUTES = _routes()


def test_the_router_exposes_the_known_routes() -> None:
    """Guards the enumeration itself: an empty list would make every case below vacuous."""
    assert {(method, path) for method, path, _ in ROUTES} >= {
        ("GET", "/admin/trading-status"),
        ("POST", "/admin/dry-evaluate"),
        ("POST", "/admin/halt"),
    }


@pytest.mark.parametrize(
    ("headers", "expected"),
    [({}, 401), ({"Authorization": "Bearer wrong"}, 403), ({"Authorization": f"Basic {TOKEN}"}, 403)],
    ids=["missing", "wrong-token", "wrong-scheme"],
)
@pytest.mark.parametrize(("method", "path", "params"), ROUTES, ids=[f"{m} {p}" for m, p, _ in ROUTES])
def test_admin_route_refuses_without_the_token(
    method: str, path: str, params: dict[str, str], headers: dict[str, str], expected: int,
) -> None:
    status = _RecordingStatus()
    app = FastAPI()
    app.include_router(build_router(admin_token=TOKEN, trading_status=status))
    response = TestClient(app).request(method, path, params=params, headers=headers)
    assert response.status_code == expected
    assert status.calls == []


@pytest.mark.parametrize(("method", "path", "params"), ROUTES, ids=[f"{m} {p}" for m, p, _ in ROUTES])
def test_admin_route_admits_the_token(method: str, path: str, params: dict[str, str]) -> None:
    """The refusal cases above are only meaningful if the same request with the token passes."""
    status = _RecordingStatus()
    app = FastAPI()
    app.include_router(build_router(admin_token=TOKEN, trading_status=status))
    response = TestClient(app).request(
        method, path, params=params, headers={"Authorization": f"Bearer {TOKEN}"},
    )
    assert response.status_code == 200
    assert len(status.calls) == 1
