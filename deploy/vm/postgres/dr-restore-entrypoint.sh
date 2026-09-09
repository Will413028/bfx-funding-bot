#!/usr/bin/env bash
set -euo pipefail

fail() {
  echo "$1" >&2
  exit 2
}

validate_restore_inputs() {
  [[ "$1" =~ ^bfx-dr-[a-z0-9][a-z0-9-]*$ ]] || fail restore_target_invalid
  [[ "$2" =~ ^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$ ]] || fail restore_target_invalid
  [[ -z "$3" || "$3" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$ ]] || fail restore_target_invalid
}

# The main entrypoint fixes root/PGDATA; arguments let the same validation
# operate on a real temporary filesystem in offline regression tests.
validate_empty_target() (
  local root="$1" data="$2" entry path
  [[ "$root" == /* && "$root" != / && -d "$root" ]] || fail restore_target_invalid
  [[ "$data" == "$root/18/docker" ]] || fail restore_target_invalid
  path="$data"
  while [[ "$path" != / ]]; do
    [[ ! -L "$path" ]] || fail restore_target_invalid
    [[ ! -e "$path" || -d "$path" ]] || fail restore_target_invalid
    path="$(dirname -- "$path")"
  done
  shopt -s nullglob dotglob
  for entry in "$root"/*; do
    [[ "$entry" == "$root/18" ]] || fail restore_target_not_empty
  done
  for entry in "$root/18"/*; do
    [[ "$entry" == "$data" ]] || fail restore_target_not_empty
  done
  for entry in "$data"/*; do
    fail restore_target_not_empty
  done
)

prepare_restore_directories() {
  local root="$1" data="$2"
  mkdir -p -- "$data"
  # pgBackRest can create PGDATA's parent as root:root 0700. PostgreSQL's
  # official entrypoint only chowns PGDATA, not this versioned parent.
  chown 70:70 "$root/18" "$data"
  chmod 0700 "$root/18" "$data"
}

check_postgres_access() {
  gosu postgres bash -ec '
    test "$(id -u)" = 70
    test "$(id -g)" = 70
    test -x "$1" && test -w "$1" || exit 1
    test -f "$2" && test ! -L "$2" && test -r "$2" || exit 1
    test -d "$3" && test ! -L "$3" && test -r "$3" && test -x "$3" || exit 1
    for fragment in "$3"/*.conf; do
      test -f "$fragment" && test ! -L "$fragment" && test -r "$fragment" || exit 1
    done
  ' postgres-access "$1" "$2" "$3" || fail restore_permissions_invalid
}

restore_backup() {
  local label="$1" target="$2"
  if [[ -n "$target" ]]; then
    gosu postgres pgbackrest --stanza=bfx --set="$label" \
      --type=time --target="$target" --target-action=promote restore
  else
    # Preserve end-of-archive recovery. target-action is invalid for type
    # default; do not silently replace this with earliest-consistent recovery.
    gosu postgres pgbackrest --stanza=bfx --set="$label" --type=default restore
  fi
}

main() {
  local root=/var/lib/postgresql
  [[ "${PGDATA:-}" == "$root/18/docker" ]] || fail restore_target_invalid
  validate_restore_inputs "${DR_VOLUME_NAME:-}" "${DR_TARGET_BACKUP_LABEL:-}" "${DR_TARGET_TIME:-}"
  mountpoint -q "$root" || fail restore_target_not_mounted
  validate_empty_target "$root" "$PGDATA"
  prepare_restore_directories "$root" "$PGDATA"
  check_postgres_access "$PGDATA" /etc/pgbackrest/pgbackrest.conf /etc/pgbackrest/conf.d
  restore_backup "$DR_TARGET_BACKUP_LABEL" "${DR_TARGET_TIME:-}"
  exec /usr/local/bin/docker-entrypoint.sh postgres \
    -c archive_mode=off -c archive_command=''
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  main "$@"
fi
