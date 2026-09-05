#!/usr/bin/env bash

set -euo pipefail

ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
UNIT_SOURCE_DIR="$ROOT/deploy/vm/systemd"
UNIT_DESTINATION=/etc/systemd/system
UNITS=(
  bfx-pgbackrest-backup.service
  bfx-pgbackrest-backup.timer
  bfx-pgbackrest-status.service
  bfx-pgbackrest-status.timer
)
TIMERS=(
  bfx-pgbackrest-backup.timer
  bfx-pgbackrest-status.timer
)

if [[ "$(uname -s)" != Linux ]]; then
  printf '%s\n' "Linux is required." >&2
  exit 1
fi

if ! command -v systemctl >/dev/null 2>&1; then
  printf '%s\n' "systemctl is required." >&2
  exit 1
fi

if ! command -v sudo >/dev/null 2>&1; then
  printf '%s\n' "sudo is required." >&2
  exit 1
fi

for unit in "${UNITS[@]}"; do
  source_path="$UNIT_SOURCE_DIR/$unit"
  if [[ ! -f "$source_path" ]]; then
    printf '%s\n' "Missing tracked unit: $source_path" >&2
    exit 1
  fi
  sudo install -m 0644 "$source_path" "$UNIT_DESTINATION/$unit"
done

sudo systemctl daemon-reload

for timer in "${TIMERS[@]}"; do
  if sudo systemctl is-active --quiet "$timer"; then
    printf '%s\n' "Timer is already active: $timer" >&2
    exit 1
  fi
  if sudo systemctl is-enabled --quiet "$timer"; then
    printf '%s\n' "Timer is already enabled: $timer" >&2
    exit 1
  fi
done

printf '%s\n' "$UNIT_DESTINATION"
printf '%s\n' "Timers remain disabled and stopped."
