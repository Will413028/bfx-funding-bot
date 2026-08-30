import os
import re
import subprocess
from pathlib import Path


SCRIPT = Path(__file__).parents[3] / "deploy" / "vm" / "soak-checkpoint.sh"


def _source() -> str:
    return SCRIPT.read_text()


def test_checkpoint_script_has_fail_closed_shell_and_absolute_report_targets():
    source = _source()

    assert "set -euo pipefail" in source
    assert 'REPORTS_DIR="${BFX_REPORTS_DIR:-/home/ubuntu/bfx/reports}"' in source
    assert "/home/ubuntu/bfx/reports" in source
    assert 'soak-checkpoint-latest.txt' in source
    assert 'soak-checkpoint-${STAMP}.txt' in source
    assert 'WINDOW="${1:-90m}"' in source
    assert 'mktemp /tmp/soak-checkpoint.' in source


def test_checkpoint_script_captures_logs_once_and_counts_idle_canary_safely():
    source = _source()

    assert source.count("docker logs --since") == 1
    assert "grep -cE" in source
    assert "|| true" in source
    assert 'docker logs --since "$WINDOW" bfx-bot > "$LOG_FILE"' in source


def test_checkpoint_script_probes_only_readonly_admin_and_health_paths():
    source = _source()

    assert "/healthz" in source
    assert "/readyz" in source
    assert "/admin/trading-status" in source
    assert "/admin/dry-evaluate" in source
    assert "/admin/halt" not in source
    assert "/admin/resume" not in source
    assert 'BFX_ADMIN_TOKEN' in source
    assert 'echo "$BFX_ADMIN_TOKEN"' not in source


def test_checkpoint_script_forbids_mutating_deployment_commands():
    source = _source().lower()

    for denied in (
        "git pull", "docker compose up", "docker compose down", "docker compose restart",
        "docker restart", "docker stop", "docker rm", "docker kill", "docker start",
        "docker compose",
        "insert ", "update ", "delete ", "truncate ", "drop ", "alter ",
        "grant ", "revoke ",
    ):
        assert denied not in source
    for pattern in (
        r"(?:echo|printf|print)\s+[^\n]*bfx_admin_token",
        r"(?:echo|printf|print)\s*\([^\n]*bfx_admin_token",
    ):
        assert not re.search(pattern, source)


def test_checkpoint_script_queries_candle_and_reconcile_state_with_select_only():
    source = _source()

    assert "funding_candle_revisions" in source
    assert "finalized_at_ms" in source
    assert "reconcile_observation" in source
    assert 'readonly_sql "SELECT' in source
    assert "INSERT " not in source
    assert "UPDATE " not in source
    assert "DELETE " not in source


def test_checkpoint_script_preserves_http_error_status_and_body_diagnostics():
    source = _source()

    assert "except urllib.error.HTTPError as exc:" in source
    assert "exc.code" in source
    assert "exc.read()" in source
    assert 'str(exc.code) + " body="' in source


def test_checkpoint_invocation_is_read_only_and_does_not_leak_admin_token(tmp_path):
    calls = tmp_path / "calls.log"
    reports = tmp_path / "reports"
    fake_docker = tmp_path / "docker"
    fake_docker.write_text(
        """#!/bin/sh
set -eu
{ printf '%s\\n' "$*" | tr '\\n' ' '; printf '\\n'; } >> "$FAKE_CALLS"
if [ "$1" = logs ]; then
  exit 0
fi
if [ "$1" = inspect ]; then
  printf '%s\\n' 'sha=sha256:test health=healthy restarts=0'
  exit 0
fi
if [ "$1" = exec ] && [ "$2" = bfx-bot ]; then
  printf '%s\\n' '/healthz: HTTP 503 body=degraded' '/readyz: HTTP 404 body=route missing' '/admin/trading-status: {"halted":false}' '/admin/dry-evaluate: {"would_submit_any":false}'
  exit 0
fi
if [ "$1" = exec ] && [ "$2" = bfx-postgres ]; then
  printf '%s\\n' '7'
  exit 0
fi
exit 99
"""
    )
    fake_docker.chmod(0o755)

    result = subprocess.run(
        [str(SCRIPT), "1m"],
        env={
            **os.environ,
            "PATH": f"{tmp_path}:{os.environ['PATH']}",
            "FAKE_CALLS": str(calls),
            "BFX_REPORTS_DIR": str(reports),
            "BFX_ADMIN_TOKEN": "TOKEN-SENTINEL",
        },
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    report = next(reports.glob("soak-checkpoint-*.txt")).read_text()
    assert "HTTP 503 body=degraded" in report
    assert "HTTP 404 body=route missing" in report
    assert "TOKEN-SENTINEL" not in report
    assert "TOKEN-SENTINEL" not in result.stdout
    assert "TOKEN-SENTINEL" not in result.stderr

    invocations = calls.read_text().splitlines()
    assert sum(line.startswith("logs ") for line in invocations) == 1
    assert all(
        line.startswith(("logs ", "inspect ", "exec bfx-bot ", "exec bfx-postgres "))
        for line in invocations
    )
    sql_calls = [line for line in invocations if line.startswith("exec bfx-postgres ")]
    assert len(sql_calls) == 4
    assert all(" -c SELECT " in line for line in sql_calls)
    assert all(
        not re.search(r"\b(insert|update|delete|truncate|drop|alter|grant|revoke)\b", line, re.I)
        for line in sql_calls
    )
