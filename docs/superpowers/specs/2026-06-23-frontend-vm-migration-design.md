# Frontend → VM 遷移設計（棄 Neon Step 2 / 棄 Vercel）

**Date:** 2026-06-23
**Status:** Design approved, pending spec review
**Context:** 接續「棄 Neon Step 1」（bot+webapi+Postgres 已搬 VM 自托）。Step 2 把 Next.js 前端從 Vercel 搬上同一台 Oracle VM 的 docker stack，Better Auth 改連 VM 本地 Postgres，**Neon + Vercel + Upstash 三個外部依賴全部退役**（除 Bitfinex venue 外零外部 serverless）。

## Goals

1. FE 容器化跑在 VM docker stack（與 bot/webapi/postgres 同 `docker-compose.bot.yml`）。
2. Better Auth 連 VM 本地 Postgres（`auth` schema、`bfx_webauth` role），不再連 Neon。
3. 公開入口改走 Tailscale Funnel → FE，Vercel 退役。
4. 移除 Upstash（session 落 Postgres、rate-limit in-memory）。
5. 全程不影響真錢 bot。

## Non-Goals

- 自訂域名 / 反代 / CDN（用 Funnel hostname；上線期 Step 3+ 再評估）。
- WebSocket 復活（v1 後端無 `/ws`，FE WS client 為 vestigial，本次不修，見 §8）。
- 多 FE 實例 / 水平擴展（單一容器；故 rate-limit in-memory 可接受）。
- 既有 auth 帳號資料遷移（Neon 402 已失聯，強制重註冊，見 §7）。

## 現況事實（探索結論）

- FE：Next.js 16.1.6 / React 19 / pnpm / Better Auth 1.6.14 + passkey/twoFactor/jwt/admin plugins。app router，route groups `(auth)`/`(dashboard)`/`(marketing)`，`[locale]` i18n（en/zh-TW）。
- Better Auth（`frontend/src/lib/auth.ts`）：`Pool({ connectionString: DATABASE_URL, options: "-c search_path=auth" })`、`secondaryStorage` 用 Upstash Redis（session + rate-limit）、jwt plugin pin `issuer="bfx-funding-bot"` / `audience="bfx-funding-backend"`（EdDSA，與後端對齊）。
- FE→後端：`/api/proxy/[...path]/route.ts` server-side 用 `auth.api.getToken()` 鑄短期 JWT，轉發到 `API_URL/api/v1/...`；另有 Server Action 直打（api-keys 明文 secret）。
- JWKS：Better Auth 內建 `/api/auth/jwks`（FE 端，經 `/api/auth/[...all]` handler）；後端 webapi 以 `BETTER_AUTH_JWKS_URL` 抓取驗證。
- `next.config.ts`：**無** `output:'standalone'`；無 edge runtime、無 next/image、無 ISR；Vercel 耦合僅 `vercel.json` + Sentry `automaticVercelMonitors`（皆可棄）。
- **WS 在 v1 是死的**：FE 有 ws-client/ws-store（`NEXT_PUBLIC_WS_URL`），但後端 `main.py` 無任何 `/ws` route。
- VM：4 核 / 23 GB RAM / 79 GB disk free → 在 VM build Next.js 無壓力。
- Funnel 現況：public 443 → `127.0.0.1:8000`（webapi）；tailnet-only → `127.0.0.1:3000`（FE 預留埠，無服務）。DNSName `oci-a1.tail357fef.ts.net`。

## 架構決策（已取捨）

**拓樸：單一 Funnel → FE，不加反代。** WS 已死、FE 自己 server-side proxy 到 webapi、JWKS 後端走 docker 內網 → 公開面只剩 FE。Funnel 本就做 TLS 終結 + 單埠 proxy，443 從 8000 改指 3000 即可。否決加 Caddy/nginx（無第二公開面需 path-route）、否決 webapi 第二 Funnel 埠（徒增攻擊面）。

**Build 位置：在 VM build。** 23 GB RAM 綽綽有餘、不餓 bot，沿用 `deploy-vm.sh` 既有 build 流程。否決 registry/CI（多餘基礎設施）。

**目標拓樸：**
```
瀏覽器 ──HTTPS(Funnel 443)──▶ frontend:3000
                                  ├─ /api/auth/*   Better Auth ─▶ postgres:5432 (bfx_webauth, search_path=auth)
                                  └─ /api/proxy/*  ─內網─▶ webapi:8000 ─▶ postgres (bfx_webapi / bot 角色分離)
webapi  : 127.0.0.1:8000 保留（tailnet debug），不再公開
postgres: 內網限定（無 published port）
backend webapi 驗 JWT ─內網─▶ http://frontend:3000/api/auth/jwks
```

## 設計組件

### 1. FE 容器化
- `frontend/next.config.ts`：加 `output: 'standalone'`。
- 新 `frontend/Dockerfile`：多階段 —— (a) `node:22-alpine` + corepack/pnpm `install --frozen-lockfile`；(b) `pnpm build`（`NEXT_PUBLIC_*` 由 build args 注入、baked 進 bundle）；(c) runtime stage 只 copy `.next/standalone` + `.next/static` + `public`，`CMD ["node","server.js"]`，`EXPOSE 3000`。
- 新 `frontend/.dockerignore`（排除 `node_modules`、`.next`、`.vercel`、`e2e`、`.env*`）。

### 2. `docker-compose.bot.yml` 加服務
- `frontend`：`build: { context: ./frontend, args: [NEXT_PUBLIC_*] }`、`image: bfx-frontend:local`、`container_name: bfx-frontend`、`env_file: .env.frontend.runtime`、`ports: "127.0.0.1:3000:3000"`（loopback，供 Funnel 連；webapi 同模式）、`deploy.resources.limits: { cpus: "2", memory: 2G }`、`labels.autoheal: "true"`、healthcheck（runtime 為 node:22-alpine 無 python → `node -e "fetch('http://localhost:3000/').then(r=>process.exit(r.ok?0:1)).catch(()=>process.exit(1))"`，start_period 寬放）、`depends_on: { postgres: service_healthy, frontend-migrate: service_completed_successfully }`。
- `frontend-migrate`（one-shot，仿 alembic `migrate`）：跑 Better Auth migrate 在 auth schema 建 BA 表；`env_file` 用 **CREATE-capable 角色（bfx 超級使用者）** 的 DATABASE_URL（見 §3）；`restart: "no"`；`depends_on: { postgres: service_healthy, migrate: service_completed_successfully }`（alembic `migrate` 先建出 auth schema，BA 表才有地方放）。
- 依賴鏈總覽：`postgres(healthy)` → `migrate`(alembic，建 schema/含 auth) → `frontend-migrate`(BA 表) → `frontend`；`bot`/`webapi` 與 FE 鏈獨立（共用 postgres+migrate，FE 失敗不影響 bot）。

### 3. Better Auth → 本地 Postgres
- **角色**（一次性 psql runbook，補 Step 1 延後的 `bfx_webauth`）：
  ```sql
  CREATE ROLE bfx_webauth LOGIN PASSWORD '<gen>';
  GRANT USAGE ON SCHEMA auth TO bfx_webauth;
  ALTER DEFAULT PRIVILEGES IN SCHEMA auth
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO bfx_webauth;
  ALTER DEFAULT PRIVILEGES IN SCHEMA auth
    GRANT USAGE, SELECT ON SEQUENCES TO bfx_webauth;
  -- 無 public/trading 表權限 → 碰不到 position_state
  ```
- **建表順序（關鍵）**：`frontend-migrate` 以 **bfx 超級使用者**（CREATE-capable，`search_path=auth`）跑 Better Auth migrate → 在 auth schema 建 user/session/account/verification/jwks/passkey/twoFactor 表。**migrate 後**再補對既有表的 grant（ALTER DEFAULT PRIVILEGES 只蓋未來表）：
  ```sql
  GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA auth TO bfx_webauth;
  GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA auth TO bfx_webauth;
  ```
- **連線字串**：`frontend` service DATABASE_URL = `postgresql://bfx_webauth:PW@postgres:5432/bfx`（**direct、無 pooler、無 sslmode**）→ `Pool(options:"-c search_path=auth")` 在本地 pg 正常（解掉 Step 1 的 pooler 拒 startup-param 500）。
- **移除 Upstash**：`auth.ts` 刪 `secondaryStorage` + `getRedis()` + `@upstash/redis` import。session 落 Postgres session 表；Better Auth rate-limit 退回 in-memory（單一 FE 容器 OK）。`env.ts` 移除 `UPSTASH_REDIS_REST_URL/TOKEN`；`package.json` 可移除 `@upstash/redis`（非必要）。
- **user_profiles JIT**：Will 註冊時 Better Auth（bfx_webauth）建 `auth.user`；app 經 webapi（bfx_webapi，Step 1 已 grant `public.user_profiles`）JIT 建 profile，FK `user_profiles→auth.user` 由 PG 內部 RI 處理（沿用 SP1 行為；若報權限再加 `GRANT REFERENCES (id) ON auth."user" TO bfx_webapi`）。

### 4. Funnel 重指 + webapi 內網化
- `tailscale funnel` 443 目標 `127.0.0.1:8000` → `127.0.0.1:3000`（指令於 cutover 執行；非 compose）。
- webapi 保留 `ports: "127.0.0.1:8000:8000"`（tailnet/debug），但移出 public Funnel。
- FE `API_URL=http://webapi:8000`（docker 內網服務名）。

### 5. JWKS 內網
- webapi 的 `~/bfx/webapi.env`：`BETTER_AUTH_JWKS_URL=http://frontend:3000/api/auth/jwks`（docker 內網，免公開往返、Funnel 掛也能驗）。issuer/audience pinned 字串不變。

### 6. 域名 / build-time env
- 公開 URL = `https://oci-a1.tail357fef.ts.net`（Funnel）。以下 build args（baked）全綁此：
  `NEXT_PUBLIC_APP_URL`、`NEXT_PUBLIC_BETTER_AUTH_URL` = `https://oci-a1.tail357fef.ts.net`；`NEXT_PUBLIC_APP_NAME=bfx-funding-bot`；`NEXT_PUBLIC_WS_URL=wss://oci-a1.tail357fef.ts.net/ws`（見 §8）；`NEXT_PUBLIC_SENTRY_DSN` 留空（停用）。
- runtime env（`.env.frontend.runtime`）：`API_URL=http://webapi:8000`、`BETTER_AUTH_URL=https://oci-a1.tail357fef.ts.net`、`BETTER_AUTH_SECRET=<gen ≥32>`、`DATABASE_URL=<bfx_webauth direct>`、`PASSKEY_RP_ID=oci-a1.tail357fef.ts.net`。
- `deploy-vm.sh` 擴充：組 `.env.frontend.runtime`（由 `~/bfx/frontend.env`）+ 把 `NEXT_PUBLIC_*` 餵給 compose build args + preflight 檢查必填。

### 7. Vercel 退役 + auth 資料重置
- VM FE E2E 通過後：刪/停 Vercel 專案、清其 env（AUTH_SECRET / DATABASE_URL / 舊 WS_URL 等）。
- ⚠️ Neon auth 帳號資料隨 402 失聯 → 在新 FE **重新註冊**（單人 pre-launch 無痛）。bot 的 BFX key 在 env（非 vault）→ 不受影響；vault 內若有舊 key 已隨 Neon 失聯（KEK 仍在）。

### 8. WebSocket（vestigial，已知降級）
v1 後端無 `/ws`，FE WS client 連不上（Vercel 期即如此，dashboard 走 REST/polling 正常）。`NEXT_PUBLIC_WS_URL` 設 wss Funnel URL 以滿足 env 驗證 + CSP connect-src，但不會連通。**移除 FE WS 程式碼為獨立 cleanup，不在本 epic。**

## Cutover 順序

1. 本機改 code（next.config standalone、Dockerfile、.dockerignore、compose 加 frontend+frontend-migrate、auth.ts 去 Upstash、env.ts、deploy-vm.sh）→ TDD → commit + **push origin/main**（deploy-vm.sh `git pull --ff-only`）。
2. VM：建 `bfx_webauth` role（先給 schema USAGE + ALTER DEFAULT PRIVILEGES）；寫 `~/bfx/frontend.env`（含生成的 secret/role pw）。
3. 部署：deploy-vm.sh 起 `frontend-migrate`（建 auth 表）→ 補 grant ON ALL TABLES → 起 `frontend` 容器。**Funnel 仍指 8000**，先經 tailnet `http://oci-a1:3000` 內部驗證 FE+auth+proxy。
4. E2E 通過 → `tailscale funnel` 443 改指 3000。公開 URL 驗證註冊/登入/proxy/JWKS 鏈。
5. 觀察 bot 全程 byte-stable、0 error。
6. 退 Vercel（刪/停 + 清 env）。

## Rollback

- FE/auth 出錯：`tailscale funnel` 443 指回 8000 + Vercel 專案重啟（退役前都保留）→ 回 Step 1 狀態。
- bot 全程不動（frontend 是獨立容器，depends_on 不反向影響 bot；frontend build/起失敗不影響已 running 的 bot）。
- `frontend-migrate` 失敗 → frontend 不起，bot/webapi 不受影響。

## 測試 / 驗證

- **單元/型別**：`cd frontend && pnpm lint`（tsc + biome）、`pnpm test`（vitest）全綠；auth.ts 去 Upstash 後相關測試更新。後端不動（`pytest -m "not integration"` 仍綠）。
- **容器**：`docker compose config` 通過；frontend image build 成功；healthcheck 綠。
- **Auth schema**：`frontend-migrate` 後 auth schema 有 BA 表；`SET ROLE bfx_webauth; SELECT position_state` = permission denied（隔離）。
- **E2E（tailnet 先、Funnel 後）**：註冊→session cookie→`/api/proxy/*` 帶 JWT→webapi 200；JWKS 後端驗證鏈通（webapi 內網抓 frontend jwks）；登入 guard/redirect。
- **真錢**：bot reconcile byte-stable、0 submit 干擾、0 error。

## 風險

- **NEXT_PUBLIC_* baked**：域名後改 → 需 rebuild + passkey rpID 變動使既有 passkey 失效（pre-launch 無痛）。
- **in-memory rate-limit**：FE 重啟丟計數、per-process（單容器 OK；多實例需回 Redis）。
- **single-VM SPOF**：FE 與真錢 bot 同機；resource limits（cpus 2 / mem 2G）防 SSR 餓 bot；23 GB RAM 餘裕大。
- **Better Auth migrate 角色**：須用 CREATE-capable 角色跑、migrate 後補 grant（順序錯則 bfx_webauth 無新表權限）。
