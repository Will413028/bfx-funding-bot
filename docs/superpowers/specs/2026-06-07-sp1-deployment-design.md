# SP1 Deployment Design — ship the Better Auth stack live

**Goal:** Deploy the merged SP1 auth swap so a real login → authenticated `/api/v1` round-trip works in production, at ~$0 incremental cost, **without weakening the real-money VM's security posture**.

**Decisions carried in (from this brainstorm):**
- **Two-process architecture is correct** (already built): the real-money daemon (`bfx-shadow`) and the standalone web-API (`main:app`) are separate runtimes sharing one codebase. Never merge into one process.
- **FE → Vercel** (free Hobby; purpose-built for Next; keeps the public/auth-handling surface off the money box).
- **web-API → the OCI VM** (Will's cost choice), as an isolated sibling container.
- **Public ingress → Tailscale Funnel** (no domain, $0, no OCI port opened — preserves the VM's default-deny posture). Cloudflare Tunnel deferred until a domain exists (clean future swap).
- **DB → scoped least-privilege roles** (spec D13): the two public apps cannot touch trading tables.
- **Scope → the full SP1 deployment** (VM web-API + Vercel FE + Neon roles + Upstash + cross-wiring + smoke test).

**Non-negotiable invariant:** the live canary daemon is never shared a process/memory with the web-API, and no change here touches the daemon's container, code, or trading tables. All edits go through the existing local-source → `deploy-vm.sh` flow; **never hand-edit the VM**.

---

## 1. Topology

```
Browser ──HTTPS──▶ Vercel (Next FE + Better Auth, public, free)
                      │  FE BFF proxy mints short-lived EdDSA JWT (auth.api.getToken)
                      ▼
            Tailscale Funnel  ── public HTTPS  https://<vm>.<tailnet>.ts.net
                      ▼  (host: tailscale funnel → 127.0.0.1:8000)
   OCI VM ┌─ webapi   container  uvicorn main:app, host-loopback :8000, cpu/mem-limited   [NEW]
          ├─ bot      container  bfx-shadow real-money daemon                              [UNCHANGED]
          ├─ migrate  one-shot   alembic upgrade head (applies auth migration)            [UNCHANGED]
          └─ autoheal                                                                     [UNCHANGED]
                      │
          Neon (auth schema + public.user_profiles + trading tables)   Upstash (Better Auth session/rate-limit)
```

- **Only the Vercel FE is browser-facing.** The web-API is public *only* via Tailscale Funnel (host-loopback bind + Funnel relay) — **no OCI security-list port is opened**. The daemon stays fully internal.
- **JWKS:** the web-API verifies JWTs by fetching the FE's `/api/auth/jwks` (cross-internet HTTPS; cached by `PyJWKClient`).
- **Issuer/audience are pinned** (`bfx-funding-bot` / `bfx-funding-backend`) — no per-deploy issuer env needed.

---

## 2. VM web-API service (`docker-compose.bot.yml` addition)

Add one service; **do not modify `bot`, `migrate`, or `autoheal`.**

```yaml
  webapi:
    image: bfx-bot:local            # same image as the daemon (built by the bot service)
    container_name: bfx-webapi
    command: ["uvicorn", "bfx_funding_bot.main:app", "--host", "0.0.0.0", "--port", "8000"]
    env_file: .env.webapi.runtime   # SEPARATE env — scoped DB role, no daemon secrets
    restart: unless-stopped
    ports:
      - "127.0.0.1:8000:8000"       # host loopback ONLY (Funnel publishes it; never 0.0.0.0)
    deploy:
      resources:
        limits:
          cpus: "0.5"
          memory: 512M              # cannot starve the daemon
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

Notes: `uvicorn` is already a dependency in the image. The container binds `0.0.0.0:8000` *inside* the container; the host mapping restricts exposure to `127.0.0.1`. `depends_on: migrate` ensures the auth schema exists before the web-API starts.

---

## 3. Tailscale Funnel (host layer — mirrors the existing Grafana loopback+Tailscale pattern)

The VM already runs Tailscale (host-installed). Publish the web-API:

```bash
# one-time on the host (idempotent):
tailscale funnel --bg --https=443 http://127.0.0.1:8000
tailscale funnel status     # shows the public https://<vm>.<tailnet>.ts.net URL
```

- Output URL `https://<vm>.<tailnet>.ts.net` → this is the FE's `API_URL`.
- TLS is provisioned automatically by Tailscale. No OCI port opened.

**Human pre-flight (Will):** in the Tailscale admin console, enable **Funnel** for the tailnet/node (`nodeAttrs` → `funnel`) and ensure **MagicDNS + HTTPS certificates** are on. Funnel serves only on 443/8443/10000 (we use 443). Bandwidth limits are irrelevant at single-user ~6 req/min.

---

## 4. Neon scoped roles (runbook SQL — applied manually to Neon, **never via Alembic**)

The daemon keeps its existing owner role (migrations run as owner — they need DDL). Add two least-privilege login roles for the public apps:

```sql
-- FE / Better Auth: auth schema only.
CREATE ROLE bfx_webauth LOGIN PASSWORD '<set-a-strong-password>';
GRANT USAGE ON SCHEMA auth TO bfx_webauth;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA auth TO bfx_webauth;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA auth TO bfx_webauth;
ALTER DEFAULT PRIVILEGES IN SCHEMA auth
  GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO bfx_webauth;
ALTER DEFAULT PRIVILEGES IN SCHEMA auth
  GRANT USAGE, SELECT ON SEQUENCES TO bfx_webauth;
-- (No grants on public/trading tables → bfx_webauth cannot read or write them.)

-- web-API: read/JIT-write user_profiles only (+ future SP4 projection views).
CREATE ROLE bfx_webapi LOGIN PASSWORD '<set-a-strong-password>';
GRANT USAGE ON SCHEMA public TO bfx_webapi;
GRANT SELECT, INSERT, UPDATE ON public.user_profiles TO bfx_webapi;
-- SP4 later: GRANT SELECT on the specific projection tables/views only — NOT the trading tables.
```

Security notes:
- New Postgres roles get **no** table privileges by default, so neither role can touch `position_state`, the event log, or any trading table unless explicitly granted (we never do).
- `CONNECT` on the database is granted to `PUBLIC` by default, so both roles can connect; that's intended.
- FK enforcement when the web-API later JIT-inserts `user_profiles` (FK → `auth.user`) does **not** require the inserting role to hold privileges on `auth.user` (Postgres performs the RI check internally). Fallback if a permission error ever appears: `GRANT REFERENCES (id) ON auth."user" TO bfx_webapi;`. (Not needed for SP1 — the `/api/v1/profile` endpoint returns JWT claims and performs no DB write.)
- Connection strings: FE `DATABASE_URL` → `bfx_webauth`; web-API `DATABASE_URL` → `bfx_webapi`; both point at the same Neon DB (pooled `-pooler` host).

---

## 5. Environment split (least-privilege isolation)

### 5a. `deploy-vm.sh` change

`deploy-vm.sh` currently assembles `.env.runtime` (daemon secrets + phase config), shared by all services. Add assembly of a **separate** `.env.webapi.runtime` so the web-API never sees the daemon's full-access `DATABASE_URL` or Bitfinex keys:

```bash
# new: assemble the web-API env (phase-independent — the web-API serves the FE in every phase)
WEBAPI_SECRETS="$HOME/bfx/webapi.env"
[ -f "$WEBAPI_SECRETS" ] || { echo "ERROR: missing $WEBAPI_SECRETS (chmod 600)"; exit 1; }
cp "$WEBAPI_SECRETS" .env.webapi.runtime
chmod 600 .env.webapi.runtime
# preflight: required web-API vars present
for v in DATABASE_URL BETTER_AUTH_JWKS_URL; do
  grep -q "^$v=." .env.webapi.runtime || { echo "ERROR: web-API var $v missing/empty"; exit 1; }
done
```

`~/bfx/webapi.env` (on the VM, chmod 600) holds:

| var | value |
|---|---|
| `DATABASE_URL` | Neon pooled URL using the **`bfx_webapi`** role |
| `BETTER_AUTH_JWKS_URL` | `https://<vercel-fe-url>/api/auth/jwks` |
| `BFX_DEPLOYMENT_ENV` | `prod` — **optional/defensive**; `core/settings.py` `Settings` has no such field, set only if a shared config-guard import requires it. The implementation plan must confirm whether `main:app` reads it. |

`better_auth_issuer` / `jwt_audience` are pinned defaults — do **not** set them. `log_level` optional.

### 5b. Vercel env (set in Vercel dashboard/CLI by Will)

| var | value |
|---|---|
| `BETTER_AUTH_SECRET` | `npx @better-auth/cli@latest secret` (≥32 chars) |
| `BETTER_AUTH_URL` / `NEXT_PUBLIC_BETTER_AUTH_URL` | the Vercel deployment URL |
| `DATABASE_URL` | Neon pooled URL using the **`bfx_webauth`** role |
| `API_URL` | the Tailscale Funnel URL `https://<vm>.<tailnet>.ts.net` |
| `UPSTASH_REDIS_REST_URL` / `UPSTASH_REDIS_REST_TOKEN` | Upstash creds |
| `PASSKEY_RP_ID` | the Vercel host (domain only, no scheme) |
| `NEXT_PUBLIC_APP_URL` / `NEXT_PUBLIC_APP_NAME` / `NEXT_PUBLIC_WS_URL` | existing FE vars (WS still in the env schema until SP2 deletes the WS stack — set a placeholder) |

---

## 6. Deployment ordering (resolves the URL chicken-and-egg)

1. **Neon:** create the two scoped roles (§4).
2. **Upstash:** provision/confirm Redis REST creds; generate `BETTER_AUTH_SECRET`.
3. **VM deploy:** populate `~/bfx/webapi.env` (with a *placeholder* `BETTER_AUTH_JWKS_URL` for now), run `deploy-vm.sh <phase>`. The `migrate` step applies the auth migration to live Neon; `bot` + `webapi` start. Run `tailscale funnel` (§3) → record the web-API public URL.
4. **Vercel deploy:** deploy the Next app; set all §5b env (including `API_URL` = the Funnel URL). Record the Vercel URL.
5. **Cross-wire:** set the VM `~/bfx/webapi.env` `BETTER_AUTH_JWKS_URL` = `<vercel-url>/api/auth/jwks`; set Vercel `BETTER_AUTH_URL`/`NEXT_PUBLIC_BETTER_AUTH_URL` = the Vercel URL. Re-run `deploy-vm.sh` (restarts `webapi`) and redeploy Vercel.
6. **Smoke test (§7).**

---

## 7. Verification (smoke test the round-trip)

- **Automated:** the gated E2E (`frontend/e2e/auth.spec.ts`, `E2E_FULL_STACK=1`) pointed at the deployed Vercel URL: register → land on `/overview` → an authenticated `/api/proxy/...` GET returns 200.
- **Manual fallback:** on the Vercel FE, register a throwaway account → confirm landing on overview → in devtools confirm a `/api/proxy/...` GET returns 200 (proves login → cookie → proxy-mint → Funnel → JWKS-verify → `/api/v1` end-to-end).
- **Health:** `tailscale funnel status` shows the URL; `curl https://<vm>.<tailnet>.ts.net/health` → `{"status":"ok"}`; `docker compose -f docker-compose.bot.yml ps` shows `webapi` healthy and `bot` unchanged/healthy.

---

## 8. Safety & rollback

- **Daemon untouched:** the change adds one isolated, resource-limited compose service; the `bot` service definition, code, and process are byte-for-byte unchanged. The web-API can crash/restart (autoheal) with zero effect on trading.
- **web-API rollback:** `docker compose -f docker-compose.bot.yml stop webapi && docker compose rm -f webapi` + `tailscale funnel reset`. The daemon keeps running.
- **DB change:** the only live-Neon mutation is the **additive** auth migration (`e61f3d1ed7ca`: new `auth` schema + `public.user_profiles`; no change to trading tables). Neon PITR covers it. Migrations run as the owner role via the existing `migrate` service — the scoped roles are runtime-only.
- **Posture preserved:** no OCI security-list change; the web-API is reachable only through Tailscale Funnel.

---

## 9. Human pre-flight checklist (Will)

1. Tailscale admin: enable **Funnel** + MagicDNS/HTTPS for the VM node.
2. Neon: run the §4 role SQL (manual `psql`/SQL editor — **not** Alembic), set strong passwords.
3. Upstash: confirm Redis REST creds (reuse existing project if present).
4. Generate `BETTER_AUTH_SECRET` (`npx @better-auth/cli@latest secret`).
5. Create `~/bfx/webapi.env` on the VM (chmod 600).
6. Set Vercel env (§5b).

---

## 10. Out of scope / deferrals

- **Cloudflare Tunnel + custom domain** — adopt when going customer-facing (SP6 flips signups); clean swap (change FE `API_URL` + the tunnel only; web-API unchanged).
- **`/two-factor` page** — later SP (login-form already redirects there).
- **SP4 projection read endpoints** — when those land, extend `bfx_webapi` grants to the specific projection views only.
- **kysely `0.28.17` override** — existing tech-debt; revisit when Better Auth fixes the adapter.
- **WS stack deletion** — SP2 (D9); the FE env still carries `NEXT_PUBLIC_WS_URL` until then.
