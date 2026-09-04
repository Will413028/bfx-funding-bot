#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$BASH_SOURCE")" && pwd)"
CONTAINER="${BFX_POSTGRES_CONTAINER:-bfx-postgres}"

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

docker exec "$CONTAINER" pgbackrest --stanza=bfx --type="$TYPE" backup
exec "$SCRIPT_DIR/status.sh" --require-rpo
