"""Mutation kill matrix: which tests kill which mutants of one module, and which tests no mutant needs.

mutmut (3.8.0, dev dependency) writes the mutants; this script runs them. mutmut itself stops at
the first failing test and records killed or survived per mutant, never which tests killed it,
so it cannot say which tests are redundant. Here every selected test runs against every mutant:

1. the repository's tracked and untracked, non-ignored files are copied to a temporary
   directory (``scripts/mutation_sandbox.py``, shared with the capital gate) and mutmut mutates the
   ``--source`` files there (the working tree is never touched; the sources are checked
   unchanged at the end);
2. the selected tests run from the copy's ``backend/``: its ``pythonpath = ["src", "."]`` puts
   the copy's package and scripts first, and a test that loads a file by its path (``deploy/vm/ops``) finds the
   copy's. A canary proves it: with ``MUTANT_UNDER_TEST=fail`` every mutated function raises,
   so some test must fail;
3. the selection runs once with ``MUTANT_UNDER_TEST=stats`` (the originals run, so this is the
   baseline: nothing may fail) and ``scripts/mutation_kill_matrix_stats.py`` records which
   mutated functions each test calls;
4. each mutant is switched on with ``MUTANT_UNDER_TEST`` (``<module>.<mutant>``, where the module
   is the name the code runs under: dotted from ``backend/src`` for the package, else
   ``--module-name``, e.g. the name a test gives ``spec_from_file_location``) and every test
   that calls its function runs (no ``-x``); the JUnit report names every test that failed or
   errored (the baseline has neither, so the mutant caused it). A mutant no test calls is
   ``not_reached`` (and survived); a run past ten times the baseline's duration (at least a
   minute) is ``timed_out``; a run that leaves no readable report (the mutant crashed pytest)
   is ``crashed``. The report is rewritten after every mutant, ``complete`` false until the
   last, so a run cut short still leaves what it judged.

The report (``--out``, JSON) has, per mutant, the test functions that killed it (parameter ids
dropped), and per test function its kills and the kills no other test shares. ``survived`` are
mutants no selected test kills: rules no test defends. ``not_in_cover`` are the test functions a
greedy cover of every killed mutant does not need: candidates to remove together, judged
against these mutants only, so a test that guards another module or documents an incident can
still be needed. Nothing is removed by this script.

Usage (paths relative to the repository root; the CI workflow ``mutation-kill-matrix.yml``
runs it on demand and uploads the report):
    cd backend
    uv run python -m scripts.mutation_kill_matrix \\
        --source backend/src/bfx_funding_bot/modules/ledger/conservation.py \\
        --out /tmp/kill-matrix.json -- -m "not integration" tests/modules/ledger
    uv run python -m scripts.mutation_kill_matrix --source deploy/vm/ops/bfx_deploy.py \\
        --module-name vm_ops_bfx_deploy_under_test --out /tmp/deploy.json \\
        --pytest-args 'tests/scripts/test_vm_deploy.py'

``--function NAME`` (repeatable) judges only the mutants of that function or method, which is
how to ask about the code one change touched: the whole of ``deployment/reconciler.py`` is
about 1,300 mutants, one method of it about 50. A report made this way speaks for those
functions only (the report lists them under ``functions``, empty for a whole-file run); its
``not_in_cover`` is not a list of tests to remove. A name that selects no mutant is an error.

Exit codes: 0 = report written; 2 = cannot judge (baseline failing, erroring or crashing,
canary not caught, or a source changed).
"""
from __future__ import annotations

import argparse
import contextlib
import json
import re
import shlex
import subprocess
import sys
import tempfile
import threading
import time
import xml.etree.ElementTree as ElementTree
from collections.abc import Iterable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from scripts import mutation_sandbox as sandbox

BACKEND = Path(__file__).resolve().parents[1]
REPO = BACKEND.parent
PACKAGE_ROOT = Path("backend/src")
_REGISTERED = re.compile(r"^mutants_\w+\['(?P<name>\w+__mutmut_\d+)'\] = ", re.MULTILINE)


class MatrixError(Exception):
    """The matrix cannot be judged: nothing it would report could be trusted."""


@dataclass(frozen=True, slots=True)
class Run:
    failed: frozenset[str]
    errored: frozenset[str]
    crashed: bool = False

    @property
    def broken(self) -> frozenset[str]:
        return self.failed | self.errored


def function_of(case_id: str) -> str:
    """A JUnit test id without its parameter id: ``f[3]`` and ``f[4]`` are one test function."""
    return case_id.split("[", 1)[0]


def read_report(report: Path) -> Run:
    """The failed and errored cases; a missing or unreadable report is a crashed run."""
    try:
        root = ElementTree.parse(report).getroot()
    except (OSError, ElementTree.ParseError):
        return Run(frozenset(), frozenset(), crashed=True)
    failed: set[str] = set()
    errored: set[str] = set()
    for case in root.iter("testcase"):
        name = f"{case.get('classname', '')}::{case.get('name', '')}"
        if case.find("error") is not None:
            errored.add(name)
        elif case.find("failure") is not None:
            failed.add(name)
    return Run(frozenset(failed), frozenset(errored))


def function_of_mutant(mutant: str) -> str:
    """The mutated function a mutant belongs to, as the stats plugin names it."""
    return mutant.rsplit("__mutmut_", 1)[0]


def tests_reaching(mutant: str, stats: Mapping[str, Iterable[str]]) -> list[str]:
    function = function_of_mutant(mutant)
    return sorted(test for test, functions in stats.items() if function in functions)


def mutant_names(mutated: Path, module: str) -> list[str]:
    """The mutants mutmut registered in one mutated file, as ``MUTANT_UNDER_TEST`` names them."""
    return [f"{module}.{m['name']}" for m in _REGISTERED.finditer(mutated.read_text())]


def summarize(kills: Mapping[str, Iterable[str]]) -> dict[str, Any]:
    """From each mutant's killing test functions: survivors, per-test kills, a greedy cover."""
    killers = {mutant: sorted(set(tests)) for mutant, tests in kills.items()}
    survived = sorted(m for m, tests in killers.items() if not tests)
    per_test: dict[str, set[str]] = {}
    for mutant, tests in killers.items():
        for test in tests:
            per_test.setdefault(test, set()).add(mutant)
    unique = {
        test: sorted(m for m in mutants if killers[m] == [test]) for test, mutants in per_test.items()
    }
    uncovered = {m for m, tests in killers.items() if tests}
    cover: list[str] = []
    while uncovered:
        best = max(sorted(per_test), key=lambda t: len(per_test[t] & uncovered))
        cover.append(best)
        uncovered -= per_test[best]
    return {
        "mutants": len(killers),
        "killed": len(killers) - len(survived),
        "survived": survived,
        "tests": {
            test: {"kills": len(per_test[test]), "unique": unique[test]} for test in sorted(per_test)
        },
        "cover": sorted(cover),
        "not_in_cover": sorted(set(per_test) - set(cover)),
        "kills": killers,
    }


def module_of(source: Path, module_name: str | None) -> str:
    """The name ``source`` runs under: dotted from ``backend/src``, else ``module_name``."""
    if module_name:
        return module_name
    if source.is_relative_to(PACKAGE_ROOT):
        return ".".join(source.relative_to(PACKAGE_ROOT).with_suffix("").parts)
    raise MatrixError(f"{source} is outside backend/src: name the module it runs under "
                      "with --module-name")


def _mutate(copy: Path, sources: Sequence[Path], module_name: str | None) -> list[str]:
    """Mutate ``sources`` in place in the copy; mutmut keeps its bookkeeping under the copy's
    ``mutants/``, relative to the working directory."""
    from mutmut.__main__ import create_mutants_for_file

    names: list[str] = []
    with contextlib.chdir(copy):
        for source in sources:
            module = module_of(source, module_name)
            (Path("mutants") / source).parent.mkdir(parents=True, exist_ok=True)
            result = create_mutants_for_file(source, source)
            if result.error is not None:
                raise MatrixError(f"mutmut could not mutate {source}: {result.error}")
            names += mutant_names(copy / source, module)
    if not names:
        raise MatrixError("mutmut wrote no mutant for the given sources")
    return names


def _pytest(copy: Path, tests: Sequence[str], report: Path, env: Mapping[str, str],
            timeout: float) -> subprocess.CompletedProcess[str] | None:
    # The copy's sources do not change after ``_mutate``: every run may share one bytecode cache.
    return sandbox.pytest(copy, ["-q", "--tb=no", "-p", "no:randomly",
                                 f"--junitxml={report}", *tests], env=env, timeout=timeout,
                          bytecode=True)


def _env(mutant: str) -> dict[str, str]:
    return {"MUTANT_UNDER_TEST": mutant}


def _baseline(copy: Path, selection: Sequence[str], workdir: Path) -> dict[str, list[str]]:
    """Run the selection on the originals, recording which mutated functions each test calls."""
    stats = workdir / "stats.json"
    env = {**_env("stats"), "KILL_MATRIX_STATS": str(stats)}
    report = workdir / "baseline.xml"
    result = _pytest(copy, ["-p", "scripts.mutation_kill_matrix_stats", *selection], report, env,
                     timeout=3600)
    run = read_report(report)
    if result is None or run.crashed or run.broken or not stats.is_file():
        tail = "" if result is None else f"{result.stdout[-2000:]}{result.stderr[-1000:]}"
        raise MatrixError(f"the selected tests do not pass cleanly without any mutant\n{tail}")
    recorded: dict[str, list[str]] = json.loads(stats.read_text())
    return recorded


def _judge(copy: Path, workdir: Path, mutant: str, tests: Sequence[str],
           timeout: float) -> Run | None:
    """The tests' result with ``mutant`` on, or None when they ran past ``timeout``."""
    selection = workdir / f"{mutant}.tests"
    selection.write_text("\n".join(tests) + "\n")
    report = workdir / f"{mutant}.xml"
    if _pytest(copy, [f"@{selection}"], report, _env(mutant), timeout) is None:
        return None
    return read_report(report)


def _report(results: Mapping[str, Run | str | None], total: int) -> dict[str, Any]:
    judged = {m: r for m, r in results.items() if isinstance(r, Run) and not r.crashed}
    report = summarize({m: {function_of(t) for t in run.broken} for m, run in judged.items()}
                       | {m: set() for m, r in results.items() if r == "not_reached"})
    report["not_reached"] = sorted(m for m, r in results.items() if r == "not_reached")
    report["timed_out"] = sorted(m for m, r in results.items() if r is None)
    report["crashed"] = sorted(m for m, r in results.items() if isinstance(r, Run) and r.crashed)
    report["complete"] = len(results) == total
    report["mutants_total"] = total
    return report


def in_functions(mutant: str, functions: Sequence[str], exact: bool = False) -> bool:
    """Whether ``mutant`` belongs to one of ``functions``, each a function or method name as
    written in the source (``_pull_if_stopped``, or ``DeploymentReconciler._pull_if_stopped``
    when two classes of the file have a method of that name). A bare name also means the
    method of that name in every class, unless ``exact``: then it is the top-level function only."""
    owner = function_of_mutant(mutant).rsplit(".", 1)[-1]          # x_name, or xǁClassǁname
    if owner.startswith("xǁ"):
        klass, _, name = owner[2:].partition("ǁ")
        return f"{klass}.{name}" in functions or (not exact and name in functions)
    return owner.removeprefix("x_") in functions


def select_mutants(names: Sequence[str], functions: Sequence[str], derived: bool = False) -> list[str]:
    """The mutants of ``functions``, or all of ``names`` when no function is named. Every name
    must select at least one mutant: a misspelt name beside a good one would otherwise be
    dropped without a word, and the report would cover less than was asked.

    ``derived`` is for names a program read off the source (``mutation_changed.py``): a bare
    name is the top-level function only, since that program writes methods as ``Class.method``,
    and a function mutmut has nothing to mutate in (a body that is one ``return name``) is left
    out; only a selection with no mutant at all is an error."""
    if not functions:
        return list(names)
    unmatched = [f for f in functions if not any(in_functions(name, [f], derived) for name in names)]
    if unmatched and (not derived or len(unmatched) == len(functions)):
        raise MatrixError(f"mutmut wrote no mutant for {', '.join(unmatched)}: name functions or "
                          "methods of the sources as the source spells them")
    return [name for name in names if in_functions(name, functions, derived)]


def survivor_diffs(copy: Path, sources: Sequence[Path], module_name: str | None,
                   survived: Sequence[str]) -> dict[str, str]:
    """mutmut's diff of each surviving mutant against the function it mutates; an empty string
    for one it cannot show. Read from the mutated copy, so call it while the copy exists."""
    from mutmut.mutation.diff_apply import get_diff_for_mutant

    texts = {module_of(source, module_name): (source, (copy / source).read_text()) for source in sources}
    diffs: dict[str, str] = {}
    for mutant in survived:
        source, text = next(((s, t) for module, (s, t) in texts.items() if mutant.startswith(module + ".")),
                            (None, ""))
        try:
            diffs[mutant] = get_diff_for_mutant(mutant, source=text, path=source) if source else ""
        except (KeyError, ValueError, IndexError, AttributeError, TypeError, OSError):
            diffs[mutant] = ""          # a diff is a convenience; the verdict does not depend on it
    return diffs


def build(sources: Sequence[Path], selection: Sequence[str], jobs: int, out: Path,
          module_name: str | None = None, functions: Sequence[str] = (), *,
          derived_functions: bool = False, show_survivors: bool = False,
          max_mutants: int | None = None) -> dict[str, Any]:
    before = sandbox.digest(sources)
    with tempfile.TemporaryDirectory(prefix="mutation-kill-matrix-") as tmp:
        workdir = Path(tmp)
        copy = workdir / "repo"
        sandbox.copy_repository(copy)
        names = select_mutants(_mutate(copy, sources, module_name), functions, derived_functions)
        if max_mutants is not None and len(names) > max_mutants:
            raise MatrixError(f"{len(names)} mutants, over the limit of {max_mutants} for one run")
        judged_functions = sorted({f for f in functions
                                   if any(in_functions(name, [f], derived_functions) for name in names)})
        started = time.monotonic()
        stats = _baseline(copy, selection, workdir)
        timeout = max(60.0, 10 * (time.monotonic() - started))
        canary = _pytest(copy, list(selection), workdir / "canary.xml", _env("fail"), timeout)
        if canary is None or not read_report(workdir / "canary.xml").broken:
            raise MatrixError("no test broke with every mutated function raising: the selected "
                              "tests do not import the mutated copy, or do not reach the module")
        results: dict[str, Run | str | None] = {}
        lock = threading.Lock()
        reached = {name: tests_reaching(name, stats) for name in names}
        for name, tests in reached.items():
            if not tests:
                results[name] = "not_reached"
        print(f"{len(names)} mutants, {len(results)} reached by no selected test", flush=True)
        with ThreadPoolExecutor(max_workers=jobs) as pool:
            futures = {pool.submit(_judge, copy, workdir, name, tests, timeout): name
                       for name, tests in reached.items() if tests}
            for future in as_completed(futures):
                name, run = futures[future], future.result()
                with lock:
                    results[name] = run
                    status = ("timed out" if run is None else "crashed" if run.crashed
                              else "killed" if run.broken else "survived")
                    print(f"[{len(results)}/{len(names)}] {name}: {status}", flush=True)
                    report = _report(results, len(names))
                    report["sources"], report["selection"] = [str(s) for s in sources], list(selection)
                    report["functions"] = judged_functions
                    out.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
        diffs = (survivor_diffs(copy, sources, module_name, _report(results, len(names))["survived"])
                 if show_survivors else None)
    if sandbox.digest(sources) != before:
        raise MatrixError("a source changed while the matrix ran")
    report = _report(results, len(names))
    report["sources"], report["selection"] = [str(s) for s in sources], list(selection)
    report["functions"] = judged_functions        # empty: every mutant of the sources was judged
    if diffs is not None:
        report["survivor_diffs"] = diffs
    out.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    return report


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0] if __doc__ else None)
    parser.add_argument("--source", type=Path, action="append", required=True,
                        help="a file to mutate, relative to the repository root (repeatable)")
    parser.add_argument("--module-name",
                        help="the module the sources run under, for a file loaded by its path")
    parser.add_argument("--out", type=Path, required=True, help="where the JSON report goes")
    parser.add_argument("--jobs", type=int, default=4, help="mutants run at once (default 4)")
    parser.add_argument("--function", action="append", default=[], metavar="NAME",
                        help="judge only the mutants of this function or method (repeatable; "
                             "Class.method when the name alone is ambiguous)")
    parser.add_argument("--pytest-args", default="",
                        help="the tests to run as one shell-quoted string (for the workflow)")
    parser.add_argument("selection", nargs="*", help="pytest arguments after --: the tests to run")
    args = parser.parse_args(argv)
    selection = [*shlex.split(args.pytest_args), *args.selection]
    if not selection:
        parser.error("name the tests to run, after -- or with --pytest-args")
    for source in args.source:
        if source.is_absolute() or ".." in source.parts or not (REPO / source).is_file():
            parser.error(f"--source must be a file relative to the repository root: {source}")
    try:
        report = build(args.source, selection, args.jobs, args.out, args.module_name,
                       args.function)
    except MatrixError as exc:
        print(f"mutation kill matrix cannot judge: {exc}", file=sys.stderr)
        return 2
    print(f"{report['killed']}/{report['mutants']} mutants killed, "
          f"{len(report['survived'])} survived ({len(report['not_reached'])} reached by no test), "
          f"{len(report['timed_out'])} timed out, {len(report['crashed'])} crashed; "
          f"{len(report['not_in_cover'])}/{len(report['tests'])} killing test functions outside "
          f"the cover (report: {args.out})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
