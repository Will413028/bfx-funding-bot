#!/usr/bin/env python3
"""Alert when backups miss the RPO or the weekly restore test stops succeeding.

Runs as root from bfx-backup-check.timer, one minute after each
bfx-pgbackrest-status run. It only reads what already exists:

- the evidence file that deploy/vm/pgbackrest/status.sh writes every five
  minutes: `measured: false`, `rpo_seconds` above 300, or a file older than 15
  minutes (the status timer itself stopped) is a problem;
- the restore-test heartbeat, but only while bfx-restore-test.timer is enabled:
  missing or older than 8 days is a problem.

A failed scheduled backup is alerted directly by bfx-pgbackrest-backup.service
(OnFailure=bfx-alert@%n.service), not here. Nothing here gates a deploy or a
resume (ADR 2026-09-25 D6): it only notifies.

A problem must be seen on two consecutive runs before it alerts (status.sh
briefly writes `backup_refresh_incomplete` while it measures); a confirmed
problem re-alerts every 6 hours; clearing it sends one "recovered" notice.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from types import ModuleType
from typing import Any

_HERE = Path(__file__).resolve().parent
_MAX_BYTES = 64 * 1024
_CODE = re.compile(r"[a-z0-9_]{1,64}")


def _load(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


bfx_notify = _load("_bfx_ops_notify", _HERE / "bfx_notify.py")

# argv -> exit status (systemctl is-enabled --quiet ...)
StatusRunner = Callable[[Sequence[str]], int]
Notifier = Callable[[str, str], None]


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        with path.open("rb") as handle:
            raw = handle.read(_MAX_BYTES + 1)
    except OSError:
        return None
    if len(raw) > _MAX_BYTES:
        return None
    try:
        value = json.loads(raw)
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def _int(value: object) -> int | None:
    return value if type(value) is int else None


def backup_problems(
    evidence: dict[str, Any] | None, *, now_ms: int, rpo_limit_s: int, max_age_s: int,
) -> list[str]:
    if evidence is None:
        return ["backup_evidence_unavailable"]
    problems: list[str] = []
    observed = _int(evidence.get("observed_at_ms"))
    if observed is None:
        problems.append("backup_evidence_invalid")
    elif observed - now_ms > 60_000:
        problems.append("backup_evidence_from_future")
    elif now_ms - observed > max_age_s * 1000:
        problems.append(f"backup_evidence_stale:{(now_ms - observed) // 60_000}min")
    if evidence.get("measured") is not True:
        code = evidence.get("error_code")
        code = code if isinstance(code, str) and _CODE.fullmatch(code) else "unknown"
        problems.append(f"backup_unmeasured:{code}")
    else:
        rpo = _int(evidence.get("rpo_seconds"))
        if rpo is None:
            problems.append("backup_evidence_invalid")
        elif rpo > rpo_limit_s:
            problems.append(f"rpo_exceeded:{rpo}s")
    return problems


def restore_problems(heartbeat: dict[str, Any] | None, *, now_ms: int, max_age_s: int) -> list[str]:
    if heartbeat is None:
        return ["restore_test_no_heartbeat"]
    observed = _int(heartbeat.get("observed_at_ms"))
    if observed is None:
        return ["restore_test_heartbeat_invalid"]
    if now_ms - observed > max_age_s * 1000:
        return [f"restore_test_stale:{(now_ms - observed) // 86_400_000}d"]
    return []


def _kind(problem: str) -> str:
    return problem.split(":", 1)[0]


def decide(
    state: dict[str, Any], problems: list[str], *, now: float, realert_s: float,
) -> tuple[tuple[str, str] | None, dict[str, Any]]:
    """Return (level, message) to send, if any, and the next state."""
    kinds = {_kind(problem) for problem in problems}
    previous = set(state.get("kinds") or [])
    alerted = set(state.get("alerted_kinds") or [])
    alerted_at = float(state.get("alerted_at") or 0.0)
    confirmed = kinds & previous
    message: tuple[str, str] | None = None
    if confirmed:
        if not confirmed <= alerted or now - alerted_at >= realert_s:
            shown = [problem for problem in problems if _kind(problem) in confirmed]
            message = ("critical", "DR check: " + ", ".join(shown)
                       + " (backups/restore tests do not gate trading; fix and watch the next run)")
            alerted, alerted_at = confirmed, now
    elif not kinds and alerted:
        message = ("info", "DR check recovered: backup evidence fresh and within RPO")
        alerted, alerted_at = set(), 0.0
    return message, {"kinds": sorted(kinds), "alerted_kinds": sorted(alerted),
                     "alerted_at": alerted_at}


def _write_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    with os.fdopen(handle, "w", encoding="utf-8") as stream:
        json.dump(state, stream, sort_keys=True)
    os.replace(temporary, path)


def _systemctl_status(argv: Sequence[str]) -> int:
    try:
        return subprocess.run(list(argv), capture_output=True, check=False, timeout=30).returncode
    except (OSError, subprocess.TimeoutExpired):
        return 1


def run_check(
    *, evidence: Path, heartbeat: Path, restore_timer: str, state_path: Path,
    rpo_limit_s: int, max_age_s: int, restore_max_age_s: int, realert_s: float,
    notify: Notifier, status: StatusRunner = _systemctl_status,
    clock: Callable[[], float] = time.time,
) -> int:
    now = clock()
    now_ms = int(now * 1000)
    problems = backup_problems(_read_json(evidence), now_ms=now_ms, rpo_limit_s=rpo_limit_s,
                               max_age_s=max_age_s)
    if status(["systemctl", "is-enabled", "--quiet", restore_timer]) == 0:
        problems += restore_problems(_read_json(heartbeat), now_ms=now_ms,
                                     max_age_s=restore_max_age_s)
    state = _read_json(state_path) or {}
    message, next_state = decide(state, problems, now=now, realert_s=realert_s)
    if message is not None:
        notify(*message)
    _write_state(state_path, next_state)
    if problems:
        print("bfx-backup-check: " + ", ".join(problems), file=sys.stderr)
        return 1
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--evidence", type=Path,
                        default=Path("/home/ubuntu/bfx/dr-evidence/backup.json"))
    parser.add_argument("--restore-heartbeat", type=Path,
                        default=Path("/home/ubuntu/bfx/dr-evidence/restore-heartbeat.json"))
    parser.add_argument("--restore-timer", default="bfx-restore-test.timer")
    parser.add_argument("--state", type=Path, default=Path("/var/lib/bfx-ops/backup-check.json"))
    parser.add_argument("--notify-config", type=Path, default=bfx_notify.DEFAULT_CONFIG)
    parser.add_argument("--rpo-seconds", type=int, default=300)
    parser.add_argument("--max-evidence-age-seconds", type=int, default=900)
    parser.add_argument("--restore-max-age-seconds", type=int, default=8 * 86_400)
    parser.add_argument("--realert-seconds", type=int, default=6 * 3600)
    args = parser.parse_args(argv)

    def notify(level: str, text: str) -> None:
        bfx_notify.send(text, level=level, config_path=args.notify_config)

    return run_check(
        evidence=args.evidence, heartbeat=args.restore_heartbeat,
        restore_timer=args.restore_timer, state_path=args.state,
        rpo_limit_s=args.rpo_seconds, max_age_s=args.max_evidence_age_seconds,
        restore_max_age_s=args.restore_max_age_seconds, realert_s=args.realert_seconds,
        notify=notify,
    )


if __name__ == "__main__":
    raise SystemExit(main())
