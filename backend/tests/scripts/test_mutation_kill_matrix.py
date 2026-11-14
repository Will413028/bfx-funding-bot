"""The kill matrix's bookkeeping: which test functions killed what, survivors and the cover.

Running mutants needs mutmut and a full pytest run per mutant, so only the pure parts are
tested here; the pilot run is in the PR that added the script.
"""
from pathlib import Path

import pytest

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


def test_a_package_file_runs_under_its_dotted_name_and_any_other_needs_one() -> None:
    assert matrix.module_of(Path("backend/src/pkg/sub/mod.py"), None) == "pkg.sub.mod"
    assert matrix.module_of(Path("deploy/vm/ops/x.py"), "x_under_test") == "x_under_test"
    with pytest.raises(matrix.MatrixError, match="--module-name"):
        matrix.module_of(Path("deploy/vm/ops/x.py"), None)


def test_a_mutant_runs_only_the_tests_that_call_its_function() -> None:
    stats = {"t::a": ["m.x_f", "m.x_g"], "t::b": ["m.x_g"], "t::c": []}
    assert matrix.function_of_mutant("m.x_g__mutmut_12") == "m.x_g"
    assert matrix.tests_reaching("m.x_g__mutmut_12", stats) == ["t::a", "t::b"]
    assert matrix.tests_reaching("m.x_h__mutmut_1", stats) == []


def test_a_missing_or_truncated_report_is_a_crashed_run(tmp_path: Path) -> None:
    truncated = tmp_path / "r.xml"
    truncated.write_text("")
    assert matrix.read_report(truncated).crashed
    assert matrix.read_report(tmp_path / "missing.xml").crashed


def test_function_names_select_the_mutants_of_functions_and_methods() -> None:
    module = "bfx_funding_bot.modules.x"
    plain = f"{module}.x_allocate__mutmut_3"
    method = f"{module}.xǁReconcilerǁ_pull_if_stopped__mutmut_12"
    other = f"{module}.xǁSweeperǁ_pull_if_stopped__mutmut_1"
    assert matrix.in_functions(plain, ["allocate"])
    assert not matrix.in_functions(plain, ["allocate_more", "x_allocate", "llocate"])
    assert matrix.in_functions(method, ["_pull_if_stopped"])
    assert matrix.in_functions(other, ["_pull_if_stopped"])           # the bare name means every class
    assert matrix.in_functions(method, ["Reconciler._pull_if_stopped"])
    assert not matrix.in_functions(other, ["Reconciler._pull_if_stopped"])
    assert not matrix.in_functions(method, ["Reconciler", "pull_if_stopped"])
    assert not matrix.in_functions(method, [])


def test_every_named_function_must_select_a_mutant() -> None:
    names = ["m.x_allocate__mutmut_1", "m.xǁReconcilerǁ_pull__mutmut_1", "m.xǁReconcilerǁ_pull__mutmut_2"]
    assert matrix.select_mutants(names, []) == names
    assert matrix.select_mutants(names, ["_pull"]) == names[1:]
    assert matrix.select_mutants(names, ["allocate", "Reconciler._pull"]) == names
    with pytest.raises(matrix.MatrixError, match=r"no mutant for _pul, Sweeper\._pull"):
        matrix.select_mutants(names, ["_pull", "_pul", "Sweeper._pull"])
    # Names derived from a diff: one with nothing to mutate is left out, none at all is an error.
    assert matrix.select_mutants(names, ["_pull", "trivial"], strict=False) == names[1:]
    with pytest.raises(matrix.MatrixError, match="no mutant for trivial, other"):
        matrix.select_mutants(names, ["trivial", "other"], strict=False)


def test_mutant_runs_share_the_copys_bytecode(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, object] = {}

    def fake(copy: Path, args: list[str], **kwargs: object) -> None:
        seen.update(kwargs, args=args)

    monkeypatch.setattr(matrix.sandbox, "pytest", fake)
    matrix._pytest(tmp_path, ["tests/x.py"], tmp_path / "r.xml", {"MUTANT_UNDER_TEST": "m"}, 60.0)
    assert seen["bytecode"] is True
    assert seen["env"] == {"MUTANT_UNDER_TEST": "m"}


def test_the_command_line_passes_the_named_functions_on(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[tuple[object, ...]] = []

    def fake(*args: object) -> dict[str, object]:
        seen.append(args)
        return {"killed": 0, "mutants": 0, "survived": [], "not_reached": [], "timed_out": [],
                "crashed": [], "not_in_cover": [], "tests": {}}

    monkeypatch.setattr(matrix, "build", fake)
    out = tmp_path / "report.json"
    source = "backend/scripts/mutation_kill_matrix.py"
    assert matrix.main(["--source", source, "--out", str(out), "--function", "a", "--function", "B.c",
                        "--", "tests/x.py"]) == 0
    assert seen[0][-1] == ["a", "B.c"]
    assert matrix.main(["--source", source, "--out", str(out), "--", "tests/x.py"]) == 0
    assert seen[1][-1] == []
