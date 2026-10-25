"""The kill matrix's bookkeeping: which test functions killed what, survivors and the cover.

Running mutants needs mutmut and a full pytest run per mutant, so only the pure parts are
tested here; the pilot run is in the PR that added the script.
"""
from pathlib import Path

from scripts import mutation_kill_matrix as matrix


def test_parameter_ids_fold_into_their_test_function() -> None:
    assert matrix.function_of("tests/a.py::test_x[3-y]") == "tests/a.py::test_x"
    assert matrix.function_of("tests/a.py::test_x") == "tests/a.py::test_x"


def test_mutant_names_are_the_ones_mutmut_registered(tmp_path: Path) -> None:
    mutated = tmp_path / "m.py"
    mutated.write_text(
        "def x_f__mutmut_1(): ...\n"
        "mutants_x_f__mutmut['_mutmut_orig'] = x_f__mutmut_orig # mutmut generated\n"
        "mutants_x_f__mutmut['x_f__mutmut_1'] = x_f__mutmut_1 # mutmut generated\n"
        "mutants_xǁCǁg__mutmut['xǁCǁg__mutmut_2'] = xǁCǁg__mutmut_2 # mutmut generated\n")
    assert matrix.mutant_names(mutated, "pkg.m") == [
        "pkg.m.x_f__mutmut_1", "pkg.m.xǁCǁg__mutmut_2"]


def test_a_report_names_failed_and_errored_cases(tmp_path: Path) -> None:
    report = tmp_path / "r.xml"
    report.write_text(
        '<testsuites><testsuite>'
        '<testcase classname="tests.a" name="test_ok"/>'
        '<testcase classname="tests.a" name="test_bad[1]"><failure/></testcase>'
        '<testcase classname="tests.a" name="test_setup"><error/></testcase>'
        '</testsuite></testsuites>')
    run = matrix.read_report(report)
    assert run.failed == {"tests.a::test_bad[1]"} and run.errored == {"tests.a::test_setup"}
    assert run.broken == {"tests.a::test_bad[1]", "tests.a::test_setup"}


def test_the_summary_finds_survivors_unique_kills_and_tests_the_cover_does_not_need() -> None:
    summary = matrix.summarize({
        "m1": {"wide", "narrow"},
        "m2": {"wide"},
        "m3": {"wide", "other"},
        "m4": {"other"},
        "m5": set(),
    })
    assert (summary["mutants"], summary["killed"], summary["survived"]) == (5, 4, ["m5"])
    assert summary["tests"] == {
        "narrow": {"kills": 1, "unique": []},
        "other": {"kills": 2, "unique": ["m4"]},
        "wide": {"kills": 3, "unique": ["m2"]},
    }
    # wide covers m1-m3, other adds m4; narrow kills nothing they leave.
    assert (summary["cover"], summary["not_in_cover"]) == (["other", "wide"], ["narrow"])


def test_two_tests_that_kill_the_same_mutants_leave_one_in_the_cover() -> None:
    """Neither has a unique kill, yet removing both would lose the mutants: the cover keeps one."""
    summary = matrix.summarize({"m1": {"a", "b"}, "m2": {"a", "b"}})
    assert summary["tests"]["a"]["unique"] == summary["tests"]["b"]["unique"] == []
    assert len(summary["cover"]) == 1 and len(summary["not_in_cover"]) == 1
