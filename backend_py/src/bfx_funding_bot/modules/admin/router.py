"""Admin FastAPI router — POST /admin/smoke-test.

Mounted by healthz.make_app() when both `smoke_runner` and `admin_token` are
provided. If either is None, the router is not built (caller skips mount).

Auth: static `Authorization: Bearer <BFX_ADMIN_TOKEN>` header check.
Single-flight: pre-check module-level _SMOKE_LOCK; 409 if held.
"""
from __future__ import annotations

import logging
from dataclasses import asdict
from typing import Literal

from fastapi import APIRouter, Header, Query, Request, status
from fastapi.responses import JSONResponse

from bfx_funding_bot.modules.admin import smoke_runner as sr_mod
from bfx_funding_bot.modules.admin.smoke_runner import SmokeRunner

log = logging.getLogger(__name__)


def build_router(*, smoke_runner: SmokeRunner, admin_token: str) -> APIRouter:
    router = APIRouter(prefix="/admin", tags=["admin"])

    @router.post("/smoke-test")
    async def smoke_test(
        request: Request,
        level: Literal["L2", "L3"] = Query(default="L3"),
        authorization: str | None = Header(default=None),
    ) -> JSONResponse:
        if not authorization:
            return JSONResponse(
                status_code=status.HTTP_401_UNAUTHORIZED,
                content={"error": "missing_auth"},
            )
        scheme, _, token = authorization.partition(" ")
        if scheme.lower() != "bearer" or token != admin_token:
            return JSONResponse(
                status_code=status.HTTP_403_FORBIDDEN,
                content={"error": "unauthorized"},
            )
        if sr_mod._SMOKE_LOCK.locked():
            return JSONResponse(
                status_code=status.HTTP_409_CONFLICT,
                content={"error": "smoke_already_running"},
            )
        result = await (
            smoke_runner.run_l2() if level == "L2"
            else smoke_runner.run_l3()
        )
        # SmokeResult is frozen dataclass — asdict() yields a JSON-friendly dict.
        return JSONResponse(status_code=200, content=asdict(result))

    return router
