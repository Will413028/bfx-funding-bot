"""Capital-rule mutation gate: every capital rule is killed by the test layer that owns it.

Each mutant is one exact source edit of a capital rule. The gate copies ``src/`` to a
temporary directory, applies the edit there (the working tree is never touched), and
runs ONLY the owning layer's test file(s) against the copy. A mutant those tests do
not kill is a rule no owning test defends, so the gate fails.

Rule ownership:
  evaluate_capital (trading/policy.py)       -> tests/modules/trading/test_policy.py
  derive_capital (trading/capital.py)        -> tests/modules/trading/test_capital.py
  allocate_capital (deployment/sizing.py)    -> tests/modules/execution/deployment/test_sizing.py
  reconciler wiring (deployment/reconciler)  -> tests/modules/execution/deployment/test_reconciler.py
  guard and command-gate re-validation       -> integration tests (need local PostgreSQL 18)

Every anchor must occur exactly once in its file: a refactor that moves or rewrites a
rule fails the gate loudly instead of silently skipping its mutant; update the anchor.

Usage:
    cd backend
    uv run python scripts/capital_mutation_gate.py                        # unit-owned mutants
    uv run python scripts/capital_mutation_gate.py --include-integration  # all mutants
    uv run python scripts/capital_mutation_gate.py --only M10,M15

Exit codes: 0 = every selected mutant killed; 1 = a mutant survived or its run errored;
2 = the gate itself cannot run (anchor drift, baseline failing, copy not imported).
"""
from __future__ import annotations

import argparse
import hashlib
import os
import re
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
PACKAGE = Path("src/bfx_funding_bot")

POLICY = PACKAGE / "modules/trading/policy.py"
CAPITAL = PACKAGE / "modules/trading/capital.py"
SIZING = PACKAGE / "modules/execution/deployment/sizing.py"
RECONCILER = PACKAGE / "modules/execution/deployment/reconciler.py"
HARD_GUARDS = PACKAGE / "modules/execution/safety/hard_guards.py"
JOURNAL = PACKAGE / "modules/ledger/_internal/journal.py"

POLICY_TESTS = ("tests/modules/trading/test_policy.py",)
CAPITAL_TESTS = ("tests/modules/trading/test_capital.py",)
SIZING_TESTS = ("tests/modules/execution/deployment/test_sizing.py",)
RECONCILER_TESTS = ("tests/modules/execution/deployment/test_reconciler.py",)
BOUNDARY_TESTS = ("tests/integration/test_capital_command_boundary.py",)
COMMAND_TESTS = ("tests/integration/test_ledger_command_authorize.py",)

_SPENDABLE = (
    "        snapshot.available_amount - snapshot.unreflected_commitments"
    " - policy.reserve_amount,\n"
)


class GateError(Exception):
    """The gate cannot judge the mutants (exit 2)."""


@dataclass(frozen=True, slots=True)
class Mutant:
    id: str
    description: str
    path: Path
    old: str
    new: str
    owners: tuple[str, ...]
    integration: bool = False


MUTANTS: tuple[Mutant, ...] = (
    Mutant("M1", "cell limit ignores max_cell_fraction", POLICY,
           "max(_ZERO, snapshot.total_capital - policy.reserve_amount) * policy.max_cell_fraction",
           "max(_ZERO, snapshot.total_capital - policy.reserve_amount) * 1", POLICY_TESTS),
    Mutant("M2", "default max_cell_fraction 0.70 -> 1", POLICY,
           'max_cell_fraction: Decimal = Decimal("0.70")',
           'max_cell_fraction: Decimal = Decimal("1")', POLICY_TESTS),
    Mutant("M3", "max_new_offer drops the cell headroom", POLICY,
           "max_new_offer = min(spendable, cell_headroom)", "max_new_offer = spendable",
           POLICY_TESTS),
    Mutant("M4", "cell headroom ignores the cell's exposure", POLICY,
           "cell_headroom = max(_ZERO, cell_limit - snapshot.cell_exposure)",
           "cell_headroom = cell_limit", POLICY_TESTS),
    Mutant("M5", "cell limit based on available instead of total capital", POLICY,
           "max(_ZERO, snapshot.total_capital - policy.reserve_amount) * policy.max_cell_fraction",
           "max(_ZERO, snapshot.available_amount - policy.reserve_amount)"
           " * policy.max_cell_fraction", POLICY_TESTS),
    Mutant("M6", "spendable does not subtract the reserve", POLICY, _SPENDABLE,
           "        snapshot.available_amount - snapshot.unreflected_commitments,\n",
           POLICY_TESTS),
    Mutant("M7", "spendable does not subtract unreflected commitments", POLICY, _SPENDABLE,
           "        snapshot.available_amount - policy.reserve_amount,\n", POLICY_TESTS),
    Mutant("M8", "allocation sizes on spendable instead of max_new_offer", SIZING,
           "limit = view.budget.max_new_offer", "limit = view.budget.spendable", SIZING_TESTS),
    Mutant("M9", "allocation does not draw down the shared pool", SIZING,
           "            remaining -= amount\n", "", SIZING_TESTS),
    Mutant("M10", "allocation is not emptiest-first", SIZING,
           "sorted(views, key=lambda name: (views[name].snapshot.cell_exposure, name))",
           "sorted(views)", SIZING_TESTS),
    Mutant("M11", "derive_capital drops this cell's unreflected attempts from E_cell", CAPITAL,
           "        if attempt.scope.cell_id == scope.cell_id:\n"
           "            exposure += attempt.amount\n", "", CAPITAL_TESTS),
    Mutant("M12", "capital policy guard drops the max_new_offer check", HARD_GUARDS,
           "amount <= 0 or amount > view.budget.max_new_offer:", "amount <= 0:",
           BOUNDARY_TESTS, integration=True),
    Mutant("M13", "command gate drops the budget check under the lock", JOURNAL,
           "    if amount > budget.max_new_offer:\n"
           '        return CommandRefused(budget.reason or "insufficient_deployable_funds")\n',
           "", COMMAND_TESTS, integration=True),
    Mutant("M14", "a single active cell is relaxed to spendable", SIZING,
           "limit = view.budget.max_new_offer",
           "limit = view.budget.max_new_offer if len(views) > 1 else view.budget.spendable",
           SIZING_TESTS),
    Mutant("M15", "reconciler reads every cell's capital with active[0]'s scope", RECONCILER,
           "self._scope.deployment_environment, symbol, cell),",
           "self._scope.deployment_environment, symbol, active[0]),", RECONCILER_TESTS),
)

_CANARY = 'raise ImportError("capital-mutation-gate canary")\n'
_IGNORE = shutil.ignore_patterns("__pycache__", "*.pyc")


def check_anchors(mutants: Sequence[Mutant], root: Path = BACKEND) -> None:
    """Every mutant's anchor occurs exactly once in its file."""
    problems = []
    for mutant in mutants:
        count = (root / mutant.path).read_text().count(mutant.old)
        if count != 1:
            problems.append(f"{mutant.id}: anchor found {count} times in {mutant.path}")
    if problems:
        raise GateError("; ".join(problems))


def _apply(src_copy: Path, path: Path, old: str, new: str) -> None:
    target = src_copy / path.relative_to("src")
    text = target.read_text()
    if text.count(old) != 1:
        raise GateError(f"anchor not unique in {path}")
    target.write_text(text.replace(old, new))


def _pytest(src_copy: Path, tests: Sequence[str], *extra: str) -> subprocess.CompletedProcess[str]:
    # -o pythonpath puts the copy ahead of the editable install; no cache is written.
    command = [sys.executable, "-m", "pytest", "-q", "-x", "-rf", "-p", "no:cacheprovider",
               "-o", f"pythonpath={src_copy} .", *extra, *tests]
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
    return subprocess.run(command, cwd=BACKEND, env=env, capture_output=True, text=True,
                          timeout=1800, check=False)


def _fresh_copy(workdir: Path, name: str) -> Path:
    copy = workdir / name / "src"
    shutil.copytree(BACKEND / "src", copy, ignore=_IGNORE)
    return copy


def _verify_copy_is_imported(workdir: Path) -> None:
    """A copy whose policy module cannot import must break collection, else tests read src/."""
    copy = _fresh_copy(workdir, "canary")
    target = copy / POLICY.relative_to("src")
    target.write_text(_CANARY + target.read_text())
    result = _pytest(copy, POLICY_TESTS, "--collect-only")
    if result.returncode == 0 or "capital-mutation-gate canary" not in result.stdout + result.stderr:
        raise GateError("the mutated copy is not what pytest imports; mutants would be untested")


def _first_failure(output: str) -> str:
    match = re.search(r"^FAILED (\S+)", output, re.MULTILINE)
    return match.group(1) if match else "?"


def _tree_digest() -> str:
    digest = hashlib.sha256()
    for path in sorted((BACKEND / "src").rglob("*.py")):
        digest.update(str(path).encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


def run(mutants: Sequence[Mutant]) -> int:
    check_anchors(mutants)
    before = _tree_digest()
    survivors: list[str] = []
    with tempfile.TemporaryDirectory(prefix="capital-mutation-gate-") as tmp:
        workdir = Path(tmp)
        _verify_copy_is_imported(workdir)
        owners = tuple(dict.fromkeys(test for mutant in mutants for test in mutant.owners))
        baseline = _pytest(_fresh_copy(workdir, "baseline"), owners)
        if baseline.returncode != 0:
            print(baseline.stdout[-4000:], baseline.stderr[-2000:], sep="\n")
            raise GateError("owning tests fail without any mutant")
        print(f"{'mutant':<6} {'result':<8} owner test")
        for mutant in mutants:
            copy = _fresh_copy(workdir, mutant.id)
            _apply(copy, mutant.path, mutant.old, mutant.new)
            result = _pytest(copy, mutant.owners)
            output = result.stdout + result.stderr
            if result.returncode == 1:
                status, detail = "killed", _first_failure(output)
            elif result.returncode == 0:
                status, detail = "SURVIVED", ", ".join(mutant.owners)
            else:
                status, detail = "ERROR", f"pytest exit {result.returncode}"
            if status != "killed":
                survivors.append(f"{mutant.id} ({mutant.description}): {status}")
                print(output[-3000:])
            print(f"{mutant.id:<6} {status:<8} {detail}  -- {mutant.description}")
    if _tree_digest() != before:
        raise GateError("src/ changed while the gate ran")
    if survivors:
        print("\nNot killed by the owning layer:\n  " + "\n  ".join(survivors))
        return 1
    print(f"\nAll {len(mutants)} mutants killed by their owning tests.")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0] if __doc__ else None)
    parser.add_argument("--include-integration", action="store_true",
                        help="also run the integration-owned mutants (needs PostgreSQL 18)")
    parser.add_argument("--only", help="comma-separated mutant ids, e.g. M10,M15")
    args = parser.parse_args(argv)
    selected = [m for m in MUTANTS if args.include_integration or not m.integration]
    if args.only:
        wanted = set(args.only.split(","))
        unknown = wanted - {m.id for m in MUTANTS}
        if unknown:
            parser.error(f"unknown mutant ids: {sorted(unknown)}")
        selected = [m for m in MUTANTS if m.id in wanted]
    try:
        return run(selected)
    except GateError as exc:
        print(f"capital mutation gate cannot run: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
