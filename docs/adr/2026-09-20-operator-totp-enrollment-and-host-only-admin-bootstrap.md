---
title: Operator TOTP 由第一方設定頁直接走 Better Auth 登錄，首位 admin 由 host-only 稽核 CLI 指派
date: 2026-09-20
status: active
tags: [bfx-funding-bot, decision, security, auth, frontend]
related-commits:
  - e9f1369
  - ea20c82
  - 6f30ad0
  - 8c49ff6
---

# Operator TOTP 自助登錄＋host-only 首位 admin bootstrap

## Context

prior state：[2026-06-06-frontend-saas-architecture](2026-06-06-frontend-saas-architecture.md) D3／D6 選了自托 Better Auth 與「動錢操作要 TOTP」；
private `/api/v1` 只接受 `BFX_OPERATOR_USER_ID` 指定、role=`admin` 的使用者。Better Auth 1.6.14 已內建
enable／verify TOTP，但 app 只做了登入時的驗證頁：Settings 依賴需要 MFA 的 backend `/me`，而還沒登錄
TOTP 就拿不到 MFA → **登錄流程自己鎖死**；也沒有可稽核的首位 admin 指派工具。放貸恢復的前提之一就是 operator 完成 TOTP。

**約束**：

- `external` 控制面經 Tailscale Funnel 公開；TOTP 是恢復類與動錢操作的授權因子。
- `external` 人類秘密（密碼、TOTP URI、backup codes）不得進 agent 對話、log 或第三方服務。
- `inherited` Better Auth 1.6.14 pinned、不在本次升級（[2026-06-06-frontend-saas-architecture](2026-06-06-frontend-saas-architecture.md)）：仍成立，升級是另一個風險面。
- `inherited` 只有一個 operator、沒有自助註冊（Release 0 containment）：仍成立，尚未上線。

## Options Considered

- **基準. 直接用 auth 函式庫的 2FA 與 admin 工具**：Better Auth 2FA plugin 以 client `twoFactor.enable` → `verifyTotp` 完成登錄，role 由 admin plugin 或 seed script 指派（[Better Auth 2FA](https://better-auth.com/docs/plugins/2fa)）。A 即此基準落在本 repo 的形狀。
- **A. 第一方 `/settings/security` 頁＋host-only 稽核 bootstrap CLI（採用）**。
- **B. 瀏覽器 console snippet 呼叫 SDK**：零程式碼；不可重複、無測試、秘密可能留在 devtools。
- **C. 自助升級成 admin 的 HTTP route**：一次到位；在公開控制面新增一條權限提升路徑。
- **D. 直接在 DB 寫 `twoFactorEnabled`／verified 旗標**：最快；等於偽造認證狀態，TOTP 形同虛設。

## Decision

- **D1 登錄頁**：`/settings/security` 以 server-side 新鮮 session（`disableCookieCache`）判定，只准 `BFX_OPERATOR_USER_ID` 且未 banned，**不要求 role 或 MFA**（打破鎖死）；不經 Python/BFF。`twoFactor.enable({password})` → 本地產生 SVG QR（不送外部 QR 服務）→ 確認已存 backup codes → `verifyTotp({trustDevice:false})`；只有真的驗證成功且重新取得的 session 顯示已登錄才算完成。秘密只存在元件 state，取消／卸載／完成即清；不進 localStorage、URL、query cache、telemetry。已登錄者只看狀態，重設／停用／passkey 不在範圍。
- **D2 恢復路徑**：`/two-factor` 登入頁加 backup code 驗證，不開 trusted device。
- **D3 首位 admin**：`frontend/scripts/bootstrap-operator.mjs`（`pnpm auth:bootstrap-operator`），不是 HTTP route。預設 dry-run；apply 需 `--apply --confirm-bootstrap-admin` 與新的專屬 audit 檔。在有界交易＋表鎖內要求目標恰為設定的 operator、有 credential、`twoFactorEnabled=true` 且恰一筆已驗證 TOTP，已有其他 admin 就拒絕；只改 role，不碰認證旗標與金流表；同一唯一 admin 重跑為冪等。commit 後只撤該 operator 已知的 Redis session／MFA marker（不全域 flush）；清理部分失敗明示為可重試，沒有完整 audit 不算成功。
- **D4 順序**：先部署技術服務（維持 halt）→ 人自己登錄 → 確認 DB 已有真實登錄 → 才跑 bootstrap apply；non-operator containment 另跑既有工具。

## Rationale

- **選 A 而非 B**：B 無法測試、無法重複，秘密流經 devtools；A 的狀態機（password → verify → complete）有 UI 與真實 Better Auth 整合測試。代價是多一頁與一個本地 QR 依賴（exact pin）。
- **選 A 而非 C**：C 在公開面新增提權路徑，一個 bug 就是帳戶接管；CLI 只能由有 host 與 DB 存取的人執行，攻擊面留在 VM 內。代價是首次設定要 SSH。
- **不選 D**：TOTP 的價值在「真的有人持有 secret」；手寫旗標讓後續所有 MFA 檢查都建立在假狀態上。
- **D1 不要求 MFA 才能登錄 MFA**：這是鎖死的根源；以「精確 operator ID＋新鮮 session＋未 banned」取代，登錄後其餘 API 照舊要求 MFA。
- **D3 的冪等與稽核先行**：先保留 audit 再動 DB，commit 後清理失敗也能重試到完成，不會留下「role 改了但沒紀錄」的狀態。

## Result

- `git log --oneline e9f1369^..8c49ff6 -- frontend/scripts/bootstrap-operator.mjs frontend/src`；程式在 repo `frontend/src/app/[locale]/(dashboard)/settings/security/`。
- 人工登錄與 production bootstrap apply 是否已完成：本 ADR 未查證（依設計屬人工步驟）；2026-09-25 起 resume／kill／幣別開關皆經 TOTP，見 [2026-09-25-lending-envelope-replaces-probation-and-account-halt](2026-09-25-lending-envelope-replaces-probation-and-account-halt.md) D4。

## Revocation Triggers

- 開放多使用者／自助註冊（Phase 5）→ admin 指派改為正式的角色管理與第二人核准，CLI 只保留 break-glass。
- 升級 Better Auth 主版本 → 重驗登錄後 session rotation 與 MFA proof 的行為。

## Related

- 來源（Provenance）：
  - spec：`2026-09-20-operator-onboarding-design.md`（原文已不在 repo，本 ADR 即紀錄）
  - plan：`2026-09-20-operator-onboarding.md`（原文已不在 repo，本 ADR 即紀錄）
- 前置：[2026-06-06-frontend-saas-architecture](2026-06-06-frontend-saas-architecture.md)（Better Auth、TOTP 範圍）；操作步驟在 repo `docs/runbooks/release-0-operator-containment.md`。
