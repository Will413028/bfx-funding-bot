"""No two entity kinds share an id, however long the run."""
from __future__ import annotations

import asyncio
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from bfx_funding_bot.modules.simulated_venue._internal.state import ID_SLOT, ID_STRIDE, VenueState
from tests.modules.simulated_venue.helpers import CTX, DAY, HOUR, World, config, make_world, trade


def _ids(w: World) -> dict[str, set[int]]:
    state = w.venue.state
    return {
        "offer": set(state.offers),
        "trade": {t.trade_id for t in state.trades},
        "loan": {lid for kind, lid in state.lendings if kind == "loan"},
        "credit": {lid for kind, lid in state.lendings if kind == "credit"},
        "ledger": {e.ledger_id for e in state.ledger},
    }


def _assert_disjoint(ids: dict[str, set[int]]) -> None:
    kinds = list(ids)
    for i, a in enumerate(kinds):
        for b in kinds[i + 1:]:
            assert ids[a].isdisjoint(ids[b]), (a, b, ids[a] & ids[b])
    for kind, values in ids.items():
        base = 40_000_000 + ID_SLOT[kind] * ID_STRIDE
        assert all(base < v < base + ID_STRIDE for v in values), kind


async def _long_run(steps: list[tuple[int, int, int]]) -> World:
    w = await make_world(funds={"UST": "100000"}, cfg=config(draw_after_ms=HOUR))
    for amount, rate, minutes in steps:
        await w.submit(amount=str(amount), rate=f"0.000{rate}")
        w.feed.add_trades("fUST", [trade(w.clock.now + 1, str(amount))])
        w.clock.advance(1 + minutes * 60_000)
        await w.rest.fetch_wallet_observations(ctx=CTX)
        _assert_disjoint(_ids(w))
    return w


def test_a_long_deterministic_run_uses_five_disjoint_id_spaces() -> None:
    steps = [(150 + (i % 5) * 10, 1 + i % 9, 90 + 50 * (i % 7)) for i in range(60)]
    w = asyncio.run(_long_run(steps))
    ids = _ids(w)
    assert all(len(v) > 5 for v in ids.values()), {k: len(v) for k, v in ids.items()}
    _assert_disjoint(ids)


@pytest.mark.property
@settings(deadline=None)
@given(st.lists(st.tuples(st.integers(150, 400), st.integers(1, 9), st.integers(1, 3000)),
                min_size=1, max_size=25))
def test_random_runs_never_collide_ids_across_kinds(steps: list[Any]) -> None:
    asyncio.run(_long_run(steps))


def test_the_first_id_of_each_kind_is_distinct_and_an_exhausted_space_is_refused() -> None:
    state = VenueState()
    firsts = {kind: state.next_id(kind, 40_000_000) for kind in ID_SLOT}
    assert len(set(firsts.values())) == len(ID_SLOT)
    state.counters["offer"] = 40_000_000 + ID_STRIDE - 1
    with pytest.raises(OverflowError):
        state.next_id("offer", 40_000_000)
    assert DAY > 0
