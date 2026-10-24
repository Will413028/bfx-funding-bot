---
title: live 級品質基線的範圍：不做與延後的項目、設計審查後刻意保留的四個機制、接受的兩個缺陷
date: 2026-10-24
status: active
tags: [bfx-funding-bot, decision, quality, ci, security, operations]
related-commits:
  - "de01d4b2^..111fb8a8"
---

# live 級品質基線：範圍、延後項與刻意保留的機制

## Context

2026-10-11 確認本 repo 的品質級別是 live、風險是 money 與 credentials：bot 自 2026-05-26 以真錢運作，但只有 owner 自己的小額資金，對外 SaaS 未上線。2026-10-12 起把 repo 補到這個級別（PR #187–#304），最後由獨立設計審查（2026-10-24）檢查新加的機制。本 ADR 記錄三類不會從程式碼看出來的決定：哪些品質項不做或延後、審查提出但決定保留的機制、以及接受不修的缺陷。

**約束**：

- `external` 單一 operator、沒有外部使用者與流量；產品沒有 LLM、沒有 ORM relationship。
- `external` GitHub 會停用 60 天沒有 commit 的 repo 的排程 workflow；Pages Direct Upload API 沒有正式文件。
- `inherited` 品質規範的 live 級清單：維護者自訂的規則，仍成立，但每項可附理由不做或延後。

## Options Considered

- **基準. live 級清單逐項做滿**：品質頁 Checklist；OWASP ASVS、Google SRE workbook 等各項的來源。
- **A. 照基準全做**。
- **B. 每項判定為已有、不適用、延後（附觸發條件）或不做（附理由），延後與不做明列（採用）**。
- 設計審查的四項（見 D3）各自是「保留現有機制」對「現在就換成審查建議的做法」。

## Decision

- **D1 不做**：負載測試與 Lighthouse 前端效能預算（沒有流量）；coding agent 的 host 權限（host 設定，不是 repo 品質項）；pre-push hook（PR 加 required checks 已是失敗點）。
- **D2 延後**（觸發條件）：
  - 無障礙與可用性 → Phase B（主控台給第二個人用時）。
  - webapi 的 Prometheus metrics → Phase B，或 webapi 發生效能事故。
  - 部署後的流程級 smoke（需要 operator 身分）→ Phase B，或發生一次「health 綠燈但功能壞掉」的部署事故。
  - 抽出共用的檢查 repo → 第二個自有 repo 完成供應鏈基線，或 owner 同意建 repo。
  - `tests/` 的型別檢查 → 依賴方向與設計反模式那一輪品質工作。
  - 上線前外部 pentest → 列入 [2026-08-31](2026-08-31-account-isolated-execution-target-architecture.md) D7 的外部 beta gate（2026-10-15 安全審查的衛生項）。
- **D3 設計審查後保留**（觸發條件）：
  - 上傳端自己用 stdlib 實作 Pages Direct Upload，不用 wrangler → 上傳後驗證或每日檢查第一次因 API 改變而失敗時，改用釘 digest 的 wrangler image。
  - 接受 GitHub 停用閒置 repo 的排程 → repo 30 天沒有 commit，或第一次發現排程被停時，加 dead man's switch（成功時 ping healthchecks.io）。
  - CI 以排除式跳過需要網路的 marker（`not live and not venue_contract and not public_site`）→ 出現第四個需要網路的 marker 時，改成一個 `network` 母 marker。
  - `pg_stat_statements` 的 extension 由 runbook 手動建立，不寫成 migration → 第二台主機或 DR 演練發現缺 extension 時改成 migration。
- **D4 接受的缺陷**：
  - F45：webapi CSP 的 `style-src` 含 `'unsafe-inline'`（ZAP 10055，Medium）→ UI 開放給 tailnet 外的使用者時，改成 webapi 每個請求產生 nonce。
  - F9：tracked 的 `deploy/vm/*.env` 沒有逐行人工核對 → 依賴 gitleaks 全歷史掃描（required check）。

## Rationale

- **D1、D2**：B 而非 A：清單的每一項都有成本，在單一 operator、沒有流量的條件下，A 的負載測試與無障礙等項沒有可量的收益；明列觸發條件讓延後不會變成遺忘。**代價**：觸發條件靠人記得檢查，Phase B 開工時要回頭看這份清單。
- **D3**：四項都已有保護或失敗時會自己暴露：Direct Upload 壞掉時上傳驗證與每日檢查都會告警；排程被停的風險在 repo 每天有 commit 時很低；排除式在第三個 marker 時仍好讀；extension 在 fresh-host 步驟裡。現在換掉的成本（VM 多一個 Node image、多一個外部 ping、改 marker 結構、owner 權限的 migration）高於它們擋下的風險。**代價**：每項都有一個要等事件才觸發的重評點。
- **D4**：F45 要先有 HTML 注入才能利用，`script-src` 已用 hash 鎖住，UI 只在 tailnet 給 operator；直接拿掉 `'unsafe-inline'` 會擋掉 Radix 執行期建立的鎖捲動樣式（讀 bundle 推論，未在瀏覽器實測），改 nonce 要動 webapi 的 static 回應。F9 的人工核對沒有可重複的檢查，gitleaks 每次都跑。

## Result

- `git log --oneline --first-parent de01d4b2^..111fb8a8`：PR #187 起到 ASVS 公開版 #304。required checks 12 個（ruleset，2026-10-24）。
- 盤點的每一項都是已有、不適用、延後或不做；VM 未證的項目為 0（2026-10-24 驗收）。

## Revocation Triggers

- 開始 Phase B 或開放外部使用者 → 逐項重看 D2 的延後項與 D4 的 F45。
- 每一條 D2、D3、D4 的觸發條件各自成立時 → 照該條處理。

## Related

- 壓縮自 gitignored 的品質基線 Prompt 3 計畫（2026-10-12 起）的範圍段、盤點附件與步驟 16 的設計審查紀錄；該目錄從未進版控，內容僅存本 ADR。
- 同一輪的其他決定：[2026-10-12-error-tracking-halt-detection-and-ci-alert-channel](2026-10-12-error-tracking-halt-detection-and-ci-alert-channel.md)、[2026-10-15-asvs-l2-scope-and-disclosure-in-a-public-repo](2026-10-15-asvs-l2-scope-and-disclosure-in-a-public-repo.md)、[2026-10-15-tailscale-native-operator-ingress-and-cli-only-high-risk-operations](2026-10-15-tailscale-native-operator-ingress-and-cli-only-high-risk-operations.md)、[2026-10-20-network-namespace-held-by-a-holder-container](2026-10-20-network-namespace-held-by-a-holder-container.md)、[2026-10-21-single-vm-least-privilege-by-db-role-not-network-segmentation](2026-10-21-single-vm-least-privilege-by-db-role-not-network-segmentation.md)、[2026-10-23-slo-error-budget-and-delivery-metrics](2026-10-23-slo-error-budget-and-delivery-metrics.md)、[2026-10-24-public-site-own-build-and-account-level-pages-token](2026-10-24-public-site-own-build-and-account-level-pages-token.md)。
