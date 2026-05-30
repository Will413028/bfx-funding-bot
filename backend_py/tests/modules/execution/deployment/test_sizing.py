from decimal import Decimal

from bfx_funding_bot.modules.execution.deployment.sizing import (
    allocate_gap,
    effective_min_usdt,
)

D = Decimal


def test_effective_min_static_153():
    assert effective_min_usdt(D("150"), D("0.02")) == D("153")


def test_effective_min_rounds_up():
    # 150 * 1.015 = 152.25 -> ceil -> 153
    assert effective_min_usdt(D("150"), D("0.015")) == D("153")


def _alloc(target, exposure, deployed, active, conc=None, min_fill=None):
    return allocate_gap(
        target=target, current_exposure=exposure, deployed=deployed,
        active_cells=active,
        concentration_pct=conc if conc is not None else D("0.70"),
        min_fill=min_fill if min_fill is not None else D("153"),
    )


def test_no_gap_returns_empty():
    assert _alloc(D("570"), D("570"), {}, ["a"]) == {}


def test_gap_below_min_returns_empty():
    # gap = 100 < 153
    assert _alloc(D("570"), D("470"), {}, ["a", "b"]) == {}


def test_single_active_cell_fills_gap_up_to_concentration_cap():
    # gap = 200, cap_per_cell = 0.70*570 = 399 -> fill whole 200 in one offer
    out = _alloc(D("570"), D("370"), {}, ["a"])
    assert out == {"a": D("200")}


def test_concentration_cap_limits_a_single_cell():
    # target 570, cap_per_cell 399. gap = 570 (exposure 0), only cell "a" active.
    # "a" can take at most 399; remaining 171 has no other active cell -> dropped.
    out = _alloc(D("570"), D("0"), {}, ["a"])
    assert out == {"a": D("399")}


def test_fills_emptiest_cell_first_then_next():
    # gap = 570, both active, both empty. emptiest-first (tiebreak cell_id):
    # "a" -> 399 (cap), remaining 171 >= 153 -> "b" -> 171.
    out = _alloc(D("570"), D("0"), {}, ["a", "b"])
    assert out == {"a": D("399"), "b": D("171")}


def test_already_deployed_reduces_headroom():
    # "a" already has 350 deployed -> headroom 399-350 = 49 < 153 -> skip "a";
    # gap = 200, "b" empty -> "b" gets 200.
    out = _alloc(D("570"), D("370"), {"a": D("350")}, ["a", "b"])
    assert out == {"b": D("200")}


def test_no_active_cells_returns_empty():
    assert _alloc(D("570"), D("0"), {}, []) == {}


def test_allocate_gap_clamps_to_available_headroom():
    # cap gap = 600 - 400 = 200, but only 140 deployable → gap clamps to 140.
    fills = allocate_gap(
        target=Decimal("600"),
        current_exposure=Decimal("400"),
        available_headroom=Decimal("140"),
        deployed={},
        active_cells=["fUST_a30"],
        concentration_pct=Decimal("0.70"),
        min_fill=Decimal("153"),
    )
    # 140 < min_fill(153) → nothing deployed (sleep).
    assert fills == {}


def test_allocate_gap_deploys_when_headroom_allows():
    fills = allocate_gap(
        target=Decimal("600"),
        current_exposure=Decimal("400"),
        available_headroom=Decimal("200"),   # >= cap gap 200
        deployed={},
        active_cells=["fUST_a30"],
        concentration_pct=Decimal("0.70"),
        min_fill=Decimal("153"),
    )
    assert fills == {"fUST_a30": Decimal("200")}


def test_allocate_gap_headroom_binds_below_cap_gap():
    # cap gap = 300, headroom 160 → deploy 160 (headroom binds, >= min_fill).
    fills = allocate_gap(
        target=Decimal("700"),
        current_exposure=Decimal("400"),
        available_headroom=Decimal("160"),
        deployed={},
        active_cells=["fUST_a30"],
        concentration_pct=Decimal("0.70"),
        min_fill=Decimal("153"),
    )
    assert fills == {"fUST_a30": Decimal("160")}


def test_allocate_gap_negative_headroom_sleeps():
    fills = allocate_gap(
        target=Decimal("600"),
        current_exposure=Decimal("400"),
        available_headroom=Decimal("-5"),
        deployed={},
        active_cells=["fUST_a30"],
        concentration_pct=Decimal("0.70"),
        min_fill=Decimal("153"),
    )
    assert fills == {}


def test_allocate_gap_default_headroom_is_unbounded():
    # No available_headroom passed → behaves as before (cap gap only).
    fills = allocate_gap(
        target=Decimal("600"),
        current_exposure=Decimal("400"),
        deployed={},
        active_cells=["fUST_a30"],
        concentration_pct=Decimal("0.70"),
        min_fill=Decimal("153"),
    )
    assert fills == {"fUST_a30": Decimal("200")}
