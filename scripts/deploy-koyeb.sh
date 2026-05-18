#!/usr/bin/env bash
# Phase 4.1 Koyeb deploy script (idempotent)
#
# Usage:
#   scripts/deploy-koyeb.sh paper      # 1hr smoke run
#   scripts/deploy-koyeb.sh shadow     # 2-4 週 long run (no auto-exit)
#
# Behaviour:
#   - Idempotent: app/service created if missing, env vars upserted if exists.
#   - Each invocation triggers a new Koyeb deployment (env-var update or fresh
#     service create both kick off build → deploy cycle).
#   - Koyeb pulls latest commit from main branch on every deployment.
#
# Prerequisites:
#   1. koyeb CLI installed:  brew install koyeb/tap/koyeb
#   2. koyeb authenticated:  `koyeb whoami` works (login via `koyeb login`
#      in a real terminal, or write ~/.koyeb.yaml manually)
#   3. 3 Koyeb secrets created via dashboard (https://app.koyeb.com/secrets):
#        - bfx-axiom-api-key  (Axiom API token with ingest + query scope)
#        - bfx-database-url   (Neon connection string, scheme must be
#                              `postgresql+asyncpg://`, NOT `postgresql://`)
#        - bfx-redis-url      (Upstash connection string, scheme `rediss://`)
#   4. Repo pushed to origin/main (Koyeb builds from GitHub).
#
# Idempotency notes:
#   - Re-running with the same phase: updates env vars (likely no-op) + redeploys.
#   - Re-running with the other phase: swaps BFX_PHASE + adjusts
#     BFX_RUN_DURATION_HOURS, then redeploys. Safe to flip back and forth.

set -euo pipefail

# ---------- Args ----------
PHASE="${1:-}"
case "$PHASE" in
  paper|shadow) ;;
  *)
    echo "Usage: $0 paper|shadow" >&2
    exit 1
    ;;
esac

# ---------- Config ----------
APP="bfx-funding-bot"
SERVICE="marketfeed"
GIT_REPO="github.com/Will413028/bfx-funding-bot"
GIT_BRANCH="main"
REGION="sin"               # match Neon ap-southeast-1
INSTANCE="nano"
AXIOM_DATASET="bfx-funding-bot"
REQUIRED_SECRETS=(bfx-axiom-api-key bfx-database-url bfx-redis-url)

# ---------- Helpers ----------
say() { printf '\033[34m→\033[0m %s\n' "$*"; }
ok() { printf '\033[32m✓\033[0m %s\n' "$*"; }
warn() { printf '\033[33m⚠\033[0m %s\n' "$*" >&2; }
die() { printf '\033[31m✗\033[0m %s\n' "$*" >&2; exit 1; }

say "Phase: $PHASE"

# ---------- 1. Prerequisite checks ----------
say "Checking prerequisites..."
command -v koyeb >/dev/null || die "koyeb CLI not installed. brew install koyeb/tap/koyeb"
koyeb whoami >/dev/null 2>&1 || die "koyeb not authenticated. Run koyeb login or write ~/.koyeb.yaml"

for secret in "${REQUIRED_SECRETS[@]}"; do
  koyeb secrets get "$secret" >/dev/null 2>&1 \
    || die "Secret '$secret' not found. Create via https://app.koyeb.com/secrets"
done
ok "koyeb CLI, auth, ${#REQUIRED_SECRETS[@]} secrets"

# ---------- 2. Git state sanity check ----------
if git rev-parse --git-dir >/dev/null 2>&1; then
  git fetch origin "$GIT_BRANCH" --quiet 2>/dev/null || true
  LOCAL=$(git rev-parse "$GIT_BRANCH" 2>/dev/null || echo "")
  REMOTE=$(git rev-parse "origin/$GIT_BRANCH" 2>/dev/null || echo "")
  if [[ -n "$LOCAL" && -n "$REMOTE" && "$LOCAL" != "$REMOTE" ]]; then
    warn "Local $GIT_BRANCH ($LOCAL) does not match origin/$GIT_BRANCH ($REMOTE)"
    warn "Koyeb will build from origin — push your local commits first if needed:"
    warn "  git push origin $GIT_BRANCH"
  fi
fi

# ---------- 3. Ensure app exists ----------
say "Ensuring app '$APP' exists..."
if koyeb apps get "$APP" >/dev/null 2>&1; then
  ok "app exists"
else
  koyeb apps create "$APP" >/dev/null
  ok "app created"
fi

# ---------- 4. Build env-var flags (phase-specific) ----------
ENV_ARGS=(
  --env "BFX_PHASE=$PHASE"
  --env "AXIOM_DATASET=$AXIOM_DATASET"
  --env 'AXIOM_API_KEY={{secret.bfx-axiom-api-key}}'
  --env 'DATABASE_URL={{secret.bfx-database-url}}'
  --env 'REDIS_URL={{secret.bfx-redis-url}}'
)
if [[ "$PHASE" == "paper" ]]; then
  ENV_ARGS+=(--env "BFX_RUN_DURATION_HOURS=1")
else
  # Shadow: explicitly delete BFX_RUN_DURATION_HOURS so daemon runs indefinitely
  ENV_ARGS+=(--env '!BFX_RUN_DURATION_HOURS')
fi

# ---------- 5. Create or update service ----------
say "Deploying service '$APP/$SERVICE' (phase=$PHASE, region=$REGION)..."
if koyeb service get "$SERVICE" --app "$APP" >/dev/null 2>&1; then
  ok "service exists — updating env vars + triggering redeploy"
  koyeb service update "$APP/$SERVICE" "${ENV_ARGS[@]}" >/dev/null
else
  ok "service missing — creating fresh"
  koyeb service create "$SERVICE" \
    --app "$APP" \
    --type worker \
    --git "$GIT_REPO" \
    --git-branch "$GIT_BRANCH" \
    --git-builder docker \
    --git-docker-dockerfile Dockerfile \
    --git-workdir backend_py \
    --regions "$REGION" \
    --instance-type "$INSTANCE" \
    "${ENV_ARGS[@]}" >/dev/null
fi

# ---------- 6. Resolve service ID + show next-step commands ----------
SVC_ID=$(koyeb service get "$SERVICE" --app "$APP" -o json 2>/dev/null \
  | grep -oE '"id":"[a-f0-9-]+"' | head -1 | cut -d'"' -f4 | head -c 8)

echo ""
ok "Deploy initiated. Service ID: $SVC_ID"
echo ""
echo "Next:"
echo "  koyeb service logs $SVC_ID -t build -f    # tail build log"
echo "  koyeb service logs $SVC_ID -f              # tail runtime log"
echo "  koyeb service describe $SVC_ID             # status + deployment list"
echo "  koyeb service redeploy $SVC_ID             # re-trigger deploy without env change"
echo ""

if [[ "$PHASE" == "paper" ]]; then
  cat <<EOF
After ~1hr (BFX_RUN_DURATION_HOURS=1), daemon auto-exits with code 0.
Then run G1 smoke locally:

  cd backend_py
  export AXIOM_API_KEY=<token-same-as-koyeb>
  export AXIOM_DATASET=$AXIOM_DATASET
  uv run python scripts/g1_smoke_check.py --hours 1

If exit 0 (PASS):  ./scripts/deploy-koyeb.sh shadow
If exit ≠ 0:        see docs/deploy/koyeb-paper.md troubleshooting
EOF
else
  cat <<EOF
Shadow phase — daemon runs continuously. Monitor:
  - Koyeb runtime log:    koyeb service logs $SVC_ID -f
  - Axiom live stream:    https://app.axiom.co (dataset: $AXIOM_DATASET)

Plan: 2-4 weeks of shadow data feeds into Phase 4.3 calibration.
To stop shadow:  koyeb service pause $SVC_ID
EOF
fi
