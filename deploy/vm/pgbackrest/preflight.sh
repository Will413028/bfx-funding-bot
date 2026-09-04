#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$BASH_SOURCE")" && pwd)"
CONTAINER="${BFX_POSTGRES_CONTAINER:-bfx-postgres}"
OUTPUT="${HOME}/bfx/dr-evidence/backup.json"

if (($# > 0)); then
  if (($# != 2)) || [[ "$1" != "--output" ]]; then
    echo "ERROR: usage: preflight.sh [--output /absolute/path/backup.json]" >&2
    exit 2
  fi
  OUTPUT="$2"
fi

[[ "$CONTAINER" =~ ^[A-Za-z0-9_.-]+$ ]] || {
  echo "ERROR: invalid postgres container name" >&2
  exit 2
}
[[ "$OUTPUT" = /* ]] || {
  echo "ERROR: evidence output path must be absolute" >&2
  exit 2
}

CHECK_LOG="$(mktemp)"
trap 'rm -f -- "$CHECK_LOG"' EXIT
CHECK_STATUS=0
docker exec "$CONTAINER" pgbackrest --stanza=bfx check \
  >"$CHECK_LOG" 2>&1 || CHECK_STATUS=$?
if ((CHECK_STATUS != 0)); then
  exit "$CHECK_STATUS"
fi

exec "$SCRIPT_DIR/status.sh" --output "$OUTPUT" --require-rpo
