"""Mutation report for the functions a change touched: which of their mutants no test kills.

``mutation_kill_matrix.py`` answers the question for a whole file in hours. A review needs it
for the code in front of it, in minutes, so this script

1. reads the change (``git diff <base> HEAD``) and keeps the lines it adds, modifies or removes
   lines beside, in the Python files under ``backend/src/bfx_funding_bot``;
2. names the functions those lines are in: top-level functions, and methods of top-level
   classes as ``Class.method`` (what mutmut mutates; a nested function counts as its outer
   one, and a line outside any function, such as a constant or a class attribute, names none);
3. runs the kill matrix for those functions only, against the unit tests of the directory that
   mirrors the source (``src/bfx_funding_bot/modules/ledger/x.py`` -> ``tests/modules/ledger``);
4. writes one Markdown summary: per source, the mutants that survived, as mutmut's diff.

It reports; it does not gate. A surviving mutant is a line the mirrored unit tests would let
change, which is a question for the reviewer, not a verdict: the mutant may be equivalent, or
an integration test may be what defends it (those are not run here). The exit code is 0 unless
the script itself fails; a source it could not judge is named in the summary with the reason
(its unit tests do not run it, or more than ``--max-mutants`` mutants would have to run). A
mutant in code no unit test runs is counted apart, not as a survivor: nothing was tried on it.

Usage (from ``backend/``):
    uv run python -m scripts.mutation_changed --base origin/dev --summary /tmp/mutation.md
    uv run python -m scripts.mutation_changed --base HEAD^1 --summary "$GITHUB_STEP_SUMMARY" \\
        --out-dir "$RUNNER_TEMP/mutation" --budget-seconds 900
"""
from __future__ import annotations

import argparse
import ast
import json
import re
import subprocess
import sys
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

from scripts import mutation_kill_matrix as matrix

BACKEND = Path(__file__).resolve().parents[1]
REPO = BACKEND.parent
PACKAGE = Path("backend/src/bfx_funding_bot")
TESTS = Path("backend/tests")
UNIT_ONLY = "not integration and not gate and not benchmark"   # the unit job's selection (ci.yml)
_HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(?P<start>\d+)(?:,(?P<count>\d+))? @@")
# Directories that mirror nothing narrower than a whole layer: too wide to be "the tests of this file".
_TOO_WIDE = frozenset({Path("backend/tests"), Path("backend/tests/modules")})
_Function = ast.FunctionDef | ast.AsyncFunctionDef
SHOWN = 40      # surviving mutants shown per source; the JSON report has all of them


@dataclass(frozen=True, slots=True)
class Judged:
    """One changed source and what the kill matrix said about its changed functions."""

    source: Path
    functions: tuple[str, ...]
    tests: Path | None = None
    survived: tuple[str, ...] = ()
    killed: int = 0
    mutants: int = 0
    skipped: str = ""                       # why it was not judged; empty when it was
    unjudged: int = 0                       # mutants that timed out or crashed: neither killed nor survived
    not_run: int = 0                        # mutants in code no selected test runs; not in ``mutants``
    diffs: dict[str, str] = field(default_factory=dict)


def changed_lines(diff: str) -> dict[Path, list[tuple[int, int]]]:
    """Per file, the line ranges (first, last; in the new file) a unified diff touches. A hunk
    that only removes lines touches the two lines the removal sits between: lines taken out of
    a function change it, and the diff alone does not say which side they belonged to."""
    ranges: dict[Path, list[tuple[int, int]]] = {}
    current: Path | None = None
    previous = ""
    for line in diff.splitlines():
        # A file header is the pair "--- old" then "+++ new"; an added line that starts with
        # "++ " also begins with "+++ ", but the line before it is not "--- ".
        if line.startswith("+++ ") and previous.startswith("--- "):
            name = line[4:].strip().strip('"')
            current = None if name == "/dev/null" else Path(name.removeprefix("b/"))
            previous = line
            continue
        previous = line
        hunk = _HUNK.match(line)
        if hunk and current is not None:
            count = int(hunk["count"]) if hunk["count"] is not None else 1
            start = int(hunk["start"])              # for a removal, the line it comes after
            last = start + count - 1 if count else start + 1
            ranges.setdefault(current, []).append((start, last))
    return ranges


def functions_at(source: str, ranges: Sequence[tuple[int, int]]) -> list[str]:
    """The functions of ``source`` that ``ranges`` touch, as the kill matrix names them: a
    top-level function by its name, a method of a top-level class as ``Class.method``. The span
    of a function starts at its first decorator. Source that does not parse names none."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []

    def touched(node: _Function) -> bool:
        first = min([node.lineno, *(decorator.lineno for decorator in node.decorator_list)])
        last = node.end_lineno or node.lineno
        return any(start <= last and end >= first for start, end in ranges)

    found: list[str] = []
    for node in tree.body:
        if isinstance(node, _Function):
            if touched(node):
                found.append(node.name)
        elif isinstance(node, ast.ClassDef):
            found += [f"{node.name}.{item.name}" for item in node.body
                      if isinstance(item, _Function) and touched(item)]
    return list(dict.fromkeys(found))       # a property and its setter share one name


def tests_for(source: Path, repo: Path = REPO) -> Path | None:
    """The nearest existing test directory that mirrors ``source``'s package directory, or None
    when the only mirror is a whole layer (``tests``, ``tests/modules``)."""
    try:
        relative = source.parent.relative_to(PACKAGE)
    except ValueError:
        return None
    candidate = TESTS / relative
    while candidate not in _TOO_WIDE and TESTS in candidate.parents:
        if (repo / candidate).is_dir():
            return candidate
        candidate = candidate.parent
    return None


def changed_functions(diff: str, repo: Path = REPO) -> dict[Path, list[str]]:
    """Per changed package source that still exists, the functions the diff touches."""
    out: dict[Path, list[str]] = {}
    for path, ranges in sorted(changed_lines(diff).items()):
        if path.suffix != ".py" or PACKAGE not in path.parents or not (repo / path).is_file():
            continue
        names = functions_at((repo / path).read_text(), ranges)
        if names:
            out[path] = names
    return out


def judge(source: Path, functions: Sequence[str], out_dir: Path, jobs: int,
          max_mutants: int | None = None) -> Judged:
    tests = tests_for(source)
    if tests is None:
        return Judged(source, tuple(functions), skipped="no test directory mirrors this file")
    selection = ["-m", UNIT_ONLY, str(tests.relative_to("backend"))]
    report_path = out_dir / (source.with_suffix("").as_posix().replace("/", "__") + ".json")
    try:
        report = matrix.build([source], selection, jobs, report_path, functions=functions,
                              derived_functions=True, show_survivors=True, max_mutants=max_mutants,
                              max_diffs=SHOWN)
    except matrix.MatrixError as exc:
        print(f"{source}: not judged: {exc}", flush=True)        # the whole reason, for the job log
        reason = str(exc).splitlines()[0]
        if reason.startswith("no test broke"):       # the matrix's canary: nothing selected runs this code
            reason = f"the unit tests under {tests} do not run this file (an integration test may)"
        return Judged(source, tuple(functions), tests, skipped=reason)
    not_run = set(report["not_reached"])     # the matrix counts these as survived
    survived = tuple(mutant for mutant in report["survived"] if mutant not in not_run)
    if not_run and len(not_run) == int(report["mutants"]):
        return Judged(source, tuple(report["functions"]), tests, not_run=len(not_run),
                      skipped=f"the unit tests under {tests} run none of these functions "
                              "(an integration test may)")
    return Judged(source, tuple(report["functions"]), tests, survived,
                  int(report["killed"]), int(report["mutants"]) - len(not_run),
                  diffs=dict(report["survivor_diffs"]), not_run=len(not_run),
                  unjudged=len(report["timed_out"]) + len(report["crashed"]))


def _cell(text: str) -> str:
    """Text for one table cell: a pipe would end the cell, a newline the row."""
    return text.replace("|", "\\|").replace("\n", " ")


def _fence(text: str) -> str:
    """A code fence longer than any run of backticks in ``text``."""
    longest = max((len(run) for run in re.findall(r"`+", text)), default=0)
    return "`" * max(3, longest + 1)


def render(judged: Sequence[Judged], not_started: Sequence[Path], base: str) -> str:
    lines = ["## Mutation report for the changed functions", ""]
    if not judged and not not_started:
        return "\n".join([*lines, f"No function under `{PACKAGE}` changed against `{base}`.", ""])
    lines += ["A surviving mutant is a change to the code that the mirrored unit tests let through. "
              "It is a question for review, not a failure: the mutant may be equivalent, or an "
              "integration test (not run here) may defend it.", "",
              "| Source | Functions | Tests | Killed | Survived |", "|---|---|---|---|---|"]
    for item in judged:
        names = ", ".join(f"`{name}`" for name in item.functions) or "-"
        if item.skipped:
            lines.append(f"| `{item.source}` | {names} | - | not judged | {_cell(item.skipped)} |")
        else:
            total = item.mutants + item.unjudged
            hung = f" ({item.unjudged} timed out or crashed)" if item.unjudged else ""
            hung += f" ({item.not_run} more in code these tests do not run)" if item.not_run else ""
            lines.append(f"| `{item.source}` | {names} | `{item.tests}` | {item.killed}/{total}{hung} "
                         f"| {len(item.survived)} |")
    for path in not_started:
        lines.append(f"| `{path}` | - | - | not judged | the time budget ran out before this file |")
    for item in judged:
        if not item.survived:
            continue
        lines += ["", f"### `{item.source}`: {len(item.survived)} surviving", ""]
        if len(item.survived) > SHOWN:
            lines += [f"The first {SHOWN} are shown; the JSON report in the artifact lists every one.", ""]
        for mutant in item.survived[:SHOWN]:
            short = mutant.rsplit(".", 1)[-1]
            diff = item.diffs.get(mutant, "") or "(mutmut could not show this mutant)"
            fence = _fence(diff)
            lines += [f"<details><summary><code>{short}</code></summary>", "",
                      f"{fence}diff", diff, fence, "", "</details>"]
    return "\n".join([*lines, ""])


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0] if __doc__ else None)
    parser.add_argument("--base", required=True, help="the revision the change is compared with")
    parser.add_argument("--summary", type=Path, required=True, help="Markdown file to append to")
    parser.add_argument("--out-dir", type=Path, help="where the JSON reports go (default: beside the summary)")
    parser.add_argument("--jobs", type=int, default=4, help="mutants run at once (default 4)")
    parser.add_argument("--budget-seconds", type=float, default=900.0,
                        help="start no further file once this much time has passed (default 900)")
    parser.add_argument("--max-mutants", type=int, default=400,
                        help="do not judge a file whose changed functions have more mutants (default 400)")
    args = parser.parse_args(argv)
    out_dir = args.out_dir or args.summary.parent
    out_dir.mkdir(parents=True, exist_ok=True)
    args.summary.parent.mkdir(parents=True, exist_ok=True)
    diff = subprocess.run(["git", "-C", str(REPO), "-c", "core.quotepath=off", "diff", "--unified=0",
                           "--no-color", args.base, "HEAD", "--", str(PACKAGE)],
                          capture_output=True, text=True, check=True).stdout
    started = time.monotonic()
    judged: list[Judged] = []
    not_started: list[Path] = []
    for source, functions in changed_functions(diff).items():
        if time.monotonic() - started > args.budget_seconds:
            not_started.append(source)
            continue
        print(f"{source}: {', '.join(functions)}", flush=True)
        judged.append(judge(source, functions, out_dir, args.jobs, args.max_mutants))
        _write_json(out_dir, judged, not_started)        # after every file: a run cut short keeps what it judged
    _write_json(out_dir, judged, not_started)
    with args.summary.open("a") as handle:
        handle.write(render(judged, not_started, args.base))
    return 0


def _write_json(out_dir: Path, judged: Sequence[Judged], not_started: Sequence[Path]) -> None:
    (out_dir / "mutation-changed.json").write_text(json.dumps(
        [{"source": str(j.source), "functions": list(j.functions), "tests": str(j.tests) if j.tests else None,
          "killed": j.killed, "mutants": j.mutants, "unjudged": j.unjudged, "not_run": j.not_run,
          "survived": list(j.survived),
          "skipped": j.skipped}
         for j in judged] + [{"source": str(p), "skipped": "time budget"} for p in not_started],
        indent=2) + "\n")


if __name__ == "__main__":
    sys.exit(main())
