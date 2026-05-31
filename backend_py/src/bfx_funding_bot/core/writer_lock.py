from __future__ import annotations

import contextlib
import hashlib
import logging
from typing import cast

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

from bfx_funding_bot.core.db import _prepare_engine_kwargs
from bfx_funding_bot.core.errors import WriterLockUnacquired

log = logging.getLogger(__name__)

_NAMESPACE = "bfx-writer"


def derive_lock_key(account_id: str, env: str) -> int:
    """Deterministic, process-independent signed int64 advisory-lock key.

    NOTE: never use builtin hash() — it is salted per process (PYTHONHASHSEED).
    """
    digest = hashlib.blake2b(
        f"{_NAMESPACE}:{account_id}:{env}".encode(), digest_size=8
    ).digest()
    return int.from_bytes(digest, "big", signed=True)


class WriterLock:
    """Holds a session-scoped pg advisory lock on a DEDICATED NullPool connection.

    The connection is never recycled, so the session — and thus the lock —
    survives for the daemon lifetime. ``verify_held()`` is the authoritative
    per-submit check.
    """

    def __init__(self, *, database_url: str, key: int) -> None:
        self._database_url = database_url
        self._key = key
        self._engine: AsyncEngine | None = None
        self._conn: AsyncConnection | None = None

    async def acquire(self) -> None:
        kwargs = _prepare_engine_kwargs(self._database_url)
        url = cast(str, kwargs["url"])
        connect_args = cast("dict[str, object]", kwargs.get("connect_args", {}))
        self._engine = create_async_engine(
            url, poolclass=NullPool, connect_args=connect_args
        )
        self._conn = await self._engine.connect()
        got = (
            await self._conn.execute(
                text("SELECT pg_try_advisory_lock(:k)"), {"k": self._key}
            )
        ).scalar()
        if not got:
            await self._close()
            raise WriterLockUnacquired(
                f"another live writer holds advisory lock key={self._key}"
            )
        log.info("writer_lock_acquired key=%s", self._key)

    async def verify_held(self) -> bool:
        """Live check: a successful query proves the session (and its session-scoped
        advisory lock) is still alive. Any failure ⇒ not held ⇒ caller fails closed.
        """
        if self._conn is None:
            return False
        try:
            await self._conn.execute(text("SELECT 1"))
            return True
        except Exception:  # any connection error means lock lost
            log.error("writer_lock_verify_failed key=%s", self._key)
            return False

    async def refresh(self) -> bool:
        """Background recovery: if the lock was lost, drop the dead conn and try to
        re-acquire. Returns the resulting held state.
        """
        if await self.verify_held():
            return True
        await self._close()
        try:
            await self.acquire()
            log.warning("writer_lock_reacquired key=%s", self._key)
            return True
        except WriterLockUnacquired:
            return False
        except Exception:
            return False

    async def release(self) -> None:
        await self._close()

    async def _close(self) -> None:
        if self._conn is not None:
            with contextlib.suppress(Exception):
                await self._conn.close()
            self._conn = None
        if self._engine is not None:
            with contextlib.suppress(Exception):
                await self._engine.dispose()
            self._engine = None
