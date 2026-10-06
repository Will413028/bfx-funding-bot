"""``PolicyStore``: read and amend the applied policy on the ledger.

The store never touches the frozen legacy event stream. What a caller observes: the
revision a head names, the refusals, the lost-update guard.

Mutations (one at a time; revert after each): the ledger store skips the head check
(``revision_changed``), enables fUSD, returns a revision other than the one written, or
reads the event stream (``test_the_ledger_store_never_touches_the_event_stream``).
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy import func, select

from bfx_funding_bot.modules.execution.event_store.tables import EventLogRow
from bfx_funding_bot.modules.execution.event_store.writer import AccountEventWriter
from bfx_funding_bot.modules.ledger import PolicyRefused, PolicyStore
from bfx_funding_bot.modules.ledger.wiring import build_policy_store
from bfx_funding_bot.modules.trading import CapitalPolicy

from .stacks import ACCOUNT, ENVIRONMENT, Stack

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


def _store(stack: Stack) -> PolicyStore:
    return build_policy_store(stack.scope)


async def test_a_missing_policy_is_refused_with_the_authoritys_code(port_stack) -> None:
    store = _store(port_stack)
    async with port_stack.factory() as session:
        with pytest.raises(PolicyRefused, match="policy_unavailable"):
            await store.read_applied(session, symbol="fUST")


async def test_apply_then_read_names_the_revision_written(port_stack) -> None:
    store = _store(port_stack)
    assert store.scope == port_stack.scope
    policy = CapitalPolicy(enabled=True, reserve_amount=Decimal("100"))
    async with port_stack.factory.begin() as session:
        written = await store.apply_policy(
            session, symbol="fUST", policy=policy, expected_revision=0, source={"by": "contract"})
    assert (written.revision, written.symbol, written.policy) == (1, "fUST", policy)
    assert (written.account_id, written.environment) == (ACCOUNT, ENVIRONMENT)
    async with port_stack.factory() as session:
        read = await store.read_applied(session, symbol="fUST")
    assert read == written
    # The stack's own capital authority reads the same head.
    async with port_stack.factory() as session:
        seen = await port_stack.capital.read_policy(session, port_stack.scope, "fUST")
    assert seen == written


async def test_a_lost_update_and_an_unsupported_policy_are_refused(port_stack) -> None:
    store = _store(port_stack)
    async with port_stack.factory.begin() as session:
        await store.apply_policy(session, symbol="fUST", policy=CapitalPolicy(enabled=True),
                                 expected_revision=0, source={})
    for symbol, policy, expected, code in (
        ("fUST", CapitalPolicy(enabled=False), 0, "revision_changed"),
        ("fUST", CapitalPolicy(enabled=False), 2, "revision_changed"),
        ("fUSD", CapitalPolicy(enabled=True), 0, "unsupported_enabled_symbol"),
        ("fXYZ", CapitalPolicy(enabled=False), 0, "unsupported_enabled_symbol"),
    ):
        async with port_stack.factory() as session:
            with pytest.raises(PolicyRefused, match=code):
                await store.apply_policy(session, symbol=symbol, policy=policy,
                                         expected_revision=expected, source={})
            await session.rollback()
    async with port_stack.factory() as session:
        assert (await store.read_applied(session, symbol="fUST")).revision == 1


async def test_the_ledger_store_never_touches_the_event_stream(port_stack, monkeypatch) -> None:
    async def forbidden(*_args, **_kwargs):
        raise AssertionError("event stream touched")

    monkeypatch.setattr(AccountEventWriter, "prepare_locked", forbidden)
    store = _store(port_stack)
    async with port_stack.factory.begin() as session:
        await store.apply_policy(session, symbol="fUST", policy=CapitalPolicy(enabled=True),
                                 expected_revision=0, source={})
    async with port_stack.factory() as session:
        await store.read_applied(session, symbol="fUST")
        assert await session.scalar(select(func.count()).select_from(EventLogRow)) == 0
