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

report_timer_check_failure() {
  printf '%s\n' "$1" >&2
  return 1
}

check_timer_state() {
  local timer="$1" allow_not_found="$2"
  local load_state active_state enabled_state active_status enabled_status

  if ! load_state=$(sudo systemctl show --property=LoadState --value "$timer" 2>/dev/null); then
    report_timer_check_failure "Unable to query timer load state: $timer" || return 1
  fi
  case "$load_state" in
    not-found)
      [[ "$allow_not_found" == true ]] && return 0
      report_timer_check_failure "Timer was not loaded after installation: $timer" || return 1
      ;;
    loaded)
      ;;
    *)
      report_timer_check_failure "Unexpected timer load state: $timer" || return 1
      ;;
  esac

  if active_state=$(sudo systemctl is-active "$timer" 2>/dev/null); then
    report_timer_check_failure "Timer is already active: $timer" || return 1
  else
    active_status=$?
    if [[ "$active_status" -ne 3 || "$active_state" != inactive ]]; then
      report_timer_check_failure "Unable to query timer active state: $timer" || return 1
    fi
  fi

  if enabled_state=$(sudo systemctl is-enabled "$timer" 2>/dev/null); then
    report_timer_check_failure "Timer is already enabled: $timer" || return 1
  else
    enabled_status=$?
    case "$enabled_status:$enabled_state" in
      1:disabled)
        ;;
      *)
        report_timer_check_failure "Unable to query timer enabled state: $timer" || return 1
        ;;
    esac
  fi
}

preflight_failed=false
for timer in "${TIMERS[@]}"; do
  if ! check_timer_state "$timer" true; then
    preflight_failed=true
  fi
done
if [[ "$preflight_failed" == true ]]; then
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
  check_timer_state "$timer" false || exit 1
done

printf '%s\n' "$UNIT_DESTINATION"
printf '%s\n' "Timers remain disabled and stopped."
