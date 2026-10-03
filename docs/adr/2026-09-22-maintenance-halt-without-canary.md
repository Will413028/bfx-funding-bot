---
title: 維護性 halt 不需真錢 canary——canary 改綁 binding 而非 halt epoch
date: 2026-09-22
status: active
amends: "[2026-09-19-dynamic-capital-and-release-canary](2026-09-19-dynamic-capital-and-release-canary.md)"
tags: [bfx-funding-bot, decision, safety, deployment, halt, canary]
---

# 維護性 halt 不需真錢 canary

## Context

[2026-09-19-dynamic-capital-and-release-canary](2026-09-19-dynamic-capital-and-release-canary.md) 的 D4 讓 release canary 綁定含
**halt epoch**，實作上是 `check_normal` 要求 `promoted.promoted_halt_id == halt.id`。
每次 halt 都產生新 id，所以**任何暫停都作廢既有 promotion**，即使 build 一個位元都沒變。

2026-09-22 實戰撞到後果。halt id=7 的理由是「operator-approved PostgreSQL 18.6 upgrade
and R2 identity-cutover preparation」——純維護暫停。要結束它必須：建 artifact、傳 1.5GB、
跑隔離還原演練、在 900 秒窗口內走完四步 ceremony，並**送出一筆真錢 canary**。
`trading_status.resume()` 本來就在，但 live 一律 `release_promotion_required`。
一個為了升級資料庫按下的暫停鍵，要靠送真錢出去才能彈回來。

同時發現 `27088ea`（authorize 時依當下 FX 重新推導金額）與 `b4e6f8a0c203` 的 trigger
（`minimum_amount` 於 prepare 後不可變）**互相鎖死**：FX 一漂，authorize 必然以
`immutable release binding` 失敗。兩者都為保護 operator，卻讓 ceremony 跑不完。

## Options Considered

- **A. 維持綁 halt epoch**：每次暫停後重新證明系統可用，最保守；代價是維護性暫停也要付一次
  真錢 canary 與完整 release 流程，而該證明與「暫停」本身沒有因果關係。
- **B. 改綁 binding ＋ halt 分類（採用）**：binding 已涵蓋 artifact／config／policy／schema／
  projector——canary 真正證明的全部對象。halt 加 `kind`，maintenance 由認證 resume 結束，
  safety／release 仍走 promotion；代價是維護暫停後不再重驗執行路徑。
- **C. 取消 canary**：最省事；代價是新 build 首次送單不再有任何有界的真錢驗證，放棄 D4 的核心價值。

## Decision

- **D4''（amends D4）**：canary 綁定**移除 halt epoch**，由 binding 單獨判定。`check_normal`
  取最新 promoted session 比對 binding：**build 變了才要新 canary，暫停次數不再影響**。
- **halt 分類**：`trading_halt` 加 `kind`（maintenance／safety／release）。maintenance 由
  authenticated resume（token ＋ `confirm=true`）結束；safety／release 仍需 promotion。
  既有 row 一律 `safety`——fail-closed，不從 reason 文字推斷。
- **trigger 解鎖**：`prepared → authorized` 允許更新 `minimum_amount`，其餘維持不可變。
- **不含**：ceremony 本身維持四步人工。做完上述後它很少觸發，而「人看過真單結果才放行」
  是 D4 的核心，不在本次放寬範圍。

## Rationale

- **選 B 而非 A**：A 把「這個 build 能不能下單」的證明，綁在「暫停過幾次」上。兩者沒有因果——
  暫停不會讓已驗證的 build 失效。代價是維護暫停後不再重驗執行路徑，但那條路徑在暫停期間
  並未改變，重驗只是重複既有結論。
- **選 B 而非 C**：新 build 首次送單仍值得一次有界的真錢驗證，那是 D4 至今成立的部分。
- **放棄了什麼**：維護暫停後不再有一次強制的端對端真錢驗證。若暫停期間**外部**環境改變
  （交易所 API 行為、憑證、網路），第一筆真單會是正常額度而非 canary 額度。
- **為何同時動 trigger**：不動它，B 也無法驗證——ceremony 在 FX 漂移時必然失敗，
  而 safety／release halt 仍要靠 ceremony 恢復。

## Expected Outcome

- 維護暫停後，operator 以認證 resume 直接恢復放貸，不產生任何 venue 寫入。
- 部署新 build 後，首次送單仍被 `release_promotion_required` 擋下，直到 canary 走完。
- authorize 在 FX 漂移時不再以 `immutable release binding` 失敗。

## Followup

- 既有 halt id=7 於 migration 後為 `safety`，須由 operator 明確改為 `maintenance` 才能直接
  resume；這是刻意的人工動作，不在 migration 內自動推斷。
- ceremony 四步是否進一步壓成一次批准，未裁定；等 D4'' 上線後看實際觸發頻率再評估。
- **本次部署暴露的同型問題**：`RELEASE_SCHEMA_HEAD` 與 `_READY_PROJECTOR_MIGRATIONS` 都是
  「隨每次 migration 手動更新」的硬編值，漏掉時分別在 artifact 內容與 daemon 啟動才爆
  （後者錯誤被包成 `one_shot_exit_nonzero:2`，要手動重跑容器才看得到真因）。兩者已補守門測試
  （`9b04515`、`8b96e17`）。**仍未守門的是 DB role 授權**——完全沒有 code-level SoT，
  見 [2026-08-31-account-isolated-execution-target-architecture](2026-08-31-account-isolated-execution-target-architecture.md) 的 D4' Amendment。

## Revocation Triggers

- 維護暫停後首筆正常單出現任何一次「環境在暫停期間改變而未被發現」的事故。
- `kind` 被誤填（safety halt 記成 maintenance）而讓不該恢復的狀態恢復。
- ceremony 觸發頻率仍高到讓 operator 想繞過它——代表綁定對象仍然抓錯。

## Review Notes

2026-12-22 檢查：維護暫停後直接 resume 用過幾次、有無因此漏驗而出事、`kind` 是否曾被誤填。

## Related

- 修訂 [2026-09-19-dynamic-capital-and-release-canary](2026-09-19-dynamic-capital-and-release-canary.md) 的 D4（halt epoch 綁定）
- [2026-08-31-account-isolated-execution-target-architecture](2026-08-31-account-isolated-execution-target-architecture.md)：control plane 與 execution
  plane 的 process boundary，及同日 D4' 的 operator 裁決 outbox Amendment
- 實作 commit `a7ed674`；migration `a7f3c1d9e204`
- 已用盡的 canary epoch 如何續開（safety halt 的出口）：[2026-09-23-operator-renews-spent-canary-epoch](2026-09-23-operator-renews-spent-canary-epoch.md)
- 2026-09-25 採用 [2026-09-25-automated-probation-replaces-release-ceremony](2026-09-25-automated-probation-replaces-release-ceremony.md)：以變更分級＋執行期限額取代每個 build 的人工 ceremony，DR 與發版脫鉤。
