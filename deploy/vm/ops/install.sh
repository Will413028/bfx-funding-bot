#!/usr/bin/env bash
# First install of the host tooling for digest deploys, alerts and DR checks.
#
#   sudo deploy/vm/ops/install.sh        # from the clean VM mirror, at the release to install
#
# Lays out exactly what bfx-deploy maintains afterwards (it re-installs from each
# release it deploys, effective from its next run), so this script is needed once:
#
#   /usr/local/lib/bfx-ops/releases/<rev>/{ops,systemd}   root-owned, from git objects
#   /usr/local/lib/bfx-ops/releases/<rev>/ops/.venv       uv sync --frozen (ops/uv.lock)
#   /usr/local/lib/bfx-ops/current -> releases/<rev>
#   /usr/local/sbin/bfx-deploy, bfx-notify                 wrappers into current
#   /home/ubuntu/bfx-releases/<rev>, current -> <rev>       clean git worktree (DR scripts)
#   /etc/systemd/system/<systemd/managed-units>            copied, then daemon-reload
#
# It never enables, starts or stops a timer: that is a separate operator step.
set -euo pipefail

ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../../.." && pwd)
OPS_ROOT=/usr/local/lib/bfx-ops
DR_ROOT=/home/ubuntu/bfx-releases
SBIN=/usr/local/sbin
UNIT_DESTINATION=/etc/systemd/system
UV=/usr/local/bin/uv
SYSTEM_PYTHON=/usr/bin/python3

if [[ "$(uname -s)" != Linux ]]; then
  echo "Linux is required." >&2
  exit 1
fi
if [[ "$(id -u)" -ne 0 ]]; then
  echo "Run as root (sudo)." >&2
  exit 1
fi
[[ -x "$UV" ]] || { echo "uv is required at $UV (see the cutover runbook)." >&2; exit 1; }
docker buildx version >/dev/null 2>&1 || { echo "docker buildx is required." >&2; exit 1; }

MIRROR_USER=$(stat -c %U "$ROOT")
as_owner() { runuser -u "$MIRROR_USER" -- "$@"; }
if [[ -n "$(as_owner git -C "$ROOT" status --porcelain --untracked-files=no)" ]]; then
  echo "The checkout at $ROOT has local changes; install from a clean checkout." >&2
  exit 1
fi
REV=$(as_owner git -C "$ROOT" rev-parse HEAD)
RELEASE="$OPS_ROOT/releases/$REV"

if [[ ! -f "$RELEASE/.installed" ]]; then
  STAGE="$OPS_ROOT/releases/.$REV.staging"
  rm -rf -- "$STAGE" "$RELEASE"
  install -d -o root -g root -m 0755 "$STAGE"
  # From git objects, not the working tree, exactly as bfx-deploy does.
  as_owner git -C "$ROOT" archive "$REV" deploy/vm/ops deploy/vm/systemd \
    | tar -x -C "$STAGE" --strip-components=2 --no-same-owner
  UV_PROJECT_ENVIRONMENT="$STAGE/ops/.venv" UV_CACHE_DIR=/var/lib/bfx-deploy/uv-cache \
    UV_PYTHON_DOWNLOADS=never \
    "$UV" sync --frozen --no-install-project --python "$SYSTEM_PYTHON" --project "$STAGE/ops"
  chown -R root:root "$STAGE"
  chmod -R u=rwX,go=rX "$STAGE"
  printf '%s\n' "$REV" >"$STAGE/.installed"
  mv -T -- "$STAGE" "$RELEASE"
fi
ln -sfn "releases/$REV" "$OPS_ROOT/.current.new"
mv -T -- "$OPS_ROOT/.current.new" "$OPS_ROOT/current"

write_wrapper() {
  local name="$1" target="$2" temporary
  temporary=$(mktemp)
  printf '#!/bin/sh\nexec %s %s "$@"\n' "$OPS_ROOT/current/ops/.venv/bin/python" \
    "$OPS_ROOT/current/ops/$target" >"$temporary"
  install -o root -g root -m 0755 "$temporary" "$SBIN/$name"
  rm -f -- "$temporary"
}
write_wrapper bfx-deploy bfx_deploy.py
write_wrapper bfx-notify bfx_notify.py

install -d -o "$MIRROR_USER" -g "$MIRROR_USER" -m 0755 "$DR_ROOT"
if [[ ! -d "$DR_ROOT/$REV" ]]; then
  as_owner git -C "$ROOT" worktree add --detach "$DR_ROOT/$REV" "$REV"
fi
ln -sfn "$REV" "$DR_ROOT/.current.new"
mv -T -- "$DR_ROOT/.current.new" "$DR_ROOT/current"

while IFS= read -r unit; do
  [[ -z "$unit" || "$unit" == \#* ]] && continue
  install -o root -g root -m 0644 "$RELEASE/systemd/$unit" "$UNIT_DESTINATION/$unit"
done <"$RELEASE/systemd/managed-units"
systemctl daemon-reload

echo "Installed release $REV: tooling in $RELEASE, DR checkout in $DR_ROOT/$REV, units in $UNIT_DESTINATION."
echo "No timer was enabled or started."
