#!/bin/bash
# Trusted-host application deployment only. See docs/runbooks/immutable-release.md.
set -euo pipefail
if [ "${1:-}" != --bundle ]; then
  echo 'Use immutable release: deploy-vm.sh --bundle /absolute/bundle.json --network NETWORK --bot-env FILE --webapi-env FILE --frontend-env FILE --halt2 FILE --dr-directory DIRECTORY' >&2
  exit 2
fi
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT/backend"
exec uv run --frozen --no-sync python -m scripts.immutable_release deploy "$@"
