"""``ManagedOfferReader``: managed vs manual offers and fingerprints on the ledger stack."""

from __future__ import annotations

import pytest

from .stacks import Venue

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

MANAGED = (Venue("m-2", "100.00000007"), Venue("m-1", "200.00000042"))
MANUAL = (Venue("x-1", "300.00000099"), Venue("y-1", "50", symbol="fUSD"))


async def _scenario(stack) -> None:
    """Two managed fUST offers, a manual fUST offer, a manual fUSD offer."""
    await stack.policy()
    await stack.policy("fUSD", enabled=False)
    await stack.snapshot("1000")
    for offer in MANAGED:
        await stack.place(offer.venue_offer_id, offer.amount)
    await stack.snapshot("400", offers=(*MANAGED, *MANUAL))


async def test_live_lists_managed_offers_ordered_and_never_manual_ones(port_stack) -> None:
    await _scenario(port_stack)
    async with port_stack.factory() as session:
        live = await port_stack.offers.live(session, port_stack.scope)
        only_usd = await port_stack.offers.live(session, port_stack.scope, ["fUSD"])
        only_ust = await port_stack.offers.live(session, port_stack.scope, ["fUST"])
    assert [(o.venue_offer_id, o.symbol) for o in live] == [("m-1", "fUST"), ("m-2", "fUST")]
    assert all(o.signal_correlation_id for o in live)
    assert only_usd == ()
    assert [o.venue_offer_id for o in only_ust] == ["m-1", "m-2"]


async def test_count_live_counts_managed_offers_of_the_symbol_only(port_stack) -> None:
    await _scenario(port_stack)
    async with port_stack.factory() as session:
        assert await port_stack.offers.count_live(session, port_stack.scope, "fUST") == 2
        assert await port_stack.offers.count_live(session, port_stack.scope, "fUSD") == 0
        assert await port_stack.offers.count_live(session, port_stack.scope, "fBTC") == 0


async def test_live_symbols_include_manual_offers(port_stack) -> None:
    await _scenario(port_stack)
    async with port_stack.factory() as session:
        assert await port_stack.offers.live_symbols(session, port_stack.scope) == {"fUST", "fUSD"}


async def test_live_symbols_of_a_scope_with_only_a_manual_offer(port_stack) -> None:
    await port_stack.policy()
    await port_stack.snapshot("700", offers=(Venue("x-1", "300"),))
    async with port_stack.factory() as session:
        assert await port_stack.offers.live_symbols(session, port_stack.scope) == {"fUST"}
        assert await port_stack.offers.live(session, port_stack.scope) == ()
        assert await port_stack.offers.count_live(session, port_stack.scope, "fUST") == 0


async def test_nothing_is_live_before_any_snapshot(port_stack) -> None:
    async with port_stack.factory() as session:
        assert await port_stack.offers.live(session, port_stack.scope) == ()
        assert await port_stack.offers.live_symbols(session, port_stack.scope) == frozenset()
        assert await port_stack.offers.count_live(session, port_stack.scope, "fUST") == 0


async def test_fingerprints_of_live_managed_offers_and_an_unresolved_commitment(port_stack) -> None:
    await _scenario(port_stack)
    await port_stack.unknown("150.00000123")
    async with port_stack.factory() as session:
        held = await port_stack.offers.fingerprints_in_use(session, port_stack.scope, "fUST")
        other = await port_stack.offers.fingerprints_in_use(session, port_stack.scope, "fUSD")
    # 7 and 42: the live managed offers; 123: the UNKNOWN attempt. The manual
    # offer's 99 is nobody's commitment.
    assert held == frozenset({7, 42, 123})
    assert other == frozenset()


async def test_an_amount_without_a_fingerprint_holds_nothing(port_stack) -> None:
    await port_stack.policy()
    await port_stack.snapshot("1000")
    await port_stack.unknown("200")  # 200.00000000: fingerprint 0 means "none"
    async with port_stack.factory() as session:
        held = await port_stack.offers.fingerprints_in_use(session, port_stack.scope, "fUST")
    assert held == frozenset()
