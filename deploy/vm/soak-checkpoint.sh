#!/usr/bin/env bash
# One-shot, read-only checkpoint for a running canary VM.
set -euo pipefail

WINDOW="${1:-90m}"
REPORTS_DIR="${BFX_REPORTS_DIR:-/home/ubuntu/bfx/reports}"
HEALTHZ_BASE_URL="${BFX_HEALTHZ_BASE_URL:-http://127.0.0.1:8080}"
case "$REPORTS_DIR" in
  /*) ;;
  *) printf '%s\n' 'BFX_REPORTS_DIR must be an absolute path' >&2; exit 2 ;;
esac
STAMP="$(date -u +%Y%m%d-%H%M)"
REPORT="${REPORTS_DIR}/soak-checkpoint-${STAMP}.txt"
LATEST="${REPORTS_DIR}/soak-checkpoint-latest.txt"
LOG_FILE="$(mktemp /tmp/soak-checkpoint.XXXXXX.log)"
trap 'rm -f "$LOG_FILE"' EXIT

mkdir -p "$REPORTS_DIR"

# Keep this as the only docker-log read. The || true is intentional: an idle
# canary, or a container with no matching lines, must still produce a report.
docker logs --since "$WINDOW" bfx-bot > "$LOG_FILE" 2>&1 || true

count_log() {
  local pattern="$1"
  grep -cE "$pattern" "$LOG_FILE" || true
}

inspect_bot() {
  docker inspect --format='sha={{.Image}} health={{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}} restarts={{.RestartCount}}' bfx-bot 2>&1 || printf '%s\n' 'unavailable'
}

probe_http() {
  docker exec bfx-bot python -c '
import json
import os
import sys
import urllib.error
import urllib.request

token = os.environ.get("BFX_ADMIN_TOKEN", "")
base_url = sys.argv[1].rstrip("/")

def call(path, method="GET"):
    headers = {"Authorization": "Bearer " + token} if token else {}
    request = urllib.request.Request(
        base_url + path, headers=headers, method=method,
        data=b"" if method == "POST" else None,
    )
    with urllib.request.urlopen(request, timeout=15) as response:
        return response.read().decode("utf-8")

def redact(body):
    return body.replace(token, "[redacted]") if token else body

for path, method in (("/healthz", "GET"), ("/readyz", "GET"),
                     ("/admin/trading-status", "GET"),
                     ("/admin/dry-evaluate", "POST")):
    try:
        body = call(path, method)
        try:
            body = json.dumps(json.loads(body), sort_keys=True)
        except json.JSONDecodeError:
            pass
        print(path + ": " + redact(body))
    except urllib.error.HTTPError as exc:
        try:
            body = exc.read().decode("utf-8", errors="replace").strip()
        except Exception:
            body = "<unreadable>"
        print(path + ": HTTP " + str(exc.code) + " body=" + redact(body or "<empty>"))
    except Exception as exc:
        print(path + ": unavailable (" + type(exc).__name__ + ")")
' "$HEALTHZ_BASE_URL" 2>&1 </dev/null || printf '%s\n' 'container probe unavailable'
}

readonly_sql() {
  docker exec bfx-postgres psql -U bfx -d bfx -X -qAt -c "$1" </dev/null 2>&1 || printf '%s\n' 'unavailable'
}

{
  echo "===== Read-only VM soak checkpoint @ $(date -u '+%Y-%m-%d %H:%M UTC') ====="
  echo "window=$WINDOW"
  echo "--- container ---"
  inspect_bot
  echo "--- HTTP probes ---"
  probe_http
  echo "--- bounded log counters ---"
  echo "scheduler_tick=$(count_log 'scheduler_tick')"
  echo "reconcile=$(count_log 'reconcile')"
  echo "divergence=$(count_log 'divergence')"
  echo "blocked=$(count_log 'blocked')"
  echo "submit=$(count_log 'submit')"
  echo "error=$(count_log '^[0-9-]+ [0-9:,]+ (ERROR|CRITICAL) ')"
  echo "--- read-only candle/reconcile SQL ---"
  echo "candle_revisions=$(readonly_sql "SELECT count(*) FROM funding_candle_revisions")"
  echo "candles_finalized=$(readonly_sql "SELECT count(*) FROM funding_candles WHERE finalized_at_ms IS NOT NULL")"
  echo "candles_not_final=$(readonly_sql "SELECT count(*) FROM funding_candles WHERE is_final = false")"
  echo "reconcile_observations=$(readonly_sql "SELECT count(*) FROM reconcile_observation")"
  echo "DONE"
} > "$REPORT" 2>&1

ln -sfn "$REPORT" "$LATEST"
