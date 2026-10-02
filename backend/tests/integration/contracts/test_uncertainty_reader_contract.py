"""``UncertaintyReader``: UNKNOWN attempts and quarantines read the same on both stacks."""

from __future__ import annotations

import pytest

from .stacks import Venue

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

UNKNOWN = "submit_outcome_unknown"
QUARANTINE = "unattributed_venue_offer"


async def _scenario(stack) -> None:
    await stack.policy()
    await stack.policy("fUSD", enabled=False)
    await stack.snapshot("1000")
    await stack.unknown("200")  # fUST
    await stack.quarantine("fUSD")


async def test_nothing_is_open_on_a_clean_scope(port_stack) -> None:
    await port_stack.policy()
    await port_stack.snapshot("1000", offers=())
    assert await port_stack.uncertainties.list_open(None, port_stack.scope) == ()
    assert not await port_stack.uncertainties.has_open(None, port_stack.scope, "fUST")


async def test_unknown_and_quarantine_are_listed_with_the_authority_kind_codes(port_stack) -> None:
    await _scenario(port_stack)
    found = await port_stack.uncertainties.list_open(None, port_stack.scope)
    assert {(item.symbol, item.kind) for item in found} == {
        ("fUST", UNKNOWN), ("fUSD", QUARANTINE),
    }
    assert {(item.exchange_account_id, item.deployment_environment) for item in found} == {
        (port_stack.scope.exchange_account_id, port_stack.scope.deployment_environment),
    }


async def test_symbol_filter_and_has_open(port_stack) -> None:
    await _scenario(port_stack)
    scope, reader = port_stack.scope, port_stack.uncertainties
    assert [i.kind for i in await reader.list_open(None, scope, "fUST")] == [UNKNOWN]
    assert [i.kind for i in await reader.list_open(None, scope, "fUSD")] == [QUARANTINE]
    assert await reader.list_open(None, scope, "fBTC") == ()
    assert await reader.has_open(None, scope, "fUST")
    assert await reader.has_open(None, scope, "fUSD")  # a quarantine alone counts
    assert not await reader.has_open(None, scope, "fBTC")


async def test_a_caller_session_reads_the_same_as_the_own_session(port_stack) -> None:
    await _scenario(port_stack)
    scope, reader = port_stack.scope, port_stack.uncertainties
    async with port_stack.factory() as session:
        assert set(await reader.list_open(session, scope)) == set(await reader.list_open(None, scope))
        assert await reader.has_open(session, scope, "fUSD")
        assert not await reader.has_open(session, scope, "fBTC")


async def test_another_scope_is_not_read(port_stack) -> None:
    await _scenario(port_stack)
    from dataclasses import replace
    from uuid import uuid4

    other = replace(port_stack.scope, exchange_account_id=uuid4())
    assert await port_stack.uncertainties.list_open(None, other) == ()
    assert not await port_stack.uncertainties.has_open(None, other, "fUST")


async def test_a_live_offer_alone_is_not_an_uncertainty(port_stack) -> None:
    await port_stack.policy()
    await port_stack.snapshot("700", offers=(Venue("manual-1", "300"),))
    assert not await port_stack.uncertainties.has_open(None, port_stack.scope, "fUST")
