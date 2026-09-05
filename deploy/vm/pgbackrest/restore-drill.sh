#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$BASH_SOURCE")" && pwd)"
exec python3 "$SCRIPT_DIR/restore_drill.py" "$@"
