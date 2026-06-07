"""FastAPI dependencies for the web-API: per-request DB session bound to the
app's session_factory, and a per-request Bitfinex auth REST client. Both are
overridden in tests via app.dependency_overrides."""
from __future__ import annotations

from collections.abc import AsyncIterator

import httpx
from fastapi import HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from bfx_funding_bot.external.bitfinex.auth_rest import BitfinexAuthREST


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


async def get_bitfinex_auth_rest() -> AsyncIterator[BitfinexAuthREST]:
    async with httpx.AsyncClient() as http:
        yield BitfinexAuthREST(http=http)
