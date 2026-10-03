---
title: Offsite DR 的 Terraform 與 VM runtime secret ownership boundary
date: 2026-09-05
status: active
tags: [bfx-funding-bot, decision, terraform, cloudflare-r2, security, deployment]
---

# Offsite DR 的 Terraform 與 VM runtime secret ownership boundary

## Context

PR [#16](https://github.com/Will413028/bfx-funding-bot/pull/16) 已合併，落地
Offsite DR 的 Terraform module、VM bootstrap、fail-closed timers 與 runbook。
production archive／R2 secret boundary／fresh backup 與 isolated restore 已執行，
但 Halt 2 的 measured restore RTO 尚未達標；仍不能讓 secret 進 repo、Terraform
state、plan、image、shell history 或 log。

## Options Considered

- **A. Hybrid ownership（採用）**：Terraform 管理可宣告且不含秘密的 R2 bucket／lifecycle；operator 或 VM-only wizard 管理 runtime token、pgBackRest secret 與 activation。
- **B. Terraform 全包**：Terraform 同時建立 bucket、token、VM secret 與 timers。集中且可重現，但 state／plan 與 provider permissions 會成為高價值 secret 及跨平台耦合邊界。
- **C. 全部 operator／VM 手動**：secret 與 activation 不進 IaC state，初始操作直觀；代價是 bucket policy、lifecycle 與 drift 難以重現，且容易遺漏審計證據。

## Decision

採用 **A. Hybrid ownership**，一個 resource 只能有一個 writer：

- Terraform 只建立 private Standard R2 bucket 與保守的 incomplete-multipart lifecycle；不建立 runtime token、不讀 secret、不把 secret 傳入 module。
- Cloudflare management token 只由 operator process environment 提供；不寫 `.tfvars`、module variable、plan、state 或 log。
- R2 runtime token 由 operator 在指定 bucket scope 建立，透過 VM-only secret wizard 注入；pgBackRest repository secret 同樣只進 VM secret fragment。
- Terraform 只輸出 timer／unit 定義與檢查所需的 non-secret wiring；enable／start 必須在 token、archive、health check 與 restore evidence gates 通過後另行執行。
- pgBackRest 擁有 WAL/base-backup retention；R2 lifecycle 只處理 incomplete multipart，避免兩套 retention writer 互相刪資料。
- Terraform state 放獨立的 remote backend／state bucket，**不得**與 pgBackRest bucket 共用（runtime token 刻意只限該 bucket）；Cloudflare management token 與 R2 S3 runtime credential 是兩類憑證，不可互換。
- Runtime token 輪換順序固定為 create-new → 注入 VM → validator → smoke → isolated restore → revoke 舊 token；Terraform 不自動 destroy 舊 token。timer installer 只裝 unit 定義（`daemon-reload` 後驗證仍 disabled／inactive），啟用只在 runbook 的量測 gate 之後。

## Rationale

相較 Terraform 全包，Hybrid 讓 bucket/lifecycle 保有可審查與可重現的 IaC，
同時把最敏感的 token、VM secret 與 activation 留在最小 runtime boundary；代價是
需要 operator wizard 與 provisioning checklist。相較全手動，Hybrid 犧牲少量初始
流程複雜度換取 ownership、drift 與 review 的清晰度；不選把 secret 塞進 state
或 plan，因為 state 的長期保存面會大於本次 DR 的必要暴露面。

## Expected Outcome

同一個 resource 不會被 Terraform 與 VM script 互相覆寫；reviewer 可在不取得 secret
的情況下審查 bucket/lifecycle plan。production activation 仍由 evidence gate 解鎖，
並能在 isolated restore 後驗證 RPO、RTO、event chain 與 projection rebuild。

## Followup

- ✅ 以 Terraform apply 建立 bucket 與 incomplete-multipart lifecycle，保存不含 secret 的 plan／apply evidence。
- ✅ 由 operator wizard 建立 bucket-scoped runtime token，注入 VM-only fragment，並完成 permission／R2 smoke test。
- ✅ 設定 pgBackRest archive／retention；fresh full/diff backup 與 preflight measured RPO=`1s`、failed archive count=`0`。
- ✅ 不連 Bitfinex 的 isolated restore drill：event chain／projection rebuild 通過；full restore RTO=`202s` 超過當時的 Halt 2 `≤60s` gate。2026-09-12 amendment 已改採 `≤3600s`，但仍需 fresh evidence 與 canary gates 才能 activation。
- 設定 pgBackRest archive／retention、timer 定義；完成 fresh archive health evidence 後才 enable／start。
- 執行不連 Bitfinex 的 isolated restore drill，量測 RPO ≤5m、restore RTO ≤60m 的 Halt 2 gate 與較寬的 DR acceptance。

## Invariants

- Secrets 不得出現在 repository、Terraform state、plan、image、shell history 或 log。
- pgBackRest 是 backup retention 的唯一 writer；R2 lifecycle 不得刪除已完成的 archive objects。
- 未通過 token／archive／restore evidence gates 前，production timers 必須維持 disabled。

## Revocation Triggers

- Terraform provider 或 state backend 無法證明 secret-free plan/state。
- R2 token scope、lifecycle 或 pgBackRest retention 需要第二個 writer 才能運作。
- isolated restore 顯示 RPO/RTO、event chain 或 projection rebuild 不可接受。

## Review Notes

2026-09-05：Will 核准採用方案 A（Hybrid ownership）；PR #16 已 merge，但本 ADR 的 production followup 尚未執行。
2026-09-12：Terraform/R2、scoped VM secret、archive、backup/preflight、identity migration/quiescence 與 isolated restore 已完成；fUST-only projection apply 可重跑且 idempotent，fUSD 維持 dark。full restore RTO=`202s` 未過當時的 Halt 2 `≤60s` gate；後續 amendment 改採 `≤3600s`，但 fresh evidence、permit、canary、reconcile 與 operator resume 仍未完成，timers 與 Bitfinex write 維持停止。

## Related

- [2026-09-04-offsite-dr-cloudflare-r2](2026-09-04-offsite-dr-cloudflare-r2.md)
- [PR #16](https://github.com/Will413028/bfx-funding-bot/pull/16)
- SDD spec：`2026-09-05-offsite-dr-terraform-design.md`（原文已不在 repo，本 ADR 即紀錄）
- SDD plan：`2026-09-05-offsite-dr-terraform-bootstrap.md`（原文已不在 repo，本 ADR 即紀錄）
- 本 ADR 亦是 user／agent 討論的原始決策紀錄，沒有另一份外部會議文件。
