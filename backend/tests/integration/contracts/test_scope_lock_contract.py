"""``ScopeLock``: the lock serializes two sessions of one scope."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from uuid import uuid4

import pytest

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

WAIT_S = 0.5


async def test_a_held_lock_makes_a_second_session_wait_until_the_first_ends(port_stack) -> None:
    await port_stack.policy()
    await port_stack.snapshot("1000")
    async with port_stack.factory() as first, port_stack.factory() as second:
        await first.begin()
        await port_stack.lock.lock(first, port_stack.scope)
        await second.begin()
        waiting = asyncio.ensure_future(port_stack.lock.lock(second, port_stack.scope))
        try:
            done, _ = await asyncio.wait({waiting}, timeout=WAIT_S)
            assert not done, "the second session took a lock the first still holds"
            await first.commit()
            await asyncio.wait_for(waiting, timeout=10)
        finally:
            waiting.cancel()
            await second.rollback()


async def test_the_lock_is_reentrant_within_one_transaction(port_stack) -> None:
    await port_stack.policy()
    await port_stack.snapshot("1000")
    async with port_stack.factory.begin() as session:
        await port_stack.lock.lock(session, port_stack.scope)
        await asyncio.wait_for(port_stack.lock.lock(session, port_stack.scope), timeout=5)


async def test_another_scope_is_not_serialized_with_this_one(port_stack) -> None:
    await port_stack.policy()
    await port_stack.snapshot("1000")
    other = replace(port_stack.scope, exchange_account_id=uuid4())
    async with port_stack.factory() as first, port_stack.factory() as second:
        await first.begin()
        await port_stack.lock.lock(first, port_stack.scope)
        await second.begin()
        await asyncio.wait_for(port_stack.lock.lock(second, other), timeout=5)
        await second.rollback()
        await first.rollback()
