"""Admin FastAPI router — smoke-test, trading-status, dry-evaluate.

Mounted by healthz.make_app() whenever `admin_token` is set and at least one
feature dependency is provided. Each route group is mounted independently:
`smoke_runner` gates POST /admin/smoke-test, `trading_status` gates GET
/admin/trading-status and POST /admin/dry-evaluate. A route whose dependency is
missing is absent (404) rather than present-but-broken.

Auth: static `Authorization: Bearer <BFX_ADMIN_TOKEN>` header check on every
route. trading-status exposes real-money balances and positions, and
dry-evaluate runs the guard chain on demand — neither is public.

Single-flight (smoke only): pre-check module-level _SMOKE_LOCK; 409 if held.
"""
from __future__ import annotations

import logging
from dataclasses import asdict
from typing import Any, Literal, Protocol

from fastapi import APIRouter, Header, Query, Request, status
from fastapi.responses import JSONResponse

from bfx_funding_bot.modules.admin import smoke_runner as sr_mod
from bfx_funding_bot.modules.admin.smoke_runner import SmokeRunner

log = logging.getLogger(__name__)


class _TradingStatusProtocol(Protocol):
    async def snapshot(self) -> dict[str, Any]: ...
    async def dry_run(
        self, *, symbol: str | None = None, amount: float | None = None,
        rate: float | None = None, period_days: int | None = None,
    ) -> dict[str, Any]: ...


def _auth_error(authorization: str | None, admin_token: str) -> JSONResponse | None:
    """Shared Bearer check. Returns a response to send, or None when authorized."""
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
    return None


def build_router(
    *,
    smoke_runner: SmokeRunner | None,
    admin_token: str,
    trading_status: _TradingStatusProtocol | None = None,
) -> APIRouter:
    router = APIRouter(prefix="/admin", tags=["admin"])

    if smoke_runner is not None:
        @router.post("/smoke-test")
        async def smoke_test(
            request: Request,
            level: Literal["L2", "L3"] = Query(default="L3"),
            authorization: str | None = Header(default=None),
        ) -> JSONResponse:
            denied = _auth_error(authorization, admin_token)
            if denied is not None:
                return denied
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

    if trading_status is not None:
        @router.get("/trading-status")
        async def trading_status_endpoint(
            authorization: str | None = Header(default=None),
        ) -> JSONResponse:
            """Effective trading state: halt verdict, installed guards, which
            config tier binds each cap, funds, and the last submit attempt.

            Exists because every observable this daemon had was an INPUT, so
            "did the pause take effect?" could only be answered by re-reading
            the thing we had just written (2026-07-27).
            """
            denied = _auth_error(authorization, admin_token)
            if denied is not None:
                return denied
            return JSONResponse(status_code=200, content=await trading_status.snapshot())

        @router.post("/dry-evaluate")
        async def dry_evaluate_endpoint(
            symbol: str | None = Query(default=None),
            amount: float | None = Query(default=None),
            rate: float | None = Query(default=None),
            period_days: int | None = Query(default=None),
            authorization: str | None = Header(default=None),
        ) -> JSONResponse:
            """Run a synthetic POST through the real guard chain; submit nothing.

            POST rather than GET because it does real work (every guard is
            evaluated, including a live writer-lock check), not because it
            mutates anything — it does not.
            """
            denied = _auth_error(authorization, admin_token)
            if denied is not None:
                return denied
            try:
                result = await trading_status.dry_run(
                    symbol=symbol, amount=amount, rate=rate, period_days=period_days,
                )
            except ValueError as exc:
                return JSONResponse(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    content={"error": str(exc)},
                )
            return JSONResponse(status_code=200, content=result)

    return router
