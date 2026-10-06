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
3. reverse transition: a drill without `--restore-test` (a release from before
   it, reached by a revert) is probed through its `--help` and run in its own
   form, `--prefix` with the scope in /home/ubuntu/bfx/restore-test.json, and its
   receipt (`restore-prefix.json`, `kind: restore_prefix`) is accepted as the
   previous wrapper accepted it. Removed, with that file, by the D3 cleanup PR;
4. on any failure exits non-zero, so OnFailure=bfx-alert@%n.service alerts
   (the Telegram credentials are root-only; this process never reads them).
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

_MAX_EVIDENCE_AGE_MS = 3_600_000
_PROBE_TIMEOUT_SECONDS = 60.0
_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}")

# (argv, timeout) -> exit status; the drill's own output goes to the journal.
DrillRunner = Callable[[Sequence[str], float], int]
# (argv, timeout) -> (exit status, stdout): the capability probe.
DrillProbe = Callable[[Sequence[str], float], tuple[int, str]]


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


def _subprocess_probe(argv: Sequence[str], timeout: float) -> tuple[int, str]:
    try:
        completed = subprocess.run(list(argv), check=False, timeout=timeout,
                                   capture_output=True, text=True)
    except (subprocess.TimeoutExpired, OSError):
        return 126, ""
    return completed.returncode, completed.stdout


def drill_has_restore_test(drill: Path, probe: DrillProbe) -> bool:
    """Whether the drill offers `--restore-test`, read from its argparse help (no run)."""
    status, stdout = probe([str(drill), "--help"], _PROBE_TIMEOUT_SECONDS)
    if status != 0 or "usage:" not in stdout:
        _fail("restore_test_drill_probe_failed")
    return re.search(r"(^|\s|\[)--restore-test\b", stdout) is not None


def legacy_arguments(path: Path) -> list[str]:
    """The pre-`--restore-test` drill's arguments, from its JSON config (as that wrapper did)."""
    try:
        config = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise RestoreTestError("restore_test_not_configured") from None
    except (OSError, ValueError):
        raise RestoreTestError("restore_test_config_unreadable") from None
    if not isinstance(config, dict) or set(config) != {"account_id", "environment", "projector_version"}:
        _fail("restore_test_config_fields")
    if not isinstance(config["account_id"], str) or _UUID.fullmatch(config["account_id"]) is None:
        _fail("restore_test_config_account_id")
    for key in ("environment", "projector_version"):
        if not isinstance(config[key], str) or _TOKEN.fullmatch(config[key]) is None:
            _fail(f"restore_test_config_{key}")
    return ["--prefix", "--account-id", config["account_id"], "--environment", config["environment"],
            "--projector-version", config["projector_version"]]


def heartbeat_from_evidence(evidence: Path, *, now_ms: int, legacy: bool = False) -> dict[str, object]:
    try:
        report = json.loads(evidence.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise RestoreTestError("restore_evidence_unreadable") from None
    accepted = (report.get("kind") == "restore_prefix" if legacy
                else report.get("restore_test") is True) if isinstance(report, dict) else False
    if not isinstance(report, dict) or report.get("measured") is not True or not accepted:
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
        "ledger_read_seconds": ledger.get("read_seconds"),
        "legacy_drill": legacy,
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
    legacy_config: Path, legacy_evidence: Path,
    runner: DrillRunner = _subprocess_drill, probe: DrillProbe = _subprocess_probe,
    clock: Callable[[], float] = time.time,
) -> int:
    try:
        legacy = not drill_has_restore_test(drill, probe)
        arguments = (legacy_arguments(legacy_config) if legacy
                     else ["--restore-test", "--output", str(evidence)])
    except RestoreTestError as exc:
        print(f"bfx-restore-test: {exc}", file=sys.stderr)
        return 2
    if legacy:
        print("bfx-restore-test: drill predates --restore-test; running its --prefix form",
              file=sys.stderr)
        evidence = legacy_evidence
    status = runner([str(drill), *arguments], timeout)
    if status != 0:
        print(f"bfx-restore-test: drill failed (exit {status}); heartbeat not refreshed",
              file=sys.stderr)
        return status if 0 < status < 256 else 1
    try:
        _write_atomic(heartbeat, heartbeat_from_evidence(evidence, now_ms=int(clock() * 1000),
                                                         legacy=legacy))
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
    # Only for a drill from before --restore-test (see the module docstring, step 3).
    # `--config` is the previous unit's spelling: if installing this release's tooling stops
    # after the wrapper but before the unit, the old unit still runs this wrapper with
    # `--config ... --evidence .../restore-prefix.json`, and both keep working.
    parser.add_argument("--legacy-config", "--config", dest="legacy_config", type=Path,
                        default=home / "bfx/restore-test.json")
    parser.add_argument("--legacy-evidence", type=Path,
                        default=home / "bfx/dr-evidence/restore-prefix.json")
    args = parser.parse_args(argv)
    return run_restore_test(drill=args.drill, evidence=args.evidence,
                            heartbeat=args.heartbeat, timeout=args.timeout_seconds,
                            legacy_config=args.legacy_config,
                            legacy_evidence=args.legacy_evidence)


if __name__ == "__main__":
    raise SystemExit(main())
