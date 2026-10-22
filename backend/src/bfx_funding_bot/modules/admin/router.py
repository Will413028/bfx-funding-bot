"""Admin FastAPI router — trading-status, dry-evaluate, halt.

Mounted by healthz.make_app() whenever `admin_token` is set and `trading_status`
is provided; `trading_status` gates GET /admin/trading-status, POST
/admin/dry-evaluate and POST /admin/halt.

Auth: static `Authorization: Bearer <token>` header check on every route,
compared in constant time. The two reads accept the admin token or the read
token (`BFX_ADMIN_READ_TOKEN`, the web API's); halt accepts the admin token
only, so the web API's credential cannot stop trading. trading-status exposes
real-money balances and positions, and dry-evaluate runs the guard chain on
demand — neither is public.
"""
from __future__ import annotations

import hmac
from typing import Any, Protocol

from fastapi import APIRouter, Header, Query, Request, status
from fastapi.responses import JSONResponse

from bfx_funding_bot.core.security_log import ACCESS_DENIED, ADMIN_HALT, clip, security_event


class _TradingStatusProtocol(Protocol):
    async def snapshot(self) -> dict[str, Any]: ...
    async def dry_run(
        self, *, symbol: str | None = None, amount: float | None = None,
        rate: float | None = None, period_days: int | None = None,
    ) -> dict[str, Any]: ...
    async def halt(self, *, reason: str, actor: str) -> dict[str, Any]: ...


def _auth_error(request: Request, authorization: str | None,
                *tokens: str | None) -> JSONResponse | None:
    """Shared Bearer check against any of ``tokens``. Returns a response to send, or None
    when authorized. A refusal is logged as ``access_denied`` with the request line and the
    peer address (the header itself is never logged)."""
    if not authorization:
        return _denied(request, status.HTTP_401_UNAUTHORIZED, "missing_auth")
    scheme, _, token = authorization.partition(" ")
    accepted = any(hmac.compare_digest(token.encode(), expected.encode())
                   for expected in tokens if expected)
    if scheme.lower() != "bearer" or not accepted:
        return _denied(request, status.HTTP_403_FORBIDDEN, "unauthorized")
    return None


def _denied(request: Request, status_code: int, error: str) -> JSONResponse:
    security_event(ACCESS_DENIED, reason=error, status=status_code, method=request.method,
                   path=clip(request.url.path),
                   client=request.client.host if request.client else None)
    return JSONResponse(status_code=status_code, content={"error": error})


def build_router(
    *,
    admin_token: str,
    read_token: str | None = None,
    trading_status: _TradingStatusProtocol | None = None,
) -> APIRouter:
    router = APIRouter(prefix="/admin", tags=["admin"])

    if trading_status is not None:
        @router.get("/trading-status")
        async def trading_status_endpoint(
            request: Request,
            authorization: str | None = Header(default=None),
        ) -> JSONResponse:
            """Effective trading state: halt verdict, installed guards, which
            config tier binds each cap, funds, and the last submit attempt.

            Exists because every observable this daemon had was an INPUT, so
            "did the pause take effect?" could only be answered by re-reading
            the thing we had just written (2026-07-27).
            """
            denied = _auth_error(request, authorization, admin_token, read_token)
            if denied is not None:
                return denied
            return JSONResponse(status_code=200, content=await trading_status.snapshot())

        @router.post("/dry-evaluate")
        async def dry_evaluate_endpoint(
            request: Request,
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
            denied = _auth_error(request, authorization, admin_token, read_token)
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
            request: Request,
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
            denied = _auth_error(request, authorization, admin_token)
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
            # actor and reason come from the query string: structured fields, never part of
            # the message, and JSON-encoded by the formatter (F37: no forged log lines).
            security_event(ADMIN_HALT, reason="halt_requested", actor=clip(actor),
                           halt_reason=clip(reason), cancel_all_complete=complete)
            return JSONResponse(
                status_code=200 if complete else status.HTTP_502_BAD_GATEWAY, content=result,
            )

    return router
