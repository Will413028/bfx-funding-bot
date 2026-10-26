"""pytest plugin for ``scripts/mutation_kill_matrix.py``: which mutated functions each test calls.

Loaded with ``-p scripts.mutation_kill_matrix_stats`` while ``MUTANT_UNDER_TEST=stats``: every
mutated function's trampoline then reports itself and runs the original code. mutmut's own
recorder needs its ``mutants/`` working directory, so the trampoline's hook is replaced by one
that keeps the names per test (setup and teardown included) and writes them, as JSON, to the
file ``KILL_MATRIX_STATS`` names.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from mutmut.mutation import trampoline

_hits: set[str] = set()
_by_test: dict[str, list[str]] = {}


def _record(name: str, caller: str | None = None) -> None:
    _hits.add(name)


def pytest_configure(config: Any) -> None:
    trampoline.record_trampoline_hit = _record


def pytest_runtest_logstart(nodeid: str, location: Any) -> None:
    _hits.clear()


def pytest_runtest_logfinish(nodeid: str, location: Any) -> None:
    _by_test[nodeid] = sorted(_hits)


def pytest_sessionfinish(session: Any, exitstatus: int) -> None:
    Path(os.environ["KILL_MATRIX_STATS"]).write_text(json.dumps(_by_test, sort_keys=True))
