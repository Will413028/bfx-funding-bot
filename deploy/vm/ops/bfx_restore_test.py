#!/usr/bin/env python3
"""Weekly isolated restore test: run the existing drill, then write a heartbeat.

Runs as the DR operator user (ubuntu) from bfx-restore-test.service, exactly
like the pgBackRest units: restore_drill.py reads its secrets and writes its
evidence under that user's home, and git-checks its config in that user's
checkout. This wrapper adds nothing to the drill's checks; it only

1. passes the drill the request from a JSON config (account, environment,
   projector version, backup label, optional PITR target, baseline path);
2. on success, requires the drill's own restore.json to be `measured: true`
   and fresh, then atomically writes the heartbeat bfx-backup-check watches;
3. on any failure exits non-zero, so OnFailure=bfx-alert@%n.service alerts
   (the Telegram credentials are root-only; this process never reads them).

The drill verifies against an operator-captured baseline for the same backup
target (docs/runbooks/offsite-dr.md section 6). How that baseline is produced for
an unattended weekly run is an open decision; see the T10 hand-off notes.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}")
_TARGET_TIME = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z")
_MAX_EVIDENCE_AGE_MS = 3_600_000

# (argv, timeout) -> exit status; the drill's own output goes to the journal.
DrillRunner = Callable[[Sequence[str], float], int]


class RestoreTestError(ValueError):
    pass


def _fail(code: str) -> None:
    raise RestoreTestError(code)


def load_request(path: Path) -> list[str]:
    """Return the drill arguments from the JSON config, or raise."""
    try:
        config = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise RestoreTestError("restore_test_not_configured") from None
    except (OSError, ValueError):
        raise RestoreTestError("restore_test_config_unreadable") from None
    expected = {"account_id", "environment", "projector_version", "backup_label",
                "target_time", "baseline"}
    if not isinstance(config, dict) or set(config) != expected:
        _fail("restore_test_config_fields")
    values: dict[str, Any] = config
    if not isinstance(values["account_id"], str) or _UUID.fullmatch(values["account_id"]) is None:
        _fail("restore_test_config_account_id")
    for key in ("environment", "projector_version", "backup_label"):
        if not isinstance(values[key], str) or _TOKEN.fullmatch(values[key]) is None:
            _fail(f"restore_test_config_{key}")
    target = values["target_time"]
    if target is not None and (not isinstance(target, str) or _TARGET_TIME.fullmatch(target) is None):
        _fail("restore_test_config_target_time")
    baseline = values["baseline"]
    if not isinstance(baseline, str) or not Path(baseline).is_absolute():
        _fail("restore_test_config_baseline")
    arguments = ["--account-id", values["account_id"], "--environment", values["environment"],
                 "--projector-version", values["projector_version"],
                 "--backup-label", values["backup_label"], "--baseline", baseline]
    if target is not None:
        arguments += ["--target-time", target]
    return arguments


def _subprocess_drill(argv: Sequence[str], timeout: float) -> int:
    try:
        return subprocess.run(list(argv), check=False, timeout=timeout).returncode
    except subprocess.TimeoutExpired:
        print("bfx-restore-test: drill timed out", file=sys.stderr)
        return 124
    except OSError:
        print("bfx-restore-test: drill not executable", file=sys.stderr)
        return 126


def heartbeat_from_evidence(evidence: Path, *, now_ms: int) -> dict[str, object]:
    try:
        report = json.loads(evidence.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise RestoreTestError("restore_evidence_unreadable") from None
    if not isinstance(report, dict) or report.get("measured") is not True:
        _fail("restore_evidence_unmeasured")
    observed = report.get("observed_at_ms")
    if type(observed) is not int or not 0 <= now_ms - observed <= _MAX_EVIDENCE_AGE_MS:
        _fail("restore_evidence_stale")
    return {
        "schema_version": 1, "kind": "restore_test_heartbeat", "observed_at_ms": now_ms,
        "restore_observed_at_ms": observed,
        "restore_run_id": report.get("restore_run_id"),
        "rto_seconds": report.get("rto_seconds"),
        "target_backup_label": report.get("target_backup_label"),
    }


def _write_atomic(path: Path, value: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(value, stream, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


def run_restore_test(
    *, config: Path, drill: Path, evidence: Path, heartbeat: Path, timeout: float,
    runner: DrillRunner = _subprocess_drill, clock: Callable[[], float] = time.time,
) -> int:
    try:
        arguments = load_request(config)
    except RestoreTestError as exc:
        print(f"bfx-restore-test: {exc}", file=sys.stderr)
        return 2
    status = runner([str(drill), *arguments], timeout)
    if status != 0:
        print(f"bfx-restore-test: drill failed (exit {status}); heartbeat not refreshed",
              file=sys.stderr)
        return status if 0 < status < 256 else 1
    try:
        _write_atomic(heartbeat, heartbeat_from_evidence(evidence, now_ms=int(clock() * 1000)))
    except (RestoreTestError, OSError) as exc:
        code = str(exc) if isinstance(exc, RestoreTestError) else type(exc).__name__
        print(f"bfx-restore-test: {code}; heartbeat not refreshed", file=sys.stderr)
        return 2
    print("bfx-restore-test: restore test passed; heartbeat refreshed", file=sys.stderr)
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    home = Path.home()
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", type=Path, default=home / "bfx/restore-test.json")
    parser.add_argument("--drill", type=Path,
                        default=home / "bfx-funding-bot/deploy/vm/pgbackrest/restore-drill.sh")
    parser.add_argument("--evidence", type=Path, default=home / "bfx/dr-evidence/restore.json")
    parser.add_argument("--heartbeat", type=Path,
                        default=home / "bfx/dr-evidence/restore-heartbeat.json")
    parser.add_argument("--timeout-seconds", type=float, default=7000.0)
    args = parser.parse_args(argv)
    return run_restore_test(config=args.config, drill=args.drill, evidence=args.evidence,
                            heartbeat=args.heartbeat, timeout=args.timeout_seconds)


if __name__ == "__main__":
    raise SystemExit(main())
