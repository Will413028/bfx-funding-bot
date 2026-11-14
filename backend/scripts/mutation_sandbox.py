"""The sandbox both mutation tools run in: a throwaway copy of the repository.

Mutants are written into the copy only; the working tree is read, never written. The
tests run from the copy's ``backend/``, where ``pythonpath = ["src", "."]`` puts the
copy's package and scripts ahead of the editable install, so they import the mutants.
Each tool still proves that with a canary of its own (a mutant every test must fail
on), because a copy the tests do not import would pass every mutant.

Used by ``capital_mutation_gate.py`` (hand-written mutants) and
``mutation_kill_matrix.py`` (mutmut's mutants); run either as ``python -m scripts.<name>``.
"""
from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
REPO = BACKEND.parent


def copy_repository(copy: Path) -> None:
    """Copy the working tree's tracked and untracked, non-ignored files into ``copy``."""
    listed = subprocess.run(
        ["git", "-C", str(REPO), "ls-files", "-z", "-co", "--exclude-standard"],
        capture_output=True, check=True,
    ).stdout.decode().split("\0")
    for name in sorted(set(filter(None, listed))):
        source = REPO / name
        target = copy / name
        if source.is_symlink():
            target.parent.mkdir(parents=True, exist_ok=True)
            target.symlink_to(os.readlink(source))
        elif source.is_file():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)


def pytest(copy: Path, args: Sequence[str], *, env: Mapping[str, str] | None = None,
           timeout: float, bytecode: bool = False) -> subprocess.CompletedProcess[str] | None:
    """Run pytest from the copy's ``backend/`` (no pytest cache); None on timeout.

    No bytecode unless ``bytecode``: a tool that rewrites a source between runs (the capital
    gate) must not meet the ``.pyc`` of the text it replaced, which Python keeps when the size
    and the second of the modification time are the same. A tool whose sources stay as they
    are for every run (the kill matrix: mutmut switches mutants by an environment variable)
    passes ``bytecode=True`` and compiles the copy once instead of once per run.
    """
    command = [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", *args]
    cache = {"PYTHONDONTWRITEBYTECODE": ""} if bytecode else {"PYTHONDONTWRITEBYTECODE": "1"}
    try:
        return subprocess.run(
            command, cwd=copy / "backend",
            env={**os.environ, **cache, **(env or {})},
            capture_output=True, text=True, timeout=timeout, check=False,
        )
    except subprocess.TimeoutExpired:
        return None


def digest(paths: Iterable[Path]) -> str:
    """A digest of these working-tree files (paths relative to the repository root)."""
    h = hashlib.sha256()
    for path in sorted(paths):
        h.update(str(path).encode())
        h.update((REPO / path).read_bytes())
    return h.hexdigest()
