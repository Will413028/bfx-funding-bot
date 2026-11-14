"""The changed-function mutation report: reading a diff, naming functions, choosing tests, rendering.

Running mutants is the kill matrix's job and is not repeated here; ``judge`` is tested with the
matrix replaced.
"""
from pathlib import Path

import pytest

from scripts import mutation_changed as changed
from scripts import mutation_kill_matrix as matrix

DIFF = """\
diff --git a/backend/src/bfx_funding_bot/modules/ledger/x.py b/backend/src/bfx_funding_bot/modules/ledger/x.py
--- a/backend/src/bfx_funding_bot/modules/ledger/x.py
+++ b/backend/src/bfx_funding_bot/modules/ledger/x.py
@@ -3 +3,2 @@ def first():
-    return 1
+    value = 1
+    return value
@@ -9,2 +10,0 @@ def second():
-    gone = 1
-    gone += 1
@@ -20,0 +20 @@ class Box:
+    size = 3
diff --git a/backend/src/bfx_funding_bot/old.py b/backend/src/bfx_funding_bot/old.py
deleted file mode 100644
--- a/backend/src/bfx_funding_bot/old.py
+++ /dev/null
@@ -1,2 +0,0 @@
-def old():
-    return 0
"""

SOURCE = '''\
"""Module."""
import functools

LIMIT = 3


def first():
    return 1


@functools.cache
def decorated():
    return 2


class Box:
    size = 3

    def method(self):
        def inner():
            return 4
        return inner()

    async def later(self):
        return 5

    class Nested:
        def deep(self):
            return 6
'''


def test_a_diff_gives_the_touched_lines_of_each_file() -> None:
    ranges = changed.changed_lines(DIFF)
    # A hunk that only removes lines touches the lines it sat between; a deleted file has none.
    assert ranges == {Path("backend/src/bfx_funding_bot/modules/ledger/x.py"): [(3, 4), (10, 11), (20, 20)]}


def test_lines_removed_from_a_function_name_it() -> None:
    source = "def first():\n    a = 1\n    return a\n\n\ndef second():\n    return 2\n"
    removed = """\
--- a/backend/src/bfx_funding_bot/m.py
+++ b/backend/src/bfx_funding_bot/m.py
@@ -3,2 +2,0 @@ def first():
-    a += 1
-    a += 2
"""
    ranges = changed.changed_lines(removed)[Path("backend/src/bfx_funding_bot/m.py")]
    assert changed.functions_at(source, ranges) == ["first"]


def test_lines_name_top_level_functions_and_methods() -> None:
    def at(*lines: int) -> list[str]:
        return changed.functions_at(SOURCE, [(line, line) for line in lines])

    assert at(8) == ["first"]
    assert at(11) == ["decorated"]                 # the decorator line belongs to the function
    assert at(20, 21) == ["Box.method"]            # a nested function counts as its outer one
    assert at(24) == ["Box.later"]
    assert at(4, 16, 17) == []                     # a constant, the class line, a class attribute
    assert at(28) == []                            # a method of a nested class: mutmut does not mutate it
    assert changed.functions_at(SOURCE, [(1, 40)]) == ["first", "decorated", "Box.method", "Box.later"]
    assert changed.functions_at("def broken(:\n", [(1, 1)]) == []


def test_the_tests_are_the_nearest_directory_that_mirrors_the_source(tmp_path: Path) -> None:
    (tmp_path / "backend/tests/modules/ledger").mkdir(parents=True)
    (tmp_path / "backend/tests/core").mkdir(parents=True)

    def tests(source: str) -> Path | None:
        return changed.tests_for(Path("backend/src/bfx_funding_bot") / source, tmp_path)

    assert tests("modules/ledger/x.py") == Path("backend/tests/modules/ledger")
    assert tests("modules/ledger/read/deep/x.py") == Path("backend/tests/modules/ledger")
    assert tests("core/x.py") == Path("backend/tests/core")
    assert tests("modules/unmirrored/x.py") is None        # only tests/modules mirrors it: a whole layer
    assert tests("top.py") is None                          # only tests does
    assert changed.tests_for(Path("deploy/vm/ops/x.py"), tmp_path) is None


def test_changed_functions_reads_only_package_sources_that_still_exist(tmp_path: Path) -> None:
    source = tmp_path / "backend/src/bfx_funding_bot/modules/ledger/x.py"
    source.parent.mkdir(parents=True)
    source.write_text("\n\ndef first():\n    value = 1\n    return value\n")
    assert changed.changed_functions(DIFF, tmp_path) == {
        Path("backend/src/bfx_funding_bot/modules/ledger/x.py"): ["first"]}
    outside = DIFF.replace("backend/src/bfx_funding_bot/modules/ledger/x.py", "backend/scripts/x.py")
    assert changed.changed_functions(outside, tmp_path) == {}


def test_judging_asks_the_matrix_for_those_functions_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, object] = {}

    def fake(sources: list[Path], selection: list[str], jobs: int, out: Path, **kwargs: object) -> dict[str, object]:
        seen.update(kwargs, sources=sources, selection=selection, out=out)
        return {"functions": ["derive_capital"], "survived": ["m.x_derive_capital__mutmut_2"], "killed": 4,
                "mutants": 5, "survivor_diffs": {"m.x_derive_capital__mutmut_2": "-a\n+b"},
                "timed_out": ["m.x_derive_capital__mutmut_7"], "crashed": []}

    monkeypatch.setattr(matrix, "build", fake)
    source = Path("backend/src/bfx_funding_bot/modules/trading/capital.py")
    result = changed.judge(source, ["derive_capital", "SymbolCapital.__post_init__"], tmp_path, 2, 300)
    assert seen["sources"] == [source]
    assert seen["selection"] == ["-m", changed.UNIT_ONLY, "tests/modules/trading"]
    assert seen["functions"] == ["derive_capital", "SymbolCapital.__post_init__"]
    assert seen["derived_functions"] is True and seen["show_survivors"] is True and seen["max_mutants"] == 300
    assert (result.killed, result.mutants, result.survived) == (4, 5, ("m.x_derive_capital__mutmut_2",))
    assert result.unjudged == 1
    assert result.functions == ("derive_capital",)          # what was judged, not what was asked

    def cannot(*args: object, **kwargs: object) -> dict[str, object]:
        raise matrix.MatrixError("no test broke with every mutated function raising\ndetail")

    monkeypatch.setattr(matrix, "build", cannot)
    skipped = changed.judge(source, ["derive_capital"], tmp_path, 2)
    assert skipped.skipped == ("the unit tests under backend/tests/modules/trading do not run this file "
                               "(an integration test may)")
    assert not skipped.survived

    def other(*args: object, **kwargs: object) -> dict[str, object]:
        raise matrix.MatrixError("mutmut wrote no mutant for f\nmore")

    monkeypatch.setattr(matrix, "build", other)
    assert changed.judge(source, ["f"], tmp_path, 2).skipped == "mutmut wrote no mutant for f"
    unmirrored = changed.judge(Path("backend/src/bfx_funding_bot/top.py"), ["f"], tmp_path, 2)
    assert unmirrored.skipped == "no test directory mirrors this file"


def test_the_summary_lists_every_source_and_shows_the_survivors() -> None:
    source = Path("backend/src/bfx_funding_bot/modules/trading/capital.py")
    judged = [
        changed.Judged(source, ("derive_capital",), Path("backend/tests/modules/trading"),
                       ("m.x_derive_capital__mutmut_2", "m.x_derive_capital__mutmut_9"), 3, 5,
                       diffs={"m.x_derive_capital__mutmut_2": "-old\n+new"}),
        changed.Judged(Path("backend/src/bfx_funding_bot/modules/ledger/x.py"), ("f",), skipped="why"),
    ]
    text = changed.render(judged, [Path("backend/src/bfx_funding_bot/late.py")], "origin/dev")
    assert "| `backend/src/bfx_funding_bot/modules/trading/capital.py` | `derive_capital` " \
           "| `backend/tests/modules/trading` | 3/5 | 2 |" in text
    assert "| not judged | why |" in text
    assert "the time budget ran out before this file" in text
    hung = changed.Judged(source, ("f",), Path("backend/tests/modules/trading"), (), 3, 3, unjudged=2)
    assert "| 3/5 (2 timed out or crashed) | 0 |" in changed.render([hung], [], "b")
    piped = changed.Judged(source, ("f",), skipped="could not mutate x: a | b\nmore")
    assert "| not judged | could not mutate x: a \\| b more |" in changed.render([piped], [], "b")
    fenced = changed.Judged(source, ("f",), Path("backend/tests/modules/trading"), ("m.x_f__mutmut_1",), 0, 1,
                            diffs={"m.x_f__mutmut_1": "-x = '```'\n+x = '````'\n</details>"})
    shown_fence = changed.render([fenced], [], "b")
    assert "`````diff\n-x = '```'" in shown_fence and "</details>\n`````\n\n</details>" in shown_fence
    assert "<code>x_derive_capital__mutmut_2</code>" in text and "-old\n+new" in text
    assert "(mutmut could not show this mutant)" in text    # mutmut_9 has no diff
    assert "modules/ledger/x.py`: " not in text             # nothing survived there: no section
    many = changed.Judged(source, ("f",), Path("backend/tests/modules/trading"),
                          tuple(f"m.x_f__mutmut_{n}" for n in range(changed.SHOWN + 3)), 0, changed.SHOWN + 3)
    shown = changed.render([many], [], "origin/dev")
    assert shown.count("<details>") == changed.SHOWN and f"The first {changed.SHOWN} are shown" in shown
    nothing = changed.render([], [], "origin/dev")
    assert "No function under `backend/src/bfx_funding_bot` changed against `origin/dev`." in nothing


def test_an_added_line_that_looks_like_a_header_is_content() -> None:
    diff = DIFF.replace("+    value = 1\n", "++ b/backend/src/bfx_funding_bot/other.py\n")
    assert list(changed.changed_lines(diff)) == [Path("backend/src/bfx_funding_bot/modules/ledger/x.py")]
    quoted = '--- "a/backend/src/bfx_funding_bot/m é.py"\n+++ "b/backend/src/bfx_funding_bot/m é.py"\n@@ -1 +1 @@\n'
    assert changed.changed_lines(quoted) == {Path("backend/src/bfx_funding_bot/m é.py"): [(1, 1)]}


def test_a_property_and_its_setter_are_one_name() -> None:
    source = ("class A:\n    @property\n    def size(self):\n        return 1\n\n"
              "    @size.setter\n    def size(self, value):\n        self._size = value\n")
    assert changed.functions_at(source, [(1, 8)]) == ["A.size"]


def test_main_reports_and_exits_zero_whatever_survives(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    sources = [Path(f"backend/src/bfx_funding_bot/modules/ledger/{name}.py") for name in ("a", "b", "c")]
    monkeypatch.setattr(changed, "changed_functions", lambda diff: {source: ["f"] for source in sources})
    monkeypatch.setattr(changed.subprocess, "run", lambda *args, **kwargs: type("Done", (), {"stdout": ""})())
    clock = iter([0.0, 1.0, 950.0, 951.0])                  # started, before a, before b, before c
    monkeypatch.setattr(changed.time, "monotonic", lambda: next(clock))
    asked: list[tuple[Path, int | None]] = []

    def judge(source: Path, functions: list[str], out_dir: Path, jobs: int, max_mutants: int | None) -> changed.Judged:
        asked.append((source, max_mutants))
        return changed.Judged(source, ("f",), Path("backend/tests/modules/ledger"), ("m.x_f__mutmut_1",), 0, 1)

    monkeypatch.setattr(changed, "judge", judge)
    summary = tmp_path / "new" / "summary.md"               # its directory does not exist yet
    summary.parent.mkdir()
    summary.write_text("earlier step\n")
    out = tmp_path / "reports"
    assert changed.main(["--base", "origin/dev", "--summary", str(summary), "--out-dir", str(out),
                         "--max-mutants", "50"]) == 0
    assert asked == [(sources[0], 50)]                      # b and c start after the budget
    text = summary.read_text()
    assert text.startswith("earlier step\n## Mutation report")   # appended, not overwritten
    assert text.count("the time budget ran out before this file") == 2
    import json
    listed = json.loads((out / "mutation-changed.json").read_text())
    assert [entry["source"] for entry in listed] == [str(source) for source in sources]
    assert listed[0]["survived"] == ["m.x_f__mutmut_1"] and listed[1]["skipped"] == "time budget"
