"""PR size gate: a pull request over the line limit must say why in its body.

Counts added plus deleted lines between the PR base and HEAD (``git diff --numstat``).
Not counted: lockfiles, Alembic revisions under ``backend/alembic/versions/``, binary
files, and paths marked ``linguist-generated`` in ``.gitattributes``. Over
``LIMIT`` lines the PR body needs a ``Size-Justification:`` line with a reason.

Usage (CI passes the body through the environment, never the command line):
    cd backend
    PR_BODY="$(gh pr view N --json body -q .body)" \\
        python3 scripts/pr_size.py --base-ref origin/main

Exit codes: 0 = within the limit or justified; 1 = too large without a justification.
"""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

REPO = Path(__file__).resolve().parents[2]
LIMIT = 1000

LOCKFILES = frozenset({
    "uv.lock",
    "pnpm-lock.yaml",
    "package-lock.json",
    "yarn.lock",
    ".terraform.lock.hcl",
})
EXCLUDED_DIRS = ("backend/alembic/versions/",)
JUSTIFICATION = re.compile(r"^Size-Justification:[ \t]*\S", re.MULTILINE)


@dataclass(frozen=True)
class FileChange:
    path: str
    lines: int | None  # None for a binary file


def parse_numstat(raw: str) -> list[FileChange]:
    """Parse ``git diff --numstat -z``; a rename is reported under its new path."""
    fields = raw.split("\0")
    changes: list[FileChange] = []
    i = 0
    while i < len(fields) and fields[i]:
        added, deleted, path = fields[i].split("\t", 2)
        if path:
            i += 1
        else:  # rename or copy: the old and new paths follow as separate fields
            path = fields[i + 2]
            i += 3
        lines = None if added == "-" else int(added) + int(deleted)
        changes.append(FileChange(path, lines))
    return changes


def is_excluded(path: str, generated: frozenset[str]) -> bool:
    return (
        PurePosixPath(path).name in LOCKFILES
        or path.startswith(EXCLUDED_DIRS)
        or path in generated
    )


def generated_paths(root: Path, paths: Iterable[str]) -> frozenset[str]:
    stdin = "".join(f"{p}\0" for p in paths)
    out = subprocess.run(
        ["git", "-C", str(root), "check-attr", "-z", "--stdin", "linguist-generated"],
        input=stdin, check=True, capture_output=True, text=True,
    ).stdout.split("\0")
    triples = zip(out[0::3], out[1::3], out[2::3], strict=False)
    return frozenset(path for path, _, value in triples if value in {"set", "true"})


def counted_lines(changes: Iterable[FileChange], generated: frozenset[str]) -> int:
    return sum(
        c.lines for c in changes if c.lines is not None and not is_excluded(c.path, generated)
    )


def check(lines: int, body: str) -> list[str]:
    if lines <= LIMIT or JUSTIFICATION.search(body):
        return []
    return [
        f"{lines} changed lines (limit {LIMIT}, lockfiles, migrations and generated files "
        "excluded); split the PR or add a 'Size-Justification: <reason>' line to its body"
    ]


def diff_changes(root: Path, base_ref: str) -> list[FileChange]:
    out = subprocess.run(  # an unknown ref fails here, never reads as "nothing changed"
        ["git", "-C", str(root), "diff", "--numstat", "-z", "-M", f"{base_ref}...HEAD"],
        check=True, capture_output=True, text=True,
    ).stdout
    return parse_numstat(out)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base-ref", required=True, help="the commit the PR merges into")
    args = parser.parse_args(argv)

    changes = diff_changes(REPO, args.base_ref)
    generated = generated_paths(REPO, (c.path for c in changes))
    lines = counted_lines(changes, generated)
    excluded = sorted(c.path for c in changes if is_excluded(c.path, generated))
    print(f"counted lines: {lines} (limit {LIMIT})")
    if excluded:
        print("not counted: " + ", ".join(excluded))

    errors = check(lines, os.environ.get("PR_BODY", ""))
    for error in errors:
        print(f"::error::{error}", file=sys.stderr)
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
