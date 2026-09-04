#!/usr/bin/env bash
set -euo pipefail

CONTAINER="${BFX_POSTGRES_CONTAINER:-bfx-postgres}"

if (($# != 1)) || [[ "$1" != "--confirm-r2-smoke" ]]; then
  echo "ERROR: operator confirmation required" >&2
  exit 2
fi
[[ "$CONTAINER" =~ ^[A-Za-z0-9_.-]+$ ]] || {
  echo "ERROR: invalid postgres container name" >&2
  exit 2
}

docker exec "$CONTAINER" pgbackrest --stanza=bfx stanza-create
docker exec "$CONTAINER" pgbackrest --stanza=bfx check
docker exec "$CONTAINER" pgbackrest --stanza=bfx --type=full backup
docker exec "$CONTAINER" pgbackrest --stanza=bfx --type=diff backup
docker exec "$CONTAINER" pgbackrest --stanza=bfx info --output=json
docker exec "$CONTAINER" pgbackrest --stanza=bfx verify
