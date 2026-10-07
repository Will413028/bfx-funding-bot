"""allocate_capital owns how one symbol's canonical budget is shared between its cells.

The views are built by the real ``evaluate_capital`` from one shared snapshot, so a
test cannot hand the allocator a budget the capital authority would never issue.
The capital-rule mutation gate (scripts/capital_mutation_gate.py) requires this file
to kill the allocator mutants on its own.
"""
from dataclasses import replace
from decimal import Decimal
from uuid import UUID

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from bfx_funding_bot.modules.execution.deployment.sizing import allocate_capital, venue_amount
from bfx_funding_bot.modules.ledger import CapitalAvailable
from bfx_funding_bot.modules.trading import (
    AppliedPolicy,
    CapitalPolicy,
    CapitalSnapshot,
    evaluate_capital,
)

D = Decimal


def _views(
    *, available: str | Decimal, exposures: dict[str, str] | dict[str, Decimal],
    total: str | Decimal | None = None, commitments: str | Decimal = "0",
    reserve: str | Decimal = "0", fraction: str | Decimal = "0.70",
    ceiling: str | Decimal | None = None, token: str = "basis-1",
) -> dict[str, CapitalAvailable]:
    """One symbol's views: shared policy, cash and basis; each cell its own exposure."""
    policy = CapitalPolicy(
        enabled=True, reserve_amount=D(reserve), max_cell_fraction=D(fraction),
        max_offer_amount=None if ceiling is None else D(ceiling),
    )
    applied = AppliedPolicy(UUID(int=1), "test", "fUST", 1, "digest", UUID(int=2), policy)
    views = {}
    for cell, exposure in exposures.items():
        snapshot = CapitalSnapshot(D(available), D(commitments),
                                   D(available if total is None else total), D(exposure))
        views[cell] = CapitalAvailable(applied, snapshot, evaluate_capital(policy, snapshot),
                                       D("0"), token)
    return views


def _limit(view: CapitalAvailable) -> Decimal:
    ceiling = view.applied.policy.max_offer_amount
    limit = view.budget.max_new_offer
    return limit if ceiling is None else min(limit, ceiling)


# ---------------------------------------------------------------------------
# Examples
# ---------------------------------------------------------------------------


def test_no_views_allocates_nothing() -> None:
    assert allocate_capital(views={}, min_fill=D("150")) == {}


def test_single_active_cell_is_bounded_by_its_own_headroom_not_by_cash() -> None:
    # Cash 1000, cell limit 700, exposure 500: headroom 200 binds although the
    # cell is the only one that could use the cash (no lone-cell relaxation).
    views = _views(available="1000", exposures={"a30": "500"})
    assert views["a30"].budget.spendable == D("1000")
    assert allocate_capital(views=views, min_fill=D("150")) == {"a30": D("200")}


def test_cash_binds_below_cell_headroom() -> None:
    # Reserve 3 leaves 197 spendable against a 700 headroom.
    views = _views(available="200", total="1000", reserve="3", exposures={"a30": "0"})
    assert allocate_capital(views=views, min_fill=D("150")) == {"a30": D("197")}


def test_cells_share_one_pool_and_the_remainder_goes_to_the_next_cell() -> None:
    # 570 spendable, each cell limited to 399: the first takes 399, the second 171.
    views = _views(available="570", exposures={"a30": "0", "p2": "0"})
    assert allocate_capital(views=views, min_fill=D("150")) == {"a30": D("399"), "p2": D("171")}


def test_the_emptiest_cell_is_filled_first_when_the_pool_is_short() -> None:
    # "a30" sorts first by name but already holds 300; "p2" is empty and must come
    # first: it takes the 400 pool alone and leaves "a30" nothing.
    views = _views(available="400", total="1000", exposures={"a30": "300", "p2": "0"})
    assert allocate_capital(views=views, min_fill=D("150")) == {"p2": D("400")}


def test_equal_exposure_breaks_ties_by_cell_id() -> None:
    views = _views(available="500", total="1000", exposures={"p2": "0", "a30": "0"})
    assert allocate_capital(views=views, min_fill=D("150")) == {"a30": D("500")}


def test_a_remainder_below_the_minimum_is_not_sent() -> None:
    views = _views(available="500", total="1000", exposures={"a30": "0", "p2": "0"})
    # a30 takes 500 (limit 700); nothing is left for p2.
    assert allocate_capital(views=views, min_fill=D("150")) == {"a30": D("500")}
    short = _views(available="140", total="1000", exposures={"a30": "0"})
    assert allocate_capital(views=short, min_fill=D("150")) == {}


def test_the_per_offer_ceiling_caps_every_cell() -> None:
    views = _views(available="1000", exposures={"a30": "0", "p2": "0"}, ceiling="200")
    assert allocate_capital(views=views, min_fill=D("150")) == {"a30": D("200"), "p2": D("200")}


def test_amounts_are_quantized_down_to_the_venue_precision() -> None:
    views = _views(available="333.123456789", total="1000", exposures={"a30": "0"})
    assert allocate_capital(views=views, min_fill=D("150")) == {"a30": D("333.12345678")}


@pytest.mark.parametrize("field", ["policy", "basis_token", "spendable"])
def test_views_from_different_capital_answers_are_refused(field: str) -> None:
    views = _views(available="1000", exposures={"a30": "0", "p2": "100"})
    other = views["p2"]
    if field == "policy":
        other = replace(other, applied=replace(other.applied, revision=2))
    elif field == "basis_token":
        other = replace(other, basis_token="basis-2")
    else:
        other = replace(other, budget=replace(other.budget, spendable=D("999")))
    with pytest.raises(ValueError, match="inconsistent capital views"):
        allocate_capital(views={"a30": views["a30"], "p2": other}, min_fill=D("150"))


# ---------------------------------------------------------------------------
# Properties
# ---------------------------------------------------------------------------

_AMOUNT = st.decimals(min_value=0, max_value=100_000, places=8,
                      allow_nan=False, allow_infinity=False)
_FRACTION = st.decimals(min_value=D("0.01"), max_value=1, places=2)


@st.composite
def _symbol_views(draw: st.DrawFn) -> tuple[dict[str, CapitalAvailable], Decimal]:
    available = draw(_AMOUNT)
    total = available + draw(_AMOUNT)
    cells = draw(st.lists(_AMOUNT, min_size=1, max_size=5))
    views = _views(
        available=available, total=total,
        commitments=draw(st.decimals(min_value=0, max_value=available, places=8)),
        reserve=draw(_AMOUNT), fraction=draw(_FRACTION),
        ceiling=draw(st.none() | st.decimals(min_value=D("0.00000001"), max_value=100_000,
                                             places=8)),
        exposures={f"c{index}": exposure for index, exposure in enumerate(cells)},
    )
    min_fill = draw(st.decimals(min_value=0, max_value=1_000, places=8))
    return views, min_fill


@pytest.mark.property
@settings(deadline=None)
@given(_symbol_views())
def test_allocation_stays_inside_the_shared_pool_and_every_cell_limit(
    case: tuple[dict[str, CapitalAvailable], Decimal],
) -> None:
    views, min_fill = case
    fills = allocate_capital(views=views, min_fill=min_fill)
    spendable = next(iter(views.values())).budget.spendable
    assert set(fills) <= set(views)
    assert sum(fills.values(), D("0")) <= spendable
    for cell, amount in fills.items():
        assert min_fill <= amount <= _limit(views[cell])
        assert amount == venue_amount(amount)


@pytest.mark.property
@settings(deadline=None)
@given(_symbol_views())
def test_a_fuller_cell_never_gets_more_than_an_emptier_one(
    case: tuple[dict[str, CapitalAvailable], Decimal],
) -> None:
    views, min_fill = case
    fills = allocate_capital(views=views, min_fill=min_fill)
    for fuller, amount in fills.items():
        for emptier, view in views.items():
            if view.snapshot.cell_exposure < views[fuller].snapshot.cell_exposure:
                assert fills.get(emptier, D("0")) >= amount


@pytest.mark.property
@settings(deadline=None)
@given(_symbol_views())
def test_the_emptiest_cell_gets_everything_it_may_take(
    case: tuple[dict[str, CapitalAvailable], Decimal],
) -> None:
    views, min_fill = case
    first = min(views, key=lambda cell: (views[cell].snapshot.cell_exposure, cell))
    view = views[first]
    expected = venue_amount(min(view.budget.spendable, _limit(view)))
    fills = allocate_capital(views=views, min_fill=min_fill)
    assert fills.get(first) == (expected if expected >= min_fill else None)
