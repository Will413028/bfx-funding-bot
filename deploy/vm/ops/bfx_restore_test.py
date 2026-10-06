#!/usr/bin/env python3
"""Isolated restore test: run the drill in ledger mode, then write a heartbeat.

Runs as the DR operator user (ubuntu) from bfx-restore-test@<release>.service --
monthly from its timer (`current`: the deployed release), and on demand from
bfx-deploy with the target release's DR scripts before a release with a pending
migration or a PostgreSQL/pgBackRest change. Like the pgBackRest units, the
drill reads its secrets and writes its evidence under that user's home and
git-checks its config in the release's clean checkout under
/home/ubuntu/bfx-releases. This wrapper adds no checks of its own; it only

1. runs `restore-drill.sh --restore-test --output <evidence>`: the drill of the
   release under test picks the verification (today: restore the newest backup to
   the end of the archive, require every scope's append-only ledger rows to equal
   production's within the restored copy's own boundary, and run the image's
   read-only boot check; no baseline, no scope configuration, no writer pause).
   This wrapper names no mode, so a later release can change the verification
   without an argument the installed wrapper does not know;
2. on success, requires that receipt to be `measured: true`, `restore_test: true`
   and fresh, then atomically writes the heartbeat bfx-backup-check watches;
3. on any failure exits non-zero, so OnFailure=bfx-alert@%n.service alerts
   (the Telegram credentials are root-only; this process never reads them).
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Sequence
from pathlib import Path

_MAX_EVIDENCE_AGE_MS = 3_600_000

# (argv, timeout) -> exit status; the drill's own output goes to the journal.
DrillRunner = Callable[[Sequence[str], float], int]


class RestoreTestError(ValueError):
    pass


def _fail(code: str) -> None:
    raise RestoreTestError(code)


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
    if (not isinstance(report, dict) or report.get("measured") is not True
            or report.get("restore_test") is not True):
        _fail("restore_evidence_unmeasured")
    observed = report.get("observed_at_ms")
    if type(observed) is not int or not 0 <= now_ms - observed <= _MAX_EVIDENCE_AGE_MS:
        _fail("restore_evidence_stale")
    ledger = report.get("ledger") if isinstance(report.get("ledger"), dict) else {}
    scopes = ledger.get("scopes")
    return {
        "schema_version": 1, "kind": "restore_test_heartbeat", "observed_at_ms": now_ms,
        "restore_observed_at_ms": observed,
        "restore_run_id": report.get("restore_run_id"),
        "rto_seconds": report.get("rto_seconds"),
        "target_backup_label": report.get("target_backup_label"),
        "ledger_scopes": len(scopes) if isinstance(scopes, list) else None,
        "ledger_rows_compared": ledger.get("rows_compared"),
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
    *, drill: Path, evidence: Path, heartbeat: Path, timeout: float,
    runner: DrillRunner = _subprocess_drill, clock: Callable[[], float] = time.time,
) -> int:
    status = runner([str(drill), "--restore-test", "--output", str(evidence)], timeout)
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
    parser.add_argument("--drill", type=Path,
                        default=home / "bfx-funding-bot/deploy/vm/pgbackrest/restore-drill.sh")
    parser.add_argument("--evidence", type=Path, default=home / "bfx/dr-evidence/restore-ledger.json")
    parser.add_argument("--heartbeat", type=Path,
                        default=home / "bfx/dr-evidence/restore-heartbeat.json")
    parser.add_argument("--timeout-seconds", type=float, default=7000.0)
    args = parser.parse_args(argv)
    return run_restore_test(drill=args.drill, evidence=args.evidence,
                            heartbeat=args.heartbeat, timeout=args.timeout_seconds)


if __name__ == "__main__":
    raise SystemExit(main())
