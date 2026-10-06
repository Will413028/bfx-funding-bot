"""Migration a0b1c2d3e4f5's frozen copy of the legacy offer -> cell resolution (pure parts).

The database side (copy, refusal, grants, equality with the pre-S1-8 loader) is in
tests/integration/test_attribution_legacy_links_migration.py.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

_PATH = (Path(__file__).resolve().parents[3] / "alembic/versions"
         / "a0b1c2d3e4f5_attribution_legacy_links.py")


def _migration() -> Any:
    spec = importlib.util.spec_from_file_location("attribution_legacy_cells", _PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


M = _migration()


def test_the_audited_decision_wins_over_the_signal_correlation() -> None:
    links = [("1", None, "s"), ("1", "d", None), ("2", None, "s")]
    cells = M.resolve_offer_cells(links, [("d", "s-d", "fUST_p2")], [("s", "fUST_a30")])
    assert cells == {"1": {"fUST_p2"}, "2": {"fUST_a30"}}


def test_a_decisions_correlation_overrides_the_diagnostics() -> None:
    cells = M.resolve_offer_cells([("3", None, "s")], [("dx", "s", "fUST_p7")],
                                  [("s", "fUST_a30")])
    assert cells == {"3": {"fUST_p7"}}


def test_disagreeing_links_keep_every_cell() -> None:
    decisions = [("d1", "s1", "fUST_p2"), ("d2", "s2", "fUST_a30")]
    by_decision = M.resolve_offer_cells([("1", "d1", None), ("1", "d2", None)], decisions, [])
    assert by_decision == {"1": {"fUST_p2", "fUST_a30"}}
    by_correlation = M.resolve_offer_cells([("1", None, "s1"), ("1", None, "s2")], decisions, [])
    assert by_correlation == {"1": {"fUST_p2", "fUST_a30"}}
    # one correlation id two diagnostics place in two cells
    ambiguous = M.resolve_offer_cells([("1", None, "s")], [], [("s", "a"), ("s", "b")])
    assert ambiguous == {"1": {"a", "b"}}


def test_unresolved_offers_and_empty_values_are_left_out() -> None:
    cells = M.resolve_offer_cells(
        [("", "d", None), ("4", None, "unknown"), ("5", None, None), ("6", None, "7")],
        [("d", "s", "fUST_p2")], [("", "x"), ("s9", ""), (7, "fUST_p30")])
    assert cells == {"6": {"fUST_p30"}}  # JSON number 7 -> "7", as the loader's str()


def test_a_fill_link_is_read_as_the_loader_read_it() -> None:
    assert M._fill_link(1004, None, "s") == ("1004", None, "s")
    assert M._fill_link(None, "1005", "") == ("1005", None, None)
    assert M._fill_link("", None, "s") == ("", None, "s")
