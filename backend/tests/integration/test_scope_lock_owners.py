"""Every writer of account money state waits behind the ledger's scope lock (ADR 2026-10-19 D4).

Each test holds the scope lock the ledger itself takes (``ScopeLock``, the lock the
journal, the authorize path and the policy store are written under) in one
transaction, starts another writer, and proves it is parked on that same advisory
lock until the first transaction ends. A writer taking a different key, or none,
never shows up waiting and the test times out.

Mutations (one at a time; revert after each): make ``OperatorRequestWorker._lock`` a
no-op (``test_an_operator_request_settles_only_under_the_scope_lock``), drop
``scope_lock.lock`` in ``amend_capital_policy``
(``test_an_owner_amendment_waits_for_the_account_writer``).
The trading-state transition has its own: ``test_halt_writer_waits_for_authorization_lock``.
"""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from decimal import Decimal
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import insert, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bfx_funding_bot.apps.bot_ports import build_policy_ports
from bfx_funding_bot.modules.execution.safety.tables import TradingControlRequestRow
from bfx_funding_bot.modules.execution.trading_control import TradingControlWorker
from bfx_funding_bot.modules.ledger import Scope
from bfx_funding_bot.modules.ledger.capital_amendment import PolicyChanges, amend_capital_policy
from bfx_funding_bot.modules.ledger.wiring import build_policy_store, build_scope_lock
from bfx_funding_bot.modules.trading import CapitalPolicy
from tests.async_wait import until

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

ENV = "ci"


async def _parked_on_an_advisory_lock(factory: async_sessionmaker[AsyncSession]) -> bool:
    async with factory() as observer:
        return bool(await observer.scalar(text(
            "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() "
            "AND wait_event_type = 'Lock' AND query LIKE 'SELECT pg_advisory_xact_lock%'"
        )))


async def _waits_behind_the_scope_lock(
    factory: async_sessionmaker[AsyncSession], scope: Scope,
    writer: Callable[[], Awaitable[Any]],
) -> Any:
    """Run ``writer`` while the scope lock is held; it must wait, then finish."""
    task: asyncio.Task[Any] | None = None
    try:
        async with factory.begin() as holder:
            await build_scope_lock().lock(holder, scope)
            task = asyncio.create_task(writer())
            await until(lambda: _parked_on_an_advisory_lock(factory),
                        what="the writer to wait on the scope lock")
            assert not task.done()
        return await task
    finally:
        if task is not None and not task.done():
            task.cancel()


async def _seed_policy(factory, scope: Scope) -> None:
    async with factory.begin() as session:
        await build_policy_store(scope).apply_policy(
            session, symbol="fUST", policy=CapitalPolicy(enabled=True, max_offer_amount=Decimal("200")),
            expected_revision=0, source={"test": True})


async def test_an_operator_request_settles_only_under_the_scope_lock(migrated_db) -> None:
    """A resume: its apply appends the transition with no lock of its own, so only the
    base worker's ``_lock`` can make it wait (a capital request would also wait inside
    ``amend_capital_policy`` and could not pin ``_lock``)."""
    factory, account = migrated_db
    scope = Scope(account)
    request_id = uuid4()
    async with factory.begin() as session:
        await session.execute(insert(TradingControlRequestRow).values(
            request_id=request_id, exchange_account_id=account, action="resume", reason="pin", requested_by="operator", created_at_ms=1))

    async def allow(session, *, account_id, user) -> bool:
        return True

    worker = TradingControlWorker(
        session_factory=factory, account_id=account, authority=allow,
        scope_lock=build_scope_lock(), clock=lambda: 2)
    assert await _waits_behind_the_scope_lock(
        factory, scope, lambda: worker.process(request_id)) == "applied"


async def test_an_owner_amendment_waits_for_the_account_writer(migrated_db) -> None:
    """The owner's ``amend_capital_policy`` script and the bot's authorize take one lock."""
    factory, account = migrated_db
    scope = Scope(account)
    await _seed_policy(factory, scope)
    policy = build_policy_ports(scope)  # what scripts/amend_capital_policy.py builds

    async def amend() -> dict[str, Any]:
        async with factory.begin() as session:
            return await amend_capital_policy(
                session, store=policy.store, scope_lock=policy.scope_lock, symbol="fUST",
                changes=PolicyChanges(enabled=False), apply_digest=None, realm=None)

    report = await _waits_behind_the_scope_lock(factory, scope, amend)
    assert report["status"] == "dry_run"
