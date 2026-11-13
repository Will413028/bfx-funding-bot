"""The per-account transaction advisory lock: the primitive under the ledger's ``ScopeLock``.

Every write of an account's money state (journal, observation acceptance, venue mirror,
trading state, request outcomes, capital policy) happens under this lock, so one
account has one writer at a time. Only ``modules.ledger._internal.clock`` may import
this module (import-linter ``xact-lock-owner``, ADR 2026-10-19 D4); everything else
takes the lock through the ledger's ``ScopeLock``, so the owner of the key stays one.
"""

from __future__ import annotations

import hashlib

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from bfx_funding_bot.core.account_identity import account_id_canonical

_NAMESPACE = "bfx-writer-xact"


def derive_transaction_lock_key(account_id: str) -> int:
    """The scope's signed int64 advisory key, in a namespace distinct from ``WriterLock``.

    The daemon-lifetime lock (``core.writer_lock``) lives on a dedicated connection.
    Reusing its key for ``pg_advisory_xact_lock`` would make every event transaction
    wait on the daemon's own session lock; a separate namespace composes both guards
    while preserving the same canonical account identity. The key carries no
    deployment environment: a database holds one realm, and advisory locks are
    per database. Never builtin ``hash()``: it is salted per process.
    """
    digest = hashlib.blake2b(f"{_NAMESPACE}:{account_id}".encode(), digest_size=8).digest()
    return int.from_bytes(digest, "big", signed=True)


async def acquire_transaction_lock(
    session: AsyncSession,
    *,
    account_id: str,
) -> int | None:
    """Acquire the account's transaction advisory lock.

    PostgreSQL owns the real serialization boundary.  SQLite has no advisory
    lock primitive and is intentionally a pure/unit-test fallback; production
    callers use canonical UUID strings before reaching this helper.
    """
    canonical_account_id = account_id_canonical(account_id)
    bind = session.bind
    if bind is None or bind.dialect.name != "postgresql":
        return None
    key = derive_transaction_lock_key(canonical_account_id)
    await session.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": key})
    return key
