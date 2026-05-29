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


def _alloc(target, exposure, deployed, active, conc=D("0.70"), min_fill=D("153")):
    return allocate_gap(
        target=target, current_exposure=exposure, deployed=deployed,
        active_cells=active, concentration_pct=conc, min_fill=min_fill,
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
