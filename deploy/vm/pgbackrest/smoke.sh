#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$BASH_SOURCE")" && pwd)"
CONTAINER="${BFX_POSTGRES_CONTAINER:-bfx-postgres}"

if (($# != 1)) || [[ "$1" != "--confirm-r2-smoke" ]]; then
  echo "ERROR: operator confirmation required" >&2
  exit 2
fi
[[ "$CONTAINER" =~ ^[A-Za-z0-9_.-]+$ ]] || {
  echo "ERROR: invalid postgres container name" >&2
  exit 2
}

umask 077
TEMP_DIR="$(mktemp -d)" || { echo "smoke_temp_failed" >&2; exit 2; }
trap 'rm -rf -- "$TEMP_DIR"' EXIT
trap 'exit 2' HUP INT TERM

stage() {
  local marker="$1"
  shift
  if ! docker exec --user postgres "$CONTAINER" pgbackrest --stanza=bfx "$@" \
      >"$TEMP_DIR/output" 2>"$TEMP_DIR/error"; then
    echo "smoke_${marker}_failed" >&2
    exit 2
  fi
}

stage stanza_create stanza-create
echo "smoke_stanza_create_ok"
stage check check
echo "smoke_check_ok"
stage full --type=full backup
echo "smoke_full_ok"
stage diff --type=diff backup
echo "smoke_diff_ok"
stage info info --output=json
if ! python3 "$SCRIPT_DIR/evidence.py" smoke-info --info-json "$TEMP_DIR/output" \
    >"$TEMP_DIR/validation" 2>&1; then
  echo "smoke_info_failed" >&2
  exit 2
fi
echo "smoke_info_ok"
stage verify verify
echo "smoke_verify_ok"
