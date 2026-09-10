#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$BASH_SOURCE")" && pwd)"
CONTAINER="${BFX_POSTGRES_CONTAINER:-bfx-postgres}"
OUTPUT="${HOME}/bfx/dr-evidence/backup.json"

[[ "$CONTAINER" =~ ^[A-Za-z0-9_.-]+$ ]] || {
  echo "ERROR: invalid postgres container name" >&2
  exit 2
}

if (($# == 1)) && [[ "$1" == "--scheduled" ]]; then
  WEEKDAY="$(date -u +%u)"
  [[ "$WEEKDAY" =~ ^[1-7]$ ]] || {
    echo "ERROR: invalid UTC weekday" >&2
    exit 2
  }
  if [[ "$WEEKDAY" == "7" ]]; then
    TYPE="full"
  else
    TYPE="diff"
  fi
elif (($# == 2)) && [[ "$1" == "--type" ]] && [[ "$2" =~ ^(full|diff)$ ]]; then
  TYPE="$2"
else
  echo "ERROR: usage: backup.sh --scheduled | --type full | --type diff" >&2
  exit 2
fi

python3 "$SCRIPT_DIR/evidence.py" backup-failure \
  --error-code backup_refresh_incomplete --output "$OUTPUT" >/dev/null 2>&1 || {
  echo "backup_evidence_unavailable" >&2
  exit 2
}
BACKUP_LOG="$(mktemp)"
trap 'rm -f -- "$BACKUP_LOG"' EXIT
BACKUP_STATUS=0
docker exec --user postgres "$CONTAINER" pgbackrest --stanza=bfx --type="$TYPE" backup \
  >"$BACKUP_LOG" 2>&1 || BACKUP_STATUS=$?
if ((BACKUP_STATUS != 0)); then
  python3 "$SCRIPT_DIR/evidence.py" backup-failure \
    --error-code pgbackrest_check_failed \
    --output "$OUTPUT" || exit 2
  exit "$BACKUP_STATUS"
fi

"$SCRIPT_DIR/status.sh" --require-rpo
