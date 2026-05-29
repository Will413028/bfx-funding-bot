#!/usr/bin/env bash
# Koyeb deploy script (idempotent) — paper / shadow / canary
#
# Usage:
#   scripts/deploy-koyeb.sh paper      # 1hr smoke run (simulated)
#   scripts/deploy-koyeb.sh shadow     # 2-4 週 long run, simulated, no auto-exit
#   scripts/deploy-koyeb.sh canary     # ⚠️ REAL MONEY: bitfinex_live, $450 cap
#
# Behaviour:
#   - Idempotent: app/service created if missing, env vars upserted if exists.
#   - Each invocation triggers a new Koyeb deployment.
#   - Koyeb pulls latest commit from main branch on every deployment.
#   - Phase flips are bidirectional: switching to paper/shadow clears the
#     canary-only live vars (BFX_EXECUTOR etc.) so flipping back never leaves a
#     `paper + bitfinex_live` mismatch (ExecutorConfigError). Safe to flip freely.
#
# Prerequisites:
#   1. koyeb CLI installed:  brew install koyeb/tap/koyeb
#   2. koyeb authenticated:  `koyeb whoami` works (login via `koyeb login`)
#   3. Koyeb secrets created via dashboard (https://app.koyeb.com/secrets):
#        - bfx-database-url   (Neon connection string, scheme must be
#                              `postgresql+asyncpg://`, NOT `postgresql://`)
#        - bfx-redis-url      (Upstash connection string, scheme `rediss://`)
#        canary additionally needs:
#        - bfx-api-key        (Bitfinex API key — Funding read + WRITE/CANCEL scope)
#        - bfx-api-secret     (Bitfinex API secret)
#   4. Repo pushed to origin/main (Koyeb builds from GitHub).
#
# Observability: structured stdout (Axiom retired in Phase 3c). View via
#   `koyeb service logs ...`. Smoke validation = PG-backed L3 HTTP endpoint.
#
# Canary specifics: see docs/deploy/koyeb-canary.md (Pre-live Gate, kill switch,
# rollback). Run the Pre-live Gate checklist BEFORE first canary deploy.

set -euo pipefail

# ---------- Args ----------
PHASE="${1:-}"
case "$PHASE" in
  paper|shadow|canary) ;;
  *)
    echo "Usage: $0 paper|shadow|canary" >&2
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

REQUIRED_SECRETS=(bfx-database-url bfx-redis-url)
if [[ "$PHASE" == "canary" ]]; then
  REQUIRED_SECRETS+=(bfx-api-key bfx-api-secret)
fi

# ---------- Helpers ----------
say() { printf '\033[34m→\033[0m %s\n' "$*"; }
ok() { printf '\033[32m✓\033[0m %s\n' "$*"; }
warn() { printf '\033[33m⚠\033[0m %s\n' "$*" >&2; }
die() { printf '\033[31m✗\033[0m %s\n' "$*" >&2; exit 1; }

say "Phase: $PHASE"

# ---------- 0. Real-money confirmation (canary only) ----------
if [[ "$PHASE" == "canary" ]]; then
  warn "CANARY = REAL MONEY. bitfinex_live will place real funding offers (\$450 cap, fUST/USDT)."
  warn "Confirm the Pre-live Gate in docs/deploy/koyeb-canary.md is complete"
  warn "(API key submit/cancel scope, funding-wallet balance, G2 audit)."
  if [[ "${BFX_CANARY_CONFIRM:-}" != "yes" ]]; then
    printf 'Type "yes" to proceed with real-money canary deploy: '
    read -r reply || die "no input (use BFX_CANARY_CONFIRM=yes for non-interactive)"
    [[ "$reply" == "yes" ]] || die "aborted: expected \"yes\", got \"$reply\""
  else
    say "BFX_CANARY_CONFIRM=yes — skipping interactive prompt"
  fi
fi

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
# Base env (all phases). `!VAR` deletes an existing Koyeb env var.
ENV_ARGS=(
  --env "BFX_PHASE=$PHASE"
  --env 'DATABASE_URL={{secret.bfx-database-url}}'
  --env 'REDIS_URL={{secret.bfx-redis-url}}'
)

case "$PHASE" in
  paper|shadow)
    # Simulated: paper executor (default). Clear any canary-only live vars so a
    # flip back from canary never leaves paper + bitfinex_live (ExecutorConfigError).
    ENV_ARGS+=(
      --env '!BFX_EXECUTOR'
      --env '!BFX_WS_CLIENT_ENABLED'
      --env '!BFX_API_KEY'
      --env '!BFX_API_SECRET'
      --env '!BFX_ALLOCATION_CAP_USDT'
      --env '!BFX_CELLS_YAML'
      --env '!BFX_SAFETY_CONFIG'
      --env '!BFX_KILL_SWITCH'
    )
    if [[ "$PHASE" == "paper" ]]; then
      # paper = throwaway 1h smoke -> ci realm (short retention, isolated from
      # the shadow calibration dataset). See config.py phase<->realm guard.
      ENV_ARGS+=(--env "BFX_RUN_DURATION_HOURS=1" --env "BFX_DEPLOYMENT_ENV=ci")
    else
      # shadow = simulated long-run calibration dataset -> shadow realm.
      ENV_ARGS+=(--env '!BFX_RUN_DURATION_HOURS' --env "BFX_DEPLOYMENT_ENV=shadow")
    fi
    ;;
  canary)
    # REAL MONEY. Values match docs/deploy/koyeb-canary.md (verified against
    # registry.py / daemon.py / config.py). bitfinex_live requires key+secret
    # and BFX_WS_CLIENT_ENABLED=true or build_executor raises ExecutorConfigError.
    # Set realm explicitly: canary is real money -> prod realm (NOT shadow; the
    # config.py guard rejects canary+shadow). Setting it here prevents the
    # residual-env drift that left a prior canary deploy on BFX_DEPLOYMENT_ENV=shadow.
    ENV_ARGS+=(
      --env "BFX_DEPLOYMENT_ENV=prod"
      --env "BFX_EXECUTOR=bitfinex_live"
      --env "BFX_WS_CLIENT_ENABLED=true"
      --env 'BFX_API_KEY={{secret.bfx-api-key}}'
      --env 'BFX_API_SECRET={{secret.bfx-api-secret}}'
      --env "BFX_ALLOCATION_CAP_USDT=450"
      --env "BFX_CELLS_YAML=/app/configs/cells.canary.yaml"
      --env "BFX_SAFETY_CONFIG=/app/configs/safety.canary.yaml"
      --env '!BFX_RUN_DURATION_HOURS'
    )
    ;;
esac

# ---------- 5. Create or update service ----------
say "Deploying service '$APP/$SERVICE' (phase=$PHASE, region=$REGION)..."
if koyeb service get "$SERVICE" --app "$APP" >/dev/null 2>&1; then
  ok "service exists — updating env vars + triggering redeploy (latest $GIT_BRANCH HEAD)"
  # --git-sha '' = deploy the latest commit of the configured branch. Required
  # because the service may be pinned to an older sha (e.g. after a --skip-build
  # redeploy). auto-deploy-on-push stays off; manual deploys pull latest.
  koyeb service update "$APP/$SERVICE" --git-sha '' "${ENV_ARGS[@]}" >/dev/null
else
  ok "service missing — creating fresh"
  # type=web + port=tcp + HTTP healthz: Koyeb workers don't support health
  # checks, so use web type with TCP port (no auto-route, mesh-internal only)
  # and HTTP health check on /healthz. See docs/deploy/koyeb-paper.md
  # "2026-05-21 transition note" for rationale.
  koyeb service create "$SERVICE" \
    --app "$APP" \
    --type web \
    --ports 8080:tcp \
    --checks 8080:http:/healthz \
    --checks-grace-period 8080=90 \
    --routes '!/' \
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

case "$PHASE" in
  paper)
    cat <<EOF
Paper auto-exits with code 0 after ~1hr (BFX_RUN_DURATION_HOURS=1).
Verify before shadow — L3 smoke endpoint (PG read-your-writes):

  curl -X POST "https://<service-url>/smoke-test?level=L3" -H "Authorization: Bearer <admin-token>"

PASS ({"status":"ok","level":"L3"}):  ./scripts/deploy-koyeb.sh shadow
FAIL / non-2xx:                        see docs/deploy/koyeb-paper.md troubleshooting
EOF
    ;;
  shadow)
    cat <<EOF
Shadow runs continuously (simulated). Monitor:
  - Koyeb runtime log:  koyeb service logs $SVC_ID -f   (structured stdout)

Plan: 2-4 weeks of shadow data feeds into Phase 4.3 calibration.
To stop shadow:  koyeb service pause $SVC_ID
EOF
    ;;
  canary)
    cat <<EOF
⚠️ CANARY IS LIVE (real money). Watch closely:
  - First offer (proves submit scope):  koyeb service logs $SVC_ID -f
      expect order_submit / ORDER_FILL structured stdout; a venue scope error
      here means the API key lacks funding write/cancel — KILL immediately.
  - L3 smoke (execution chain landed in PG):
      curl -X POST "https://<service-url>/smoke-test?level=L3" -H "Authorization: Bearer <admin-token>"

KILL SWITCH (block all new offers):
  koyeb service update $APP/$SERVICE --env "BFX_KILL_SWITCH=true"

Rollback to shadow (stop real money):
  ./scripts/deploy-koyeb.sh shadow

Full procedure: docs/deploy/koyeb-canary.md
EOF
    ;;
esac
