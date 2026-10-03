"""Random operation sequences keep the venue's invariants, observed only through the wire."""
from __future__ import annotations

import asyncio
from collections import defaultdict
from decimal import Decimal
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from bfx_funding_bot.modules.simulated_venue.events import InterestPaid
from tests.modules.simulated_venue.helpers import (
    ACCOUNT,
    CTX,
    DAY,
    T0,
    World,
    config,
    make_world,
    trade,
)

D = Decimal
_SYMBOL = "fUST"

op = st.one_of(
    st.tuples(st.just("submit"), st.integers(150, 400), st.integers(1, 9), st.integers(2, 5)),
    st.tuples(st.just("cancel"), st.integers(0, 50)),
    st.tuples(st.just("cancel_all")),
    st.tuples(st.just("advance"), st.integers(1, 4000)),
    st.tuples(st.just("trades"), st.integers(10, 900), st.integers(2, 5), st.integers(0, 600)),
    st.tuples(st.just("restart")),
)


async def _history_ids(w: World, stream: str) -> set[int]:
    rows = (await w.post(f"v2/auth/r/funding/{stream}/{_SYMBOL}/hist",
                         {"start": 0, "end": w.clock.now + DAY, "limit": 500})).json()
    return {int(r[0]) for r in rows}


async def _check(w: World, previous: dict[str, set[int]], ops_seen: list[Any]) -> dict[str, set[int]]:
    rest = w.rest
    wallets = await rest.fetch_wallet_observations(ctx=CTX)
    offers = await rest.fetch_active_offer_observations(ctx=CTX)
    credits = await rest.fetch_active_credit_observations(ctx=CTX)
    loans = await rest.fetch_active_loan_observations(ctx=CTX)
    (wallet,) = wallets
    assert wallet.available is not None and wallet.available >= 0, ops_seen
    resting = sum((o.amount for o in offers), D(0))
    lent = sum((c.amount for c in (*credits, *loans)), D(0))
    # wallet + offered + lent conservation, from the wire alone
    assert wallet.available + resting + lent == wallet.balance, ops_seen

    state = w.venue.state
    interest = sum((e.amount for e in await w.store.load(ACCOUNT)
                    if isinstance(e, InterestPaid)), D(0))
    assert wallet.balance == state.deposits["UST"] + interest, ops_seen  # balance identity

    filled: dict[int, Decimal] = defaultdict(lambda: D(0))
    for t in state.trades:
        filled[t.offer_id] += t.amount
    for offer in state.offers.values():
        assert D(0) <= offer.remaining <= offer.amount_original
        assert filled[offer.offer_id] == offer.amount_original - offer.remaining, ops_seen
        assert (offer.status in ("EXECUTED",)) == (offer.remaining == 0), ops_seen

    now_active = {
        "offers": {int(o.venue_offer_id) for o in offers},
        "credits": {int(c.credit_id) for c in credits},
        "loans": {int(c.credit_id) for c in loans},
    }
    for stream, gone in ((s, previous[s] - now_active[s]) for s in now_active):
        # every disappearance has a same-id history row (lag 0)
        assert gone <= await _history_ids(w, stream), (stream, gone, ops_seen)
    return now_active


async def _run(ops: list[Any]) -> World:
    w = await make_world(funds={"UST": "2000"}, asks=[("0.0003", 2, "300"), ("0.0001", 3, "50")])
    previous: dict[str, set[int]] = {"offers": set(), "credits": set(), "loans": set()}
    seen: list[Any] = []
    for step in ops:
        seen.append(step)
        kind = step[0]
        if kind == "submit":
            await w.submit(amount=str(step[1]), rate=f"0.000{step[2]}", period=step[3])
        elif kind == "cancel" and w.venue.state.offers:
            ids = sorted(w.venue.state.offers)
            await w.cancel(ids[step[1] % len(ids)])
        elif kind == "cancel_all":
            await w.post("v2/auth/w/funding/offer/cancel/all", {"currency": "UST"})
        elif kind == "advance":
            w.clock.advance(step[1] * 60_000)
        elif kind == "trades":
            w.feed.add_trades(_SYMBOL, [trade(w.clock.now + 1, str(step[1]), period=step[2])])
            w.clock.advance(1 + step[3] * 60_000)
        elif kind == "restart":
            before = w.venue.state
            reborn = await make_world(store=w.store, feed=w.feed, clock=w.clock)
            assert reborn.venue.state == before  # fold(load()) == live state
            w = reborn
        previous = await _check(w, previous, seen)
    return w


def test_the_driver_really_exercises_fills_expiry_interest_cancel_and_restart() -> None:
    ops: list[Any] = [
        ("submit", 300, 1, 2), ("submit", 150, 5, 3), ("trades", 800, 2, 5), ("advance", 90),
        ("restart",), ("trades", 800, 3, 0), ("cancel", 1), ("advance", 4000),
        ("advance", 4000), ("advance", 4000), ("submit", 200, 2, 2), ("cancel_all",),
    ]
    w = asyncio.run(_run(ops))
    state = w.venue.state
    assert state.trades and state.ledger
    assert any(lend.status == "CLOSED (expired)" for lend in state.lendings.values())
    assert {"EXECUTED", "CANCELED"} <= {o.status for o in state.offers.values()}


@pytest.mark.property
@settings(max_examples=60, deadline=None)
@given(st.lists(op, min_size=1, max_size=30))
def test_random_operation_sequences_keep_venue_invariants(ops: list[Any]) -> None:
    asyncio.run(_run(ops))


@pytest.mark.property
@settings(max_examples=25, deadline=None)
@given(st.lists(op, min_size=1, max_size=25), st.sampled_from([0, 3_000, 90_000]))
def test_invariants_hold_with_history_lag_once_it_has_passed(ops: list[Any], lag: int) -> None:
    async def run() -> None:
        w = await make_world(funds={"UST": "2000"}, cfg=config(history_lag_ms=lag),
                             asks=[("0.0003", 2, "300")])
        for step in ops:
            if step[0] == "submit":
                await w.submit(amount=str(step[1]), rate=f"0.000{step[2]}", period=step[3])
            elif step[0] == "cancel_all":
                await w.post("v2/auth/w/funding/offer/cancel/all", {"currency": "UST"})
            elif step[0] == "trades":
                w.feed.add_trades(_SYMBOL, [trade(w.clock.now + 1, str(step[1]), period=step[2])])
                w.clock.advance(1 + step[3] * 60_000)
            elif step[0] == "advance":
                w.clock.advance(step[1] * 60_000)
        w.clock.advance(lag + 1)  # everything terminal has aged into history
        offers = await w.rest.fetch_active_offer_observations(ctx=CTX)
        terminal = {o.offer_id for o in w.venue.state.offers.values() if not o.resting}
        active = {int(o.venue_offer_id) for o in offers}
        assert active.isdisjoint(terminal)
        assert terminal <= await _history_ids(w, "offers")
        lending_gone = {lend.lending_id for lend in w.venue.state.lendings.values()
                        if lend.kind == "loan" and not lend.active}
        assert lending_gone <= await _history_ids(w, "loans")
        assert w.venue.state.high_water_ms >= T0

    asyncio.run(run())
