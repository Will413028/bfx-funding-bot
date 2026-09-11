#!/bin/bash
# Deploy the bfx canary stack on the Oracle VM. Run ON THE VM from the repo root.
# Usage: ./scripts/deploy-vm.sh <paper|shadow|shadow-p14|canary>
set -euo pipefail

PHASE="${1:-}"
case "$PHASE" in paper|shadow|shadow-p14|canary) ;; *) echo "usage: $0 <paper|shadow|shadow-p14|canary>"; exit 1 ;; esac

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
# Pull FIRST: the env assembly below reads deploy/vm/<phase>.env from the repo.
# Pulling after assembly would deploy the previous commit's profile.
git pull --ff-only origin main
SECRETS="$HOME/bfx/bot.env"
MIGRATION_SECRETS="$HOME/bfx/migrate.env"
PHASE_ENV="deploy/vm/${PHASE}.env"

[ -f "$SECRETS" ]   || { echo "ERROR: missing secrets $SECRETS (chmod 600)"; exit 1; }
[ -f "$MIGRATION_SECRETS" ] || { echo "ERROR: missing migration secrets $MIGRATION_SECRETS (chmod 600)"; exit 1; }
[ -f "$PHASE_ENV" ] || { echo "ERROR: missing $PHASE_ENV"; exit 1; }

# Assemble the single env_file the compose references: secrets + phase config.
cat "$SECRETS" "$PHASE_ENV" > .env.runtime
chmod 600 .env.runtime
cp "$MIGRATION_SECRETS" .env.migrate.runtime
chmod 600 .env.migrate.runtime
grep -q '^DATABASE_URL=.' .env.migrate.runtime || {
  echo "ERROR: DATABASE_URL missing/empty in $MIGRATION_SECRETS"
  exit 1
}

# Preflight: required vars present.
need_common="DATABASE_URL BFX_PHASE BFX_DEPLOYMENT_ENV BFX_EXECUTION_POLICY BFX_EXCHANGE_ACCOUNT_ID BFX_VAULT_KEK"
need_canary="BFX_EXECUTOR BFX_WS_CLIENT_ENABLED BFX_ALLOCATION_CAP_USDT BFX_CELLS_YAML BFX_SAFETY_CONFIG"
req="$need_common"; [ "$PHASE" = canary ] && req="$req $need_canary BFX_PROJECTOR_VERSION BFX_HALT2_EVIDENCE_REPORT BFX_EXPECTED_IMAGE_DIGEST"
for v in $req; do
  grep -q "^$v=." .env.runtime || { echo "ERROR: required var $v missing/empty for phase $PHASE"; exit 1; }
done
for v in BFX_ACCOUNT_ID BFX_API_KEY BFX_API_SECRET; do
  if grep -q "^$v=" .env.runtime; then
    echo "ERROR: legacy identity/credential variable $v is not supported; use account-owned vault + BFX_EXCHANGE_ACCOUNT_ID"
    exit 1
  fi
done
EXCHANGE_ACCOUNT_ID=$(grep '^BFX_EXCHANGE_ACCOUNT_ID=' .env.runtime | tail -1 | cut -d= -f2-)
python3 -c 'from sys import argv; from uuid import UUID; value = argv[1]; assert str(UUID(value)) == value' "$EXCHANGE_ACCOUNT_ID" 2>/dev/null || {
  echo "ERROR: BFX_EXCHANGE_ACCOUNT_ID must be a canonical UUID"
  exit 1
}

EXECUTION_POLICY=$(grep '^BFX_EXECUTION_POLICY=' .env.runtime | tail -1 | cut -d= -f2-)
case "$PHASE:$EXECUTION_POLICY" in
  paper:paper|shadow:book_guarded|shadow-p14:optimizer_shadow|canary:book_guarded|canary:optimizer_live) ;;
  *)
    echo "ERROR: execution policy $EXECUTION_POLICY is incompatible with phase $PHASE"
    exit 1
    ;;
esac

if [ "$EXECUTION_POLICY" != paper ]; then
  for v in BFX_BOOK_MAX_AGE_SECONDS BFX_BOOK_RECONCILE_INTERVAL_SECONDS BFX_BOOK_MAX_DOWN_PCT; do
    grep -q "^$v=." .env.runtime || { echo "ERROR: required var $v missing/empty for policy $EXECUTION_POLICY"; exit 1; }
  done
fi
if [ "$EXECUTION_POLICY" = optimizer_live ]; then
  for v in BFX_FILL_MODEL_ARTIFACT BFX_OPTIMIZER_FEE_RATE; do
    grep -q "^$v=." .env.runtime || { echo "ERROR: required var $v missing/empty for policy optimizer_live"; exit 1; }
  done
fi

# --- web-API env (separate from the daemon: scoped DB role, NO daemon secrets) ---
WEBAPI_SECRETS="$HOME/bfx/webapi.env"
[ -f "$WEBAPI_SECRETS" ] || { echo "ERROR: missing $WEBAPI_SECRETS (chmod 600)"; exit 1; }
cp "$WEBAPI_SECRETS" .env.webapi.runtime
chmod 600 .env.webapi.runtime

require_nonempty_env() {
  local file="$1" var="$2" value
  value=$(grep "^${var}=" "$file" | tail -1 | cut -d= -f2- || true)
  [ -n "${value//[[:space:]]/}" ] || {
    echo "ERROR: required var $var missing/empty in $file"
    exit 1
  }
}

for v in DATABASE_URL BETTER_AUTH_JWKS_URL BFX_VAULT_KEK BFX_OPERATOR_ROLE; do
  grep -q "^$v=." .env.webapi.runtime || { echo "ERROR: web-API var $v missing/empty in $WEBAPI_SECRETS"; exit 1; }
done
require_nonempty_env .env.webapi.runtime BFX_OPERATOR_USER_ID
BACKEND_OPERATOR_ROLE=$(grep '^BFX_OPERATOR_ROLE=' .env.webapi.runtime | tail -1 | cut -d= -f2-)
[ "$BACKEND_OPERATOR_ROLE" = admin ] || {
  echo "ERROR: web-API BFX_OPERATOR_ROLE must be admin for operator-only containment"
  exit 1
}

# --- frontend env (Better Auth FE: scoped bfx_webauth role, VM redis, server-only) ---
FRONTEND_SECRETS="$HOME/bfx/frontend.env"
[ -f "$FRONTEND_SECRETS" ] || { echo "ERROR: missing $FRONTEND_SECRETS (chmod 600)"; exit 1; }
cp "$FRONTEND_SECRETS" .env.frontend.runtime
chmod 600 .env.frontend.runtime
for v in NEXT_PUBLIC_APP_URL NEXT_PUBLIC_BETTER_AUTH_URL API_URL BETTER_AUTH_SECRET BETTER_AUTH_URL DATABASE_URL REDIS_URL PASSKEY_RP_ID; do
  grep -q "^$v=." .env.frontend.runtime || { echo "ERROR: frontend var $v missing/empty in $FRONTEND_SECRETS"; exit 1; }
done
require_nonempty_env .env.frontend.runtime BFX_OPERATOR_USER_ID
require_nonempty_env .env.frontend.runtime BFX_OPERATOR_ROLE
FRONTEND_OPERATOR_ID=$(grep '^BFX_OPERATOR_USER_ID=' .env.frontend.runtime | tail -1 | cut -d= -f2-)
BACKEND_OPERATOR_ID=$(grep '^BFX_OPERATOR_USER_ID=' .env.webapi.runtime | tail -1 | cut -d= -f2-)
[ "$FRONTEND_OPERATOR_ID" = "$BACKEND_OPERATOR_ID" ] || {
  echo "ERROR: frontend and web-API BFX_OPERATOR_USER_ID values must match"
  exit 1
}
FRONTEND_OPERATOR_ROLE=$(grep '^BFX_OPERATOR_ROLE=' .env.frontend.runtime | tail -1 | cut -d= -f2-)
[ "$FRONTEND_OPERATOR_ROLE" = admin ] || {
  echo "ERROR: frontend BFX_OPERATOR_ROLE must be admin for operator-only containment"
  exit 1
}
# Export NEXT_PUBLIC_* so compose build-args bake the correct public URLs.
set -a; . ./.env.frontend.runtime; set +a

# Show the committed canary safety configuration before confirmation.
if [ "$PHASE" = canary ]; then
  # Read from .env.runtime, never from the shell env: this script runs under
  # `set -u` and never sources that file, so ${BFX_SAFETY_CONFIG} is unbound and
  # would abort the deploy. It did, on the first version of this block — and the
  # failure was invisible because the caller happened to be grepping the output.
  SAFETY_HOST=$(grep -oE 'configs/safety[^ ]*\.yaml' .env.runtime | head -1 | sed 's#^#backend_py/#')
  echo "--- effective real-money limits ---"
  echo "  per-symbol caps: $(grep -E '^\s+caps:' "$SAFETY_HOST" 2>/dev/null | sed 's/^ *//' || echo '??? could not read '"$SAFETY_HOST")"
  echo "  BFX_ALLOCATION_CAP_USDT  : $(grep '^BFX_ALLOCATION_CAP_USDT=' .env.runtime | cut -d= -f2)"
  # Captured rather than inlined so the "unset" case is explicit at a glance.
  # (The previous inline `... || echo '<unset>'` was in fact correct: `||` binds
  # to the whole pipeline, and under pipefail a failing grep does trigger it.
  # Verified on the 2026-07-27 deploy that unset the flag — it printed <unset>.)
  KILL_SWITCH=$(grep '^BFX_KILL_SWITCH=' .env.runtime | cut -d= -f2- || true)
  echo "  BFX_KILL_SWITCH          : ${KILL_SWITCH:-<unset>} (break-glass only)"
  echo "  ⚠ the durable halt is a row in trading_halt, NOT visible in this file."
  echo "    check it:  GET /admin/trading-status  ·  POST /admin/dry-evaluate"
  echo "-----------------------------------"

  # Canary is real money.  It must be explicit even for an interactive shell;
  # keep this after the safety summary, but before evidence verification or
  # docker, so an omitted variable cannot progress toward a deploy.
  if [ "${BFX_CANARY_CONFIRM:-}" != yes ]; then
    echo "ERROR: canary deploy needs interactive confirmation; BFX_CANARY_CONFIRM=yes is required" >&2
    exit 1
  fi

  # An immutable, redacted report must agree with fresh account-local DB,
  # artifact, projector, image, and committed safety-config observations.
  # This command is read-only and must pass before Docker is reached.
  EVIDENCE_REPORT=$(grep '^BFX_HALT2_EVIDENCE_REPORT=' .env.runtime | tail -1 | cut -d= -f2-)
  [ -r "$EVIDENCE_REPORT" ] || {
    echo "ERROR: Halt 2 evidence report is unreadable: $EVIDENCE_REPORT"
    exit 1
  }
  DEPLOYMENT_ENV=$(grep '^BFX_DEPLOYMENT_ENV=' .env.runtime | tail -1 | cut -d= -f2-)
  PROJECTOR_VERSION=$(grep '^BFX_PROJECTOR_VERSION=' .env.runtime | tail -1 | cut -d= -f2-)
  EXPECTED_IMAGE_DIGEST=$(grep '^BFX_EXPECTED_IMAGE_DIGEST=' .env.runtime | tail -1 | cut -d= -f2-)
  (
    cd backend_py
    uv run python scripts/halt2_cutover.py preflight \
      --account-id "$EXCHANGE_ACCOUNT_ID" \
      --environment "$DEPLOYMENT_ENV" \
      --evidence "$EVIDENCE_REPORT" \
      --projector-version "$PROJECTOR_VERSION" \
      --image-digest "$EXPECTED_IMAGE_DIGEST" \
      --config-artifact "$ROOT/$SAFETY_HOST"
  ) || {
    echo "ERROR: Halt 2 preflight failed; deployment remains stopped"
    exit 1
  }
fi

# --- local pgBackRest DR boundary (validation only; no remote operations) ---
PGBACKREST_CONFIG_REL="deploy/vm/pgbackrest/pgbackrest.conf"
PGBACKREST_CONFIG="$ROOT/$PGBACKREST_CONFIG_REL"
PGBACKREST_SECRET_DIR="$HOME/bfx/pgbackrest/conf.d"
[ -r "$PGBACKREST_CONFIG" ] || {
  echo "ERROR: missing pgBackRest config" >&2
  exit 1
}
[ -f "$PGBACKREST_CONFIG" ] && [ ! -L "$PGBACKREST_CONFIG" ] || {
  echo "ERROR: pgBackRest config must be a clean tracked artifact" >&2
  exit 1
}
git -C "$ROOT" ls-files --error-unmatch -- "$PGBACKREST_CONFIG_REL" \
  >/dev/null 2>&1 || {
  echo "ERROR: pgBackRest config must be a clean tracked artifact" >&2
  exit 1
}
git -C "$ROOT" diff --quiet -- "$PGBACKREST_CONFIG_REL" \
  >/dev/null 2>&1 || {
  echo "ERROR: pgBackRest config must be a clean tracked artifact" >&2
  exit 1
}
git -C "$ROOT" diff --cached --quiet -- "$PGBACKREST_CONFIG_REL" \
  >/dev/null 2>&1 || {
  echo "ERROR: pgBackRest config must be a clean tracked artifact" >&2
  exit 1
}
python3 "$ROOT/deploy/vm/pgbackrest/secret_validation.py" --secret-dir "$PGBACKREST_SECRET_DIR"
PGBACKREST_SECRET_OPTION_PATTERN='^[[:space:]]*repo1-(s3-(endpoint|bucket|key|key-secret)|cipher-pass)[[:space:]]*=[[:space:]]*[^[:space:]]'
PGBACKREST_TRACKED_SECRET_STATUS=0
git grep -q -E "$PGBACKREST_SECRET_OPTION_PATTERN" -- . ':!docs/superpowers/specs/**' \
  || PGBACKREST_TRACKED_SECRET_STATUS=$?
if [ "$PGBACKREST_TRACKED_SECRET_STATUS" -eq 0 ]; then
  echo "ERROR: secret pgBackRest option is tracked" >&2
  exit 1
fi
if [ "$PGBACKREST_TRACKED_SECRET_STATUS" -ne 1 ]; then
  echo "ERROR: unable to scan tracked pgBackRest options" >&2
  exit 1
fi

# The bind-mounted spool/log must be writable by postgres in Alpine (UID/GID 70).
sudo install -d -m 0750 -o 70 -g 70 \
  "$HOME/bfx/pgbackrest/spool" \
  "$HOME/bfx/pgbackrest/log"
install -d -m 0750 -o "$(id -u)" -g "$(id -g)" "$HOME/bfx/dr-evidence"
docker compose -f docker-compose.bot.yml config --quiet

export GIT_SHA="$(git rev-parse --short HEAD)"
docker compose -f docker-compose.bot.yml build --build-arg GIT_SHA="$GIT_SHA"

# Task 1 uses project labels because org.opencontainers.image metadata does not
# encode the pinned PostgreSQL base digest or pgBackRest source checksum.
EXPECTED_POSTGRES_BASE_DIGEST="sha256:d3e1620b530c944afa6e887d22eb899824da68e19c52024bf98f5220c88a65b2"
EXPECTED_PGBACKREST_VERSION="2.59.1"
EXPECTED_PGBACKREST_SOURCE_SHA256="1cd522afc33b8ff846ef88c55dc238717c9c8817a4f6ca7c9f64887de9c7402d"
ACTUAL_POSTGRES_BASE_DIGEST=$(docker image inspect \
  --format='{{ index .Config.Labels "org.bfx.postgresql.base-digest" }}' \
  bfx-postgres:local) || {
  echo "ERROR: unable to inspect bfx-postgres:local base digest label" >&2
  exit 1
}
ACTUAL_PGBACKREST_VERSION=$(docker image inspect \
  --format='{{ index .Config.Labels "org.bfx.pgbackrest.version" }}' \
  bfx-postgres:local) || {
  echo "ERROR: unable to inspect bfx-postgres:local pgBackRest version label" >&2
  exit 1
}
ACTUAL_PGBACKREST_SOURCE_SHA256=$(docker image inspect \
  --format='{{ index .Config.Labels "org.bfx.pgbackrest.source-sha256" }}' \
  bfx-postgres:local) || {
  echo "ERROR: unable to inspect bfx-postgres:local pgBackRest source checksum label" >&2
  exit 1
}
[ "$ACTUAL_POSTGRES_BASE_DIGEST" = "$EXPECTED_POSTGRES_BASE_DIGEST" ] || {
  echo "ERROR: bfx-postgres:local base digest label mismatch" >&2
  exit 1
}
[ "$ACTUAL_PGBACKREST_VERSION" = "$EXPECTED_PGBACKREST_VERSION" ] || {
  echo "ERROR: bfx-postgres:local pgBackRest version label mismatch" >&2
  exit 1
}
[ "$ACTUAL_PGBACKREST_SOURCE_SHA256" = "$EXPECTED_PGBACKREST_SOURCE_SHA256" ] || {
  echo "ERROR: bfx-postgres:local pgBackRest source checksum label mismatch" >&2
  exit 1
}

if [ "$PHASE" = canary ]; then
  ACTUAL_IMAGE_DIGEST=$(docker image inspect --format='{{.Id}}' bfx-bot:local) || {
    echo "ERROR: unable to inspect built bfx-bot:local image digest"
    exit 1
  }
  [ "$ACTUAL_IMAGE_DIGEST" = "$EXPECTED_IMAGE_DIGEST" ] || {
    echo "ERROR: built bfx-bot:local image digest mismatch"
    exit 1
  }
fi
docker compose -f docker-compose.bot.yml up -d --remove-orphans
echo "deployed phase=$PHASE sha=$GIT_SHA"
docker compose -f docker-compose.bot.yml ps
