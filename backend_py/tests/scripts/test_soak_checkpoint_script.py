import os
import re
import subprocess
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from json import loads
from pathlib import Path
from threading import Thread


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
    assert 'BFX_HEALTHZ_BASE_URL:-http://127.0.0.1:8080' in source


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
        "grant ", "revoke ", "create ", "vacuum ", "reindex ",
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

    class Handler(BaseHTTPRequestHandler):
        def _respond(self, status: int, body: str) -> None:
            payload = body.encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def do_GET(self):  # noqa: N802
            if self.path == "/healthz":
                self._respond(503, '{"state":"degraded TOKEN-SENTINEL"}')
            elif self.path == "/readyz":
                self._respond(503, '{"state":"starting TOKEN-SENTINEL"}')
            elif self.path == "/admin/trading-status":
                self._respond(200, '{"note":"TOKEN-SENTINEL", "halted":false}')
            else:
                self._respond(404, '{"error":"TOKEN-SENTINEL missing route"}')

        def do_POST(self):  # noqa: N802
            if self.path == "/admin/dry-evaluate":
                self._respond(200, '{"note":"TOKEN-SENTINEL", "would_submit_any":false}')
            else:
                self._respond(404, '{"error":"TOKEN-SENTINEL missing route"}')

        def log_message(self, *_args):
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    Thread(target=server.serve_forever, daemon=True).start()
    fake_docker.write_text(
        """#!/bin/sh
set -eu
python - "$FAKE_CALLS" "$@" <<'PY'
import json
import sys
with open(sys.argv[1], "a", encoding="utf-8") as calls:
    json.dump(sys.argv[2:], calls)
    calls.write("\\n")
PY
if [ "$#" -eq 4 ] && [ "$1" = logs ] && [ "$2" = --since ] && [ "$4" = bfx-bot ]; then
  exit 0
fi
    if [ "$#" -eq 3 ] && [ "$1" = inspect ] && [ "$2" = --format=* ] && [ "$3" = bfx-bot ]; then
  printf '%s\\n' 'sha=sha256:test health=healthy restarts=0'
  exit 0
fi
if [ "$#" -eq 5 ] && [ "$1" = exec ] && [ "$2" = bfx-bot ] && [ "$3" = python ] && [ "$4" = -c ]; then
  exec python -c "$5"
fi
if [ "$#" -eq 11 ] && [ "$1" = exec ] && [ "$2" = bfx-postgres ] && [ "$3" = psql ] && [ "$4" = -U ] && [ "$5" = bfx ] && [ "$6" = -d ] && [ "$7" = bfx ] && [ "$8" = -X ] && [ "$9" = -qAt ] && [ "${10}" = -c ]; then
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
            "BFX_HEALTHZ_BASE_URL": f"http://127.0.0.1:{server.server_port}",
            "BFX_ADMIN_TOKEN": "TOKEN-SENTINEL",
        },
        capture_output=True,
        text=True,
        check=False,
    )

    server.shutdown()
    assert result.returncode == 0, result.stderr
    report = next(reports.glob("soak-checkpoint-*.txt")).read_text()
    assert "/healthz: HTTP 503 body={\"state\":\"degraded [redacted]\"}" in report
    assert "/readyz: HTTP 503 body={\"state\":\"starting [redacted]\"}" in report
    assert '"note": "[redacted]"' in report
    assert "TOKEN-SENTINEL" not in report
    assert "TOKEN-SENTINEL" not in result.stdout
    assert "TOKEN-SENTINEL" not in result.stderr

    invocations = [loads(line) for line in calls.read_text().splitlines()]
    assert len(invocations) == 7  # 1 logs + 1 inspect + 1 bot probe + 4 SQL
    assert invocations.count(["logs", "--since", "1m", "bfx-bot"]) == 1
    inspect_calls = [argv for argv in invocations if argv and argv[0] == "inspect"]
    assert len(inspect_calls) == 1
    assert len(inspect_calls[0]) == 3 and inspect_calls[0][1].startswith("--format=") and inspect_calls[0][2] == "bfx-bot"
    bot_execs = [argv for argv in invocations if argv[:2] == ["exec", "bfx-bot"]]
    assert len(bot_execs) == 1
    assert bot_execs[0][2:4] == ["python", "-c"]
    assert "/healthz" in bot_execs[0][4] and "/readyz" in bot_execs[0][4]
    sql_calls = [argv for argv in invocations if argv[:2] == ["exec", "bfx-postgres"]]
    assert len(sql_calls) == 4
    for argv in sql_calls:
        assert argv[:10] == ["exec", "bfx-postgres", "psql", "-U", "bfx", "-d", "bfx", "-X", "-qAt", "-c"]
        statements = [part.strip() for part in argv[10].split(";") if part.strip()]
        assert len(statements) == 1 and statements[0].upper().startswith("SELECT ")
        assert not re.search(r"\b(insert|update|delete|truncate|drop|alter|grant|revoke|create|vacuum|reindex)\b", argv[10], re.I)
