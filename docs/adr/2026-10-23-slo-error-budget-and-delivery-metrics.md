---
title: SLO 附錯誤預算政策、上線清單是每個幣別開始真錢放貸前的關卡、交付指標以部署結束時服務是否停著判定失敗
date: 2026-10-23
status: active
tags: [bfx-funding-bot, decision, slo, operations, deploy, metrics]
related-commits:
  - 1e786bed
  - fc504e5a
---

# SLO、上線清單與交付指標

## Context

live 級要有服務目標（SLO）、上線前的放行清單、與交付結果指標（變更失敗率、恢復時間）。2026-10-23 開工時原計畫的前提有三處不成立：

- 原計畫的 SLO「只定目標，不定錯誤預算政策」，這是計畫自訂的縮減。
- 原本要整合的 `release-0-operator-containment.md` 在移除 app 登入後已失效，只剩轉址。
- 週報以 `bfx_research` 執行，讀不到 `deployments`；`failed` 狀態有 17 個寫入點，6 個沒動到正式環境、8 個讓服務停著、3 個看情況，`detail` 文字無法可靠分類（部分會加前綴）。

**約束**：

- `external` 單一 operator，真錢放貸目前暫停；目標是全自動放貸（`AGENTS.md`「營運原則」）。
- `inherited` 部署不改變交易狀態、migration 時 bot 與 webapi 全停（[2026-09-25](2026-09-25-ci-registry-digest-deploy.md) Amendment 2026-10-15）：仍成立，所以 migration 的計畫內停機是常態。
- `inherited` 研究與週報用自己的 DB role（[2026-10-15](2026-10-15-tailscale-native-operator-ingress-and-cli-only-high-risk-operations.md) Amendment 2026-10-21）：仍成立，加權限要用 SELECT。

## Options Considered

### SLO

- **基準. 目標＋量測查詢＋錯誤預算政策，告警依預算消耗速度**：Google SRE Workbook ch.2〈Implementing SLOs〉、ch.5〈Alerting on SLOs〉（multi-window burn rate）。
- **A. 只定目標與量測查詢**（原計畫）。
- **B. 目標＋查詢＋現值＋一段錯誤預算政策；burn-rate 告警延後（採用）**。

### 失敗部署怎麼判定

- **A. 解析 `failed` 列的 `detail` 文字**：不需 migration；文字無法可靠分類。
- **B. `deployments` 新欄位記錄結束時 bot／webapi 是否停著（來自部署工具的 `Attempt.bot_stopped`／`webapi_stopped`）（採用）**。

## Decision

- **D1 = 計畫 D16 Q1**：選 B。`docs/slo.md` 定 bot 存活率、reconcile 週期成功率、告警送達率；告警送達率只涵蓋 bot 內的告警，主機端另寫範圍。burn-rate 告警等恢復真錢放貸（規則在 homelab）。
- **D2 = 計畫 D16 Q2**：`docs/runbooks/launch-checklist.md` 是「某個幣別開始真錢放貸」前的放行關卡；release-0 轉址頁保留（README 與 ADR 連入，錨點不動）。
- **D3 = 計畫 D16 Q3**：選 B。新欄位 `deployments.service_degraded`，`bfx_research` 加 SELECT，報告腳本 `backend/scripts/report_delivery_metrics.py` 進週報；舊列為 NULL，報告從上線日起算。定義：失敗＝`rolled_back`＋讓服務停著的 `failed`；migration 的計畫內停機不算失敗；恢復時間用 DORA 2023 的 failed deployment recovery time（下一個 `deployed` 的 `finished_at` − 失敗列的 `finished_at`），rollback 以 `finished_at − started_at` 為上界並標明；逃逸到正式環境的 bug 數另列，來自 `docs/incidents/`。

## Rationale

- **D1**：只有目標沒有政策時，預算用完不會改變任何行為，等於沒有 SLO；政策一段就夠。burn-rate 告警延後：放貸暫停時消耗速度沒有意義，**代價**是恢復放貸前要先補規則。
- **D2**：放貸全自動，所以「上線」不是一次性事件，而是每個幣別第一次開真錢；把清單綁在這個事件上，fUSD 開啟時就會用到。
- **D3**：A 把分類建在會變的文字上，改一個前綴指標就錯；B 由部署工具在當下記事實，**代價**是一個 migration 與一個 SELECT 權限。不把 migration 停機算失敗：全停部署是刻意設計，算進去會讓指標懲罰正確做法。

## Result

- `git log --oneline -1 1e786bed`（PR #274：SLO、上線清單、資安事故 runbook、事故範本）；`git log --oneline -1 fc504e5a`（PR #275：`service_degraded`、報告腳本）。
- SLO 現值 2026-10-23 在 VM 量測：存活 98.63%、reconcile 99.40%、告警送達 100%，目標 98.5%／99%／99%。
- 第一筆 `service_degraded` 有值的部署在 PR #275 之後；如預期。

## Revocation Triggers

- 恢復真錢放貸 → 加 multi-window burn-rate 告警。
- 變成多實例或有外部使用者 → 重定 SLO 目標並把 webapi 納入。

## Related

- 壓縮自 gitignored 的品質基線 Prompt 3 計畫（2026-10-12 起）的 D16；該目錄從未進版控，內容僅存本 ADR。
- `docs/slo.md`、`docs/runbooks/launch-checklist.md`、`docs/runbooks/deploy.md`（`service_degraded` 的定義）。
