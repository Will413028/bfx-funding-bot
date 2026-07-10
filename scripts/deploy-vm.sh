#!/bin/bash
# Deploy the bfx canary stack on the Oracle VM. Run ON THE VM from the repo root.
# Usage: ./scripts/deploy-vm.sh <paper|shadow|canary>
set -euo pipefail

PHASE="${1:-}"
case "$PHASE" in paper|shadow|canary) ;; *) echo "usage: $0 <paper|shadow|canary>"; exit 1 ;; esac

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
# Pull FIRST: the env assembly below reads deploy/vm/<phase>.env from the repo —
# pulling after it deploys the PREVIOUS commit's env config (bit us 2026-07-10:
# freshly committed BFX_REPRICE/CLAMP flags were silently absent from .env.runtime,
# reverting E1 to observe-only for ~15 min on a live bot).
git pull --ff-only origin main
SECRETS="$HOME/bfx/bot.env"
PHASE_ENV="deploy/vm/${PHASE}.env"

[ -f "$SECRETS" ]   || { echo "ERROR: missing secrets $SECRETS (chmod 600)"; exit 1; }
[ -f "$PHASE_ENV" ] || { echo "ERROR: missing $PHASE_ENV"; exit 1; }

# Assemble the single env_file the compose references: secrets + phase config.
cat "$SECRETS" "$PHASE_ENV" > .env.runtime
chmod 600 .env.runtime

# Preflight: required vars present.
need_common="DATABASE_URL BFX_PHASE BFX_DEPLOYMENT_ENV"
need_canary="BFX_EXECUTOR BFX_WS_CLIENT_ENABLED BFX_API_KEY BFX_API_SECRET BFX_ALLOCATION_CAP_USDT BFX_CELLS_YAML BFX_SAFETY_CONFIG"
req="$need_common"; [ "$PHASE" = canary ] && req="$req $need_canary"
for v in $req; do
  grep -q "^$v=." .env.runtime || { echo "ERROR: required var $v missing/empty for phase $PHASE"; exit 1; }
done

# --- web-API env (separate from the daemon: scoped DB role, NO daemon secrets) ---
WEBAPI_SECRETS="$HOME/bfx/webapi.env"
[ -f "$WEBAPI_SECRETS" ] || { echo "ERROR: missing $WEBAPI_SECRETS (chmod 600)"; exit 1; }
cp "$WEBAPI_SECRETS" .env.webapi.runtime
chmod 600 .env.webapi.runtime
for v in DATABASE_URL BETTER_AUTH_JWKS_URL BFX_VAULT_KEK; do
  grep -q "^$v=." .env.webapi.runtime || { echo "ERROR: web-API var $v missing/empty in $WEBAPI_SECRETS"; exit 1; }
done

# --- frontend env (Better Auth FE: scoped bfx_webauth role, VM redis, server-only) ---
FRONTEND_SECRETS="$HOME/bfx/frontend.env"
[ -f "$FRONTEND_SECRETS" ] || { echo "ERROR: missing $FRONTEND_SECRETS (chmod 600)"; exit 1; }
cp "$FRONTEND_SECRETS" .env.frontend.runtime
chmod 600 .env.frontend.runtime
for v in NEXT_PUBLIC_APP_URL NEXT_PUBLIC_BETTER_AUTH_URL API_URL BETTER_AUTH_SECRET BETTER_AUTH_URL DATABASE_URL REDIS_URL PASSKEY_RP_ID; do
  grep -q "^$v=." .env.frontend.runtime || { echo "ERROR: frontend var $v missing/empty in $FRONTEND_SECRETS"; exit 1; }
done
# Export NEXT_PUBLIC_* so compose build-args bake the correct public URLs.
set -a; . ./.env.frontend.runtime; set +a

# Real-money gate.
if [ "$PHASE" = canary ] && [ "${BFX_CANARY_CONFIRM:-}" != yes ]; then
  read -r -p "CANARY = REAL MONEY (per-symbol caps from the safety config; live fUST funded, fUSD dark). Type 'yes' to proceed: " ans
  [ "$ans" = yes ] || { echo "aborted"; exit 1; }
fi

export GIT_SHA="$(git rev-parse --short HEAD)"
docker compose -f docker-compose.bot.yml build --build-arg GIT_SHA="$GIT_SHA"
docker compose -f docker-compose.bot.yml up -d --remove-orphans
echo "deployed phase=$PHASE sha=$GIT_SHA"
docker compose -f docker-compose.bot.yml ps
