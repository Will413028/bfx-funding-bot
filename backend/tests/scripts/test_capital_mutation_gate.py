"""The capital mutation gate's fixed mutant list still matches the code it mutates.

The gate itself runs only when a capital file changes (and nightly); these checks run
in every unit job, so a refactor that moves a rule's text fails here first.
"""
import subprocess
import sys
from pathlib import Path

import pytest

from scripts import capital_mutation_gate as gate

BACKEND = Path(__file__).resolve().parents[2]


def test_every_mutant_anchor_occurs_exactly_once() -> None:
    gate.check_anchors(gate.MUTANTS)


def test_the_mutant_list_is_complete_and_each_mutant_changes_the_source() -> None:
    assert [m.id for m in gate.MUTANTS] == [f"M{n}" for n in range(1, 19)]
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


_BROKEN_FIXTURE = '''
import pytest

@pytest.fixture
def database():
    raise RuntimeError("PostgreSQL binaries not found")

def test_owner(database):
    assert False
'''


def _junit_run(tmp_path: Path, body: str) -> gate.Verdict:
    """Run a real pytest session on ``body`` and judge it as the gate would."""
    (tmp_path / "test_owner.py").write_text(body)
    report = tmp_path / "junit.xml"
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-p", "no:xdist",
         f"--junitxml={report}", "test_owner.py"],
        cwd=tmp_path, capture_output=True, text=True, check=False,
    )
    return gate.classify(result.returncode, report)


def test_a_run_with_only_setup_errors_is_not_a_kill(tmp_path: Path) -> None:
    # pytest exits 1 here too; the gate must not read that as "killed".
    verdict = _junit_run(tmp_path, _BROKEN_FIXTURE)
    assert verdict.status == "error"
    assert "test_owner" in verdict.detail


def test_a_failed_test_without_errors_is_a_kill(tmp_path: Path) -> None:
    verdict = _junit_run(tmp_path, "def test_owner():\n    assert False\n")
    assert verdict == gate.Verdict("killed", "test_owner::test_owner")


def test_a_passing_run_is_a_survivor(tmp_path: Path) -> None:
    assert _junit_run(tmp_path, "def test_owner():\n    pass\n").status == "survived"


def test_a_missing_report_is_an_error(tmp_path: Path) -> None:
    assert gate.classify(1, tmp_path / "absent.xml").status == "error"
