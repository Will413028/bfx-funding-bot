#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$BASH_SOURCE")" && pwd)"
CONTAINER="${BFX_POSTGRES_CONTAINER:-bfx-postgres}"
OUTPUT="${HOME}/bfx/dr-evidence/backup.json"
REQUIRE_RPO=false

while (($# > 0)); do
  case "$1" in
    --output)
      (($# >= 2)) || { echo "ERROR: --output requires a path" >&2; exit 2; }
      OUTPUT="$2"
      shift 2
      ;;
    --require-rpo)
      REQUIRE_RPO=true
      shift
      ;;
    *)
      echo "ERROR: unsupported argument" >&2
      exit 2
      ;;
  esac
done

[[ "$CONTAINER" =~ ^[A-Za-z0-9_.-]+$ ]] || {
  echo "ERROR: invalid postgres container name" >&2
  exit 2
}
[[ "$OUTPUT" = /* ]] || {
  echo "ERROR: evidence output path must be absolute" >&2
  exit 2
}

# Invalidate the previous measurement before any capture/allocation can fail.
python3 "$SCRIPT_DIR/evidence.py" backup-failure \
  --error-code backup_refresh_incomplete --output "$OUTPUT" >/dev/null 2>&1 || {
  echo "backup_evidence_unavailable" >&2
  exit 2
}
TEMP_DIR="$(mktemp -d)"
trap 'rm -rf -- "$TEMP_DIR"' EXIT
ARCHIVER_FILE="$TEMP_DIR/archiver.tsv"
INFO_FILE="$TEMP_DIR/info.json"
COMMAND_LOG="$TEMP_DIR/command.log"

SQL="SELECT
  (EXTRACT(EPOCH FROM clock_timestamp()) * 1000)::bigint,
  COALESCE(((EXTRACT(EPOCH FROM last_archived_time) * 1000)::bigint)::text, ''),
  COALESCE(last_archived_wal, ''),
  failed_count,
  COALESCE(((EXTRACT(EPOCH FROM last_failed_time) * 1000)::bigint)::text, '')
FROM pg_stat_archiver;"

ARCHIVER_STATUS=0
docker exec --user postgres "$CONTAINER" psql -X -qAt -F $'\t' \
  -v ON_ERROR_STOP=1 -h /var/run/postgresql -U bfx -d bfx -c "$SQL" \
  >"$ARCHIVER_FILE" 2>"$COMMAND_LOG" || ARCHIVER_STATUS=$?
if ((ARCHIVER_STATUS != 0)); then
  : >"$ARCHIVER_FILE"
fi

INFO_STATUS=0
docker exec --user postgres "$CONTAINER" pgbackrest --stanza=bfx info --output=json \
  >"$INFO_FILE" 2>"$COMMAND_LOG" || INFO_STATUS=$?
if ((INFO_STATUS != 0)); then
  : >"$INFO_FILE"
fi

EVIDENCE_STATUS=0
python3 "$SCRIPT_DIR/evidence.py" backup \
  --archiver-tsv "$ARCHIVER_FILE" \
  --info-json "$INFO_FILE" \
  --config "$SCRIPT_DIR/pgbackrest.conf" \
  --output "$OUTPUT" \
  --rpo-limit-seconds 300 || EVIDENCE_STATUS=$?

if ((ARCHIVER_STATUS != 0 || INFO_STATUS != 0 || EVIDENCE_STATUS != 0)); then
  echo "backup_evidence_unavailable" >&2
  exit 2
fi
cat "$OUTPUT"
if [[ "$REQUIRE_RPO" != true ]]; then
  exit 0
fi
