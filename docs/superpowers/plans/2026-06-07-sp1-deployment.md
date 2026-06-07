# SP1 Deployment Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.
>
> **Hybrid plan:** Tasks 1–2 are code/config (executor-doable, locally testable, committable). Tasks 3–8 are a **MANUAL runbook for Will** — they require Tailscale-admin access, Neon SQL, Vercel creds, and prod-VM SSH that the executor does not have. The executor prepares; Will runs the manual steps.

**Goal:** Ship the merged SP1 Better Auth stack live — Next FE on Vercel, the standalone web-API on the OCI VM behind Tailscale Funnel, Neon scoped roles — so a real login → authenticated `/api/v1` round-trip works, at ~$0 incremental cost, without touching the real-money daemon.

**Architecture:** Add one isolated `webapi` container (uvicorn `main:app`, host-loopback, cpu/mem-limited) to the existing `docker-compose.bot.yml`; publish it via host `tailscale funnel` (no OCI port opened); the Vercel FE's BFF proxy mints a JWT and calls the Funnel URL; the web-API verifies it via the FE's JWKS. Two new least-privilege Neon roles keep both public apps off the trading tables. The daemon container is byte-for-byte unchanged.

**Tech Stack:** Docker Compose v2, FastAPI/uvicorn, Tailscale Funnel, Neon Postgres (scoped roles), Upstash Redis, Vercel, Better Auth 1.6.14.

**Spec:** `docs/superpowers/specs/2026-06-07-sp1-deployment-design.md`

---

## File Structure

- **Modify** `docker-compose.bot.yml` — add the `webapi` service (only addition; `bot`/`migrate`/`autoheal` untouched).
- **Modify** `scripts/deploy-vm.sh` — assemble a separate `.env.webapi.runtime` (scoped role, no daemon secrets) + preflight.
- **Modify** `.gitignore` — ignore `.env.webapi.runtime`.
- Runbook content (Neon SQL, Tailscale, Vercel env) lives in this plan + the spec; secrets follow the existing `~/bfx/*.env` (chmod 600) convention on the VM and second-brain `secrets/INDEX.md` for metadata.

---

## Task 1: Add the `webapi` compose service

**Files:**
- Modify: `docker-compose.bot.yml`
- Modify: `.gitignore`

- [ ] **Step 1: Confirm `.env.webapi.runtime` is git-ignored**

Check whether `.env.runtime` is already ignored and add the web-API runtime file alongside it.
Run: `grep -nE "env\.runtime|^\.env" .gitignore`
If `.env.webapi.runtime` is not covered, add a line to `.gitignore`:
```
.env.webapi.runtime
```
(If a broad `.env*` rule already covers it, skip — verify with `git check-ignore .env.webapi.runtime` returning the path.)

- [ ] **Step 2: Add the `webapi` service to `docker-compose.bot.yml`**

Append this service (do NOT modify `bot`, `migrate`, or `autoheal`):
```yaml
  webapi:
    image: bfx-bot:local
    container_name: bfx-webapi
    command: ["uvicorn", "bfx_funding_bot.main:app", "--host", "0.0.0.0", "--port", "8000"]
    env_file: .env.webapi.runtime
    restart: unless-stopped
    ports:
      - "127.0.0.1:8000:8000"
    deploy:
      resources:
        limits:
          cpus: "0.5"
          memory: 512M
    labels:
      autoheal: "true"
    healthcheck:
      test: ["CMD", "python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:8000/health').status==200 else 1)"]
      interval: 30s
      timeout: 5s
      retries: 3
      start_period: 30s
    depends_on:
      migrate:
        condition: service_completed_successfully
```

- [ ] **Step 3: Validate the compose file parses**

Create a throwaway env so `docker compose config` can resolve `env_file`, then validate:
```bash
printf 'DATABASE_URL=postgresql+asyncpg://u:p@localhost:5432/db\nBETTER_AUTH_JWKS_URL=http://localhost:3000/api/auth/jwks\n' > .env.webapi.runtime
docker compose -f docker-compose.bot.yml config --quiet && echo "COMPOSE_OK"
docker compose -f docker-compose.bot.yml config | grep -A2 "bfx-webapi" | head
```
Expected: `COMPOSE_OK`; the rendered config shows `bfx-webapi` with `127.0.0.1:8000:8000` and the cpu/mem limits.

- [ ] **Step 4: Local container smoke test (`/health` works without a real DB)**

Build the image (via the `bot` service) and run the web-API container directly:
```bash
docker compose -f docker-compose.bot.yml build bot
docker run --rm -d --name webapi-smoke -p 127.0.0.1:8000:8000 \
  -e DATABASE_URL='postgresql+asyncpg://u:p@localhost:5432/db' \
  -e BETTER_AUTH_JWKS_URL='http://localhost:3000/api/auth/jwks' \
  bfx-bot:local uvicorn bfx_funding_bot.main:app --host 0.0.0.0 --port 8000
sleep 3
curl -fsS http://127.0.0.1:8000/health && echo
docker rm -f webapi-smoke
```
Expected: `{"status":"ok"}`. (`main.py` lifespan tolerates an unreachable DB — `/health` stays up — so a dummy `DATABASE_URL` is fine for this smoke.)

- [ ] **Step 5: Clean up the throwaway env**

Run: `rm -f .env.webapi.runtime`
Confirm `git status` shows only `docker-compose.bot.yml` (+ `.gitignore` if changed) modified.

- [ ] **Step 6: Commit**
```bash
git add docker-compose.bot.yml .gitignore
git commit -m "🚀 Deploy: add isolated webapi compose service (uvicorn main:app, loopback, resource-limited)"
```

---

## Task 2: Extend `deploy-vm.sh` to assemble the web-API env

**Files:**
- Modify: `scripts/deploy-vm.sh`

- [ ] **Step 1: Add web-API env assembly + preflight**

In `scripts/deploy-vm.sh`, AFTER the existing block that writes `.env.runtime` (the `cat "$SECRETS" "$PHASE_ENV" > .env.runtime; chmod 600 .env.runtime` lines) and its daemon preflight loop, add:
```bash
# --- web-API env (separate from the daemon: scoped DB role, NO daemon secrets) ---
WEBAPI_SECRETS="$HOME/bfx/webapi.env"
[ -f "$WEBAPI_SECRETS" ] || { echo "ERROR: missing $WEBAPI_SECRETS (chmod 600)"; exit 1; }
cp "$WEBAPI_SECRETS" .env.webapi.runtime
chmod 600 .env.webapi.runtime
for v in DATABASE_URL BETTER_AUTH_JWKS_URL; do
  grep -q "^$v=." .env.webapi.runtime || { echo "ERROR: web-API var $v missing/empty in $WEBAPI_SECRETS"; exit 1; }
done
```

- [ ] **Step 2: Shell syntax check**

Run: `bash -n scripts/deploy-vm.sh && echo "SYNTAX_OK"`
Expected: `SYNTAX_OK`. (If `shellcheck` is installed: `shellcheck scripts/deploy-vm.sh` — pre-existing warnings acceptable, no NEW errors in the added block.)

- [ ] **Step 3: Validate the env-assembly logic in isolation**

Confirm the new block behaves correctly without running the full deploy:
```bash
# success path
tmp=$(mktemp -d); printf 'DATABASE_URL=postgresql://x\nBETTER_AUTH_JWKS_URL=https://x/api/auth/jwks\n' > "$tmp/webapi.env"
( WEBAPI_SECRETS="$tmp/webapi.env"; cp "$WEBAPI_SECRETS" "$tmp/out"; \
  for v in DATABASE_URL BETTER_AUTH_JWKS_URL; do grep -q "^$v=." "$tmp/out" || { echo "FAIL:$v"; exit 1; }; done; echo "ASSEMBLE_OK" )
# failure path (missing var)
printf 'DATABASE_URL=postgresql://x\n' > "$tmp/webapi.env"
( for v in DATABASE_URL BETTER_AUTH_JWKS_URL; do grep -q "^$v=." "$tmp/webapi.env" || { echo "CORRECTLY_REJECTED:$v"; break; }; done )
rm -rf "$tmp"
```
Expected: `ASSEMBLE_OK` then `CORRECTLY_REJECTED:BETTER_AUTH_JWKS_URL`.

- [ ] **Step 4: Commit**
```bash
git add scripts/deploy-vm.sh
git commit -m "🚀 Deploy: assemble separate .env.webapi.runtime (scoped role) in deploy-vm.sh"
```

---

## Task 3: (MANUAL — Will) Create Neon scoped roles

> Run in the Neon SQL editor / `psql` against the production DB. **Do NOT use Alembic or the MCP for this.** Set strong passwords; record them in `~/second-brain/secrets/` per convention.

- [ ] **Step 1: Create the two least-privilege roles**
```sql
CREATE ROLE bfx_webauth LOGIN PASSWORD '<strong-password-1>';
GRANT USAGE ON SCHEMA auth TO bfx_webauth;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA auth TO bfx_webauth;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA auth TO bfx_webauth;
ALTER DEFAULT PRIVILEGES IN SCHEMA auth GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO bfx_webauth;
ALTER DEFAULT PRIVILEGES IN SCHEMA auth GRANT USAGE, SELECT ON SEQUENCES TO bfx_webauth;

CREATE ROLE bfx_webapi LOGIN PASSWORD '<strong-password-2>';
GRANT USAGE ON SCHEMA public TO bfx_webapi;
GRANT SELECT, INSERT, UPDATE ON public.user_profiles TO bfx_webapi;
```
> Prereq: the `auth` schema + `public.user_profiles` must exist first. They are created by migration `e61f3d1ed7ca`, which runs in Task 5's deploy (the `migrate` service). If you run this SQL BEFORE the first deploy, the `auth`-schema grants will error — either run the migration first (Task 5) then this SQL, OR run the `CREATE ROLE` lines now and the `GRANT` lines after Task 5. **Recommended order: Task 5 deploy first (creates schema), then this SQL.**

- [ ] **Step 2: Verify the grants**
```sql
\du bfx_webauth
\du bfx_webapi
-- confirm bfx_webapi has NO privileges on a trading table, e.g.:
SELECT has_table_privilege('bfx_webapi', 'public.position_state', 'SELECT');  -- expect: f
```
Expected: roles exist; `has_table_privilege(... position_state ...)` returns `f` (false). Build each role's pooled Neon connection string for use in Tasks 4 & 6.

---

## Task 4: (MANUAL — Will) Secrets + Tailscale Funnel

- [ ] **Step 1: Generate the Better Auth secret**

Run: `npx @better-auth/cli@latest secret`
Record the ≥32-char output for Vercel (`BETTER_AUTH_SECRET`, Task 6).

- [ ] **Step 2: Confirm Upstash Redis REST creds**

Reuse the existing Upstash project if present; otherwise create one. Record `UPSTASH_REDIS_REST_URL` + `UPSTASH_REDIS_REST_TOKEN` for Vercel (Task 6).

- [ ] **Step 3: Enable Tailscale Funnel for the VM node**

In the Tailscale admin console: enable **Funnel** (ACL `nodeAttrs` → `funnel`) for the VM node and ensure **MagicDNS + HTTPS certificates** are enabled for the tailnet.

- [ ] **Step 4: Create `~/bfx/webapi.env` on the VM** (initial — JWKS URL is a placeholder until Task 6)

SSH to the VM (`ssh -i ~/.ssh/id_ed25519_will413028 ubuntu@<vm-ip>`), then:
```bash
umask 077
cat > ~/bfx/webapi.env <<'EOF'
DATABASE_URL=postgresql+asyncpg://bfx_webapi:<password-2>@<neon-pooler-host>/<db>?sslmode=require
BETTER_AUTH_JWKS_URL=https://placeholder.invalid/api/auth/jwks
EOF
chmod 600 ~/bfx/webapi.env
```
> Note: use the `+asyncpg` driver (the web-API is async SQLAlchemy). The placeholder JWKS URL lets the service start (it will return 401/503 on real requests until Task 6 wires the real Vercel URL — expected during bring-up).

---

## Task 5: (MANUAL — Will) Deploy the VM stack + publish Funnel

> ⚠️ This runs `git pull --ff-only origin main` then rebuilds. **It deploys whatever is on `origin/main`**, which currently includes SP1 + any other merged-but-undeployed changes (per memory: adaptive-period / band-sweep strategy merges). Those new strategies are config-gated/inert (governed by the safety/cells config, not code presence), so a wholesale `main` deploy updates the daemon code without arming them — **but confirm that's acceptable before deploying.** Compare `git log --oneline <vm-current-sha>..origin/main` first. This deploy also restarts the real-money daemon (normal reconcile-on-restart) and applies the auth migration to live Neon.

- [ ] **Step 1: Pre-deploy review**

On the VM repo: `git fetch origin main && git log --oneline HEAD..origin/main` — review every commit that will deploy. Confirm intent. Neon PITR covers the additive auth migration.

- [ ] **Step 2: Deploy**

Run (on the VM, the phase you currently run — e.g. `canary`):
```bash
cd ~/bfx/<repo> && ./scripts/deploy-vm.sh canary
```
Expected: pulls main; `migrate` runs `alembic upgrade head` (applies `e61f3d1ed7ca` — auth schema + user_profile); `bot` rebuilds + restarts; `bfx-webapi` starts. `docker compose -f docker-compose.bot.yml ps` shows `bot` healthy and `bfx-webapi` healthy.

- [ ] **Step 3: Run Task 3 SQL now if not already done** (schema now exists).

- [ ] **Step 4: Publish the web-API via Funnel + record the URL**
```bash
tailscale funnel --bg --https=443 http://127.0.0.1:8000
tailscale funnel status
curl -fsS https://<vm>.<tailnet>.ts.net/health && echo
```
Expected: status shows the public URL; `/health` → `{"status":"ok"}`. Record `https://<vm>.<tailnet>.ts.net` as the FE `API_URL`.

---

## Task 6: (MANUAL — Will) Deploy the Vercel FE

- [ ] **Step 1: Set Vercel env** (dashboard or `vercel env add`)

| var | value |
|---|---|
| `BETTER_AUTH_SECRET` | from Task 4 Step 1 |
| `DATABASE_URL` | Neon pooled URL, `bfx_webauth` role (Task 3) |
| `API_URL` | the Funnel URL (Task 5 Step 4) |
| `BETTER_AUTH_URL` | (set in Step 3 once the Vercel URL is known) |
| `NEXT_PUBLIC_BETTER_AUTH_URL` | (set in Step 3) |
| `UPSTASH_REDIS_REST_URL` / `UPSTASH_REDIS_REST_TOKEN` | from Task 4 Step 2 |
| `PASSKEY_RP_ID` | the Vercel host (domain only) |
| `NEXT_PUBLIC_APP_URL` / `NEXT_PUBLIC_APP_NAME` / `NEXT_PUBLIC_WS_URL` | FE basics (WS placeholder OK until SP2) |

- [ ] **Step 2: Deploy + record the Vercel URL**

Run: `cd frontend && vercel --prod` (or push to the Vercel-connected branch). Record the production URL.

- [ ] **Step 3: Set the URL-dependent Vercel vars**

Set `BETTER_AUTH_URL` and `NEXT_PUBLIC_BETTER_AUTH_URL` to the Vercel production URL; set `PASSKEY_RP_ID` to its host. Redeploy so they take effect.

---

## Task 7: (MANUAL — Will) Cross-wire the JWKS URL + restart

- [ ] **Step 1: Point the web-API at the FE's JWKS**

On the VM, edit `~/bfx/webapi.env`: set `BETTER_AUTH_JWKS_URL=https://<vercel-url>/api/auth/jwks`. Then re-run the deploy to re-assemble + restart the web-API:
```bash
cd ~/bfx/<repo> && ./scripts/deploy-vm.sh canary
docker compose -f docker-compose.bot.yml exec webapi sh -c 'echo $BETTER_AUTH_JWKS_URL'
```
Expected: the env shows the real Vercel JWKS URL; `bfx-webapi` healthy.

- [ ] **Step 2: Confirm JWKS reachability from the web-API**
```bash
docker compose -f docker-compose.bot.yml exec webapi python -c \
  "import urllib.request,json; print(json.load(urllib.request.urlopen('$BETTER_AUTH_JWKS_URL'))['keys'][0]['alg'])"
```
Expected: `EdDSA` (the FE's JWKS endpoint serves an Ed25519 key).

---

## Task 8: Smoke test the end-to-end round-trip

- [ ] **Step 1: Automated E2E against the live stack (preferred)**

The gated happy-path test exists (`frontend/e2e/auth.spec.ts`). Point it at the deployed FE:
```bash
cd frontend && E2E_FULL_STACK=1 PLAYWRIGHT_BASE_URL=https://<vercel-url> pnpm test:e2e -- auth.spec.ts
```
> Note: `playwright.config.ts` currently hardcodes `baseURL: http://localhost:3000` and a `pnpm dev` webServer. For a remote run, either temporarily set `baseURL`/disable `webServer` via env, or do the manual check in Step 2. (A small config tweak to honor `PLAYWRIGHT_BASE_URL` is an optional follow-up.)
Expected: sign up → land on `/overview` → `/api/proxy/...` GET returns 200.

- [ ] **Step 2: Manual round-trip (always do this)**

On the Vercel FE: register a throwaway account → confirm redirect to `/overview` → in browser devtools Network, confirm a `/api/proxy/...` GET returns **200** (proves login → cookie → proxy JWT mint → Funnel → JWKS verify → `/api/v1`). Confirm an unauthenticated visit to `/overview` redirects to `/login`.

- [ ] **Step 3: Health + isolation check**
```bash
curl -fsS https://<vm>.<tailnet>.ts.net/health         # {"status":"ok"}
docker compose -f docker-compose.bot.yml ps             # bot + bfx-webapi both healthy
docker stats --no-stream bfx-webapi                     # within the 0.5 cpu / 512M limits
```
Expected: web-API healthy + within limits; the `bot` (daemon) healthy and undisturbed.

---

## Post-deploy

- Update memory `frontend-saas-architecture`: SP1 DEPLOYED (Funnel URL, Vercel URL, deploy date), and the `frontend SaaS architecture` MEMORY.md line.
- Record the new secrets (Neon role passwords, BETTER_AUTH_SECRET) in `~/second-brain/secrets/INDEX.md` metadata.
- Rollback (if needed): `docker compose -f docker-compose.bot.yml stop webapi && docker compose -f docker-compose.bot.yml rm -f webapi && tailscale funnel reset` (daemon unaffected); `git revert` the FE if needed. The additive auth migration can stay (harmless) or PITR if you must.
