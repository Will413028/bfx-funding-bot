# Frontend → VM 遷移 Implementation Plan（棄 Neon Step 2 / 棄 Vercel）

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 Next.js 前端從 Vercel 搬上 Oracle VM 的 docker stack，Better Auth 改連 VM 本地 Postgres + VM 自托 Redis，退役 Neon + Vercel；Upstash 改為 VM 自托 Redis。

**Architecture:** 單一 Tailscale Funnel(443) → FE 容器(3000)；FE server-side proxy 到內網 `webapi:8000`；Better Auth 連 `postgres:5432`（`bfx_webauth`、`auth` schema、direct 無 pooler）+ `redis:6379`（secondaryStorage：session + rate-limit）。

**Tech Stack:** Next.js 16.1.6 / React 19 / pnpm / Better Auth 1.6.14 / ioredis / Postgres 18 / Redis 7 / Docker Compose / Tailscale Funnel。

## 與初版 spec 的偏差（planning 期發現，已驗證）

1. **不需 `frontend-migrate` / better-auth CLI**：alembic `e61f3d1ed7ca` 已建 6 個 BA 表於 `auth` schema，Step 1 `alembic upgrade head` 已在 VM 建好（已查證）。
2. **不需 `auth.session` 表**：採「自托 Redis」（非「丟 Upstash」），session 仍存 Redis（secondaryStorage），不進 DB。

## Global Constraints

- 套 migration 經 compose `migrate` 服務自動跑（本 epic 無新 migration → migrate no-op）；**不可用 MCP 直接 SQL**。
- `pnpm` 指令在 `frontend/`。
- Better Auth JWT issuer=`bfx-funding-bot`、audience=`bfx-funding-backend`（pinned，不可改）。
- FE→DB：`postgresql://bfx_webauth:PW@postgres:5432/bfx`（**direct、無 -pooler、無 sslmode**）。
- FE→Redis：`redis://redis:6379`（內網 docker 服務名）。
- 公開域名 = `https://oci-a1.tail357fef.ts.net`；`NEXT_PUBLIC_*` build-time baked 全綁此。
- 真錢 bot 全程 byte-stable、0 干擾；FE/redis 為獨立容器，build/起失敗不得影響 running 的 bot。
- commit emoji prefix；結尾 `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`；push 用 gh 帳號 `Will413028`。

---

### Task 1: Better Auth 換 Upstash → ioredis（指 VM Redis）

**Files:**
- Modify: `frontend/src/lib/auth.ts`
- Modify: `frontend/src/lib/env.ts:16-17`
- Modify: `frontend/package.json`（remove `@upstash/redis`，add `ioredis`）
- Modify: `frontend/e2e/README.md`（Upstash env → REDIS_URL）

**Interfaces:**
- Produces: `auth` 模組以 `ioredis` 連 `process.env.REDIS_URL` 當 secondaryStorage（session + rate-limit）。

- [ ] **Step 1: 換相依套件**

```bash
cd /Users/will/second-brain/projects/startup/bfx-funding-bot/frontend
pnpm remove @upstash/redis
pnpm add -E ioredis
```
Expected: `package.json` 移除 `@upstash/redis`、加入 `ioredis`（exact 版本）；`pnpm-lock.yaml` 更新。

- [ ] **Step 2: 改 `frontend/src/lib/auth.ts` 第 2 行 import**

把 `import { Redis } from "@upstash/redis";` 改為：
```typescript
import Redis from "ioredis";
```

- [ ] **Step 3: 替換 lines 8-36（cache 型別 + getPool + getRedis）**

```typescript
// Lazy, globalThis-cached singletons (pg Pool + Redis).
//
// On serverless/HMR the auth module is re-evaluated repeatedly; caching the
// clients on `globalThis` avoids leaking a new pool/connection per re-eval.
// Both are constructed lazily (inside the getters) so importing this module is
// side-effect-free and `next build` never connects anywhere.
const globalForAuth = globalThis as unknown as {
  _bfxAuthPool?: Pool;
  _bfxAuthRedis?: Redis;
};

function getPool(): Pool {
  // VM-local Postgres, dedicated `auth` schema. Direct (no -pooler) so the
  // `-c search_path=auth` startup option is honored.
  globalForAuth._bfxAuthPool ??= new Pool({
    connectionString: process.env.DATABASE_URL,
    options: "-c search_path=auth",
  });
  return globalForAuth._bfxAuthPool;
}

function getRedis(): Redis {
  // VM-local Redis (docker service `redis`), standard protocol via ioredis.
  // biome-ignore lint/style/noNonNullAssertion: required server env, validated in lib/env.ts.
  globalForAuth._bfxAuthRedis ??= new Redis(process.env.REDIS_URL!);
  return globalForAuth._bfxAuthRedis;
}
```

- [ ] **Step 4: 替換 secondaryStorage block（lines 46-59）為 ioredis API**

```typescript
  // Sessions + rate-limit counters live in VM-local Redis (ioredis), not Postgres.
  secondaryStorage: {
    get: async (key) => {
      return await getRedis().get(key); // ioredis returns string | null
    },
    set: async (key, value, ttl) => {
      if (ttl) await getRedis().set(key, value, "EX", ttl);
      else await getRedis().set(key, value);
    },
    delete: async (key) => {
      await getRedis().del(key);
    },
  },
```
（rateLimit block 維持原樣 `storage: "secondary-storage"`，只更新其上方註解第 73 行為 `// Route rate-limit counters to Redis (secondaryStorage alone does NOT do this).`）

- [ ] **Step 5: 改 `frontend/src/lib/env.ts` lines 16-17**

把：
```typescript
  UPSTASH_REDIS_REST_URL: z.string().url(),
  UPSTASH_REDIS_REST_TOKEN: z.string().min(1),
```
改為：
```typescript
  REDIS_URL: z.string().min(1),
```

- [ ] **Step 6: 更新 `frontend/e2e/README.md`**

把 Upstash 區塊（`# Upstash (sessions + rate-limit counters)` + 兩行 `UPSTASH_REDIS_REST_*`）改為：
```
# Redis (sessions + rate-limit counters)
REDIS_URL=redis://localhost:6379
```

- [ ] **Step 7: typecheck + lint + 測試全綠**

```bash
cd /Users/will/second-brain/projects/startup/bfx-funding-bot/frontend
pnpm lint && pnpm test
```
Expected: tsc 0 error、biome clean、vitest 108 tests pass。

- [ ] **Step 8: Commit**

```bash
cd /Users/will/second-brain/projects/startup/bfx-funding-bot
git add frontend/src/lib/auth.ts frontend/src/lib/env.ts frontend/package.json frontend/pnpm-lock.yaml frontend/e2e/README.md
git commit -m "$(printf '♻️ Refactor: Better Auth secondaryStorage Upstash -> ioredis (VM Redis)\n\nSwap @upstash/redis (REST) for ioredis pointed at REDIS_URL (VM-local redis\ncontainer). secondaryStorage behavior unchanged (sessions + rate-limit in\nRedis), so no DB session table needed. env.ts UPSTASH_* -> REDIS_URL.\n\nCo-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>')"
```

---

### Task 2: Next standalone output + Dockerfile + .dockerignore

**Files:**
- Modify: `frontend/next.config.ts:6-8`
- Create: `frontend/Dockerfile`
- Create: `frontend/.dockerignore`

**Interfaces:**
- Produces: 可 build 的 `bfx-frontend:local` image，listen 3000，`server.js` standalone。

- [ ] **Step 1: `frontend/next.config.ts` 加 `output`**

把 lines 6-8 替換為：
```typescript
const nextConfig: NextConfig = {
  reactCompiler: true,
  output: "standalone",
};
```

- [ ] **Step 2: 建 `frontend/.dockerignore`**

```
node_modules
.next
.vercel
.git
e2e
playwright-report
test-results
*.log
.env*
```

- [ ] **Step 3: 建 `frontend/Dockerfile`**

```dockerfile
# syntax=docker/dockerfile:1
FROM node:22-alpine AS base
RUN corepack enable
WORKDIR /app

FROM base AS deps
COPY package.json pnpm-lock.yaml ./
RUN pnpm install --frozen-lockfile

FROM base AS builder
COPY --from=deps /app/node_modules ./node_modules
COPY . .
# NEXT_PUBLIC_* are baked into the client bundle at build time.
ARG NEXT_PUBLIC_APP_URL
ARG NEXT_PUBLIC_APP_NAME
ARG NEXT_PUBLIC_WS_URL
ARG NEXT_PUBLIC_BETTER_AUTH_URL
ENV NEXT_PUBLIC_APP_URL=$NEXT_PUBLIC_APP_URL \
    NEXT_PUBLIC_APP_NAME=$NEXT_PUBLIC_APP_NAME \
    NEXT_PUBLIC_WS_URL=$NEXT_PUBLIC_WS_URL \
    NEXT_PUBLIC_BETTER_AUTH_URL=$NEXT_PUBLIC_BETTER_AUTH_URL \
    NEXT_TELEMETRY_DISABLED=1
# env.ts validates server env at import (process.exit(1) on miss). Throwaway
# placeholders so `next build` passes — overridden at runtime by env_file.
ENV API_URL=http://build.invalid \
    BETTER_AUTH_SECRET=build_time_placeholder_secret_min_32_chars_xx \
    BETTER_AUTH_URL=http://build.invalid \
    DATABASE_URL=postgresql://build:build@build.invalid:5432/build \
    REDIS_URL=redis://build.invalid:6379 \
    PASSKEY_RP_ID=build.invalid
RUN pnpm build

FROM base AS runner
ENV NODE_ENV=production NEXT_TELEMETRY_DISABLED=1 PORT=3000 HOSTNAME=0.0.0.0
RUN addgroup -S nodejs && adduser -S nextjs -G nodejs
COPY --from=builder /app/public ./public
COPY --from=builder --chown=nextjs:nodejs /app/.next/standalone ./
COPY --from=builder --chown=nextjs:nodejs /app/.next/static ./.next/static
USER nextjs
EXPOSE 3000
CMD ["node", "server.js"]
```

- [ ] **Step 4: 本機 build image**

```bash
cd /Users/will/second-brain/projects/startup/bfx-funding-bot/frontend
docker build \
  --build-arg NEXT_PUBLIC_APP_URL=https://oci-a1.tail357fef.ts.net \
  --build-arg NEXT_PUBLIC_APP_NAME=bfx-funding-bot \
  --build-arg NEXT_PUBLIC_WS_URL=wss://oci-a1.tail357fef.ts.net/ws \
  --build-arg NEXT_PUBLIC_BETTER_AUTH_URL=https://oci-a1.tail357fef.ts.net \
  -t bfx-frontend:verify .
```
Expected: build 成功。

- [ ] **Step 5: 起容器 + curl（不連 DB/Redis 也要能起）**

```bash
docker rm -f bfx-fe-verify >/dev/null 2>&1
docker run -d --name bfx-fe-verify -p 3100:3000 \
  -e API_URL=http://api.invalid -e BETTER_AUTH_SECRET=verify_placeholder_secret_min_32_chars_xx \
  -e BETTER_AUTH_URL=http://localhost:3100 -e DATABASE_URL=postgresql://x:x@db.invalid:5432/x \
  -e REDIS_URL=redis://r.invalid:6379 -e PASSKEY_RP_ID=localhost bfx-frontend:verify
sleep 4
curl -fsS -o /dev/null -w "%{http_code}\n" -L http://localhost:3100/ || true
docker logs bfx-fe-verify 2>&1 | tail -5
docker rm -f bfx-fe-verify; docker rmi bfx-frontend:verify
```
Expected: HTTP 200（`/` → i18n redirect → 200）；logs 顯示 `Ready`。

- [ ] **Step 6: Commit**

```bash
cd /Users/will/second-brain/projects/startup/bfx-funding-bot
git add frontend/next.config.ts frontend/Dockerfile frontend/.dockerignore
git commit -m "$(printf '🔧 Chore: frontend standalone Docker build (output:standalone + Dockerfile)\n\nMulti-stage pnpm build -> Next standalone runner (node:22-alpine). NEXT_PUBLIC_*\nbaked via build-args; throwaway server-env placeholders (incl REDIS_URL) satisfy\nenv.ts build-time validation.\n\nCo-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>')"
```

---

### Task 3: docker-compose 加 `redis` + `frontend` 服務

**Files:**
- Modify: `docker-compose.bot.yml`（加 `redis` 服務、`frontend` 服務、頂層 volume `bfx_redisdata`）

**Interfaces:**
- Produces: `redis`（內網 6379）、`frontend`（`127.0.0.1:3000`）服務。

- [ ] **Step 1: 加 `redis` 服務（在 `postgres` 區塊之後）**

```yaml
  redis:
    image: redis:7-alpine
    container_name: bfx-redis
    restart: unless-stopped
    command: ["redis-server", "--appendonly", "yes"]
    volumes:
      - bfx_redisdata:/data
    healthcheck:
      test: ["CMD", "redis-cli", "ping"]
      interval: 10s
      timeout: 5s
      retries: 5
      start_period: 10s
    labels:
      autoheal: "true"
    # no ports: internal-only (Better Auth secondaryStorage on the compose network)
```

- [ ] **Step 2: 加 `frontend` 服務（在 `webapi` 區塊之後、頂層 `volumes:` 之前）**

```yaml
  frontend:
    build:
      context: frontend
      dockerfile: Dockerfile
      args:
        NEXT_PUBLIC_APP_URL: ${NEXT_PUBLIC_APP_URL:-https://oci-a1.tail357fef.ts.net}
        NEXT_PUBLIC_APP_NAME: ${NEXT_PUBLIC_APP_NAME:-bfx-funding-bot}
        NEXT_PUBLIC_WS_URL: ${NEXT_PUBLIC_WS_URL:-wss://oci-a1.tail357fef.ts.net/ws}
        NEXT_PUBLIC_BETTER_AUTH_URL: ${NEXT_PUBLIC_BETTER_AUTH_URL:-https://oci-a1.tail357fef.ts.net}
    image: bfx-frontend:local
    container_name: bfx-frontend
    env_file: .env.frontend.runtime
    restart: unless-stopped
    ports:
      - "127.0.0.1:3000:3000"
    deploy:
      resources:
        limits:
          cpus: "2"
          memory: 2G
    labels:
      autoheal: "true"
    healthcheck:
      test: ["CMD", "node", "-e", "fetch('http://localhost:3000/').then(r=>process.exit(r.ok?0:1)).catch(()=>process.exit(1))"]
      interval: 30s
      timeout: 5s
      retries: 3
      start_period: 40s
    depends_on:
      postgres:
        condition: service_healthy
      redis:
        condition: service_healthy
      migrate:
        condition: service_completed_successfully
```

- [ ] **Step 3: 頂層 `volumes:` 加 `bfx_redisdata`**

把現有：
```yaml
volumes:
  bfx_pgdata:
```
改為：
```yaml
volumes:
  bfx_pgdata:
  bfx_redisdata:
```

- [ ] **Step 4: 驗 compose config**

```bash
cd /Users/will/second-brain/projects/startup/bfx-funding-bot
printf 'DATABASE_URL=postgresql+asyncpg://bfx:x@postgres:5432/bfx\nPOSTGRES_USER=bfx\nPOSTGRES_PASSWORD=x\nPOSTGRES_DB=bfx\n' > .env.runtime
cp .env.runtime .env.webapi.runtime; cp .env.runtime .env.frontend.runtime
GIT_SHA=test docker compose -f docker-compose.bot.yml config >/dev/null && echo VALID || echo INVALID
rm -f .env.runtime .env.webapi.runtime .env.frontend.runtime
```
Expected: `VALID`（services 含 redis、frontend；volumes 含 bfx_redisdata）。

- [ ] **Step 5: Commit**

```bash
git add docker-compose.bot.yml
git commit -m "$(printf '🔧 Chore: add redis + frontend services to compose (FE on VM)\n\nredis:7-alpine (appendonly, named volume, internal-only) backs Better Auth\nsecondaryStorage. frontend Next standalone on 127.0.0.1:3000, NEXT_PUBLIC_*\nvia build-args, cpus2/mem2G cap, depends_on postgres+redis+migrate.\n\nCo-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>')"
```

---

### Task 4: deploy-vm.sh 擴充 + frontend.env 範本

**Files:**
- Modify: `scripts/deploy-vm.sh`
- Create: `deploy/vm/frontend.env.example`

- [ ] **Step 1: 建 `deploy/vm/frontend.env.example`**

```bash
# ~/bfx/frontend.env (VM, chmod 600) — Better Auth FE runtime (server-only).
# Build-time NEXT_PUBLIC_* live here too; deploy-vm.sh exports them as build-args.
NEXT_PUBLIC_APP_URL=https://oci-a1.tail357fef.ts.net
NEXT_PUBLIC_APP_NAME=bfx-funding-bot
NEXT_PUBLIC_WS_URL=wss://oci-a1.tail357fef.ts.net/ws
NEXT_PUBLIC_BETTER_AUTH_URL=https://oci-a1.tail357fef.ts.net
API_URL=http://webapi:8000
BETTER_AUTH_URL=https://oci-a1.tail357fef.ts.net
BETTER_AUTH_SECRET=<openssl rand -base64 48>
DATABASE_URL=postgresql://bfx_webauth:<pw>@postgres:5432/bfx
REDIS_URL=redis://redis:6379
PASSKEY_RP_ID=oci-a1.tail357fef.ts.net
```

- [ ] **Step 2: 在 `scripts/deploy-vm.sh:36` 之後插入 frontend env 組裝**

```bash
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
```

- [ ] **Step 3: 語法檢查**

```bash
cd /Users/will/second-brain/projects/startup/bfx-funding-bot
bash -n scripts/deploy-vm.sh && echo "syntax OK"
```
Expected: `syntax OK`。

- [ ] **Step 4: Commit + push 全部 code task**

```bash
git add scripts/deploy-vm.sh deploy/vm/frontend.env.example
git commit -m "$(printf '🔧 Chore: deploy-vm.sh assembles frontend runtime env + bakes NEXT_PUBLIC_*\n\nReads ~/bfx/frontend.env -> .env.frontend.runtime (chmod 600), preflights\nrequired vars (incl REDIS_URL), exports NEXT_PUBLIC_* for compose build-args.\n\nCo-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>')"
gh auth switch --user Will413028
git push origin main
git log --oneline -6
```
Expected: Task 1-4 commits 在 origin/main。

---

### Task 5: VM cutover（操作型 runbook，互動執行）

> 非 code commit。主 loop SSH 互動執行、每步驗證。密碼 VM 端生成不外傳。本 epic 無新 migration → migrate no-op。

**5a. 建 `bfx_webauth` role + grant 既有 6 個 auth 表**

- [ ] 生 pw + 建 role + grants（auth 表已全部存在，直接 ON ALL TABLES）：
```bash
ssh -i ~/.ssh/id_ed25519_will413028 ubuntu@<vm> 'WPW=$(openssl rand -hex 24); echo "$WPW" > /tmp/webauth_pw
docker exec -i bfx-postgres psql -U bfx -d bfx -v ON_ERROR_STOP=1 <<SQL
DO $$ BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname='"'"'bfx_webauth'"'"') THEN
    CREATE ROLE bfx_webauth LOGIN PASSWORD '"'"'$WPW'"'"';
  ELSE ALTER ROLE bfx_webauth LOGIN PASSWORD '"'"'$WPW'"'"'; END IF;
END $$;
GRANT USAGE ON SCHEMA auth TO bfx_webauth;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA auth TO bfx_webauth;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA auth TO bfx_webauth;
ALTER DEFAULT PRIVILEGES IN SCHEMA auth GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO bfx_webauth;
ALTER DEFAULT PRIVILEGES IN SCHEMA auth GRANT USAGE, SELECT ON SEQUENCES TO bfx_webauth;
SQL'
```
- [ ] 隔離驗證：`SET ROLE bfx_webauth; SELECT count(*) FROM auth."user";`（OK）；`SELECT * FROM position_state;`（permission denied）。

**5b. 寫 `~/bfx/frontend.env`**
```bash
ssh ... 'cd ~/bfx; WPW=$(cat /tmp/webauth_pw); SECRET=$(openssl rand -base64 48 | tr -d "\n")
cat > frontend.env <<EOF
NEXT_PUBLIC_APP_URL=https://oci-a1.tail357fef.ts.net
NEXT_PUBLIC_APP_NAME=bfx-funding-bot
NEXT_PUBLIC_WS_URL=wss://oci-a1.tail357fef.ts.net/ws
NEXT_PUBLIC_BETTER_AUTH_URL=https://oci-a1.tail357fef.ts.net
API_URL=http://webapi:8000
BETTER_AUTH_URL=https://oci-a1.tail357fef.ts.net
BETTER_AUTH_SECRET=$SECRET
DATABASE_URL=postgresql://bfx_webauth:$WPW@postgres:5432/bfx
REDIS_URL=redis://redis:6379
PASSKEY_RP_ID=oci-a1.tail357fef.ts.net
EOF
chmod 600 frontend.env; rm -f /tmp/webauth_pw'
```

**5c. 部署（起 redis + build/起 frontend；migrate no-op）**
- [ ] `cd ~/bfx-funding-bot && BFX_CANARY_CONFIRM=yes ./scripts/deploy-vm.sh canary`
- [ ] ⚠️ 觀察 bot reconcile byte-stable、0 error；redis healthy；frontend healthy。

**5d. webapi JWKS 改內網**
- [ ] 改 `~/bfx/webapi.env`：`BETTER_AUTH_JWKS_URL=http://frontend:3000/api/auth/jwks`；重跑 deploy。

**5e. 內網驗證（Funnel 仍指 8000，經 tailnet）**
- [ ] 經 `http://oci-a1:3000`：頁面載入；`/api/auth/*` 註冊一帳號 → 確認 session 進 Redis：`docker exec bfx-redis redis-cli keys '*'`（應見 session key）；登入後 `/api/proxy/*` 帶 JWT 打 webapi → 200；webapi log 顯示內網抓 jwks + JWT 驗證通過。

**5f. 切 Funnel 443 → 3000**
- [ ] `tailscale funnel --bg 3000`；`tailscale funnel status` 確認 public 指 3000。
- [ ] 公開 `https://oci-a1.tail357fef.ts.net` 驗證註冊/登入/dashboard/proxy 全鏈。

**5g. 觀察 + 退 Vercel**
- [ ] 觀察 bot 全程 byte-stable、0 干擾。
- [ ] 退 Vercel：dashboard 停用/刪 + 清 env。

**Rollback:** `tailscale funnel --bg 8000` + Vercel 重啟（退役前保留）。bot 不動；FE/redis 起不來不影響 running bot/webapi。

---

## Self-Review

**Spec coverage：** §1 FE 容器化→Task 2 ✓；§2 compose redis+frontend→Task 3 ✓；§3 Better Auth→本地 PG（Task 5a role/grant）+ ioredis 換接（Task 1）✓；§4 Funnel/webapi 內網→Task 5f/5d ✓；§5 JWKS 內網→Task 5d ✓；§6 域名/build env→Task 2/3/4 ✓；§7 Vercel 退役+重註冊→Task 5g ✓；§8 WS vestigial→NEXT_PUBLIC_WS_URL（Task 2/4）✓。

**Placeholder scan：** code 步驟皆完整；runbook 的 `<pw>`/`<vm>` 為部署期生成值。

**Type/naming consistency：** ioredis `Redis` default import 一致；`REDIS_URL` 跨 env.ts/Dockerfile/compose/frontend.env/deploy preflight 一致；`bfx_webauth`、`redis://redis:6379`、`bfx_redisdata` 名稱一致；無新 alembic migration（head 維持 `028cf1a1554e`）。

**已知風險：** NEXT_PUBLIC_* baked（域名後改需 rebuild + passkey 失效）；Redis 為新容器（session/rate-limit 載體、非真錢、免備份、autoheal 顧、內網無密碼）；single-VM SPOF（resource limits 緩解）。
