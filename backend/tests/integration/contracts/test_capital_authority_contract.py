"""``CapitalAuthority``: the same port-level observables on the legacy and ledger stacks.

Intended divergences are named in the tests that have them (basis token shape,
the reason code of a missing snapshot); every other assertion is shared.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from bfx_funding_bot.modules.ledger import CapitalAvailable, CapitalBlocked

from .stacks import NOW, Venue

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


async def _read(stack, symbol="fUST", **kwargs):
    return await stack.capital.read(stack.capital_scope(symbol), now_ms=NOW, **kwargs)


async def test_budget_of_a_simple_snapshot(port_stack) -> None:
    await port_stack.policy()
    await port_stack.snapshot("1000")
    read = await _read(port_stack)
    assert isinstance(read, CapitalAvailable), read
    assert read.snapshot.available_amount == Decimal("1000")
    assert read.snapshot.total_capital == Decimal("1000")
    assert read.budget.spendable == Decimal("900")  # 1000 less the 100 reserve
    assert read.applied.revision == 1 and read.applied.symbol == "fUST"
    assert read.unattributed_credit_exposure == 0
    assert read.basis_token  # opaque; the shape differs by authority (below)


async def test_basis_token_is_opaque_and_follows_the_basis(port_stack) -> None:
    await port_stack.policy()
    await port_stack.snapshot("1000")
    first = await _read(port_stack)
    again = await _read(port_stack)
    assert isinstance(first, CapitalAvailable) and isinstance(again, CapitalAvailable)
    assert first.basis_token == again.basis_token
    await port_stack.snapshot("1000")  # a new accepted snapshot is a new basis
    moved = await _read(port_stack)
    assert isinstance(moved, CapitalAvailable)
    assert moved.basis_token != first.basis_token
    # Intended divergence: legacy names the snapshot event, the ledger (query, clock). The
    # ledger's token also moves with a command (clock bump), legacy's does not: see
    # test_ledger_read_ports.py.
    assert moved.basis_token.startswith("ledger:v1:") == (port_stack.name == "ledger")


async def test_unreflected_commitment_is_charged_once(port_stack) -> None:
    await port_stack.policy()
    await port_stack.snapshot("1000")
    await port_stack.place("m-1", "200")
    held = await _read(port_stack)
    assert isinstance(held, CapitalAvailable)
    assert held.snapshot.unreflected_commitments == Decimal("200")
    assert held.budget.spendable == Decimal("700")
    await port_stack.snapshot("800", offers=(Venue("m-1", "200"),))
    reflected = await _read(port_stack)
    assert isinstance(reflected, CapitalAvailable)
    assert reflected.snapshot.unreflected_commitments == 0
    assert reflected.snapshot.cell_exposure == Decimal("200")
    assert reflected.budget.spendable == Decimal("700")


async def test_missing_policy_blocks(port_stack) -> None:
    await port_stack.snapshot("1000")
    read = await _read(port_stack)
    assert isinstance(read, CapitalBlocked)
    assert read.reason == "policy_unavailable"


async def test_no_snapshot_blocks(port_stack) -> None:
    await port_stack.policy()
    read = await _read(port_stack)
    assert isinstance(read, CapitalBlocked)
    assert read.reason == "snapshot_unavailable"


async def test_open_unknown_blocks_only_its_symbol(port_stack) -> None:
    await port_stack.policy()
    await port_stack.policy("fUSD", enabled=False)
    await port_stack.snapshot("1000")
    await port_stack.unknown("200")
    blocked = await _read(port_stack, "fUST")
    assert isinstance(blocked, CapitalBlocked)
    assert blocked.reason == "execution_unknown"
    other = await _read(port_stack, "fUSD")
    assert isinstance(other, CapitalAvailable)
    assert other.budget.reason == "policy_disabled"


async def test_read_policy_answers_without_a_snapshot(port_stack) -> None:
    await port_stack.policy()
    async with port_stack.factory() as session:
        applied = await port_stack.capital.read_policy(session, port_stack.scope, "fUST")
        missing = await port_stack.capital.read_policy(session, port_stack.scope, "fUSD")
    assert not isinstance(applied, CapitalBlocked)
    assert applied.revision == 1 and applied.policy.reserve_amount == Decimal("100")
    assert isinstance(missing, CapitalBlocked) and missing.reason == "policy_unavailable"


async def test_read_with_a_caller_session_agrees_with_the_own_session_read(port_stack) -> None:
    await port_stack.policy()
    await port_stack.snapshot("1000")
    own = await _read(port_stack)
    async with port_stack.factory.begin() as session:
        joined = await _read(port_stack, session=session)
    assert isinstance(own, CapitalAvailable) and isinstance(joined, CapitalAvailable)
    assert (joined.budget, joined.snapshot, joined.basis_token) == (
        own.budget, own.snapshot, own.basis_token,
    )
