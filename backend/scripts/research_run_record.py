"""Write the run record of a research runner next to its report.

The record itself is `bfx_funding_bot.modules.backtest.run_record`; this is the part a
runner needs around it: which code is running, and where the record goes
(`<report>.run.json`, beside the report's `.md` and `.json`).
"""
from __future__ import annotations

import argparse
import logging
import os
import subprocess
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from bfx_funding_bot.modules.backtest.run_record import (
    InputFingerprint,
    RunFacts,
    build_run_record,
    write_run_record,
)

logger = logging.getLogger(__name__)

UNKNOWN_VERSION = "unknown"
_REPO = Path(__file__).resolve().parents[2]


def _git(*args: str) -> str:
    return subprocess.check_output(["git", "-C", str(_REPO), *args], text=True,
                                   stderr=subprocess.DEVNULL).strip()


def resolve_code_version(environ: Mapping[str, str] | None = None) -> str:
    """The image's commit (`BFX_SERVICE_VERSION`), else the checkout's HEAD.

    A checkout with uncommitted or untracked files under `backend/` is marked `-dirty`:
    its commit alone does not reproduce the run. "unknown" when neither source exists (also the image default
    when it was built without a commit).
    """
    env = os.environ if environ is None else environ
    version = env.get("BFX_SERVICE_VERSION", "").strip()
    if version and version != UNKNOWN_VERSION:
        return version
    try:
        head = _git("rev-parse", "HEAD")
        # Untracked files count: a new module the run imports is not in the commit either.
        dirty = _git("status", "--porcelain", "--", "backend")
    except (OSError, subprocess.CalledProcessError):
        return UNKNOWN_VERSION
    return f"{head}-dirty" if dirty else head


def record_path(report: Path) -> Path:
    return report.with_suffix(".run.json")


@dataclass(frozen=True)
class RunStart:
    """What a runner knows before it computes anything.

    The code version is taken here, not when the record is written: a commit or branch
    switch during a long run must not be recorded as the code that ran.
    """

    registry_entry: str
    runner: str
    started_at: datetime
    code_version: str

    @classmethod
    def begin(cls, registry_entry: str, runner: str) -> RunStart:
        code_version = resolve_code_version()
        if code_version == UNKNOWN_VERSION:
            logger.warning("run record: code version is unknown (no BFX_SERVICE_VERSION, no git)")
        return cls(registry_entry, runner, datetime.now(UTC), code_version)


def registry_entry_arg(raw: str) -> str:
    """argparse type for --registry-entry: refuse at parse time what the record refuses."""
    if not raw or raw != raw.strip():
        raise argparse.ArgumentTypeError("must be non-empty with no surrounding whitespace")
    return raw


def report_path_arg(raw: str) -> Path:
    """argparse type for --output: the `.json` and `.run.json` siblings must be other files."""
    path = Path(raw).expanduser()
    if path.suffix == ".json":
        raise argparse.ArgumentTypeError("is the markdown report; its .json sibling is derived")
    return path


def write_research_run_record(
    report: Path,
    start: RunStart,
    *,
    params: Mapping[str, object],
    inputs: Iterable[InputFingerprint],
) -> Path:
    """Record the run whose report (`.md` plus its `.json` sibling) was just written."""
    record = build_run_record(
        RunFacts(registry_entry=start.registry_entry, runner=start.runner,
                 code_version=start.code_version, started_at=start.started_at,
                 finished_at=datetime.now(UTC)),
        params=params, inputs=inputs,
        output_files=[report, report.with_suffix(".json")],
    )
    target = record_path(report)
    write_run_record(target, record)
    return target
