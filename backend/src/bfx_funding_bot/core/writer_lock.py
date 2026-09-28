from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
from typing import cast

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncConnection,
    AsyncEngine,
    AsyncSession,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from bfx_funding_bot.core.account_identity import account_id_canonical
from bfx_funding_bot.core.db import _prepare_engine_kwargs
from bfx_funding_bot.core.errors import WriterLockUnacquired

log = logging.getLogger(__name__)

_NAMESPACE = "bfx-writer"
_TRANSACTION_NAMESPACE = "bfx-writer-xact"


def _derive_lock_key(namespace: str, account_id: str, env: str) -> int:
    digest = hashlib.blake2b(
        f"{namespace}:{account_id}:{env}".encode(), digest_size=8
    ).digest()
    return int.from_bytes(digest, "big", signed=True)


def derive_lock_key(account_id: str, env: str) -> int:
    """Deterministic, process-independent signed int64 advisory-lock key.

    NOTE: never use builtin hash() — it is salted per process (PYTHONHASHSEED).
    """
    return _derive_lock_key(_NAMESPACE, account_id, env)


def derive_transaction_lock_key(account_id: str, env: str) -> int:
    """Derive the transaction-lock key in a namespace distinct from WriterLock.

    The daemon-lifetime lock lives on a dedicated connection.  Reusing its key
    for ``pg_advisory_xact_lock`` would make every event transaction wait on the
    daemon's own session lock; a separate namespace composes both guards while
    preserving the same canonical account/environment identity.
    """
    return _derive_lock_key(_TRANSACTION_NAMESPACE, account_id, env)


async def acquire_transaction_lock(
    session: AsyncSession,
    *,
    account_id: str,
    deployment_environment: str,
) -> int | None:
    """Acquire the account/environment transaction advisory lock.

    PostgreSQL owns the real serialization boundary.  SQLite has no advisory
    lock primitive and is intentionally a pure/unit-test fallback; production
    callers use canonical UUID strings before reaching this helper.
    """
    canonical_account_id = account_id_canonical(account_id)
    bind = session.bind
    if bind is None or bind.dialect.name != "postgresql":
        return None
    key = derive_transaction_lock_key(canonical_account_id, deployment_environment)
    await session.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": key})
    return key


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
        # asyncpg connections are NOT safe for concurrent ops. Serialize every
        # access to self._conn / self._engine so a background refresh() tick can
        # never overlap a per-submit verify_held() (which would raise
        # InterfaceError and fail the safety guard closed). Non-reentrant: public
        # methods hold it; internal helpers (_acquire_locked/_verify_held_locked/
        # _close) run UNLOCKED and are only called from already-locked contexts.
        self._conn_lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._conn_lock:
            await self._acquire_locked()

    async def _acquire_locked(self) -> None:
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
        async with self._conn_lock:
            return await self._verify_held_locked()

    async def _verify_held_locked(self) -> bool:
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
        async with self._conn_lock:
            if await self._verify_held_locked():
                return True
            await self._close()
            try:
                await self._acquire_locked()
                log.warning("writer_lock_reacquired key=%s", self._key)
                return True
            except WriterLockUnacquired:
                # Another live writer grabbed the lock — single-writer contention.
                log.warning("writer_lock_reacquire_failed_contended key=%s", self._key)
                return False
            except Exception:
                log.error("writer_lock_reacquire_failed key=%s", self._key)
                return False

    async def release(self) -> None:
        async with self._conn_lock:
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
