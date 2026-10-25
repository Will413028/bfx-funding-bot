"""Mutation kill matrix: which tests kill which mutants of one module, and which tests no mutant needs.

mutmut (3.8.0, dev dependency) writes the mutants; this script runs them. mutmut itself stops at
the first failing test and records killed or survived per mutant, never which tests killed it,
so it cannot say which tests are redundant. Here every selected test runs against every mutant:

1. the repository's tracked files are copied to a temporary directory and mutmut mutates the
   ``--source`` files there (the working tree is never touched; the sources are checked
   unchanged at the end);
2. the selected tests run from the copy's ``backend/``: its ``pythonpath = ["src"]`` puts the
   copy's package first, and a test that loads a file by its path (``deploy/vm/ops``) finds the
   copy's. A canary proves it: with ``MUTANT_UNDER_TEST=fail`` every mutated function raises,
   so some test must fail;
3. each mutant is switched on with ``MUTANT_UNDER_TEST`` (``<module>.<mutant>``, where the module
   is the name the code runs under: dotted from ``backend/src`` for the package, else
   ``--module-name``, e.g. the name a test gives ``spec_from_file_location``) and the whole
   selection runs (no
   ``-x``); the JUnit report names every test that failed or errored (the baseline has
   neither, so the mutant caused it). A run past ten times the baseline's duration (at least
   a minute) is stopped and listed under ``timed_out``.

The report (``--out``, JSON) has, per mutant, the test functions that killed it (parameter ids
dropped), and per test function its kills and the kills no other test shares. ``survived`` are
mutants no selected test kills: rules no test defends. ``not_in_cover`` are the test functions a
greedy cover of every killed mutant does not need: candidates to remove together, judged
against these mutants only, so a test that guards another module or documents an incident can
still be needed. Nothing is removed by this script.

Usage (paths relative to the repository root; the CI workflow ``mutation-kill-matrix.yml``
runs it on demand and uploads the report):
    cd backend
    uv run python scripts/mutation_kill_matrix.py \\
        --source backend/src/bfx_funding_bot/modules/ledger/conservation.py \\
        --out /tmp/kill-matrix.json -- -m "not integration" tests/modules/ledger
    uv run python scripts/mutation_kill_matrix.py --source deploy/vm/ops/bfx_deploy.py \\
        --module-name vm_ops_bfx_deploy_under_test --out /tmp/deploy.json \\
        --pytest-args 'tests/scripts/test_vm_deploy.py'

Exit codes: 0 = report written; 2 = cannot judge (baseline failing or erroring, canary not
caught, a run without a report, or src/ changed).
"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ElementTree
from collections.abc import Iterable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any

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

    @property
    def broken(self) -> frozenset[str]:
        return self.failed | self.errored


def function_of(case_id: str) -> str:
    """A JUnit test id without its parameter id: ``f[3]`` and ``f[4]`` are one test function."""
    return case_id.split("[", 1)[0]


def read_report(report: Path) -> Run:
    root = ElementTree.parse(report).getroot()
    failed: set[str] = set()
    errored: set[str] = set()
    for case in root.iter("testcase"):
        name = f"{case.get('classname', '')}::{case.get('name', '')}"
        if case.find("error") is not None:
            errored.add(name)
        elif case.find("failure") is not None:
            failed.add(name)
    return Run(frozenset(failed), frozenset(errored))


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


def _digest(paths: Iterable[Path]) -> str:
    digest = hashlib.sha256()
    for path in paths:
        digest.update(str(path).encode())
        digest.update((REPO / path).read_bytes())
    return digest.hexdigest()


def module_of(source: Path, module_name: str | None) -> str:
    """The name ``source`` runs under: dotted from ``backend/src``, else ``module_name``."""
    if module_name:
        return module_name
    if source.is_relative_to(PACKAGE_ROOT):
        return ".".join(source.relative_to(PACKAGE_ROOT).with_suffix("").parts)
    raise MatrixError(f"{source} is outside backend/src: name the module it runs under "
                      "with --module-name")


def _copy_repository(copy: Path) -> None:
    tracked = subprocess.run(["git", "-C", str(REPO), "ls-files", "-z"], capture_output=True,
                             check=True).stdout.decode().split("\0")
    for name in filter(None, tracked):
        source = REPO / name
        if source.is_file() and not source.is_symlink():
            target = copy / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
        elif source.is_symlink():
            target = copy / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.symlink_to(os.readlink(source))


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


def _pytest(copy: Path, selection: Sequence[str], report: Path, mutant: str | None,
            timeout: float) -> subprocess.CompletedProcess[str] | None:
    command = [sys.executable, "-m", "pytest", "-q", "--tb=no", "-p", "no:randomly",
               "-p", "no:cacheprovider", f"--junitxml={report}", *selection]
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
    env.pop("MUTANT_UNDER_TEST", None)
    if mutant is not None:
        env["MUTANT_UNDER_TEST"] = mutant
    try:
        return subprocess.run(command, cwd=copy / "backend", env=env, capture_output=True,
                              text=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired:
        return None


def _run(copy: Path, selection: Sequence[str], workdir: Path, mutant: str | None,
         timeout: float = 3600) -> Run | None:
    """The selection's result with ``mutant`` on, or None when it ran past ``timeout``."""
    report = workdir / f"{mutant or 'baseline'}.xml"
    result = _pytest(copy, selection, report, mutant, timeout)
    if result is None:
        return None
    if not report.is_file():
        raise MatrixError(f"{mutant or 'baseline'}: pytest exit {result.returncode}, no report\n"
                          f"{result.stdout[-2000:]}{result.stderr[-1000:]}")
    return read_report(report)


def build(sources: Sequence[Path], selection: Sequence[str], jobs: int,
          module_name: str | None = None) -> dict[str, Any]:
    before = _digest(sources)
    with tempfile.TemporaryDirectory(prefix="mutation-kill-matrix-") as tmp:
        workdir = Path(tmp)
        copy = workdir / "repo"
        _copy_repository(copy)
        names = _mutate(copy, sources, module_name)
        started = time.monotonic()
        baseline = _run(copy, selection, workdir, None)
        timeout = max(60.0, 10 * (time.monotonic() - started))
        if baseline is None or baseline.broken:
            raise MatrixError("the selected tests fail or error without any mutant")
        canary = _run(copy, selection, workdir, "fail")
        if canary is None or not canary.broken:
            raise MatrixError("no test broke with every mutated function raising: the selected "
                              "tests do not import the mutated copy, or do not reach the module")
        with ThreadPoolExecutor(max_workers=jobs) as pool:
            runs = dict(zip(names, pool.map(
                lambda name: _run(copy, selection, workdir, name, timeout), names), strict=True))
    if _digest(sources) != before:
        raise MatrixError("a source changed while the matrix ran")
    report = summarize({m: {function_of(t) for t in run.broken}
                        for m, run in runs.items() if run is not None})
    report["timed_out"] = sorted(m for m, run in runs.items() if run is None)
    report["sources"] = [str(s) for s in sources]
    report["selection"] = list(selection)
    return report


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0] if __doc__ else None)
    parser.add_argument("--source", type=Path, action="append", required=True,
                        help="a file to mutate, relative to the repository root (repeatable)")
    parser.add_argument("--module-name",
                        help="the module the sources run under, for a file loaded by its path")
    parser.add_argument("--out", type=Path, required=True, help="where the JSON report goes")
    parser.add_argument("--jobs", type=int, default=4, help="mutants run at once (default 4)")
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
        report = build(args.source, selection, args.jobs, args.module_name)
    except MatrixError as exc:
        print(f"mutation kill matrix cannot judge: {exc}", file=sys.stderr)
        return 2
    args.out.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    print(f"{report['killed']}/{report['mutants']} mutants killed, "
          f"{len(report['survived'])} survived, {len(report['timed_out'])} timed out; "
          f"{len(report['not_in_cover'])}/{len(report['tests'])} killing test functions outside "
          f"the cover (report: {args.out})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
