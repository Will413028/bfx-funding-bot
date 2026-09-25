#!/usr/bin/env bash
# Install the host tooling for digest deploys, alerts and DR checks.
#
#   sudo deploy/vm/ops/install.sh        # from a reviewed checkout on the VM
#
# Copies deploy/vm/ops/*.py to /usr/local/lib/bfx-ops (root-owned, so the root
# units never execute files another user can edit), adds bfx-deploy and
# bfx-notify wrappers to /usr/local/sbin, installs the unit files and reloads
# systemd. It never enables, starts or stops a timer: enabling is a separate,
# explicit operator step (see the T10 cutover notes).
set -euo pipefail

ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../../.." && pwd)
OPS_SOURCE="$ROOT/deploy/vm/ops"
UNIT_SOURCE="$ROOT/deploy/vm/systemd"
LIB=/usr/local/lib/bfx-ops
SBIN=/usr/local/sbin
UNIT_DESTINATION=/etc/systemd/system
TOOLS=(bfx_deploy.py bfx_notify.py bfx_backup_check.py bfx_restore_test.py change_class.py)
UNITS=(
  bfx-alert@.service
  bfx-deploy.service
  bfx-deploy.timer
  bfx-backup-check.service
  bfx-backup-check.timer
  bfx-restore-test.service
  bfx-restore-test.timer
  bfx-pgbackrest-backup.service
)

if [[ "$(uname -s)" != Linux ]]; then
  echo "Linux is required." >&2
  exit 1
fi
if [[ "$(id -u)" -ne 0 ]]; then
  echo "Run as root (sudo)." >&2
  exit 1
fi
for tool in "${TOOLS[@]}"; do
  [[ -f "$OPS_SOURCE/$tool" ]] || { echo "Missing tracked tool: $tool" >&2; exit 1; }
done
for unit in "${UNITS[@]}"; do
  [[ -f "$UNIT_SOURCE/$unit" ]] || { echo "Missing tracked unit: $unit" >&2; exit 1; }
done

install -d -o root -g root -m 0755 "$LIB"
for tool in "${TOOLS[@]}"; do
  install -o root -g root -m 0644 "$OPS_SOURCE/$tool" "$LIB/$tool"
done
python3 -m py_compile "${TOOLS[@]/#/$LIB/}"
rm -rf -- "$LIB/__pycache__"

write_wrapper() {
  local name="$1" target="$2" temporary
  temporary=$(mktemp)
  printf '#!/bin/sh\nexec /usr/bin/python3 %s "$@"\n' "$LIB/$target" >"$temporary"
  install -o root -g root -m 0755 "$temporary" "$SBIN/$name"
  rm -f -- "$temporary"
}
write_wrapper bfx-deploy bfx_deploy.py
write_wrapper bfx-notify bfx_notify.py

for unit in "${UNITS[@]}"; do
  install -o root -g root -m 0644 "$UNIT_SOURCE/$unit" "$UNIT_DESTINATION/$unit"
done
systemctl daemon-reload

echo "Installed tools in $LIB, wrappers bfx-deploy/bfx-notify in $SBIN, units in $UNIT_DESTINATION."
echo "No timer was enabled or started."
