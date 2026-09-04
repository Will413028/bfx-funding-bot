"""Offline behavioral contracts for pgBackRest operator wrappers."""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
PG_BACKREST_DIR = ROOT / "deploy/vm/pgbackrest"
TOKEN_SENTINEL = "TOKEN-SENTINEL"


def test_pgbackrest_wrappers_fail_closed_and_have_no_mutating_sql() -> None:
    for name in ("status.sh", "preflight.sh", "backup.sh", "smoke.sh"):
        source = (PG_BACKREST_DIR / name).read_text(encoding="utf-8")
        assert "set -euo pipefail" in source
        assert not re.search(
            r"\b(insert|update|delete|truncate|drop|alter|grant|revoke)\b",
            source,
            re.I,
        )
    assert "--confirm-r2-smoke" in (PG_BACKREST_DIR / "smoke.sh").read_text(
        encoding="utf-8"
    )


def test_status_is_read_only_and_does_not_restore_or_archive_push() -> None:
    source = (PG_BACKREST_DIR / "status.sh").read_text(encoding="utf-8")
    assert "pg_stat_archiver" in source
    assert "info --output=json" in source
    assert "--require-rpo" in source
    assert "archive-push" not in source
    assert "restore" not in source


def _fake_docker(tmp_path: Path) -> tuple[Path, Path]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log_path = tmp_path / "docker.log"
    docker = bin_dir / "docker"
    docker.write_text(
        """#!/usr/bin/env bash
set -euo pipefail
printf '%s\\n' "$*" >> "$FAKE_DOCKER_LOG"
printf '%s\\n' 'TOKEN-SENTINEL' >&2
case "$*" in
  *psql*)
    [[ "$*" == *'POSTGRES_USER'* && "$*" == *'POSTGRES_DB'* ]] || exit 42
    printf '%s\\n' "$FAKE_ARCHIVER_TSV"
    ;;
  *'pgbackrest --stanza=bfx info --output=json'*) printf '%s\\n' "$FAKE_INFO_JSON" ;;
  *) exit 41 ;;
esac
""",
        encoding="utf-8",
    )
    docker.chmod(0o755)
    return bin_dir, log_path


def _info_json() -> str:
    return json.dumps(
        [
            {
                "name": "bfx",
                "backup": [
                    {
                        "label": "20260903031700-F",
                        "type": "full",
                        "timestamp": {
                            "start": {"epoch": 1756875000},
                            "stop": {"epoch": 1756875120},
                        },
                    },
                    {
                        "label": "20260904031700-D",
                        "type": "diff",
                        "timestamp": {
                            "start": {"epoch": 1756961220},
                            "stop": {"epoch": 1756961240},
                        },
                    },
                ],
            }
        ]
    )


def _run_status(tmp_path: Path, archiver_tsv: str) -> tuple[subprocess.CompletedProcess[str], Path, Path]:
    bin_dir, log_path = _fake_docker(tmp_path)
    output = tmp_path / "backup.json"
    environment = {
        **os.environ,
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "FAKE_DOCKER_LOG": str(log_path),
        "FAKE_ARCHIVER_TSV": archiver_tsv,
        "FAKE_INFO_JSON": _info_json(),
        "BFX_POSTGRES_CONTAINER": "bfx-postgres-test",
    }
    completed = subprocess.run(
        [str(PG_BACKREST_DIR / "status.sh"), "--output", str(output), "--require-rpo"],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    return completed, output, log_path


def test_status_collects_one_select_and_redacts_command_diagnostics(tmp_path: Path) -> None:
    completed, output, log_path = _run_status(
        tmp_path,
        "1756961300000\t1756961240000\t00000001000000000000000A\t0\t",
    )

    assert completed.returncode == 0
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["rpo_seconds"] == 60
    docker_log = log_path.read_text(encoding="utf-8")
    assert len(re.findall(r"\bSELECT\b", docker_log, re.I)) == 1
    assert docker_log.count("pgbackrest --stanza=bfx info --output=json") == 1
    assert TOKEN_SENTINEL not in completed.stdout
    assert TOKEN_SENTINEL not in completed.stderr
    assert TOKEN_SENTINEL not in json.dumps(report)


def test_status_returns_nonzero_and_measured_false_for_stale_archive(tmp_path: Path) -> None:
    completed, output, _ = _run_status(
        tmp_path,
        "1756961300000\t1756960999000\t00000001000000000000000A\t0\t",
    )

    assert completed.returncode != 0
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["measured"] is False
    assert report["error_code"] == "archive_lag_exceeded"
    assert "rpo_seconds" not in report
    assert TOKEN_SENTINEL not in completed.stdout
    assert TOKEN_SENTINEL not in completed.stderr
    assert TOKEN_SENTINEL not in json.dumps(report)
