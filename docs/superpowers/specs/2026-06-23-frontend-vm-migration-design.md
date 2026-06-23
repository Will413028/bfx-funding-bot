# Frontend → VM 遷移設計（棄 Neon Step 2 / 棄 Vercel）

**Date:** 2026-06-23
**Status:** Design approved (rev: self-host Redis)，pending plan execution
**Context:** 接續「棄 Neon Step 1」（bot+webapi+Postgres 已搬 VM 自托）。Step 2 把 Next.js 前端從 Vercel 搬上同一台 Oracle VM 的 docker stack，Better Auth 改連 VM 本地 Postgres + VM 自托 Redis，**Neon + Vercel 退役、Upstash 改為 VM 自托 Redis**（除 Bitfinex venue 外零外部 serverless）。

## Goals

1. FE 容器化跑在 VM docker stack（與 bot/webapi/postgres 同 `docker-compose.bot.yml`）。
2. Better Auth 連 VM 本地 Postgres（`auth` schema、`bfx_webauth` role），不再連 Neon。
3. 公開入口改走 Tailscale Funnel → FE，Vercel 退役。
4. **自托 Redis 取代 Upstash**：Better Auth `secondaryStorage`（session + rate-limit）改指 VM Redis 容器；client 由 `@upstash/redis`（REST）換成 `ioredis`（標準 Redis 協定）。
5. 全程不影響真錢 bot。

## Non-Goals

- 自訂域名 / 反代 / CDN（用 Funnel hostname；上線期 Step 3+ 再評估）。
- WebSocket 復活（v1 後端無 `/ws`，FE WS client 為 vestigial，本次不修，見 §8）。
- 多 FE 實例 / 水平擴展（單一容器即可；Redis-backed rate-limit 已可跨實例，但非本次目標）。
- 既有 auth 帳號資料遷移（Neon 402 已失聯，強制重註冊，見 §7）。

## 現況事實（探索 + 查證結論）

- FE：Next.js 16.1.6 / React 19 / pnpm / Better Auth 1.6.14 + passkey/twoFactor/jwt/admin plugins。app router、route groups `(auth)`/`(dashboard)`/`(marketing)`、`[locale]` i18n（en/zh-TW）。
- Better Auth（`frontend/src/lib/auth.ts`）：`Pool({ connectionString: DATABASE_URL, options: "-c search_path=auth" })`（user/account/verification/jwks/twoFactor/passkey 存 Postgres）；`secondaryStorage` 用 **Upstash Redis**（session + rate-limit）；jwt plugin pin `issuer="bfx-funding-bot"` / `audience="bfx-funding-backend"`（EdDSA，與後端對齊）。
- **alembic 已管 BA schema**：`e61f3d1ed7ca` 已建 6 個 BA 表（user/account/verification/jwks/twoFactor/passkey）於 `auth` schema，Step 1 `alembic upgrade head` 已在 VM 建好（已查證）。**無 `session` 表**——session 一向存 Upstash。→ 保留 secondaryStorage（改指 VM Redis）後 session 仍在 Redis，**不需 DB session 表、不需 better-auth CLI/frontend-migrate**。
- FE→後端：`/api/proxy/[...path]/route.ts` server-side 用 `auth.api.getToken()` 鑄短期 JWT，轉發到 `API_URL/api/v1/...`；另有 Server Action 直打。
- JWKS：Better Auth 內建 `/api/auth/jwks`（FE 端）；後端 webapi 以 `BETTER_AUTH_JWKS_URL` 抓取驗證。
- `next.config.ts`：**無** `output:'standalone'`；無 edge runtime/next-image/ISR；Vercel 耦合僅 `vercel.json` + Sentry `automaticVercelMonitors`（皆可棄）。
- WS 在 v1 是死的（後端 `main.py` 無 `/ws`）；FE WS client 為 vestigial。
- VM：4 核 / 23 GB RAM / 79 GB disk free → 在 VM build Next.js 無壓力。
- Funnel：public 443 → `127.0.0.1:8000`（webapi）；tailnet-only → `127.0.0.1:3000`（FE 預留埠）。DNSName `oci-a1.tail357fef.ts.net`。

## 架構決策（已取捨）

**拓樸：單一 Funnel → FE，不加反代。** WS 已死、FE 自己 server-side proxy 到 webapi、JWKS 後端走 docker 內網 → 公開面只剩 FE。Funnel 443 從 8000 改指 3000 即可。否決 Caddy/nginx 反代、否決 webapi 第二 Funnel 埠。

**Build 位置：在 VM build。** 23 GB RAM 綽綽有餘、不餓 bot，沿用 `deploy-vm.sh` build 流程。否決 registry/CI。

**Redis：VM 自托容器（取代 Upstash），client 換 ioredis。** Upstash SDK 只說 REST，無法指本地 Redis → 換標準 `ioredis`。secondaryStorage 行為不變（session + rate-limit 仍在 Redis），故不需 DB session 表。否決「丟 Upstash 改 in-memory」（會需新增 session 表 + rate-limit 退化）；否決 SRH(serverless-redis-http) 代理（多餘容器）。

**目標拓樸：**
```
瀏覽器 ──HTTPS(Funnel 443)──▶ frontend:3000
                                  ├─ /api/auth/*   Better Auth ─┬─▶ postgres:5432 (bfx_webauth, search_path=auth: user/account/jwks/…)
                                  │                              └─▶ redis:6379    (secondaryStorage: session + rate-limit)
                                  └─ /api/proxy/*  ─內網─▶ webapi:8000 ─▶ postgres (bfx_webapi / bot 角色分離)
webapi  : 127.0.0.1:8000 保留（tailnet debug），不再公開
postgres / redis : 內網限定（無 published port）
backend webapi 驗 JWT ─內網─▶ http://frontend:3000/api/auth/jwks
```

## 設計組件

### 1. FE 容器化
- `frontend/next.config.ts`：加 `output: 'standalone'`。
- 新 `frontend/Dockerfile`：多階段 node:22-alpine + pnpm → `.next/standalone` runtime；`NEXT_PUBLIC_*` 由 build args baked；build 期給 server-env 佔位值（含 `REDIS_URL`）以過 `env.ts` import 驗證。
- 新 `frontend/.dockerignore`。

### 2. `docker-compose.bot.yml` 加服務
- `redis`：`redis:7-alpine`、`command: redis-server --appendonly yes`、named volume `bfx_redisdata`、無 published port（內網限定）、healthcheck `redis-cli ping`、`restart: unless-stopped`、autoheal label。
- `frontend`：`build:{context:frontend, args:[NEXT_PUBLIC_*]}`、`image: bfx-frontend:local`、`env_file: .env.frontend.runtime`、`ports: "127.0.0.1:3000:3000"`（供 Funnel）、`deploy.resources.limits {cpus:2, memory:2G}`、autoheal、healthcheck（node fetch）、`depends_on: {postgres: healthy, redis: healthy, migrate: completed}`。
- **無 frontend-migrate**（BA 表已由 alembic 管理）。

### 3. Better Auth → 本地 Postgres + VM Redis
- **角色**（一次性 psql runbook，補 Step 1 延後的 `bfx_webauth`）：`CREATE ROLE bfx_webauth LOGIN`；`GRANT USAGE ON SCHEMA auth`；`GRANT SELECT,INSERT,UPDATE,DELETE ON ALL TABLES IN SCHEMA auth`（6 個既有表）；`GRANT USAGE,SELECT ON ALL SEQUENCES IN SCHEMA auth`；`ALTER DEFAULT PRIVILEGES IN SCHEMA auth ...`（未來表）。無 public/trading 表權限 → 碰不到 position_state。
- **連線字串**：`frontend` DATABASE_URL = `postgresql://bfx_webauth:PW@postgres:5432/bfx`（**direct、無 pooler、無 sslmode** → `-c search_path=auth` 正常，解掉 Step 1 pooler 500）。
- **Redis（換 client，不丟）**：`auth.ts` 的 `@upstash/redis` import → `ioredis`；`getRedis()` 由 `Redis.fromEnv()` → `new Redis(process.env.REDIS_URL!)`；secondaryStorage 三個 closure 改用 ioredis API（`get`、`set(key,value,"EX",ttl)`、`del`）；`rateLimit.storage` 維持 `"secondary-storage"`。`env.ts` 的 `UPSTASH_REDIS_REST_URL/TOKEN` → `REDIS_URL`。`package.json`：移除 `@upstash/redis`、加 `ioredis`。
- **不需 session 表 / 不需 better-auth migrate**（session 留 Redis；其餘 BA 表 alembic 已建）。
- **user_profiles JIT**：沿用 SP1（bfx_webapi 經 webapi 建 public.user_profiles，FK→auth.user 由 PG RI 處理）。

### 4. Funnel 重指 + webapi 內網化
- `tailscale funnel` 443 目標 8000 → 3000（cutover 執行；非 compose）。webapi 保留 `127.0.0.1:8000`（tailnet/debug）不公開。FE `API_URL=http://webapi:8000`（內網）。

### 5. JWKS 內網
- webapi `~/bfx/webapi.env`：`BETTER_AUTH_JWKS_URL=http://frontend:3000/api/auth/jwks`（docker 內網）。issuer/audience pinned，不變。

### 6. 域名 / build-time env
- 公開 URL = `https://oci-a1.tail357fef.ts.net`。build args（baked）：`NEXT_PUBLIC_APP_URL`、`NEXT_PUBLIC_BETTER_AUTH_URL` = 該 URL；`NEXT_PUBLIC_APP_NAME=bfx-funding-bot`；`NEXT_PUBLIC_WS_URL=wss://…/ws`（見 §8）；`NEXT_PUBLIC_SENTRY_DSN` 留空。
- runtime env（`.env.frontend.runtime`）：`API_URL=http://webapi:8000`、`BETTER_AUTH_URL=https://…`、`BETTER_AUTH_SECRET=<gen ≥32>`、`DATABASE_URL=<bfx_webauth direct>`、`REDIS_URL=redis://redis:6379`、`PASSKEY_RP_ID=oci-a1.tail357fef.ts.net`。
- `deploy-vm.sh` 擴充：組 `.env.frontend.runtime`（由 `~/bfx/frontend.env`）、export `NEXT_PUBLIC_*` 給 compose build args、preflight。

### 7. Vercel 退役 + auth 資料重置
- VM FE E2E 通過後：刪/停 Vercel 專案、清 env。
- ⚠️ Neon auth 帳號資料隨 402 失聯 → 新 FE **重新註冊**（單人 pre-launch 無痛）。bot BFX key 在 env（非 vault）不受影響。

### 8. WebSocket（vestigial，已知降級）
v1 後端無 `/ws`，FE WS client 連不上（Vercel 期即如此，dashboard 走 REST/polling）。`NEXT_PUBLIC_WS_URL` 設 wss Funnel URL 以滿足 env 驗證 + CSP，但不連通。FE WS 程式碼移除為獨立 cleanup，不在本 epic。

## Cutover 順序

1. 本機改 code（next standalone、Dockerfile、.dockerignore、compose 加 redis+frontend、auth.ts/env.ts/package.json 換 ioredis、deploy-vm.sh）→ TDD/驗證 → commit + **push origin/main**。
2. VM：建 `bfx_webauth` role + grants（既有 6 auth 表）；寫 `~/bfx/frontend.env`（secret/role pw/REDIS_URL）。
3. 部署：deploy-vm.sh 起 redis + build/起 frontend（migrate no-op，無新 migration）。**Funnel 仍指 8000**，先經 tailnet `http://oci-a1:3000` 驗 FE+auth(註冊→Redis session)+proxy。
4. webapi `BETTER_AUTH_JWKS_URL` 改內網重部署。
5. E2E 通過 → `tailscale funnel` 443 改指 3000；公開 URL 驗證全鏈。
6. 觀察 bot byte-stable → 退 Vercel。

## Rollback
- FE/auth 出錯：`tailscale funnel` 443 指回 8000 + Vercel 重啟（退役前都保留）。
- bot 全程不動（frontend/redis 為獨立容器，depends_on 不反向影響 bot；FE build/起失敗不影響 running 的 bot）。

## 測試 / 驗證
- **單元/型別**：`cd frontend && pnpm lint`（tsc+biome）+ `pnpm test`（vitest）全綠；ioredis 換接後相關測試（若有）更新。後端不動。
- **容器**：`docker compose config` 通過；frontend image build 成功；redis/frontend healthcheck 綠。
- **隔離**：`SET ROLE bfx_webauth; SELECT position_state` = permission denied。
- **E2E（tailnet 先、Funnel 後）**：註冊→session 存 Redis→`/api/proxy/*` 帶 JWT→webapi 200；JWKS 後端內網驗證鏈通；登入 guard/redirect。
- **真錢**：bot reconcile byte-stable、0 submit 干擾、0 error。

## 風險
- **NEXT_PUBLIC_* baked**：域名後改 → rebuild + passkey rpID 變動使既有 passkey 失效（pre-launch 無痛）。
- **Redis 新容器**：session/rate-limit 載體；非真錢、可由重登重建 → 免備份；appendonly+volume 避免重啟登出；autoheal 顧存活。內網無密碼（docker 網路為信任邊界；requirepass 為可選硬化）。
- **single-VM SPOF**：FE/redis 與真錢 bot 同機；resource limits 防 SSR 餓 bot；23 GB RAM 餘裕大。
