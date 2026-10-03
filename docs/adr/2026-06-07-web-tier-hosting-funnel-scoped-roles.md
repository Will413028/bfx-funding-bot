---
title: Web tier 部署拓樸：webapi 與真錢 daemon 分 process、Tailscale Funnel 作唯一公開入口、DB 最小權限 role；2026-06-23 前端與 Redis 一併收回 VM
date: 2026-06-07
status: active
tags: [bfx-funding-bot, decision, deployment, security, auth, frontend, container]
related-commits:
  - "7de6170^..f9cc09e"
  - acdf0ad
  - "8547016^..bd10170"
  - 00b5394
---

# Web tier 部署拓樸：分 process、Funnel 單一入口、最小權限 role

## Context

prior state：SP1（[2026-06-06-frontend-saas-architecture](2026-06-06-frontend-saas-architecture.md)）已 merge 自托 Better Auth（Next app）與以 JWKS 驗證的獨立 FastAPI web-API（`main:app`），但尚未部署。真錢 daemon 跑在自管 VM（[2026-05-31-koyeb-to-vm-single-writer-lock](2026-05-31-koyeb-to-vm-single-writer-lock.md)），VM 採 default-deny、不開 OCI 安全清單 port。2026-06-07 決定 SP1 怎麼上線；2026-06-23 Neon 回 402 失聯、DB 已自托（「棄 Neon Step 1」），接著決定前端與 session 儲存怎麼跟著搬。

**約束**：

- `external` 沒有自有網域、增量成本目標近乎零；使用者只有 operator 一人（~6 req/min）。
- `external` 2026-06-23 起 Neon 不可用，舊 auth 帳號資料無法取回。
- `inherited` epic 不變式「live daemon 絕不與 SaaS／多租戶碼共享 process／memory」：仍成立，daemon 仍是唯一送單者。
- `inherited` VM 已跑 Tailscale（Grafana 已用 loopback＋Tailscale 模式）：仍成立。

## Options Considered

**公開入口**：
- **基準. 開 443 + 網域 + 反向代理自動 TLS**（Caddy automatic HTTPS，<https://caddyserver.com/docs/automatic-https>）。
- **A. Tailscale Funnel（採用）**：不開 port、TLS 自動、免網域；代價是只能用 Tailscale 配發的主機名、只服務 443/8443/10000。
- **B. Cloudflare Tunnel**：同樣不開 port，但需要網域。

**web-API 放哪**：A. 併入 daemon process；B. **同 VM 的獨立容器（採用）**；C. 另一台主機／serverless。

**前端（2026-06-07）**：A. **Vercel Hobby（採用）**；B. 同 VM。**前端（2026-06-23 重評）**：A. 維持 Vercel；B. **收回 VM，Funnel 只指前端（採用）**；C. VM 上加 Caddy／nginx 反代分流；D. webapi 開第二個 Funnel port。

**session／rate-limit 儲存（2026-06-23）**：A. 丟掉 Upstash 改 in-memory；B. serverless-redis-http（SRH）代理保留 Upstash SDK；C. **VM 自托 Redis 容器＋`ioredis`（採用）**。

## Decision

- **D1 = SP1-deploy「two-process」**：daemon 與 webapi 同一 codebase、兩個 runtime，**永不合併**；webapi 為獨立 compose service（cpu 0.5／mem 512M 上限、只綁 host loopback），`bot`／`migrate` 定義不動。
- **D2 = SP1-deploy「public ingress」**：公開入口用 **Tailscale Funnel**；Cloudflare Tunnel 延到有網域、要開放客戶時再換（只改 FE `API_URL` 與 tunnel）。
- **D3 = SP1-deploy §4/§5**：兩個最小權限 login role——`bfx_webauth` 只有 `auth` schema、`bfx_webapi` 只有逐表明確授權；migration 一律由 owner role 跑。webapi 用**獨立 env 檔**，看不到 daemon 的全權 `DATABASE_URL` 與 Bitfinex key。
- **D4 = FE-VM spec「拓樸」（2026-06-23，取代 D2 的 Vercel 部分）**：前端容器化上 VM，**Funnel 443 只指前端**；webapi 退回內網（前端 server-side proxy 經 docker 網路呼叫），JWKS 也走內網 `http://frontend:…/api/auth/jwks`；Vercel 退役。
- **D5 = FE-VM spec「Redis」**：自托 Redis 7（appendonly＋volume、無 published port、內網不設密碼），Better Auth `secondaryStorage`（session＋rate-limit）改用 `ioredis`，因此**不需要 DB session 表、也不跑 `better-auth migrate`**。
- **D6 = FE-VM spec §7**：舊 auth 帳號隨 Neon 遺失，改為**重新註冊**，不做資料搬遷。
- **D7 = FE-VM spec「Build 位置」**：前端 image 在 VM 上 build（已被 [2026-09-25-ci-registry-digest-deploy](2026-09-25-ci-registry-digest-deploy.md) 取代）。

## Rationale

- **D1**：webapi 對外、會 crash 或被打，daemon 管真錢；分 process 讓 webapi 重啟對交易零影響，資源上限讓 SSR／API 不會餓死 daemon。不選 C：多一台主機的成本與維運，對單一 operator 沒有對應收益。
- **D2 選 Funnel 而非基準**：基準要網域並開 port，破壞 VM 的 default-deny；Funnel 不開任何安全清單 port、TLS 自動。代價：主機名綁 tailnet、`NEXT_PUBLIC_*` 與 passkey rpID 都綁它，換網域要 rebuild 並讓既有 passkey 失效（pre-launch 可接受）。不選 Cloudflare Tunnel：現在沒網域。
- **D3**：新 Postgres role 預設沒有任何表權限，只授權需要的表，兩個公開 app 就碰不到 `position_state` 與 event log；這是真錢與公開面之間的 DB 邊界，相較只靠應用程式碼檢查更難被繞過。
- **D4 選 B 而非 C/D**：WebSocket 已死、前端本來就 server-side proxy 到 webapi、JWKS 可走內網，公開面只剩前端一個，不需要反代；第二個 Funnel port 等於多暴露一個不必要的公開面。代價：前端、Redis 與真錢 bot 同機（單點故障），以資源上限緩解。
- **D5 選 C**：in-memory 要新增 DB session 表且 rate-limit 在重啟後歸零；SRH 是多餘的一層容器。Upstash SDK 只講 REST，連不到本地 Redis，所以必須換 client。Redis 只存可重登重建的 session，故不備份；代價是內網不設密碼，docker 網路即信任邊界。

## Result

- `git log --oneline 7de6170^..f9cc09e`（webapi service、獨立 env）、`acdf0ad`（SP2 加 `BFX_VAULT_KEK` preflight）；2026-06-07 SP1 上線，scripted E2E sign-up → `/api/proxy/profile` 200（見 [2026-06-06-frontend-saas-architecture](2026-06-06-frontend-saas-architecture.md) Result）。
- `git log --oneline 8547016^..bd10170`（ioredis、standalone build、compose redis＋frontend、`deploy-vm.sh`）；`00b5394` 前端 host port 改 3001（3000 被 Grafana 佔用）。2026-06-23 Neon、Vercel 歸零（ARCHITECTURE §8）。
- 已演進：D3 原本規定 GRANT 走 psql runbook、不進 migration（role 是 out-of-band 建的，測試環境沒有會炸）；現行 migration 以 `IF EXISTS (SELECT FROM pg_roles …)` 條件授權／收回（例：`1c435a35dcb4`），ARCHITECTURE §8 記為「授權與收回都在 migration」。建 role 本身仍是 out-of-band。

## Followup

- 在 repo 的 runbook 補上 `bfx_webauth`／`bfx_webapi` 的建立與基本授權、以及 Funnel 設定（目前只寫在 SDD spec／plan 裡；pgBackRest 實體還原會帶回 role，但全新主機 bootstrap 仍需要這一步）。

## Revocation Triggers

- 取得網域並開放客戶註冊 → D2 改 Cloudflare Tunnel 或基準反代。
- 前端或 webapi 負載開始影響 daemon（CPU／記憶體爭用）→ 重評 D1/D4 的同機部署。
- session 需要跨主機或不可遺失 → 重評 D5 的無密碼、無備份 Redis。

## Related

- 來源（Provenance）：
  - SP1 deployment spec：`2026-06-07-sp1-deployment-design.md`（原文已不在 repo，本 ADR 即紀錄）
  - SP1 deployment plan：`2026-06-07-sp1-deployment.md`（原文已不在 repo，本 ADR 即紀錄）
  - FE→VM spec：`2026-06-23-frontend-vm-migration-design.md`（原文已不在 repo，本 ADR 即紀錄）
  - FE→VM plan：`2026-06-23-frontend-vm-migration.md`（原文已不在 repo，本 ADR 即紀錄）
- 相關 ADR：[2026-06-06-frontend-saas-architecture](2026-06-06-frontend-saas-architecture.md)（epic、auth 選型）、[2026-06-07-sp2-bfx-key-vault](2026-06-07-sp2-bfx-key-vault.md)、[2026-09-25-ci-registry-digest-deploy](2026-09-25-ci-registry-digest-deploy.md)（現行 build／部署）、2026-05-26-bfx-productization-host-saas（產品方向決策，不在本 repo）（產品化方向）。
