"""Quality ratchet: suppression counts and executed-test counts only move the good way.

Two numbers are locked in ``backend/quality-ratchet.json``:

* ``suppressions``: per kind (``noqa``, ``type: ignore``, ``biome-ignore``, skipped
  tests, ...), counted over every tracked source file. The count must EQUAL the lock:
  a new suppression fails, and so does removing one without lowering the lock, so the
  lock never drifts above the code.
* ``tests_executed``: tests that ran (not skipped) in the CI JUnit reports of the
  backend unit, gate and integration jobs and the frontend vitest job, summed. The
  count must be at least the lock (raise the lock when it grows).

A pull request that loosens the lock (raises a suppression count or lowers the test
count) must carry a commit with a ``Ratchet-Reason:`` trailer; lowering the test count
also needs a ``Removed-Test:`` trailer naming each removed test.

Usage:
    cd backend
    uv run python scripts/quality_ratchet.py                      # suppressions only
    uv run python scripts/quality_ratchet.py --junit-dir DIR      # plus executed tests
    uv run python scripts/quality_ratchet.py --base-ref SHA       # plus loosening trailers

Exit codes: 0 = within the lock; 1 = the ratchet moved the wrong way.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import xml.etree.ElementTree as ElementTree
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
LOCK_PATH = "backend/quality-ratchet.json"

SOURCE_SUFFIXES = frozenset({".py", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".mts", ".cjs", ".cts"})
# They spell out every pattern below on purpose.
EXCLUDED_PATHS = frozenset({
    "backend/scripts/quality_ratchet.py",
    "backend/tests/scripts/test_quality_ratchet.py",
})

PATTERNS: Mapping[str, re.Pattern[str]] = {
    "noqa": re.compile(r"#\s*noqa\b"),
    "ruff: ignore": re.compile(r"#\s*ruff:\s*(?:noqa|ignore)\b"),
    "type: ignore": re.compile(r"#\s*type:\s*ignore\b"),
    "pyright: ignore": re.compile(r"#\s*pyright:\s*ignore\b"),
    "biome-ignore": re.compile(r"biome-ignore\b"),
    "eslint-disable": re.compile(r"eslint-disable\b"),
    "ts-expect-error": re.compile(r"@ts-expect-error\b"),
    "ts-ignore": re.compile(r"@ts-ignore\b"),
    "pytest.skip": re.compile(r"\bpytest\.skip\("),
    "pytest.mark.skip": re.compile(r"\bpytest\.mark\.skip\b"),
    "xfail": re.compile(r"\bxfail\b"),
    "skipif": re.compile(r"\bskipif\b"),
    "test.skip": re.compile(r"\btest\.skip\("),
    "it.skip": re.compile(r"\bit\.skip\("),
    "describe.skip": re.compile(r"\bdescribe\.skip\("),
}

RATCHET_REASON = "Ratchet-Reason"
REMOVED_TEST = "Removed-Test"


def count_text(text: str) -> dict[str, int]:
    return {kind: len(pattern.findall(text)) for kind, pattern in PATTERNS.items()}


def is_counted(path: str) -> bool:
    return Path(path).suffix in SOURCE_SUFFIXES and path not in EXCLUDED_PATHS


def count_suppressions(root: Path, paths: Iterable[str]) -> dict[str, int]:
    totals = dict.fromkeys(PATTERNS, 0)
    for path in paths:
        if not is_counted(path):
            continue
        file = root / path
        if not file.is_file():  # tracked but deleted in the working tree
            continue
        for kind, n in count_text(file.read_text(encoding="utf-8", errors="replace")).items():
            totals[kind] += n
    return totals


def tracked_files(root: Path) -> list[str]:
    out = subprocess.run(
        ["git", "-C", str(root), "ls-files", "-z"], check=True, capture_output=True, text=True
    ).stdout
    return [p for p in out.split("\0") if p]


def executed_tests(junit_dir: Path) -> int:
    reports = sorted(junit_dir.rglob("*.xml"))
    if not reports:
        raise SystemExit(f"no JUnit reports under {junit_dir}")
    executed = 0
    for report in reports:
        for case in ElementTree.parse(report).getroot().iter("testcase"):
            if case.find("skipped") is None:
                executed += 1
    return executed


def compare(
    lock: Mapping[str, object], suppressions: Mapping[str, int], tests: int | None
) -> list[str]:
    errors: list[str] = []
    locked = lock["suppressions"]
    assert isinstance(locked, dict)
    if set(locked) != set(PATTERNS):
        errors.append(f"lock kinds {sorted(locked)} != counted kinds {sorted(PATTERNS)}")
    for kind, n in suppressions.items():
        want = locked.get(kind)
        if want is None or n == want:
            continue
        if n > want:
            errors.append(f"{kind}: {n} found, lock is {want} (new suppression)")
        else:
            errors.append(f"{kind}: {n} found, lock is {want}; lower the lock to {n}")
    if tests is not None:
        want_tests = lock["tests_executed"]
        assert isinstance(want_tests, int)
        if tests < want_tests:
            errors.append(f"tests_executed: {tests} ran, lock is {want_tests} (tests removed)")
        elif tests > want_tests:
            print(f"note: {tests} tests ran, lock is {want_tests}; raise tests_executed to {tests}")
    return errors


def loosened(base: Mapping[str, object], head: Mapping[str, object]) -> tuple[list[str], bool]:
    """Return the loosened entries and whether tests_executed went down."""
    changes: list[str] = []
    base_s, head_s = base["suppressions"], head["suppressions"]
    assert isinstance(base_s, dict) and isinstance(head_s, dict)
    for kind, n in head_s.items():
        if n > base_s.get(kind, 0):
            changes.append(f"{kind}: {base_s.get(kind, 0)} -> {n}")
    base_t, head_t = base["tests_executed"], head["tests_executed"]
    assert isinstance(base_t, int) and isinstance(head_t, int)
    tests_down = head_t < base_t
    if tests_down:
        changes.append(f"tests_executed: {base_t} -> {head_t}")
    return changes, tests_down


def trailers(root: Path, base_ref: str, key: str) -> list[str]:
    out = subprocess.run(
        ["git", "-C", str(root), "log", f"--format=%(trailers:key={key},valueonly)",
         f"{base_ref}..HEAD"],
        check=True, capture_output=True, text=True,
    ).stdout
    return [line.strip() for line in out.splitlines() if line.strip()]


def check_loosening(root: Path, base_ref: str, head: Mapping[str, object]) -> list[str]:
    shown = subprocess.run(
        ["git", "-C", str(root), "show", f"{base_ref}:{LOCK_PATH}"],
        capture_output=True, text=True,
    )
    if shown.returncode != 0:  # the lock is new in this change
        return []
    changes, tests_down = loosened(json.loads(shown.stdout), head)
    if not changes:
        return []
    errors: list[str] = []
    if not trailers(root, base_ref, RATCHET_REASON):
        errors.append(
            f"the lock was loosened ({'; '.join(changes)}) without a commit carrying a "
            f"'{RATCHET_REASON}:' trailer"
        )
    if tests_down and not trailers(root, base_ref, REMOVED_TEST):
        errors.append(
            f"tests_executed went down without a '{REMOVED_TEST}:' trailer naming each removed test"
        )
    return errors


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--junit-dir", type=Path, help="directory holding the CI JUnit reports")
    parser.add_argument("--base-ref", help="PR base commit; checks trailers when the lock loosens")
    args = parser.parse_args(argv)

    lock = json.loads((REPO / LOCK_PATH).read_text())
    suppressions = count_suppressions(REPO, tracked_files(REPO))
    tests = executed_tests(args.junit_dir) if args.junit_dir else None
    print(json.dumps({"suppressions": suppressions, "tests_executed": tests}, indent=2))

    errors = compare(lock, suppressions, tests)
    if args.base_ref:
        errors += check_loosening(REPO, args.base_ref, lock)
    for error in errors:
        print(f"error: {error}", file=sys.stderr)
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
