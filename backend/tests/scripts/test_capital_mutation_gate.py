"""The capital mutation gate's fixed mutant list still matches the code it mutates.

The gate itself runs only when a capital file changes (and nightly); these checks run
in every unit job, so a refactor that moves a rule's text fails here first.
"""
from pathlib import Path

import pytest

from scripts import capital_mutation_gate as gate

BACKEND = Path(__file__).resolve().parents[2]


def test_every_mutant_anchor_occurs_exactly_once() -> None:
    gate.check_anchors(gate.MUTANTS)


def test_the_mutant_list_is_complete_and_each_mutant_changes_the_source() -> None:
    assert [m.id for m in gate.MUTANTS] == [f"M{n}" for n in range(1, 16)]
    for mutant in gate.MUTANTS:
        assert mutant.old != mutant.new
        assert mutant.owners
        for owner in mutant.owners:
            assert (BACKEND / owner).is_file(), owner
        # Integration-owned mutants are exactly those whose owners need PostgreSQL.
        assert mutant.integration == all(o.startswith("tests/integration/") for o in mutant.owners)


def test_anchor_drift_is_an_error_not_a_skip(tmp_path: Path) -> None:
    mutant = gate.MUTANTS[0]
    target = tmp_path / mutant.path
    target.parent.mkdir(parents=True)
    target.write_text("# the rule moved elsewhere\n")
    with pytest.raises(gate.GateError, match="M1: anchor found 0 times"):
        gate.check_anchors([mutant], root=tmp_path)
