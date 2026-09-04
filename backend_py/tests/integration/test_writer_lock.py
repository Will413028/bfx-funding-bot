from __future__ import annotations

from uuid import uuid4

import pytest

from bfx_funding_bot.core.errors import WriterLockUnacquired
from bfx_funding_bot.core.writer_lock import (
    WriterLock,
    derive_lock_key,
    derive_transaction_lock_key,
)

pytestmark = pytest.mark.integration


def _url(pg_engine) -> str:
    return pg_engine.url.render_as_string(hide_password=False)


async def test_key_is_stable_and_namespaced() -> None:
    k1 = derive_lock_key("acct-A", "prod")
    k2 = derive_lock_key("acct-A", "prod")
    k3 = derive_lock_key("acct-A", "shadow")
    assert k1 == k2 and k1 != k3
    assert -(2**63) <= k1 < 2**63


async def test_transaction_key_is_stable_but_does_not_self_conflict() -> None:
    session_key = derive_lock_key("acct-A", "prod")
    transaction_key = derive_transaction_lock_key("acct-A", "prod")
    assert transaction_key == derive_transaction_lock_key("acct-A", "prod")
    assert transaction_key != session_key
    assert -(2**63) <= transaction_key < 2**63


async def test_acquire_then_second_contends_then_release(pg_engine) -> None:
    acct = f"wl-{uuid4().hex[:8]}"
    key = derive_lock_key(acct, "prod")
    a = WriterLock(database_url=_url(pg_engine), key=key)
    await a.acquire()
    assert await a.verify_held() is True

    b = WriterLock(database_url=_url(pg_engine), key=key)
    with pytest.raises(WriterLockUnacquired):
        await b.acquire()

    await a.release()
    # now b can take it
    await b.acquire()
    assert await b.verify_held() is True
    await b.release()


async def test_verify_held_false_after_release(pg_engine) -> None:
    acct = f"wl-{uuid4().hex[:8]}"
    lock = WriterLock(database_url=_url(pg_engine), key=derive_lock_key(acct, "prod"))
    await lock.acquire()
    await lock.release()
    assert await lock.verify_held() is False


async def test_refresh_reacquires_after_transient_loss(pg_engine) -> None:
    acct = f"wl-{uuid4().hex[:8]}"
    lock = WriterLock(database_url=_url(pg_engine), key=derive_lock_key(acct, "prod"))
    await lock.acquire()
    # Simulate a dropped connection: closing ends the server-side session, which
    # releases the session-scoped advisory lock.
    await lock._close()
    assert await lock.refresh() is True
    assert await lock.verify_held() is True
    await lock.release()
