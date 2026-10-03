---
title: SP2 BFX-key vault — app-managed envelope encryption + fail-closed Bitfinex verify
date: 2026-06-07
status: active
tags: [bfx-funding-bot, decision, saas, security, vault, crypto]
related-commits:
  - "236fb87^..bf165e9"
---

# SP2 BFX-key vault — app-managed envelope encryption + fail-closed Bitfinex verify

`frontend-saas-architecture` epic 的子專案 SP2（見 [2026-06-06-frontend-saas-architecture](2026-06-06-frontend-saas-architecture.md)）。壓自 repo spec `docs/superpowers/specs/2026-06-07-sp2-bfx-key-vault-design.md`（committed `0e24448`；原文已不在 repo，本 ADR 即紀錄）。

## Context

SaaS 平台要托管使用者的 Bitfinex API key——一個 custody-sensitive 動作。SP1 已立好 auth + `user_profiles` 表（自托 Better Auth、web-API JWKS 驗證）。SP2 要決定：secret 如何 at-rest 加密、verify 驗到多嚴、schema 怎麼擺——且 v1 雖單用戶（Will=user #1）但授權邊界要 per-user，為 SP6 多租戶免費鋪路。

## Options Considered

- **at-rest 加密**：A. cloud KMS（AWS/GCP KMS）；B. app-managed envelope（env KEK + per-record DEK + AES-256-GCM + `key_version`）；C. 單把 key 直接對稱加密。
- **verify 程度**：A. 簽章能過即算有效；B. permission-aware fail-closed（funding-write ON 且 withdraw OFF 才 verified）。
- **key 基數**：A. 多 key/user；B. 單 key/user（unique index）。
- **加密範圍**：A. 連 api_key 一起加密；B. 只加密 secret、api_key 存明文。
- **crypto 歸宿**：A. FastAPI web-API；B. 前端 / 另一服務。
- **schema**：A. 新表；B. 改造 dormant Go-era `APIKey` model。

## Decision

- **D1 = spec D1** — at-rest 用 **app-managed envelope**（B）。
- **D2 = spec D2** — verify 用 **permission-aware fail-closed**（B）。
- **D3 = spec D3** — **單一 key/user**（B）。
- **D4 = spec D4** — **只加密 secret**（B），api_key 存明文。
- **D5 = spec D5** — crypto 歸宿 **FastAPI web-API**（KEK 在其 env）。
- **D6 = spec D6** — **改造 dormant `APIKey` model**（FK → `user_profiles.user_id`、envelope 欄位）。

## Rationale

- **D1**：envelope 與自托 Better Auth 的零鎖定 / portability 立場一致（無 cloud 依賴），KEK 與 Better Auth 簽章 key 同風險等級（epic 已接受）；未來可用 cloud KMS wrap 這把 KEK、**零資料遷移**。代價：KEK 是 crown jewel，外洩=全 secret 可解（緩解：當 Better Auth key 同等保管 + `key_version` 支援 rotation）。不選 cloud KMS：增 cloud 依賴、違 portability。
- **D2**：托管他人 key 的產品，verify 該是真安全 gate，直接執行「拿你 key 但無法提款」承諾——相較「只驗有效」對 custody 風險零保護。transport error 不降級（不因網路 blip 誤標 failed）。
- **D3**：一人一 Bitfinex 帳號=自然模型，FE 也只顯示一把；取代=先 DELETE 再 POST；多 key 是 YAGNI。
- **D4**：api_key 是識別符、無 secret 無法簽名（業界標準）；secret read 永遠 masked `"****"`。
- **D5**：v1 唯一解密路徑=verify，SP6 執行層也在 Python 解密——單一 crypto 家。
- **D6**：dormant 表從未進 migration → 乾淨 CREATE TABLE，且保留 FE `/api-keys` 契約；代價=要 swap SP1 的 `user_profiles` unique index → unique constraint（PG FK target 需 constraint 非 bare index）。

## Result

- 12-task subagent-driven TDD + per-task spec/quality 兩階段 review（crypto/verify 用 opus 安全 lens）+ final 整合 review = READY_TO_MERGE。進度 `git log --oneline 236fb87^..bf165e9`。
- Gate：backend 1260 unit + 15 SP2 integration（testcontainers PG）、FE 2 vitest、ruff/mypy/biome/tsc clean。merged main `bf165e9` --no-ff。
- 不變式落地：明文單向流（FE Server Action → web-API、不走 client `/api/proxy`、唯一解密=verify）、AAD=user_id 綁 owner、跨用戶存取 → 404。

## Followup

- ✅ 2026-06-16 xhigh review 7 findings 全修（`66ff8ad`：transient 不 demote、KEK 不符→503 `vault_key_mismatch`、withdraw default-deny、IntegrityError→409）+ pushed + **DEPLOYED live**（migrate 撞 prod 殘留舊 `api_keys` 表）。
- ⚠️ **KEK 輪替未支援**：`decrypt_secret` 忽略 `key_version`、只用單一 KEK——D1 的「`key_version` 支援 rotation」目前只是欄位預留。KEK 已 live，若照舊 runbook `openssl rand` 換掉，所有既有 `api_keys` verify 回 503 且無 re-wrap 路徑可救；輪替前必先做 key_version-aware 多 KEK + re-wrap migration。
- Model/DB drift：`UserProfile` model 仍宣告 unique `Index`、migration 建 unique constraint——無害（本 plan 不 autogenerate），記給未來跑 autogenerate 者。

## Lessons

### Rules

- **R1**：SQLAlchemy model 若會在 sqlite 跑單元測試但部署在 PG，server_default 只能用兩方言都有的函式（`func.current_timestamp()`）；function-based default（`now()` / `gen_random_uuid()` 是 PG-only，sqlite INSERT 才炸）改 client-side `default=`。（同類通用規則：review 必須實際執行，不能只讀。）

## Amendment (2026-09-27): 壓縮複核後的現況對照

spec／plan 全文複核，D1–D6 與原裁定一致；以下是後來被改掉、讀本 ADR 容易誤判的地方（依 ARCHITECTURE §1、§8 與 `core/crypto.py`）：

- **AAD 與 owner**：原不變式「AAD=user_id」已由 Halt 1 改為 canonical `ExchangeAccount.id` UUID（`encrypt_secret_with_aad`），credential 屬於 exchange account 而非登入 user；`user_id` 版本只剩相容包裝。
- **D5 解密歸宿擴大**：daemon 不再讀 `BFX_API_KEY`／`BFX_API_SECRET`，改由 account-owned credential vault 解密，所以 `BFX_VAULT_KEK` 現在同時在 webapi 與 daemon 的環境裡；「v1 唯一解密路徑 = verify」已不成立，KEK 外洩面相應擴大。
- **KEK 輪替仍未支援**：`_CURRENT_KEY_VERSION = 1`，decrypt 不看 `key_version`；上方 Followup 的警告仍有效。
- plan 內的 review amendment（不新增 dead `bfx_vault_kek` setting、跨方言 default、`webapi.env` 是整檔 `cp` 不是 `source`）已落在程式與 R1，無其他未記裁定。

## Related

- 原 spec：`2026-06-07-sp2-bfx-key-vault-design.md`（committed `0e24448`）；plan `2026-06-07-sp2-bfx-key-vault.md`（`496b1a3` + pre-flight 修正 `fc8d968`）（原文已不在 repo，本 ADR 即紀錄）。
- 來源：
  - spec：`2026-06-07-sp2-bfx-key-vault-design.md`（原文已不在 repo，本 ADR 即紀錄）
  - plan：`2026-06-07-sp2-bfx-key-vault.md`（原文已不在 repo，本 ADR 即紀錄）
- Epic ADR：[2026-06-06-frontend-saas-architecture](2026-06-06-frontend-saas-architecture.md)；部署拓樸：[2026-06-07-web-tier-hosting-funnel-scoped-roles](2026-06-07-web-tier-hosting-funnel-scoped-roles.md)。
