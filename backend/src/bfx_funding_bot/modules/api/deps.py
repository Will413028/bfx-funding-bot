"""FastAPI dependencies for the web-API: per-request DB session bound to the
app's session_factory, and a per-request Bitfinex auth REST client. Both are
overridden in tests via app.dependency_overrides."""
from __future__ import annotations

import asyncio
import math
import os
from collections.abc import AsyncIterator
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import httpx
from fastapi import HTTPException, Request, status
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from bfx_funding_bot.external.bitfinex.auth_rest import BitfinexAuthREST
from bfx_funding_bot.modules.ledger import OperatorEvidence, OperatorReads

MAX_READINESS_TIMEOUT_SECONDS = 10.0


@lru_cache(maxsize=1)
def _expected_alembic_heads() -> frozenset[str]:
    """Resolve the migration heads shipped in this image.

    The probe must not carry a second, hand-maintained revision constant: the
    Alembic graph beside the application is the schema contract.  A missing
    or unreadable graph returns an empty set so readiness fails closed.
    """
    try:
        from alembic.config import Config
        from alembic.script import ScriptDirectory

        alembic_ini = Path(__file__).resolve().parents[4] / "alembic.ini"
        if not alembic_ini.is_file():
            return frozenset()
        script = ScriptDirectory.from_config(Config(str(alembic_ini)))
        return frozenset(script.get_heads())
    except Exception:
        return frozenset()


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


@dataclass(frozen=True, slots=True)
class ReadModels:
    """The read-side ports of the capital authority this process booted under."""

    operator_reads: OperatorReads
    operator_evidence: OperatorEvidence


async def get_read_models(request: Request) -> ReadModels:
    models: ReadModels | None = getattr(request.app.state, "read_models", None)
    if models is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="db_not_configured"
        )
    return models


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
                result = await session.execute(
                    text("SELECT version_num FROM alembic_version")
                )
                versions = {str(version) for version in result.scalars().all()}
                expected_heads = _expected_alembic_heads()
                if not expected_heads or versions != expected_heads:
                    return False
                # The authority is read once at boot: a database that has since
                # switched epoch must not keep serving the old read models.
                booted = getattr(request.app.state, "authority", None)
                if booted is not None:
                    latest = await session.execute(
                        text(
                            "SELECT authority FROM capital_authority_epoch "
                            "ORDER BY epoch_seq DESC LIMIT 1"
                        )
                    )
                    if latest.scalars().all() != [booted]:
                        return False
    # Readiness is a fail-closed gate.  This also covers malformed driver
    # results and an unreadable migration graph without turning /ready into a
    # 500 that a deployment health check could mistake for an app crash.
    except Exception:
        return False
    return True


async def get_bitfinex_auth_rest() -> AsyncIterator[BitfinexAuthREST]:
    async with httpx.AsyncClient() as http:
        yield BitfinexAuthREST(http=http)
