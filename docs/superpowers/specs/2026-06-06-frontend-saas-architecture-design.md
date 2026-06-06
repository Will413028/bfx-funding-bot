# 前端 SaaS 架構重思 — Design Spec

- **日期**：2026-06-06
- **類型**：程式級架構 spec（multi-sub-project program 的總綱）
- **狀態**：DRAFT，待 Will review
- **作者**：brainstorming（subagent-driven 探索 + best-practice 研究）
- **下一步**：通過後 → 第一個子專案「Auth swap」走 writing-plans

---

## 0. TL;DR

使用者原問「規劃前端開發、需要哪些功能」。Forensic 調查後發現：**前端早已是 feature-complete 的 SaaS client，缺的是後端。** 真正的工作是把 Python 重寫時砍掉的整個 HTTP/SaaS 層長回來，並趁 pre-launch 把前端架構現代化到 2026 best practice。

v1 定位從「客戶儀表板」轉為 **operator console**（你監控 live canary + G3 驗證），客戶頁面凍結在 flag 後保留，身份層多租戶就緒但 signups OFF。Auth 採 **自托 Better Auth**（OSS，跑在 Next app 的 `/api/auth/*`，表在你自己的 Postgres）——零平台鎖定、DB-agnostic。

---

## 1. 背景與起點（forensic 發現）

### 1.1 前端：成品

`frontend/` 是 Next.js 16（App Router）+ React 19 + TanStack Query v5 + Zustand + next-intl（en/zh-TW）+ shadcn/ui + Tailwind 4，部署 Vercel。8 頁、18 feature component、91 TS 檔、完整手刻 JWT auth（cookie-proxy）、Sentry/Axiom。架構成熟、無 mock（mock 只在 unit test）。

### 1.2 後端：只有 3 個 route

daemon 只服務 `GET /health`、`GET /healthz`、`POST /admin/smoke-test`。前端對 `${API_URL}/api/v1/*` 發出的 **20 個 endpoint，後端實作 0 個**。連 `POST /api/v1/auth/login` 都 404 → 現在連登入都過不去。

### 1.3 根因

完整 27-endpoint SaaS HTTP API 本來存在於 **Go 後端（2026-05-29 刪除，commit `b9c1f37`）**。Python 重寫刻意砍掉整個 HTTP/多租戶層，變成單帳號 event-sourced daemon。前端從未被重新指向——仍等 Go 時代契約。Go-era 設計藍本 `backend_architecture.md`（git `1992b99`）與被刪的 Go handler 已從 git 復原，作為重建的字面參考。

### 1.4 關鍵利多：資料層已多租戶就緒

現有 daemon 每張表都已用 `(account_id, deployment_environment)` 複合鍵；`users`/`api_keys`/`user_configs`/`executions`/`billing_records` 五張表已存在 schema（dormant，migration 建了沒人用 HTTP 讀），欄位與 FE DTO 幾乎 1:1。

---

## 2. 已鎖定的決策

| # | 決策 | 選擇 | 理由摘要 |
|---|------|------|---------|
| D1 | v1 後端範圍 | **SP1–SP5**（auth/身份、BFX-key vault、config CRUD、投影/讀端點、plan-gating+限流） | 多租戶真錢執行 SP6 是 10 種真錢災難所在，且被驗證 gate 擋住 |
| D2 | 多租戶真錢執行（SP6） | **延後**至 G3 profitability gate 過關（~2026-08-04） | 安全 + 對齊自身 roadmap |
| D3 | Billing | **出 v1，只留 stub**（`users.plan` 欄 + stub endpoint） | 驗證過再收錢；v1 僅 plan-gating-by-cap |
| D4 | BFX API-key vault | **KMS envelope 加密** | trust value-prop「我们拿你的 key 但無法提款」；有 rotation |
| D5 | Venue | **Bitfinex-only** | funding 市場 per-venue 流動性/策略需各自驗證 |
| D6 | 前端重思深度 | **審查 + 現代化**（保留 Next 16 底座，非 greenfield） | FE 已現代，greenfield 多半只重造現有；YAGNI |
| D7 | v1 產品定位 | **operator console 為主**；客戶頁凍結在 flag 後保留；多租戶身份就緒但 **signups OFF**（Will=user #1） | 現有 FE 答錯問題（客戶 APY），v1 需答 operator 問題；翻 flag 即客戶 SaaS |
| D8 | Auth 架構 | **自托 Better Auth**（OSS，跑在 Next app，表在你自己的 Postgres） | 2026 業界默認；零平台鎖定/DB-agnostic（對齊你最在意的 portability）；Python JWKS 驗證 + SQL join `user_id`，無 user-sync；非「rolling your own」。Neon Auth（同引擎）為未來想卸 ops 時的零遷移 managed 替代 |
| D9 | 即時性 | **Polling**（`refetchInterval`）+ 刪掉死 WS stack；SSE 留後面、WS 留 SP6 | 單用戶 ~6 req/min、零額外後端工作 |
| D10 | 策略設定面 | **唯讀 cells/standing-quote**（無客戶 knob 表單） | 引擎不能 hot-load，「儲存策略」表單會是謊言 |
| D11 | 暗色主題 | **always-dark**（移除 inert `dark:` 機制） | 機構級終端不需 light mode（要時再加 next-themes） |
| D12 | 組織模型 | **flat users now**，`org_id` 留 SP6 | users 在你 DB，永不被 provider tenant model 卡住 |
| D13 | DB 拓樸 | **同一個 Neon DB + 專屬 `auth` schema + web app scoped DB role**；**Alembic 唯一 migration 工具**（port `better-auth generate` 出的 schema，永不跑 `better-auth migrate`）；SP4 同 DB 讀 trading 表 | 同 DB=業界默認、FK 完整、SP4 免跨 DB；schema+scoped role=真錢安全邊界（web app 動不了 trading 表）；單一 Alembic=與現有 live-Neon 手刻 migration 工作流一致。caveat：共用 2-CU/PITR，v1 可忽略、日後可升獨立 project（可逆）|
| D14 | MFA | TOTP enroll（+passkey 選配）；**step-up 只在動錢操作**（改 key/升 cap/關 kill-switch） | Better Auth 無專屬 step-up API → 用 session-freshness 強制 re-verify；唯讀儀表板不擋 |

---

## 3. 目標架構

一條 spine：**managed 身份在 Neon → FastAPI 用 JWKS 驗證 + 用 `user_id` join → role-routed 單一 shell → RSC 抓 owned data + polling 抓 live data（全 scope/tag by user）→ operator 視圖渲染 event-sourced DB 投影 → tokenized primitive 呈現。** 每層都多租戶形狀但 signups-off，上線 = 翻 flag 而非重寫。

### 3.1 Auth（load-bearing）

**自托 Better Auth（OSS，跑在 Next app 的 `/api/auth/*`，session 表在你自己的 Postgres）；保留 BFF cookie/proxy 殼，只換 token 來源。**

決策依據（2026 web research + context7/官方文件驗證，2026-06-06）：

- **業界默認**：多份 2026 獨立評比一致將 Better Auth 列為新建 self-host Next.js SaaS 的首選。Auth.js/NextAuth 2025-09-26 進維護模式（自家團隊都導向 Better Auth）、Lucia 2025-03 棄用、Clerk 是「衝到 ~50K MAU 就遷移」的付費 DX。
- **滿足兩個硬約束**：(A) 獨立 Python 後端用 JWKS 無狀態驗證（Better Auth JWT plugin 暴露 `/api/auth/jwks`，EdDSA 預設、可 RS256/ES256；docs 明示「token 可在你自己的 service 驗證、不需額外 verify call 或 DB 查詢」）；(B) user 表在**你自己的 Postgres**，app 表普通 SQL FK/join，**無 webhook 同步**。
- **零平台鎖定 / DB-agnostic**：離開 Neon 換任何 Postgres = 改 connection string + `pg_dump`。對齊你最在意的 portability。
- **Python 驗證**（service-to-service）：
  ```python
  # pip install pyjwt[crypto]  (PyJWKClient + jose)
  jwks = PyJWKClient("https://<next-app>/api/auth/jwks")
  signing_key = jwks.get_signing_key_from_jwt(token).key
  payload = jwt.decode(token, signing_key, algorithms=["EdDSA","RS256"],
                       issuer=ISS, audience=AUD)  # 驗 iss/aud/exp，refetch on unknown kid
  ```
- **JWT plugin 用途**：跨 service token，**不取代 session model**——瀏覽器/BFF 用 Better Auth session，FastAPI 用 JWT/JWKS。
- **MFA**：TOTP + passkey 內建（比 Clerk 領先，Clerk MFA 要付費）；對敏感操作（改 key、升 cap、關 kill-switch）加 step-up。

**保留（已是 best practice，IETF OAuth-for-Browser-Based-Apps BCP 評 BFF 為最安全、明確推薦給真錢/個資 app）**：BFF proxy 殼（path-traversal guard、query passthrough、body-once-for-retry、browser 永不見 token）、CSP/HSTS、middleware route 保護（改用 Better Auth `getSession()`/`getSessionCookie()`，取代手刻 base64 decode `exp`）。
**刪 / 換（~100 行手刻）**：`(auth)/actions.ts` login/register/logout → `authClient.signIn/signUp/signOut`（打 `/api/auth/*`）；手刻 `auth_token`/`refresh_token` cookie → Better Auth 自己的 session cookie；`route.ts` `tryRefresh()`/dedup + 401→refresh→retry → 移除（Better Auth 自管 session lifetime，401-retry 可留薄薄「re-mint JWT 一次」守門）。**丟掉 `AUTH_SECRET`**（middleware 從未驗簽、本就 dead）。**`BFX_ADMIN_TOKEN` 維持獨立**（admin/smoke 用 static，user/dashboard 走 JWKS）。

**推薦資料流**：① 登入 → `authClient.signIn` 打 Next app 的 `/api/auth/*`，Better Auth 對 Postgres 驗證、設自己的 httpOnly Secure SameSite session cookie（瀏覽器只持有不透明 cookie，永不見 JWT）→ ② `middleware.ts` 檢查 session → ③ browser → `/api/proxy/<path>` `credentials:'include'`、無 Authorization header → ④ **proxy server 端** mint 短命 JWT（`authClient.token()`）→ ⑤ proxy → FastAPI `Authorization: Bearer <jwt>` → ⑥ FastAPI dependency 用 cached JWKS 驗 → ⑦ `WHERE user_id=sub` 直接 join（user 表在同一 Postgres）。

**新增檔**：`lib/auth.ts`（betterAuth + `jwt()` plugin）、`/api/auth/[...all]/route.ts`、FastAPI JWKS-verify dependency。

**替代方案（為何不選）**：
- **Neon Auth**（managed，同引擎 Better Auth 1.4.18，表在 `neon_auth` schema）：想卸掉 auth ops 時的零遷移替代——可隨時 `pg_dump` + 切自托或反向。**不選為 v1 起點**因：仍 Beta，且把 login 建在 Beta managed 上對真錢產品不如直接用它底層的 GA OSS lib（你還掌握 patch 時機）。
- **Clerk/WorkOS/Stytch/Kinde**：user 在平台外 → webhook 同步影子表（single-writer 真錢系統最不想要的 eventually-consistent pipeline）。WorkOS 列為未來 B2B/SOC2/SSO 的 reserve。
- **Keycloak/Zitadel/Ory/SuperTokens/Logto**：自帶 schema、跨界 join + 重 ops，不適合 solo 真錢 v1。
- Auth.js v5（維護模式）、Supabase Auth（拖進第二個 Postgres）：不採。

**自托責任（誠實，且輕）**：訂 Dependabot/GitHub security advisory 即時 bump（CVE-2025-61928 CVSS 9.3 只影響**不會開的 api-key plugin**，一週內修）；rate-limit storage 指向**現有 Upstash Redis**（別留 in-memory——唯一真 footgun）；簽章 key 當 crown jewel、可 rotate。**不開 api-key plugin**。

### 3.2 產品 / UX 資訊架構

**v1 新增 `(ops)` operator console route group；客戶 `(dashboard)`/`(marketing)/pricing`/`register` 凍結在 flag 後（保留不刪）；多租戶身份就緒但 signups OFF。** 模式 = 「同一畫布上 role-based 視圖」：一個 sidebar、一個 auth、共用 plumbing。

新增 6 個 operator 視圖（全是現有 event-sourced 表的投影，**後端資料都已存在**）：

| 路由 | 補上的缺口 | 資料來源 |
|------|-----------|---------|
| `ops/` Mission Control（bento） | phase/cap/git-sha + 多元件 healthz（ws liveness、executor DOWN/DEGRADED、reconcile age、TaskGroup）+ live ledger（reserved/realized/n_credits/last_reconciled_at）+ kill-switch + **G3 gate banner** | `position_state`、`/healthz` |
| `ops/validation`（G3） | **被卡的 gate（~2026-08-04）**：bot-vs-idle 週度（primary）、MR-alpha（非 gating 診斷）、windows X/8、`INSUFFICIENT_DATA\|PASS\|FAIL` | G3 report 資料 |
| `ops/reconcile` | checkpoint 時間軸、venue-vs-ledger drift、`event_seq_fence` high-water、freshness、state-parity divergence feed | `reconcile_observation` |
| `ops/safety` | `SAFETY_TRIGGER` feed + L1/L2 guard 即時狀態（per-symbol 24h-NAV-loss 5% / drawdown 10%） | `diagnostics` |
| `ops/cells` | per-cell standing-quote（POST/SKIP、rate、period、TTL）+ exposure vs `cap_per_cell` + `offer_claims` FSM | `StandingQuoteStore`、`offer_claims` |
| `ops/events` | append-only `event_log` explorer（forensic SoT viewer） | `event_log` |

**保留/隱藏/延後**：
- **保留 on**：`(auth)/login`、完整身份層（多租戶、`users.plan` 預設 `'free'`）。
- **隱藏（flag 不刪）**：`(marketing)/pricing`、`(dashboard)/{overview,api-keys,strategy,history,settings}`、`/register`（signups 關 → 導去 login/waitlist；marketing CTA 改「Request early access / Sign in」）。這些已是正確 tenant 形狀，是 SP6/billing 的目標，零重寫。
- **不出貨**：客戶 `strategy-form` knob 組（引擎非 hot-loadable，會是謊言）→ 改用唯讀 operator 狀態。
- **提進 `(ops)`**：`history` Executions → `ops/events`；`overview` 的 `MarketPanel`/`RateChart`（FRR/regime operator 有用）。
- **全域 filter bar**：persist 時間範圍 + cell + symbol；ops 預設窗 = 「this week」。

**無重寫的客戶 SaaS 路徑（additive）**：(1) 身份已多租戶 → 翻 `NEXT_PUBLIC_SIGNUPS_OPEN`；(2) 一個 role-routed sidebar：`role=operator`→`(ops)`、`role=customer`→`(dashboard)`；(3) 凍結頁在 SP1–SP5 端點就緒時自動點亮；(4) **operator 讀端點 = 客戶讀端點 scope by `account_id`**——先建 operator 讀 API 就是 SP4 的 groundwork。

### 3.3 狀態 / 資料抓取

**RSC 殼 + TanStack Query island（prefetch→HydrationBoundary→`useSuspenseQuery`）+ Server-Action mutations + per-user `cacheTag`。live data v1 = polling。**

三類資料分流：
- **(A) 慢的 owned data**（`user/me`、config、api-keys、plan）→ server 抓、cache、stream。`page.tsx` 為 async RSC，用 api-client 的 `isServer` cookie 分支 `prefetchQuery`，回 `<HydrationBoundary state={dehydrate(qc)}><XClient/></HydrationBoundary>`；client island 用同 key `useSuspenseQuery`。加 `app/get-query-client.ts = cache(() => new QueryClient(...))`，dehydrate pending queries（`shouldDehydrateQuery` 含 `status==='pending'`）讓 section stream。各 section 各自 `<Suspense>`，取代 overview 全有全無 skeleton。
- **(B) live telemetry**（offers、earnings tick、FRR/regime、heartbeat）→ client、TanStack Query、`refetchInterval`（offers/heartbeat ~10s，earnings/market ~30–60s），短 `staleTime`，`refetchIntervalInBackground:false`。**刪 `ws-client.ts`/`ws-store.ts`/`use-dashboard-ws.ts`**（對不存在的 `/auth/ws-token`+`/api/v1/ws` 無限重連）。chart/offers/MarketPanel 由 Query cache 餵。
- **(C) mutations** → Server Actions（auth actions 是先例）。client 留 `useMutation`，`mutationFn` 呼叫 action（pending/error 來自 Query、執行 + `revalidateTag` 在 action）；success 再 `invalidateQueries`。**vault submit 關鍵**：明文 key 經 Server Action 直送 server 端 KMS envelope 加密，**絕不**走 client proxy POST。

**Caching**：owned reads 用 `'use cache'` + `cacheTag(\`user:${uid}:config|keys\`)` + 短 `cacheLife`；live telemetry `cache:'no-store'`/不 prefetch（偏新鮮，真錢）。**現在就 tag-per-user**（Will=user #1）= 未來 SP6 per-user 失效免費。Zustand 收窄（短暫 UI/connection state）；polling-only 則 WS store 整個刪。`nuqs` URL/filter state，`react-hook-form`+Zod 表單。

**v1 即時性 = POLLING**（單用戶可忽略、serverless 友善、走現有 proxy、零額外後端工作）。
**演進**：延遲不夠時加 **一條** FastAPI `StreamingResponse(media_type='text/event-stream')` + Upstash Redis pub/sub 從 OCI daemon fan-out；SSE 走現有 cookie-proxy（純 HTTP GET，**不需 ws-token**），client transport 換 `EventSource`→`invalidateQueries`，**所有 consumer hook 不變**（都讀 Query cache）。WS 留 SP6/互動控制。

### 3.4 設計系統

**保留 shadcn/Tailwind-4/feature-sliced 底座；把散落成 copy-paste utility string 的設計語言 token 化 + 元件化。** 缺口是「tokenization」非重建。

**3 層 token（`globals.css` `@theme`，HSL→OKLCH，shadcn 2026 預設）**：
- **L1 primitives**（raw OKLCH ramp，無語意）：`--color-ink-950..50` 中性 + 5 個 skill 狀態色（yield/tech/idle/error/bot）+ blue accent；`--radius-card`、`--shadow-microglow: inset 0 1px 0 0 rgb(255 255 255/.10)`、`--ring-hairline`、motion primitives。
- **L2 semantics**：加 `--color-surface-{base,card,glass}`、`--color-border-hairline`，**大補丁 = STATUS LEXICON** `--color-status-{yielding,idle,error,bot}`（各含 `-bg`/`-border`），讓 18 處 inline `emerald/rose/amber/violet` 變 `text-status-*`。接上 dead 的 `--chart-1..5`。motion token `--duration-{fast,normal,slow}` + `--ease-{out,spring}`。
- **L3 component token**（選用）：`--card-padding`、`--button-radius`。

L2 經 `@theme inline` 暴露。**強制規則**：feature 元件只能用 `bg-card`/`border-border`/`text-status-*`/`text-muted-foreground`/`bg-surface-glass`，禁 `bg-white/[0.02]`/`border-white/5`/`text-zinc-400`/hex。**加 Biome/ESLint guard** 禁 `src/features/**`、`src/components/shared/**` 的任意 `*-[...]` 色值 + raw `zinc/emerald/...-NNN` + hex。

**薄 primitive 層**：`SurfaceCard`（CVA，`tone` 變體，`interactive`）、`GlassPanel`（`backdrop-blur-xl`）、`StatusDot`/`StatusBadge`（pulsing heartbeat）、**numeric 家族**（`NumericValue` tabular-nums+dim decimal/symbol、`RateDisplay` APY-hero+dailyRate-muted、`PeriodBadge` 短 vs 30d-lock microglow、`AnimatedNumber` wrap `@number-flow/react`）、**charts**（共享 chart theme/`<AreaChartCard>` 讀 `var(--color-chart-*)`）、**Bento+motion**（`BentoGrid`/`BentoCell span`、`motion/react` + `LazyMotion`/`m` 做 heartbeat pulse/count-up/button spring/staggered load）。

**加 dep**：`motion`、`@number-flow/react`。其餘已有。always-dark → 移除 `@custom-variant dark` + `dark:` 變體。

---

## 4. 子專案拆解 + v1 邊界

| # | 子專案 | 邊界 | 依賴 | v1? | 真錢風險 |
|---|--------|------|------|-----|---------|
| **SP1** | Auth/身份核心（**自托 Better Auth**） | Better Auth 跑 Next `/api/auth/*`（session 表在 Postgres）+ FastAPI JWKS 驗證 dependency（SP2–5 授權邊界）+ app `user_profile` 表（FK Better Auth user id、`plan`、future `org_id`、JIT provision）；刪手刻發 token | Better Auth（自托）+ Postgres | ✅ | 無 |
| **SP2** | BFX-key KMS vault + Bitfinex verify | `api-keys` CRUD+verify；明文經 Server Action→server KMS envelope；vault row FK `user_profile.user_id` | SP1 | ✅ | 無 |
| **SP3** | 策略 config CRUD（v1 唯讀為主） | `configs`；v1 surface cells/standing-quote 唯讀，write 側極簡 | SP1 | ✅ | 無 |
| **SP4** | 投影/讀端點（**operator-first**） | 先建 `ops/{ledger,reconcile,safety,cells,events,g3}`；客戶端點 = 同投影 scope by `account_id` | SP1,SP2 | ✅ | 無 |
| **SP5** | Plan-gating + 限流 | per-IP + per-user token bucket；plan→cap；讀 verified principal 的 `role`/`plan` | SP1–4 | ✅ | 無 |
| **SP6** | 多租戶執行控制平面 | DB-row reconciler：有 verified key+active config+方案 → 確保一 per-tenant daemon 跑；取代 hardcoded `AccountContext`；**絕不與 canary 共 process** | SP2,3,5 | ❌ 延後 | **高** |
| **SP7** | Billing/訂閱 | `billing`+月結+Stripe；寫 `users.plan` | SP5 | ❌ stub-first | 中 |

**執行模型決策（SP6 用）**：結構用 **C（API 層與執行 fleet 完全解耦）**、執行用 **A（一租戶一 daemon/容器）**。canary 變「tenant 0」、新執行碼幾乎為零；Bitfinex 私有 WS 是 per-account → B（共享 process 多工）省不到最貴資源卻把真錢風險集中到共享 in-memory state。控制平面 = DB-row reconciler（重用 codebase 既有 deployment-reconciler pattern）。**不變式：live canary daemon 絕不與任何新 SaaS/多租戶碼共享 process/memory。**

---

## 5. 重構順序（每步可獨立出貨）

1. **Auth swap**（解鎖一切）：加 `lib/auth.ts`（betterAuth + `jwt()` plugin）+ `/api/auth/[...all]/route.ts`；`(auth)/actions.ts` 改 `authClient.*`；刪手刻 cookie/`tryRefresh`/`AUTH_SECRET`；保留 BFF proxy 殼（server 端 mint 短命 JWT 注入）；rate-limit 指 Upstash Redis、開 MFA、**不開 api-key plugin**。FastAPI 立 `user_profile` + JWKS 驗證 dependency。Will 註冊 user #1、`role=operator`、signups OFF。
2. **Live data working**：接 SP4 讀端點；live hooks 改 `refetchInterval`；**刪 WS stack**。（立即解鎖可用儀表板——現在 live 層是死的。）
3. **`(ops)` console**：加 route group + role-routed sidebar；對 SP4 投影建 6 視圖；flag-hide 客戶頁 + pricing + register；提 Executions/MarketPanel 進 ops。
4. **RSC + hydration**：加 `get-query-client`；owned-data 頁拆 RSC 殼 + island（prefetch/HydrationBoundary）；per-section `<Suspense>`。
5. **mutations → Server Actions** + `cacheTag`（尤其 vault submit）。
6. **設計系統**：token（OKLCH 3 層 + status lexicon + lint guard）→ primitive（SurfaceCard/numeric/charts）→ bento+motion → 機械式 feature 遷移。更新 stale 的 `frontend/CLAUDE.md`。
7. **SSE**：只在實測 polling 延遲不足時。

**不動（已 best-practice）**：cookie-proxy auth 縫 + path-traversal guard、typed `ApiError`+`{data}` unwrap api-client、query-keys factory、per-feature hooks、`useInfiniteQuery` cursor 分頁、Zustand 收窄、3 層元件分層、`format.ts`、shadcn primitives。

---

## 6. 後端回沖（SP1–SP5 reshape）

- **SP1（最大改動，大幅縮小）**：從「手刻 IdP」變「Better Auth 自管身份 + FastAPI 驗 + 擁 app-side profile」。Better Auth（lib in Next app）負責 password/session/MFA/reset/rotation；FastAPI 只建 JWKS 驗證 dependency（SP2–5 授權邊界）+ `user_profile` 表（JIT provision，帶 `plan`、future `org_id`）。`(auth)/actions.ts` + proxy refresh 分支由 Better Auth client/server SDK 取代（~-100 行手刻）。
- **SP2**：概念不變，但 FE 強制正確 ingestion——明文 key 經 Server Action → server KMS envelope，絕不 client proxy hop。vault row 普通 SQL join `user_profile.user_id`。
- **SP3**：v1 surface cells/standing-quote 唯讀（引擎非 hot-loadable），write 側 v1 極簡；客戶 knob-write 是 SP6。
- **SP4（reshape + 前置）**：先建 operator 讀端點為 event-sourced 表投影；客戶讀端點 = 同投影 scope by `account_id`，是 groundwork 非繞路。polling → 這些是 v1 唯一即時機制（無 stream endpoint）。
- **SP5**：讀 verified provider principal + `user_profile` 的 `role`/`plan`，非自簽 claim。
- **即時**：v1 無 SSE/WS endpoint；需要時加一條 `StreamingResponse` + Redis pub/sub fan-out。

---

## 7. 風險與緩解

| 風險 | 嚴重度 | 緩解 |
|------|--------|------|
| **自托 Better Auth 的 CVE/patch 責任** | 中 | 訂 Dependabot/GitHub security advisory、即時 bump（CVE-2025-61928 一週內修、且只影響不會開的 api-key plugin）；失敗模式是「疏於關注」非「工作量大」 |
| 自托 rate-limit 預設 in-memory | 低 | 一行指向現有 Upstash Redis（跳過才是 footgun） |
| 簽章 key 外洩 | 中 | key 當 crown jewel、env 管理、可 rotate；JWKS 公鑰輪替 |
| 未來想卸 auth ops | 低 | 切 managed Neon Auth（同引擎、零 user-data 遷移）；或 B2B/SOC2 時上 WorkOS |
| 把 SaaS/多租戶碼意外碰到 live canary | **高** | SP6 延後 + 不變式「canary 絕不與新碼共 process」；v1（SP1–5）完全不碰 daemon/credentials-in-execution |
| FE↔BE 契約漂移（envelope/欄位名） | 中 | 保留 `{data}`/`{error:{code,message}}` envelope；用 Pydantic 對齊 FE DTO；型別測試 |
| polling 延遲不足 | 低 | 預先約定 SSE 觸發門檻（如「10s poll 對 heartbeat/offers 明顯太慢」），measured upgrade 非臆測 |

---

## 8. 不在 v1 範圍（out of scope）

- SP6 多租戶真錢執行（延到 G3 gate 過關）
- SP7 真實收費（Stripe + 月結 cron）——v1 只 stub + `users.plan` 欄
- 客戶 strategy knob 寫入表單（引擎 hot-reload）
- 多 venue（Bitget/OKX）抽象
- SSE/WebSocket push channel（除非 polling 實測不足）
- org/tenant SQL 模型（flat users now）
- light mode（always-dark）

---

## 9. 開放項（待逐子專案 brainstorm 細化）

1. ~~DB 拓樸 / schema / MFA 政策~~ → 已決（D13/D14）：同 DB + `auth` schema、Alembic 唯一工具、TOTP+動錢 step-up。剩具體值：JWKS `iss`/`aud`（`aud="bfx-funding-backend"`）、`sub`=uuid、`requireEmailVerification:false`(v1)、Better Auth 版本（`better-auth@1.6.14`）、Upstash `secondaryStorage`+`rateLimit.storage="secondary-storage"`。
2. `user_profile` JIT provision 的觸發點與 race 處理（首次 request 建 row、FK 到 Better Auth user id）。
3. `(ops)` 6 視圖各自的 DTO 與投影 SQL（哪些可直接讀表、哪些要 aggregate）。
4. G3 report 資料現在在 `docs/research/` 檔案——`ops/validation` 要讀 DB 投影還是另存查詢表。
5. 設計系統 OKLCH ramp 的實際色值（對 3 個 UI skill 的 status 色定稿）。
6. i18n：operator 視圖是否需 zh-TW/en 雙語，還是 operator-only 先 en。

---

## 附錄：核心檔案

- Auth/proxy：`frontend/src/app/[locale]/(auth)/actions.ts`、`frontend/src/app/api/proxy/[...path]/route.ts`、`frontend/src/middleware.ts`、`frontend/src/lib/env.ts`
- 資料層：`frontend/src/lib/api-client.ts`、`frontend/src/lib/query-keys.ts`、`frontend/src/providers/query-provider.tsx`、`frontend/src/lib/ws-client.ts`（刪）、`frontend/src/stores/ws-store.ts`（刪）
- IA：`frontend/src/app/[locale]/`（加 `(ops)/`、凍 `(dashboard)/`+`(marketing)/pricing`）、`frontend/src/components/layout/sidebar-nav.tsx`
- 設計：`frontend/src/app/globals.css`、`frontend/src/components/ui/{card,button,badge}.tsx`、`frontend/src/components/shared/stat-card.tsx`、`frontend/src/lib/format.ts`
- 後端參考：`backend_py/ARCHITECTURE.md`、`backend_py/src/bfx_funding_bot/modules/accounts/tables.py`、Go-era 藍本（git `1992b99:backend_architecture.md`）、Go API（`b9c1f37` 前）
- 設計契約：`.claude/skills/{ui-premium-dark-theme,ui-financial-typography,ui-bento-layout-motion}/SKILL.md`
- Auth 文件：Better Auth docs（JWT/JWKS plugin、Next.js integration、organization plugin、rate-limit、MFA）；Neon Auth（managed 同引擎，未來替代）：`neon.com/docs/auth/overview`、`/auth/guides/plugins/jwt`
