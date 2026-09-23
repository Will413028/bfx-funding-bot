"""Authenticated account adapter for the daemon's canonical read-only status.

No budget calculation or execution endpoint exists in this adapter. The internal
admin credential stays server-side; user mutations use durable release sessions.
"""
from __future__ import annotations

import os
from collections.abc import Awaitable, Callable
from typing import Any

import httpx
from fastapi import APIRouter, Depends, HTTPException

from bfx_funding_bot.modules.api.account_scope import ExchangeAccountContext, require_account_member
from bfx_funding_bot.modules.api.ratelimit import shared_rate_limit_dependency


async def _read_daemon(path: str) -> dict[str, Any]:
    token = os.environ.get("BFX_ADMIN_TOKEN")
    if not token:
        raise RuntimeError("daemon_status_unconfigured")
    async with httpx.AsyncClient(base_url="http://bfx-bot:8080", timeout=15,
                                follow_redirects=False, trust_env=False) as client:
        response = await client.request("GET" if path.endswith("trading-status") else "POST",
            path, headers={"Authorization": f"Bearer {token}"})
        response.raise_for_status()
        value: dict[str, Any] = response.json()
        return value


def build_funding_status_router(*,
    reader: Callable[[str], Awaitable[dict[str, Any]]] = _read_daemon,
) -> APIRouter:
    router = APIRouter(prefix="/api/v1/exchange-accounts/{exchange_account_id}",
        tags=["funding"], dependencies=[Depends(shared_rate_limit_dependency())])

    @router.get("/funding-status")
    async def funding_status(
        context: ExchangeAccountContext = Depends(require_account_member),  # noqa: B008
    ) -> dict[str, Any]:
        def require_scope(value: dict[str, Any]) -> None:
            if (value.get("account_id") != str(context.exchange_account_id)
                    or value.get("deployment_environment") != context.deployment_environment):
                raise RuntimeError("daemon_scope_mismatch")

        try:
            snapshot = await reader("/admin/trading-status")
            require_scope(snapshot)
            dry_run = await reader("/admin/dry-evaluate")
            require_scope(dry_run)
            return {"data": {**snapshot, "dry_run": dry_run}}
        except Exception:
            # Never return internal URLs, credentials, a different account, or cached amounts.
            raise HTTPException(status_code=503, detail="funding_status_unavailable") from None

    return router
