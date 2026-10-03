---
title: Frontend SaaS architecture — rebuild backend, self-hosted Better Auth, operator-console v1
date: 2026-06-06
status: active
tags: [bfx-funding-bot, decision, saas, architecture, auth, frontend]
related-commits:
  - b8c666d
  - e164c88
  - "236fb87^..bf165e9"
  - "1565ba1^..5d42bc4"
---

# Frontend SaaS architecture — rebuild backend, self-hosted Better Auth, operator-console v1

Epic-level ADR。壓自 repo spec `docs/superpowers/specs/2026-06-06-frontend-saas-architecture-design.md`（committed `b8c666d`；原文已不在 repo，本 ADR 即紀錄）。子專案決策見各 SP ADR（SP2 = [2026-06-07-sp2-bfx-key-vault](2026-06-07-sp2-bfx-key-vault.md)）。

## Context

Forensic 發現（反直覺）：`frontend/` 早已 feature-complete（Next 16 / React 19 / TanStack Query / shadcn，8 頁 18 component、無 mock），但後端只服務 3 個 route——FE 要的 20 個 `/api/v1/*` 後端實作 0 個，連 login 都 404。根因：完整 SaaS HTTP / 多租戶層本在 Go 後端，2026-05-29 Python 重寫時刻意砍掉。5 張 dormant 表（users/api_keys/...）已用 `(account_id, deployment_environment)` 複合鍵=資料層多租戶就緒。Will 拍板「重建完整 SaaS 後端 + best-practice 重思 FE」。

## Options Considered

- **FE 重思深度**：A. greenfield 重寫；B. 審查 + 現代化既有 Next 16。
- **Auth**：A. 自托 Better Auth（OSS、跑 Next app、表在你 Postgres）；B. Neon Auth（managed、同引擎、Beta）；C. Clerk / WorkOS（user 在平台外、要 webhook 同步）；D. Keycloak / Zitadel / Ory（自帶 schema、重 ops）。
- **DB 拓樸**：A. 同一 Neon DB + 專屬 `auth` schema + web app scoped role；B. 獨立 auth DB。
- **即時性**：A. 修 WebSocket stack；B. polling；C. SSE。
- **v1 定位**：A. 客戶儀表板；B. operator console（signups OFF）。

## Decision

- **D1 = spec D6** — FE **審查 + 現代化**（非 greenfield）。
- **D2 = spec D7** — v1 **= operator console**：客戶頁凍在 flag 後、多租戶身份就緒但 **signups OFF**（Will=user #1）。
- **D3 = spec D8** — auth **= 自托 Better Auth**（Neon Auth 為未來想卸 ops 時的零遷移 managed 替代）。
- **D4 = spec D13** — **同一 Neon DB + `auth` schema + web app scoped role**；**Alembic 唯一 migration 工具**（port `better-auth generate` 出的 schema，永不跑 `better-auth migrate`）。
- **D5 = spec D9/D10/D11/D12** — 即時性 **polling**（刪死 WS）；策略面 **唯讀**；**always-dark**；**flat users**（org_id 留 SP6）。
- **D6 = spec D1/D2/D3/D5/D14** — v1 範圍 **SP1–SP5**（SP6 多租戶真錢執行延至 G3 gate ~2026-08-04）；MFA **TOTP + step-up 只在動錢操作**；billing 出 v1 **只 stub**；venue **Bitfinex-only**。

## Rationale

- **D1**：FE 已現代，greenfield 多半只重造現有（YAGNI）；審查+現代化代價低、風險小。
- **D2**：現有 FE 答錯問題（客戶 APY 儀表板），v1 真正需要 operator 視角（Mission Control / G3 / reconcile / safety）；翻 flag 即客戶 SaaS——身份/多租戶就緒但不開放。
- **D3（最 load-bearing）**：獨立 Python 後端用 **JWKS** 無狀態驗證 + user 表在**自己的 Postgres** 可 SQL join（無 webhook 同步）+ **零平台鎖定 / DB-agnostic**（對齊 Will 最在意的 portability）。不選 Clerk/WorkOS：user 在平台外、要 webhook。不選 Keycloak/Zitadel：自帶 schema、重 ops。代價：自托維護 OSS lib（緩解：用維護中 lib ≠ rolling your own、責任輕；錢在 BFX vault 不在 login）。
- **D4**：同 DB=FK 完整、SP4 免跨 DB 讀 trading；`auth` schema + scoped role=真錢安全邊界（web app 動不了 trading 表，SP1 已驗 `bfx_webapi` 對 `position_state` SELECT=f）；單一 Alembic=與既有 live-Neon 手刻 migration 工作流一致。代價：共用 2-CU / PITR（v1 可忽略、日後升獨立 project 可逆）。
- **D5**：單用戶 ~6 req/min，polling 零額外後端工作；WS stack 對不存在端點無限重連=純負債；引擎不能 hot-load，「儲存策略」表單會是謊言 → 唯讀。
- **D6**：SP6 是 10 種真錢災難所在、且被 G3 profitability gate 擋住 → 延後對齊安全與自身 roadmap；step-up 只擋動錢（唯讀儀表板不擋）；billing 驗證過再收。

## Result

- **SP1（auth-swap）** ✅ merged `e164c88` + **deployed live 2026-06-07**（scripted E2E sign-up → `/api/proxy/profile` → 200）。
- **SP2（BFX-key vault）** ✅ merged `bf165e9`、undeployed（見 [2026-06-07-sp2-bfx-key-vault](2026-06-07-sp2-bfx-key-vault.md)）。
- SP3–SP5 pending。整體進度 `git log --oneline b8c666d..`。
- **執行模型決策（SP6 用）**：結構 **C**（API 層與執行 fleet 完全解耦）+ 執行 **A**（一租戶一 daemon/容器），canary=tenant 0。**不變式：live canary daemon 絕不與任何新 SaaS / 多租戶碼共享 process / memory。**

## Followup

- ~~SP3（config CRUD）~~ ✅ 2026-06-17（見 Amendment D11，後被 account-scoped draft 取代）；SP4（投影讀端點）/ SP5（plan-gating + 限流）待做。
- SP6 多租戶真錢執行：G3 gate（~2026-08-04）過後才解凍。
- billing 真收費 + 客戶 signups 開放：v1 後、驗證過再說。

## Amendment (2026-09-27): 壓縮補記的 spec／plan 裁定

spec 全文複核後，原 Decision 漏記、但仍約束現行程式或會被重提的裁定：

### Amendment Decision

- **D7 = spec §3.1「保留」**：保留 BFF proxy 殼，瀏覽器只持有 Better Auth 的不透明 httpOnly session cookie、**永不見 JWT**；proxy 在 server 端 mint 短命 EdDSA JWT 轉給 FastAPI。依據 IETF OAuth 2.0 for Browser-Based Apps BCP 對 BFF 的建議。JWT plugin 只作 service-to-service token，**不取代 session model**。
- **D8 = spec §3.1「自托責任」**：**永不啟用 Better Auth api-key plugin**（CVE-2025-61928 只影響它）；rate-limit 儲存不得留 in-memory（當時指 Upstash，現為 VM Redis，見 [2026-06-07-web-tier-hosting-funnel-scoped-roles](2026-06-07-web-tier-hosting-funnel-scoped-roles.md)）；依 security advisory 即時升版。
- **D9 = SP1 plan「Key decisions」**：FastAPI web-API 是**獨立 process**（`main:app`），與真錢 daemon 分開部署；issuer `bfx-funding-bot`、audience `bfx-funding-backend` 寫死不走 env；`sub` = Better Auth user id（TEXT，非 uuid）；v1 `requireEmailVerification:false`。
- **D10 = spec §3.3/§3.4**：polling 不夠時的演進路線是**一條 SSE**（`StreamingResponse`，走既有 cookie-proxy、consumer hook 不變），WS 留給互動控制；觸發條件須是實測延遲不足，不是預期。設計系統採 3 層 token（OKLCH primitives → semantic／status lexicon → component）＋ lint 禁止 feature 元件寫 raw 色值。
- **D11 = SP3 spec（2026-06-16）**：`/api/v1/configs` 只**儲存**策略偏好、daemon 不讀（inert）；既有前端就是契約、後端配合不改前端；一人一份（upsert）；後端驗證**只鏡像前端 Zod**（正數、min≤max），**刻意不加** openspec 時代的領域上下限。

### Amendment Rationale

- **D7**：瀏覽器不持 token，XSS 拿不到可重放的 bearer，相較把 JWT 放 localStorage 或 client header 攻擊面小；代價是每個 API 呼叫都要多一跳 Next server。
- **D11**：後端比前端嚴格會造成「前端接受、後端 400」的分歧；config 在 v1 不影響任何送單，寬鬆上下限的代價近乎零。領域上下限的最終處置見 [2026-09-23-go-era-openspec-design-disposition](2026-09-23-go-era-openspec-design-disposition.md)（放棄，config draft 進 live 路徑時才一併設計）。
- 現況（2026-09-27）：D4 的「同一 Neon DB」已隨 2026-06-23 退役 Neon 變成同一個 VM 自托 Postgres 18，`auth` schema＋scoped role 的邊界不變，印證 D3 的 portability 論點；SP3 的 per-user `user_configs` 已由 Halt 1 的 account-scoped config draft 取代（以 `ExchangeAccount.id` 定址、draft ≠ applied，見 ARCHITECTURE §1 與 `modules/api/config.py`）。SP3 實作 `git log --oneline 1565ba1^..5d42bc4`。

## Review Notes

auth 選型半年後值得複查 Neon Auth 是否 GA、要不要卸 ops。

## Related

- 原 spec：`2026-06-06-frontend-saas-architecture-design.md`（`b8c666d`）；SP1 plan `2026-06-06-sp1-auth-swap.md`（原文已不在 repo，本 ADR 即紀錄）。
- 來源：
  - spec：`2026-06-06-frontend-saas-architecture-design.md`（原文已不在 repo，本 ADR 即紀錄）
  - SP1 plan：`2026-06-06-sp1-auth-swap.md`（原文已不在 repo，本 ADR 即紀錄）
  - SP3 spec：`2026-06-16-sp3-strategy-config-crud-design.md`（原文已不在 repo，本 ADR 即紀錄）
  - SP3 plan：`2026-06-17-sp3-strategy-config-crud.md`（原文已不在 repo，本 ADR 即紀錄）
- 子專案 ADR：[2026-06-07-sp2-bfx-key-vault](2026-06-07-sp2-bfx-key-vault.md)；部署拓樸：[2026-06-07-web-tier-hosting-funnel-scoped-roles](2026-06-07-web-tier-hosting-funnel-scoped-roles.md)。
