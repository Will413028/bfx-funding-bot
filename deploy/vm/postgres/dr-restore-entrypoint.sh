#!/usr/bin/env bash
set -euo pipefail

case "$DR_VOLUME_NAME" in
  bfx-dr-*) ;;
  *) echo "ERROR: restore volume is not a bfx-dr resource" >&2; exit 2 ;;
esac

[ "$DR_VOLUME_NAME" != bfx_pgdata ]
: "$DR_TARGET_BACKUP_LABEL"

rm -rf "$PGDATA"/*
if [ -n "$DR_TARGET_TIME" ]; then
  pgbackrest --stanza=bfx --set="$DR_TARGET_BACKUP_LABEL" \
    --type=time --target="$DR_TARGET_TIME" --target-action=promote restore
else
  pgbackrest --stanza=bfx --set="$DR_TARGET_BACKUP_LABEL" \
    --target-action=promote restore
fi

exec /usr/local/bin/docker-entrypoint.sh postgres \
  -c archive_mode=off -c archive_command=''
