"""FastAPI dependencies for the web-API: per-request DB session bound to the
app's session_factory, and a per-request Bitfinex auth REST client. Both are
overridden in tests via app.dependency_overrides."""
from __future__ import annotations

import asyncio
import math
import os
from collections.abc import AsyncIterator

import httpx
from fastapi import HTTPException, Request, status
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from bfx_funding_bot.external.bitfinex.auth_rest import BitfinexAuthREST

MAX_READINESS_TIMEOUT_SECONDS = 10.0


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    factory = getattr(request.app.state, "session_factory", None)
    if factory is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="db_not_configured"
        )
    async with factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


def _readiness_timeout_seconds() -> float:
    """Return a finite, positive, bounded readiness timeout from configuration."""
    try:
        timeout_seconds = float(os.environ.get("BFX_READINESS_TIMEOUT_SECONDS", "2.0"))
    except ValueError:
        return 2.0
    if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
        return 2.0
    return min(timeout_seconds, MAX_READINESS_TIMEOUT_SECONDS)


async def database_is_ready(request: Request) -> bool:
    """Probe the configured database without changing application data."""
    factory = getattr(request.app.state, "session_factory", None)
    if factory is None:
        return False

    try:
        async with asyncio.timeout(_readiness_timeout_seconds()):
            async with factory() as session:
                await session.execute(text("SELECT 1"))
    except (SQLAlchemyError, TimeoutError, OSError):
        return False
    return True


async def get_bitfinex_auth_rest() -> AsyncIterator[BitfinexAuthREST]:
    async with httpx.AsyncClient() as http:
        yield BitfinexAuthREST(http=http)
