# Frontend → VM 遷移 Implementation Plan（棄 Neon Step 2 / 棄 Vercel）

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 Next.js 前端從 Vercel 搬上 Oracle VM 的 docker stack，Better Auth 改連 VM 本地 Postgres，退役 Neon + Vercel + Upstash。

**Architecture:** 單一 Tailscale Funnel(443) → FE 容器(3000)；FE server-side proxy 到內網 `webapi:8000`；Better Auth 連 `postgres:5432`（`bfx_webauth` role、`auth` schema、direct 無 pooler）。session 改存 Postgres（丟 Upstash）。

**Tech Stack:** Next.js 16.1.6 / React 19 / pnpm / Better Auth 1.6.14 / Postgres 18 / Docker Compose / Tailscale Funnel。

## 與 spec 的偏差（planning 期發現，已驗證）

1. **不需要 `frontend-migrate` / better-auth CLI**：alembic `e61f3d1ed7ca` 已把 6 個 BA 表（user/account/verification/jwks/twoFactor/passkey）建進 `auth` schema，Step 1 的 `alembic upgrade head` 早已在 VM 建好（已查證）。spec §2/§3 的 frontend-migrate 服務取消。
2. **新增需求：`auth.session` 表**。原本 session 存 Upstash（secondaryStorage），DB 無 session 表。丟 Upstash 後 BA 改用 DB session 表 → 用 alembic 補建（Task 1）。BA 表由 alembic 管理（非 BA CLI），與既有慣例一致。

## Global Constraints

- 套 migration 一律 `cd backend_py && uv run alembic upgrade head`（部署經 compose `migrate` 服務自動跑）；**不可用 MCP 直接 SQL**。
- `pytest` 在 `backend_py/`；`pnpm` 指令在 `frontend/`。
- Better Auth JWT issuer=`bfx-funding-bot`、audience=`bfx-funding-backend`（pinned 字串，不可改）。
- FE→DB 連線：`postgresql://bfx_webauth:PW@postgres:5432/bfx`（**direct、無 -pooler、無 sslmode**；`-c search_path=auth` 才正常）。
- 公開域名 = `https://oci-a1.tail357fef.ts.net`（Funnel）；`NEXT_PUBLIC_*` build-time baked 全綁此。
- 真錢 bot 全程 byte-stable、0 干擾；FE 是獨立容器，build/起失敗不得影響 running 的 bot。
- commit emoji prefix（✨/🐛/🔧/📝/✅…）；commit 結尾 `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`。
- push 用 gh 帳號 `Will413028`。

---

### Task 1: alembic migration — 加 `auth.session` 表

**Files:**
- Create: `backend_py/alembic/versions/c3d4e5f6a7b8_add_auth_session_table.py`
- Verify against: throwaway local Postgres 18

**Interfaces:**
- Consumes: 既有 head `028cf1a1554e`（Step 1 schema-drift fix），`auth.user` 表（e61f3d1ed7ca）。
- Produces: 新 alembic head `c3d4e5f6a7b8`；`auth.session` 表供 Better Auth（去 Upstash 後）存 session。

- [ ] **Step 1: 寫 migration 檔**

```python
"""add auth.session table (Better Auth DB sessions after dropping Upstash)

Revision ID: c3d4e5f6a7b8
Revises: 028cf1a1554e
Create Date: 2026-06-23

Better Auth 1.6.14 core session table. Originally sessions lived in Upstash
(secondaryStorage); dropping Upstash moves them to the DB, which requires this
table. Schema mirrors the BA core session model + admin plugin's
`impersonatedBy`. Style matches e61f3d1ed7ca (auth schema, quoted camelCase,
TEXT ids, tz timestamps).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c3d4e5f6a7b8"
down_revision: str | None = "028cf1a1554e"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "session",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("token", sa.Text(), nullable=False),
        sa.Column("expiresAt", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "userId",
            sa.Text(),
            sa.ForeignKey("auth.user.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("ipAddress", sa.Text(), nullable=True),
        sa.Column("userAgent", sa.Text(), nullable=True),
        sa.Column(
            "createdAt",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column(
            "updatedAt",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        # admin plugin: the admin impersonating this session (nullable).
        sa.Column("impersonatedBy", sa.Text(), nullable=True),
        sa.UniqueConstraint("token", name="session_token_key"),
        schema="auth",
    )
    op.create_index(
        "session_userId_idx", "session", ["userId"], unique=False, schema="auth"
    )


def downgrade() -> None:
    op.drop_index("session_userId_idx", table_name="session", schema="auth")
    op.drop_table("session", schema="auth")
```

- [ ] **Step 2: 本機 throwaway pg 套全鏈驗證**

```bash
cd /Users/will/second-brain/projects/startup/bfx-funding-bot/backend_py
docker rm -f bfx-pg-verify >/dev/null 2>&1
docker run -d --name bfx-pg-verify -e POSTGRES_USER=bfx -e POSTGRES_PASSWORD=x -e POSTGRES_DB=bfx -p 55432:5432 postgres:18-alpine
for i in $(seq 1 20); do docker exec bfx-pg-verify pg_isready -U bfx -d bfx >/dev/null 2>&1 && break; sleep 1; done
export DATABASE_URL="postgresql+asyncpg://bfx:x@localhost:55432/bfx"
uv run alembic upgrade head
```
Expected: 跑到 `028cf1a1554e -> c3d4e5f6a7b8` 無錯。

- [ ] **Step 3: 確認 session 表 + alembic head 單一**

```bash
docker exec bfx-pg-verify psql -U bfx -d bfx -tAc "select tablename from pg_tables where schemaname='auth' order by 1;"
uv run alembic heads
```
Expected: auth 含 `session`；`uv run alembic heads` 顯示單一 `c3d4e5f6a7b8 (head)`。清理：`docker rm -f bfx-pg-verify`。

- [ ] **Step 4: Commit**

```bash
cd /Users/will/second-brain/projects/startup/bfx-funding-bot
git add backend_py/alembic/versions/c3d4e5f6a7b8_add_auth_session_table.py
git commit -m "$(printf '✨ Feat: add auth.session table (Better Auth DB sessions, post-Upstash)\n\nDropping Upstash secondaryStorage moves Better Auth sessions from Redis to\nthe DB; the auth schema had no session table (sessions lived in Upstash).\nMirrors BA 1.6.14 session model + admin plugin impersonatedBy.\n\nCo-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>')"
```

---

### Task 2: 移除 Better Auth 的 Upstash（session→Postgres、rate-limit→memory）

**Files:**
- Modify: `frontend/src/lib/auth.ts`
- Modify: `frontend/src/lib/env.ts:16-17`
- Modify: `frontend/e2e/README.md`（移除 Upstash env 區塊）

**Interfaces:**
- Consumes: `auth.session` 表（Task 1）。
- Produces: `auth` 模組不再依賴 Upstash；session 落 Postgres；rate-limit in-memory。

- [ ] **Step 1: 改 `frontend/src/lib/auth.ts` — 移除 Redis import**

把第 2 行 `import { Redis } from "@upstash/redis";` 刪除。

- [ ] **Step 2: 改 globalThis cache 與移除 getRedis()**

把 lines 8-36 整段（註解 + `globalForAuth` 型別 + `getPool` + `getRedis`）替換為：

```typescript
// Lazy, globalThis-cached pg Pool singleton.
//
// On serverless/HMR the auth module is re-evaluated repeatedly; caching the pg
// Pool on `globalThis` avoids leaking a new connection pool on every re-eval.
const globalForAuth = globalThis as unknown as {
  _bfxAuthPool?: Pool;
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
```

- [ ] **Step 3: 移除 secondaryStorage block 與改 rateLimit storage**

把 lines 46-59（`// Sessions + rate-limit ...` 註解 + 整個 `secondaryStorage: { ... },`）刪除。
把 rateLimit block（lines 73-83）替換為：

```typescript
  // Sessions now live in the Postgres `auth.session` table (no secondaryStorage).
  // Rate-limit counters are in-memory (single long-running FE container).
  rateLimit: {
    enabled: true,
    storage: "memory",
    customRules: {
      "/sign-in/email": { window: 60, max: 5 },
      "/sign-up/email": { window: 60, max: 5 },
      "/two-factor/*": { window: 60, max: 5 },
      "/forget-password": { window: 60, max: 3 },
    },
  },
```

- [ ] **Step 4: 改 `frontend/src/lib/env.ts` — 移除 Upstash vars**

刪除 lines 16-17：
```typescript
  UPSTASH_REDIS_REST_URL: z.string().url(),
  UPSTASH_REDIS_REST_TOKEN: z.string().min(1),
```

- [ ] **Step 5: 更新 `frontend/e2e/README.md`**

移除 Upstash env 範例區塊（`# Upstash (sessions + rate-limit counters)` + 兩行 `UPSTASH_REDIS_REST_*`）。

- [ ] **Step 6: typecheck + lint + 既有測試全綠**

```bash
cd /Users/will/second-brain/projects/startup/bfx-funding-bot/frontend
pnpm lint && pnpm test
```
Expected: tsc 0 error、biome clean、vitest 108 tests pass。若有 test 直接 import `@upstash/redis` 或 `secondaryStorage` 則更新（已查：無單元測試引用，僅 e2e 註解）。

- [ ] **Step 7: Commit**

```bash
cd /Users/will/second-brain/projects/startup/bfx-funding-bot
git add frontend/src/lib/auth.ts frontend/src/lib/env.ts frontend/e2e/README.md
git commit -m "$(printf '♻️ Refactor: drop Upstash from Better Auth (sessions->Postgres, rate-limit->memory)\n\nRemove @upstash/redis secondaryStorage; BA sessions now persist in the\nauth.session table. rateLimit storage -> in-memory (single FE container).\nDirect DB URL (no -pooler) honors -c search_path=auth.\n\nCo-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>')"
```

---

### Task 3: Next standalone output + Dockerfile + .dockerignore

**Files:**
- Modify: `frontend/next.config.ts:6-8`
- Create: `frontend/Dockerfile`
- Create: `frontend/.dockerignore`

**Interfaces:**
- Produces: 可 build 的 `bfx-frontend:local` image，listen 3000，`server.js` standalone。

- [ ] **Step 1: `frontend/next.config.ts` 加 `output: 'standalone'`**

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
# env.ts validates server env at import (process.exit(1) on miss). Provide
# throwaway placeholders so `next build` passes — overridden at runtime by env_file.
ENV API_URL=http://build.invalid \
    BETTER_AUTH_SECRET=build_time_placeholder_secret_min_32_chars_xx \
    BETTER_AUTH_URL=http://build.invalid \
    DATABASE_URL=postgresql://build:build@build.invalid:5432/build \
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

- [ ] **Step 4: 本機 build image（驗 Dockerfile + standalone 正確）**

```bash
cd /Users/will/second-brain/projects/startup/bfx-funding-bot/frontend
docker build \
  --build-arg NEXT_PUBLIC_APP_URL=https://oci-a1.tail357fef.ts.net \
  --build-arg NEXT_PUBLIC_APP_NAME=bfx-funding-bot \
  --build-arg NEXT_PUBLIC_WS_URL=wss://oci-a1.tail357fef.ts.net/ws \
  --build-arg NEXT_PUBLIC_BETTER_AUTH_URL=https://oci-a1.tail357fef.ts.net \
  -t bfx-frontend:verify .
```
Expected: build 成功，最後產出 runner image。

- [ ] **Step 5: 起容器 + curl 驗證（不連 DB 也要能起並回應）**

```bash
docker rm -f bfx-fe-verify >/dev/null 2>&1
docker run -d --name bfx-fe-verify -p 3100:3000 \
  -e API_URL=http://api.invalid -e BETTER_AUTH_SECRET=verify_placeholder_secret_min_32_chars_xx \
  -e BETTER_AUTH_URL=http://localhost:3100 -e DATABASE_URL=postgresql://x:x@db.invalid:5432/x \
  -e PASSKEY_RP_ID=localhost bfx-frontend:verify
sleep 4
curl -fsS -o /dev/null -w "%{http_code}\n" -L http://localhost:3100/ || true
docker logs bfx-fe-verify 2>&1 | tail -5
docker rm -f bfx-fe-verify; docker rmi bfx-frontend:verify
```
Expected: HTTP 200（`/` → 經 i18n redirect 到 `/en` → 200）；logs 顯示 `Ready`。

- [ ] **Step 6: Commit**

```bash
cd /Users/will/second-brain/projects/startup/bfx-funding-bot
git add frontend/next.config.ts frontend/Dockerfile frontend/.dockerignore
git commit -m "$(printf '🔧 Chore: frontend standalone Docker build (output:standalone + Dockerfile)\n\nMulti-stage pnpm build -> Next standalone runner (node:22-alpine). NEXT_PUBLIC_*\nbaked via build-args; throwaway server-env placeholders satisfy env.ts build-time\nvalidation (overridden at runtime).\n\nCo-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>')"
```

---

### Task 4: docker-compose 加 `frontend` 服務

**Files:**
- Modify: `docker-compose.bot.yml`（在 `webapi` 後、`volumes:` 前加 `frontend` 服務）

**Interfaces:**
- Consumes: `bfx-frontend` build context、`.env.frontend.runtime`（Task 5 由 deploy 組）、`postgres`/`migrate` 服務。
- Produces: `frontend` 服務 listen `127.0.0.1:3000`（供 Funnel）。

- [ ] **Step 1: 加 `frontend` 服務（在 `webapi` 區塊之後、頂層 `volumes:` 之前）**

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
      # node:22-alpine ships no python/curl/wget — use node global fetch.
      test: ["CMD", "node", "-e", "fetch('http://localhost:3000/').then(r=>process.exit(r.ok?0:1)).catch(()=>process.exit(1))"]
      interval: 30s
      timeout: 5s
      retries: 3
      start_period: 40s
    depends_on:
      postgres:
        condition: service_healthy
      migrate:
        condition: service_completed_successfully
```

- [ ] **Step 2: 驗 compose config（用臨時 env）**

```bash
cd /Users/will/second-brain/projects/startup/bfx-funding-bot
printf 'DATABASE_URL=postgresql+asyncpg://bfx:x@postgres:5432/bfx\nPOSTGRES_USER=bfx\nPOSTGRES_PASSWORD=x\nPOSTGRES_DB=bfx\n' > .env.runtime
cp .env.runtime .env.webapi.runtime
cp .env.runtime .env.frontend.runtime
GIT_SHA=test docker compose -f docker-compose.bot.yml config >/dev/null && echo VALID || echo INVALID
rm -f .env.runtime .env.webapi.runtime .env.frontend.runtime
```
Expected: `VALID`。

- [ ] **Step 3: Commit**

```bash
git add docker-compose.bot.yml
git commit -m "$(printf '🔧 Chore: add frontend service to compose (FE on VM)\n\nNext standalone container on 127.0.0.1:3000 (for Funnel), build-args bake\nNEXT_PUBLIC_*, env_file .env.frontend.runtime, cpus 2/mem 2G cap so SSR cannot\nstarve the real-money bot, depends_on postgres+migrate.\n\nCo-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>')"
```

---

### Task 5: deploy-vm.sh 擴充 + frontend.env 範本

**Files:**
- Modify: `scripts/deploy-vm.sh`（加 frontend env 組裝 + build-arg 匯出 + preflight）
- Create: `deploy/vm/frontend.env.example`

**Interfaces:**
- Consumes: VM 上 `~/bfx/frontend.env`（Task 6 runbook 建立）。
- Produces: `.env.frontend.runtime`、export `NEXT_PUBLIC_*` 給 compose build。

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
PASSKEY_RP_ID=oci-a1.tail357fef.ts.net
```

- [ ] **Step 2: 在 deploy-vm.sh 加 frontend env 組裝（webapi 區塊之後、real-money gate 之前）**

在 `scripts/deploy-vm.sh:36` 之後插入：
```bash
# --- frontend env (Better Auth FE: scoped bfx_webauth role, server-only) ---
FRONTEND_SECRETS="$HOME/bfx/frontend.env"
[ -f "$FRONTEND_SECRETS" ] || { echo "ERROR: missing $FRONTEND_SECRETS (chmod 600)"; exit 1; }
cp "$FRONTEND_SECRETS" .env.frontend.runtime
chmod 600 .env.frontend.runtime
for v in NEXT_PUBLIC_APP_URL NEXT_PUBLIC_BETTER_AUTH_URL API_URL BETTER_AUTH_SECRET BETTER_AUTH_URL DATABASE_URL PASSKEY_RP_ID; do
  grep -q "^$v=." .env.frontend.runtime || { echo "ERROR: frontend var $v missing/empty in $FRONTEND_SECRETS"; exit 1; }
done
# Export NEXT_PUBLIC_* so compose build-args bake the correct public URLs.
set -a; . ./.env.frontend.runtime; set +a
```

- [ ] **Step 3: 確認 build 那行會帶到 build-args**

`docker compose build` 會讀 compose `build.args` 的 `${NEXT_PUBLIC_*}`（已由 Step 2 export）。確認 `scripts/deploy-vm.sh:46` 的 build 行不需改（compose 自動代入環境變數）。在該行前確認 export 生效（無額外修改即可）。

- [ ] **Step 4: 語法檢查**

```bash
cd /Users/will/second-brain/projects/startup/bfx-funding-bot
bash -n scripts/deploy-vm.sh && echo "syntax OK"
```
Expected: `syntax OK`。

- [ ] **Step 5: Commit**

```bash
git add scripts/deploy-vm.sh deploy/vm/frontend.env.example
git commit -m "$(printf '🔧 Chore: deploy-vm.sh assembles frontend runtime env + bakes NEXT_PUBLIC_*\n\nReads ~/bfx/frontend.env -> .env.frontend.runtime (chmod 600), preflights\nrequired vars, exports NEXT_PUBLIC_* so compose build-args bake the public URLs.\nAdds deploy/vm/frontend.env.example template.\n\nCo-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>')"
```

- [ ] **Step 6: push 全部 code task 到 origin/main**

```bash
gh auth switch --user Will413028
git push origin main
git log --oneline -6
```
Expected: Task 1-5 commits 全在 origin/main。

---

### Task 6: VM cutover（操作型 runbook，互動執行）

> 非 code commit。由主 loop SSH 互動執行，每步驗證。所有密碼在 VM 端生成、不外傳。

**6a. 建 `bfx_webauth` role + grant 既有 auth 表（含新 session）**

- [ ] 生密碼 + 建 role + grants（migration 套用「後」才 grant ON ALL TABLES，因新 session 表由本次 deploy 的 migrate 建）。先建 role + schema USAGE + ALTER DEFAULT PRIVILEGES：
```bash
ssh ... ubuntu@<vm> 'WPW=$(openssl rand -hex 24); cd ~/bfx
# 寫進 frontend.env（DATABASE_URL 用此 pw）— 見 6c
docker exec -i bfx-postgres psql -U bfx -d bfx -v ON_ERROR_STOP=1 <<SQL
DO $$ BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname='"'"'bfx_webauth'"'"') THEN
    CREATE ROLE bfx_webauth LOGIN PASSWORD '"'"'$WPW'"'"';
  ELSE ALTER ROLE bfx_webauth LOGIN PASSWORD '"'"'$WPW'"'"'; END IF;
END $$;
GRANT USAGE ON SCHEMA auth TO bfx_webauth;
ALTER DEFAULT PRIVILEGES IN SCHEMA auth GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO bfx_webauth;
ALTER DEFAULT PRIVILEGES IN SCHEMA auth GRANT USAGE, SELECT ON SEQUENCES TO bfx_webauth;
SQL
echo "$WPW" > /tmp/webauth_pw'  # 暫存供 6c 寫 frontend.env，寫完即刪
```
Expected: role 建立、grants OK。

**6b. 部署（migrate 套 session 表 + build/起 frontend）— 先不動 Funnel**

- [ ] 寫 `~/bfx/frontend.env`（用 6a 的 pw；BETTER_AUTH_SECRET=openssl rand）：
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
PASSKEY_RP_ID=oci-a1.tail357fef.ts.net
EOF
chmod 600 frontend.env; rm -f /tmp/webauth_pw'
```
- [ ] 部署：`cd ~/bfx-funding-bot && BFX_CANARY_CONFIRM=yes ./scripts/deploy-vm.sh canary`
  - migrate 套 `c3d4e5f6a7b8`（建 `auth.session`）→ build frontend image（NEXT_PUBLIC_* baked）→ 起 `frontend`。bot/webapi recreate（env 不變則 byte-stable；postgres 因 .env.runtime 不變不 recreate）。
  - ⚠️ 觀察 bot reconcile byte-stable、0 error。
- [ ] migration 後補 grant ON ALL TABLES（session 現已存在）：
```bash
ssh ... 'docker exec -i bfx-postgres psql -U bfx -d bfx -v ON_ERROR_STOP=1 <<SQL
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA auth TO bfx_webauth;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA auth TO bfx_webauth;
SQL'
```
- [ ] 隔離驗證：`SET ROLE bfx_webauth; SELECT count(*) FROM auth.session;` 應 OK；`SELECT * FROM position_state;` 應 permission denied。

**6c. webapi JWKS 改內網**

- [ ] 改 `~/bfx/webapi.env` 的 `BETTER_AUTH_JWKS_URL=http://frontend:3000/api/auth/jwks`，重跑 deploy（或 `docker compose up -d webapi` 後確認 .env.webapi.runtime 已更新 → 用 deploy 較穩）。

**6d. 內網驗證（Funnel 仍指 8000，先經 tailnet）**

- [ ] FE 容器 healthy；經 tailnet `http://oci-a1:3000` 驗證：頁面載入、`/api/auth/*` 註冊一個帳號 → session cookie；登入後 `/api/proxy/*` 帶 JWT 打 webapi → 200；webapi log 顯示 JWKS 抓取 `http://frontend:3000/api/auth/jwks` 成功、JWT 驗證通過。

**6e. 切 Funnel 443 → 3000**

- [ ] `tailscale funnel --bg 3000`（或 `tailscale serve` 重設 443→127.0.0.1:3000）；確認 `tailscale funnel status` public 指 3000。
- [ ] 公開 `https://oci-a1.tail357fef.ts.net` 驗證：註冊/登入/dashboard/proxy 全鏈。

**6f. 觀察 + 退 Vercel**

- [ ] 觀察 bot 全程 byte-stable、0 error、0 干擾。
- [ ] 退 Vercel：`vercel project rm` 或 dashboard 停用 + 清 env（保留至此確認 VM FE 穩定後）。

**Rollback:** `tailscale funnel --bg 8000`（指回 webapi）+ Vercel 重啟（退役前都留著）。bot 不動。frontend build/起失敗 → 不影響 running 的 bot/webapi。

---

## Self-Review

**Spec coverage：**
- §1 FE 容器化 → Task 3 ✓
- §2 compose frontend → Task 4 ✓（frontend-migrate 取消，見偏差 1）
- §3 Better Auth→本地 + 去 Upstash → Task 2（去 Upstash）+ Task 6a（role/grant）+ Task 1（session 表，偏差 2）✓
- §4 Funnel 重指 + webapi 內網 → Task 6e + 6c ✓
- §5 JWKS 內網 → Task 6c ✓
- §6 域名/build-time env → Task 3/4/5（NEXT_PUBLIC_* baked）✓
- §7 Vercel 退役 + auth 重置 → Task 6f（重註冊：fresh auth schema，Will 重建帳號）✓
- §8 WS vestigial → NEXT_PUBLIC_WS_URL 設 wss Funnel（Task 3/5），不連通、已知降級 ✓

**Placeholder scan：** migration/auth.ts/Dockerfile/compose 皆完整程式碼；runbook 的 `<pw>`/`<vm>` 為部署期生成值（非 TODO）。

**Type/naming consistency：** alembic head 鏈 `028cf1a1554e → c3d4e5f6a7b8`；auth 表名/欄位 quoted camelCase 與 e61f3d1ed7ca 一致；`bfx_webauth` role 名跨 Task 6 一致；`auth.session` FK→`auth.user.id` 與既有 account/twoFactor/passkey 一致。

**已知風險：** NEXT_PUBLIC_* baked（域名後改需 rebuild + passkey 失效，pre-launch 無痛）；in-memory rate-limit per-process；single-VM SPOF（resource limits 緩解）。
