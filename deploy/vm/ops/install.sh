#!/usr/bin/env bash
# First install of the host tooling for digest deploys, alerts, DR checks and the
# operator CLI wrapper.
#
#   sudo deploy/vm/ops/install.sh        # from a clean checkout at the release to install
#
# On a running host, run it from the DR checkout of the deployed release
# (/home/ubuntu/bfx-releases/current) with bfx-deploy.timer stopped and
# bfx-deploy.service inactive. Re-run it whenever a release adds an entry point in
# /usr/local/sbin, changes the sudoers drop-in or bumps GH_VERSION: bfx-deploy never
# writes those (until the pinned gh is installed, bfx-deploy refuses every new release
# with attestation_verifier_missing).
#
# Lays out exactly what bfx-deploy maintains afterwards (it re-installs from each
# release it deploys, effective from its next run), so this script is needed once:
#
#   /usr/local/lib/bfx-ops/releases/<rev>/{ops,systemd}   root-owned, from git objects
#   /usr/local/lib/bfx-ops/releases/<rev>/ops/.venv       uv sync --frozen (ops/uv.lock)
#   /usr/local/lib/bfx-ops/current -> releases/<rev>
#   /usr/local/lib/bfx-ops/gh/<GH_VERSION>/bin/gh            the pinned GitHub CLI that verifies
#                                                           each release's provenance (bfx_deploy.py
#                                                           GH_BINARY); bfx-deploy never installs it
#   /usr/local/sbin/bfx-deploy, bfx-notify, bfx-op          wrappers into current
#   /etc/sudoers.d/bfx-op                                   sudo keeps SSH_CONNECTION for bfx-op
#   /home/ubuntu/bfx-releases/<rev>, current -> <rev>       clean git worktree (DR scripts)
#   /etc/systemd/system/<systemd/managed-units>            copied, then daemon-reload
#
# It never enables, starts or stops a timer: that is a separate operator step.
set -euo pipefail

# Physical path: run through /home/ubuntu/bfx-releases/current, a logical path would be
# the root-owned symlink, and its owner would become MIRROR_USER below.
ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../../.." && pwd -P)
OPS_ROOT=/usr/local/lib/bfx-ops
DR_ROOT=/home/ubuntu/bfx-releases
SBIN=/usr/local/sbin
UNIT_DESTINATION=/etc/systemd/system
UV=/usr/local/bin/uv
SYSTEM_PYTHON=/usr/bin/python3
# The GitHub CLI release bfx-deploy verifies provenance with: an exact version from
# https://github.com/cli/cli/releases (published >= 7 days before it is pinned) and the
# sha256 of gh_<version>_linux_arm64.tar.gz from that release's checksums file. A bump
# changes GH_VERSION here and in bfx_deploy.py (a test keeps them equal).
GH_VERSION=2.102.0
GH_SHA256=7862c86c72f43df3a2d93ddde6f473285b4e2af61b494849846827e513ef6484
GH_DIR="$OPS_ROOT/gh/$GH_VERSION"

if [[ "$(uname -s)" != Linux ]]; then
  echo "Linux is required." >&2
  exit 1
fi
if [[ "$(id -u)" -ne 0 ]]; then
  echo "Run as root (sudo)." >&2
  exit 1
fi
[[ -x "$UV" ]] || { echo "uv is required at $UV (see docs/runbooks/fresh-host-setup.md)." >&2; exit 1; }
docker buildx version >/dev/null 2>&1 || { echo "docker buildx is required." >&2; exit 1; }
[[ "$(uname -m)" == aarch64 ]] || { echo "The pinned gh is linux_arm64; this host is $(uname -m)." >&2; exit 1; }

# Idempotent: an installed gh that reports the pinned version stays; anything else at
# that path is replaced, from a download whose sha256 must match before it is unpacked.
gh_reports() { local found; found=$("$1" --version 2>/dev/null) || return 1; [[ "${found%%$'\n'*}" == "gh version $GH_VERSION ("*")" ]]; }
if ! gh_reports "$GH_DIR/bin/gh"; then
  gh_download=$(mktemp -d)
  gh_archive="$gh_download/gh_${GH_VERSION}_linux_arm64.tar.gz"
  curl --fail --silent --show-error --location --proto '=https' --tlsv1.2 --output "$gh_archive" \
    "https://github.com/cli/cli/releases/download/v${GH_VERSION}/gh_${GH_VERSION}_linux_arm64.tar.gz"
  echo "$GH_SHA256  $gh_archive" | sha256sum --check --strict --quiet
  tar -x -z -f "$gh_archive" -C "$gh_download" --no-same-owner "gh_${GH_VERSION}_linux_arm64/bin/gh"
  gh_stage="$OPS_ROOT/gh/.$GH_VERSION.staging"
  rm -rf -- "$gh_stage"
  install -d -o root -g root -m 0755 "$OPS_ROOT/gh" "$gh_stage" "$gh_stage/bin"
  install -o root -g root -m 0755 "$gh_download/gh_${GH_VERSION}_linux_arm64/bin/gh" "$gh_stage/bin/gh"
  rm -rf -- "$gh_download"
  gh_reports "$gh_stage/bin/gh" || { echo "The downloaded gh does not report $GH_VERSION." >&2; exit 1; }
  rm -rf -- "$GH_DIR"
  mv -T -- "$gh_stage" "$GH_DIR"
fi

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
write_wrapper bfx-op bfx_op.py

# bfx-op reads the caller's tailnet peer from SSH_CONNECTION, which sudo's env_reset
# drops; keep exactly that variable, for exactly that command.
SUDOERS_DROPIN=/etc/sudoers.d/bfx-op
sudoers_temporary=$(mktemp)
printf 'Defaults!%s env_keep += "SSH_CONNECTION"\n' "$SBIN/bfx-op" >"$sudoers_temporary"
visudo -cqf "$sudoers_temporary"
install -o root -g root -m 0440 "$sudoers_temporary" "$SUDOERS_DROPIN"
rm -f -- "$sudoers_temporary"

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
echo "Provenance verifier: $GH_DIR/bin/gh ($GH_VERSION)."
echo "No timer was enabled or started."
