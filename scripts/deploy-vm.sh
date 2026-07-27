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

# Show what will ACTUALLY bind before anyone confirms. BFX_ALLOCATION_CAP_USDT is
# only a fallback for a symbol missing from the safety config's caps, and
# assert_caps_invariant guarantees no canary symbol is missing — so that env var
# binds nothing here. Setting it to 0 on 2026-07-27 read as "paused" and halted
# nothing; the bot kept lending for hours against caps.fUST=10000.
if [ "$PHASE" = canary ]; then
  # Read from .env.runtime, never from the shell env: this script runs under
  # `set -u` and never sources that file, so ${BFX_SAFETY_CONFIG} is unbound and
  # would abort the deploy. It did, on the first version of this block — and the
  # failure was invisible because the caller happened to be grepping the output.
  SAFETY_HOST=$(grep -oE 'configs/safety[^ ]*\.yaml' .env.runtime | head -1 | sed 's#^#backend_py/#')
  echo "--- effective real-money limits ---"
  echo "  per-symbol caps (BINDING): $(grep -E '^\s+caps:' "$SAFETY_HOST" 2>/dev/null | sed 's/^ *//' || echo '??? could not read '"$SAFETY_HOST")"
  echo "  BFX_ALLOCATION_CAP_USDT  : $(grep '^BFX_ALLOCATION_CAP_USDT=' .env.runtime | cut -d= -f2) (fallback only — does NOT bind configured symbols)"
  # Captured rather than inlined so the "unset" case is explicit at a glance.
  # (The previous inline `... || echo '<unset>'` was in fact correct: `||` binds
  # to the whole pipeline, and under pipefail a failing grep does trigger it.
  # Verified on the 2026-07-27 deploy that unset the flag — it printed <unset>.)
  KILL_SWITCH=$(grep '^BFX_KILL_SWITCH=' .env.runtime | cut -d= -f2- || true)
  echo "  BFX_KILL_SWITCH          : ${KILL_SWITCH:-<unset>} (break-glass only)"
  echo "  ⚠ the durable halt is a row in trading_halt, NOT visible in this file."
  echo "    check it:  GET /admin/trading-status  ·  POST /admin/dry-evaluate"
  echo "-----------------------------------"
fi

# Real-money gate.
if [ "$PHASE" = canary ] && [ "${BFX_CANARY_CONFIRM:-}" != yes ]; then
  # Without this check a non-interactive run (ssh 'cmd', CI, cron) reaches `read`,
  # gets EOF, and dies via `set -e` printing NOTHING — while .env.runtime has
  # already been rewritten above. The deploy looks like it worked and the
  # containers keep running the previous config. Bit us 2026-07-27 pausing the
  # canary: cap was 0 on disk and still 10000 in the live process.
  if [ ! -t 0 ]; then
    echo "ERROR: canary deploy needs interactive confirmation but stdin is not a TTY." >&2
    echo "       Nothing was deployed; .env.runtime may already be regenerated." >&2
    echo "       Re-run with BFX_CANARY_CONFIRM=yes to confirm non-interactively." >&2
    exit 1
  fi
  read -r -p "CANARY = REAL MONEY (per-symbol caps from the safety config; live fUST funded, fUSD dark). Type 'yes' to proceed: " ans
  [ "$ans" = yes ] || { echo "aborted"; exit 1; }
fi

export GIT_SHA="$(git rev-parse --short HEAD)"
docker compose -f docker-compose.bot.yml build --build-arg GIT_SHA="$GIT_SHA"
docker compose -f docker-compose.bot.yml up -d --remove-orphans
echo "deployed phase=$PHASE sha=$GIT_SHA"
docker compose -f docker-compose.bot.yml ps
