---
title: 冷啟動時重播 scheduler 跳過的那個 boundary，重啟不再損失最多一小時的 quote
date: 2026-09-23
status: active
amends: "[2026-05-29-deployment-reconciler](2026-05-29-deployment-reconciler.md)"
tags: [bfx-funding-bot, decision, marketfeed, standing-quote, deployment, release]
---

# 冷啟動重播被跳過的 boundary

## Context

[2026-05-29-deployment-reconciler](2026-05-29-deployment-reconciler.md) D1 讓 signal 層只在 1h candle boundary 寫 `StandingQuote`、reconciler 朝它收斂。
`StandingQuoteStore` 是 in-memory、冷啟動為空，而 `Scheduler.register_from_now` 只掛**下一個** boundary——
07:01 起來的 process 要等到 08:00 才有 quote。當時這只是「重啟後晚一點開始放貸」。

2026-09-22 它變成死結：release ceremony 需要的 DR 收據只活 900 秒，而刷新收據一定要停容器跑 DR 演練，
等於一定重啟 bot——收據 15 分鐘 vs quote 最多 60 分鐘，兩者永遠對不上。`halt2_cutover` 註解記的
「2026-09-20 開了五個窗口、四個過期」就是這個時鐘。

## Options Considered

- **A. 維持現狀、精算刷新時機**：零 code 變更；要讓 bot 恰好在整點前 1–2 分鐘起來，失手就再等一小時。
- **B. quote store 持久化到 PostgreSQL**：重啟後讀回；但會把一個已過 TTL 的舊 quote 帶進新 process，
  並新增一張需要 schema、遷移與失效規則的表。
- **C. 開機時重播被跳過的 boundary（採用）**：對每個 cell 以 `last_candle_close_mts` 呼叫**同一條**
  `on_scheduler_tick`，quote 由當下已封存的 candle 重算。

## Decision

- **D1'（extends D1）**：採 C。只重播 `register_from_now` 會跳過的那一個 boundary，走與整點 tick 完全相同的路徑；
  失敗 fail-open（log 後照常開機，退回原本的冷啟動行為）。
- **D2**：重播的 quote 以 **boundary 時間**作 `created_at_ms`，而非開機時間——否則一個已花掉 59 分鐘 TTL 的
  boundary 會拿到一份全新的 TTL，reconciler 會以為一小時前的訊號是新的。整點 tick 的行為不變。

## Rationale

- **選 C 而非 A**：A 把一個結構性衝突交給人手計時；今天實際因此錯過兩個窗口。
- **選 C 而非 B**：boundary 的輸入（已封存的 candle、已 warm 的 strategy registry）在開機時本來就都在，
  quote 是它們的純函數——重算比保存更便宜也更正確，且不必處理「讀回的是否已過期」。
- **為何必須走同一條路徑**：另寫一套重建邏輯，只要與整點 tick 有任何分歧，就比不重建更糟。
- **代價**：開機多一次策略計算，並多出兩筆 SIGNAL／DECISION 事件——signal 層只寫 `StdoutEventSink`、
  **不寫 ledger**，所以增加的是 telemetry，不是歷史，也不影響 projection 或重播。

## Expected Outcome

- 部署或刷新收據後，quote 在開機當下即存在。實測：`2365254` 上線後 27 秒出第一個決策（前一版同情境等 9 分鐘到整點）；
  此後每次部署／刷新皆 14 秒內 `standing_quote_rehydrated`。
- release ceremony 不必再對整點，收據的 ~14 分鐘窗口可完整使用。

## Revocation Triggers

- signal 層開始寫 ledger（DECISION 進 event_log）→ 重播會產生重複歷史，需改為先查該 boundary 是否已有決策。
- 出現「重播的 quote 與同一 boundary 的整點 quote 不一致」→ 路徑已分歧，停用重播。

## Lessons

### Rules

- **R1**：兩個各自合理的時間常數（收據 900 秒、quote 1 小時 boundary）只有在同一個流程裡相遇時才衝突，
  而那個流程（刷新收據必然重啟）在兩者各自設計時都不存在。`Rule: 為任何有時效的證據或狀態訂 TTL 時，
  列出「取得它的程序會讓哪些其他狀態失效、那些狀態多久才回來」；後者比前者長就是結構性死結。`

## Related

- 延伸 [2026-05-29-deployment-reconciler](2026-05-29-deployment-reconciler.md) D1（standing quote 只在 boundary 刷新）
- 衝突的另一端：[2026-09-19-dynamic-capital-and-release-canary](2026-09-19-dynamic-capital-and-release-canary.md) 的 DR 收據 900 秒窗口
- 實作 commit `2365254`；測試 `tests/modules/marketfeed/test_standing_quote_rehydrate.py`
- 本 ADR 即原始紀錄，無外部來源文件
- Lessons R1 的通用概念：fail-closed guard 組合死鎖
