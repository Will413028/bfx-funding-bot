"""Admin FastAPI router — trading-status, dry-evaluate, halt.

Mounted by healthz.make_app() whenever `admin_token` is set and `trading_status`
is provided; `trading_status` gates GET /admin/trading-status, POST
/admin/dry-evaluate and POST /admin/halt.

Auth: static `Authorization: Bearer <BFX_ADMIN_TOKEN>` header check on every
route. trading-status exposes real-money balances and positions, and
dry-evaluate runs the guard chain on demand — neither is public.
"""
from __future__ import annotations

import logging
from typing import Any, Protocol

from fastapi import APIRouter, Header, Query, status
from fastapi.responses import JSONResponse

log = logging.getLogger(__name__)


class _TradingStatusProtocol(Protocol):
    async def snapshot(self) -> dict[str, Any]: ...
    async def dry_run(
        self, *, symbol: str | None = None, amount: float | None = None,
        rate: float | None = None, period_days: int | None = None,
    ) -> dict[str, Any]: ...
    async def halt(self, *, reason: str, actor: str) -> dict[str, Any]: ...


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
    admin_token: str,
    trading_status: _TradingStatusProtocol | None = None,
) -> APIRouter:
    router = APIRouter(prefix="/admin", tags=["admin"])

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

        @router.post("/halt")
        async def halt_endpoint(
            reason: str = Query(min_length=1),
            actor: str = Query(default="admin-api"),
            authorization: str | None = Header(default=None),
        ) -> JSONResponse:
            """Kill switch: HALTED, then cancel every funding offer at the venue.

            `reason` is required — an unexplained stop is the one nobody can
            safely undo six weeks later. 200 means the state is HALTED and every
            currency's cancel-all was acknowledged; 502 means HALTED is in force
            but the venue part did not fully land (see `cancel_all`) — call
            again to retry it."""
            denied = _auth_error(authorization, admin_token)
            if denied is not None:
                return denied
            try:
                result = await trading_status.halt(reason=reason, actor=actor)
            except ValueError as exc:
                return JSONResponse(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    content={"error": str(exc)},
                )
            complete = bool(result.get("cancel_all_complete"))
            log.warning("admin_halt actor=%s reason=%s cancel_all_complete=%s",
                        actor, reason, complete)
            return JSONResponse(
                status_code=200 if complete else status.HTTP_502_BAD_GATEWAY, content=result,
            )

    return router
