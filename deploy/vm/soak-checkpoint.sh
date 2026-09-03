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

# Keep this as the only docker-log read. Capture failures are recorded so an
# unavailable log source cannot be reported as zero events.
if docker logs --since "$WINDOW" bfx-bot > "$LOG_FILE" 2>&1; then
  LOG_CAPTURE_OK=1
  LOG_CAPTURE_STATUS=0
else
  LOG_CAPTURE_STATUS=$?
  LOG_CAPTURE_OK=0
fi

count_log() {
  local pattern="$1"
  if [[ "$LOG_CAPTURE_OK" -ne 1 ]]; then
    printf 'unavailable (docker logs exit status %s)' "$LOG_CAPTURE_STATUS"
    return 0
  fi
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

load_canary_scope() {
  local scope
  if ! scope="$(docker exec bfx-bot python -c '
import os
from uuid import UUID

account = UUID(os.environ["BFX_EXCHANGE_ACCOUNT_ID"])
environment = os.environ["BFX_DEPLOYMENT_ENV"]
if environment not in {"prod", "shadow", "ci"}:
    raise ValueError("invalid deployment environment")
print(str(account) + "|" + environment)
' 2>/dev/null)"; then
    return 1
  fi
  CANARY_ACCOUNT_ID="${scope%%|*}"
  CANARY_ENVIRONMENT="${scope#*|}"
}

readonly_canary_sql() {
  local query="$1"
  if [[ -z "${CANARY_ACCOUNT_ID:-}" || -z "${CANARY_ENVIRONMENT:-}" ]]; then
    printf '%s\n' 'unavailable (canary scope unavailable)'
    return 0
  fi
  docker exec bfx-postgres psql -U bfx -d bfx -X -qAt \
    -v account="$CANARY_ACCOUNT_ID" -v environment="$CANARY_ENVIRONMENT" \
    -c "$query" </dev/null 2>&1 || printf '%s\n' 'unavailable'
}

CANARY_ACCOUNT_ID=""
CANARY_ENVIRONMENT=""
load_canary_scope || true

{
  echo "===== Read-only VM soak checkpoint @ $(date -u '+%Y-%m-%d %H:%M UTC') ====="
  echo "window=$WINDOW"
  echo "--- container ---"
  inspect_bot
  echo "--- HTTP probes ---"
  probe_http
  echo "--- log capture ---"
  if [[ "$LOG_CAPTURE_OK" -eq 1 ]]; then
    echo "log_capture=ok source=docker logs bfx-bot"
  else
    echo "log_capture=unavailable source=docker logs bfx-bot exit_status=$LOG_CAPTURE_STATUS"
  fi
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
  echo "--- account-local canary evidence SQL ---"
  echo "canary_scope=${CANARY_ACCOUNT_ID:-unavailable}|${CANARY_ENVIRONMENT:-unavailable}"
  echo "open_execution_uncertainties=$(readonly_canary_sql "SELECT count(*) FROM execution_uncertainties WHERE exchange_account_id = :'account'::uuid AND deployment_environment = :'environment' AND state = 'open'")"
  echo "projector_lag=$(readonly_canary_sql "SELECT GREATEST(COALESCE((SELECT max(event_seq) FROM event_log WHERE exchange_account_id = :'account'::uuid AND deployment_environment = :'environment'), 0) - COALESCE((SELECT min(last_event_seq) FROM projection_heads WHERE exchange_account_id = :'account'::uuid AND deployment_environment = :'environment'), 0), 0)")"
  echo "last_two_reconcile_fences=$(readonly_canary_sql "SELECT event_seq_fence || '@' || observed_at_ms FROM (SELECT event_seq_fence, observed_at_ms FROM reconcile_observation WHERE exchange_account_id = :'account'::uuid AND deployment_environment = :'environment' ORDER BY id DESC LIMIT 2) AS latest ORDER BY observed_at_ms ASC")"
  echo "persistent_halt=$(readonly_canary_sql "SELECT COALESCE((SELECT halted::text FROM trading_halt WHERE exchange_account_id = :'account'::uuid AND deployment_environment = :'environment' ORDER BY id DESC LIMIT 1), 'false')")"
  echo "DONE"
} > "$REPORT" 2>&1

ln -sfn "$REPORT" "$LATEST"
