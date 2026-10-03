---
title: 資金授權讀取改為「acceptance 證明前綴、讀取只 fold fence 之後」，以 prefix hash 綁定 snapshot
date: 2026-09-20
status: "superseded-by: [2026-09-28-ledger-journal-and-venue-mirror-replace-event-sourcing](2026-09-28-ledger-journal-and-venue-mirror-replace-event-sourcing.md)"
tags: [bfx-funding-bot, decision, capital, safety, performance, event-sourcing]
related-commits:
  - 3dfc49f
  - 3b8b735
  - 68e88cb
  - f922b2f
  - 5cd5b61
  - 9b97531
  - 5d018fc
  - 17d7387
---

# 資金授權讀取以 prefix hash 綁定 snapshot、只 fold fence 之後

## Context

prior state：[2026-09-19-dynamic-capital-and-release-canary](2026-09-19-dynamic-capital-and-release-canary.md) D3 的資金 authority 每次讀取都從零重推：
`_read_capital` 把全部 `RESERVATION_INTENT`（當時 3,703 筆）反序列化兩次，`_historical_cycles` 每次讀整段 event log。
2026-09-20 production `POST /admin/dry-evaluate` 顯示 `capital_policy eval_timeout >2.0s`，**每筆 offer 都被擋**；
`bfx-bot` 單核 ~100%、PostgreSQL ~0%、連線 `idle in transaction`——成本全在 Python 反序列化，且隨歷史線性成長。

**約束**：

- `inherited` guard 2 秒 timeout 一律 fail-closed（`GUARD_EVAL_TIMEOUT_SECONDS`）：仍成立，timeout 必須擋，但不該是正確性邊界。
- `inherited` 不變式：不可變 intent 定義資金宇宙、projection 缺列是損毀而非現金釋放、fence 前的歷史 cycle 需精確終態證據、任何讀不到／不符都 fail-closed：仍成立，來自真錢帳務。
- `inherited` 「cursor 不是完整性證明」（cache-not-source-of-truth）：仍成立。
- `external` `event_log` append-only，PL/pgSQL `guard_capital_event()` 拒絕更新帶 capital 欄位的列。

## Options Considered

- **基準. event sourcing 的 snapshot＋tail fold**：定期把聚合狀態存成 snapshot，讀取從 snapshot 往後重播新事件（[Microsoft Event Sourcing pattern](https://learn.microsoft.com/en-us/azure/architecture/patterns/event-sourcing) 的 snapshot 段）。
- **A. 維持每次讀取全量重推**：正確性最直觀；工作量隨歷史成長，fail-closed timeout 會把它**無聲地**變成全面阻斷。
- **B. 新增 `capital_positions` 物化表**：讀取快；但複製一份 `classification`，多出一個能與 snapshot 不一致的東西——這正是要消除的失敗模式。
- **C. 以 cursor 標記「已處理到哪」**：簡單；cursor 無法證明其下的列沒被弄丟。
- **D. 既有 `capital_snapshots` 綁定 ledger 前綴的 content hash，讀取只 fold fence 之後（採用）**：基準的具體化，snapshot 就是那個 snapshot，不另開表。

## Decision

- **D1 = plan Task 1**：rolling `prefix_hash = H(prev ‖ canonical_event_record)` 由 `AccountEventWriter` 在唯一 append 入口維護，存在**旁邊的** insert-only `event_prefix_hashes`（不寫在 `event_log` 上，因為 append-only trigger 會拒絕、歷史列也永遠封不上）；backfill 在 migration 內決定性完成，缺鏈一律擋。
- **D2 = plan Task 2**：`capital_snapshots.covered_prefix_hash` 記 acceptance 當下 evidence event 的前綴 hash；acceptance 另持久化 legacy intent 已在 fence 前終結的證明與 `classification["settled"]`；歷史未結清時**記錄裁決**（`authorization_blocked_reason`）而非拒絕觀測。
- **D3 = plan Task 3**：讀取驗 `covered_prefix_hash`（不符 → `snapshot_prefix_diverged`），然後只 fold `event_seq > command_fence` 的 tail；以 `reflected | settled` 集合縮小，不在讀取時做 per-attempt 推導。
- **D4 = plan Task 4 re-scope**：全量重推就是 acceptance（約每 90 秒一次），**不在 reconcile tick 再加一次**；缺的是「前一 snapshot 帶到 tail 的帳」與「新推導」的比對，列為 Followup。
- **D5 = plan Task 5**：timeout 降為病態偵測；guard 用掉 `GUARD_EVAL_WARN_FRACTION`（0.5）預算即記 `guard_slow` 並**仍放行**。

## Rationale

- **選 D 而非 A**：A 的代價不是慢，而是失敗模式——無界工作搭 fail-closed timeout，歷史一長就靜默變成「全部擋掉」，第一個訊號就是全面停擺。D 讓讀取結構上有界，timeout 回到只抓病態。
- **選 D 而非 B**：B 的第二份資料需要自己的一致性證明；D 把證明綁在已存在的 snapshot 列上，驗證只讀一列（evidence event 本來就會載入）。
- **選 D 而非 C**：cursor 只說「處理到哪」，prefix hash 說「處理的就是這段內容」——少一列、改一列都會讓 hash 不符而擋下。
- **prefix hash 蓋不到的事**：它偵測前綴被竄改，不證明前綴裡的 intent 已結清，所以 D2 的結清證明必須另外在 acceptance 做完並存下。
- **D4 不加第二次重推**：兩條 fold 同基礎、只差 fold 範圍，fence 前已在 acceptance 證明、`_assert_no_unknown` 擋住未結 attempt；差異窗口只有一個 reconcile 週期，由 CI differential 測試釘住。代價：runtime 比對尚未實作，在那之前是縱深防禦少一層。
- **D5 代價**：`guard_slow` 放行意味著慢但未超時的 guard 不擋單；換到的是第一次退化就有訊號。

## Result

- `git log --oneline 3dfc49f^..9b97531 -- backend_py/src backend_py/alembic docs`（當時目錄名 `backend_py/`；含 `5d018fc` 結清證明、`17d7387` guard_slow）；`test_capital_read_work_does_not_grow_with_history` 由 25 vs 769 次反序列化（歷史放大 32 倍）變為有界，strict xfail 轉 XPASS 後移除標記。
- 通則已寫入 repo `backend/ARCHITECTURE.md` §4「授權與稽核分離」：任何需要走訪歷史的判斷在 acceptance 做完並存結論，授權路徑只讀結論。
- 同一條前綴鏈後來支撐每月還原測試的 prefix 比對（`d75b288`，[2026-09-04-pgbackrest-in-postgres-image-and-staged-isolated-restore](2026-09-04-pgbackrest-in-postgres-image-and-staged-isolated-restore.md) D5）。

## Followup

- `accept_snapshot` 內比對「前一 snapshot 帳目帶過 tail」與「新推導 classification」，不符即停機（原 plan：actor `reconcile`、reason `capital_position_diverged`；現行模型下屬第 3 級 `HALTED/auto`；未實作）；需先有「注入 classification 竄改必被偵測」的 RED 測試。
- guard 層的 bounded-work 斷言（數 query／工作量而非牆鐘）尚未接到 `capital_policy` guard 本身。

## Revocation Triggers

- acceptance 本身的全量推導逼近 reconcile 週期或 guard 預算 → 把歷史證明改為增量（每次 acceptance 只證明新增前綴）。
- 出現 prefix hash 相符但資金判斷錯誤的案例 → 重評 D2 的「acceptance 證明」涵蓋範圍。

## Lessons

### Rules

- **R1**：無界工作放在 fail-closed timeout 後面，歷史一長會靜默變成全面阻斷，且沒有任何退化訊號。Rule: 授權熱路徑的工作量必須與歷史長度無關（以計數型測試釘住），timeout 只當病態偵測，並在用掉一半預算時先發告警。

## Related

- 來源（Provenance）：plan `2026-09-20-capital-authority-bounded-reads.md`（原文已不在 repo，本 ADR 即紀錄）。量測數字（3,703 intents、CPU／連線狀態）取自該 plan 的 Evidence 段；2.31s vs 2.0s 取自 repo `backend/ARCHITECTURE.md` §4。
- 上層：[2026-09-19-dynamic-capital-and-release-canary](2026-09-19-dynamic-capital-and-release-canary.md) D3（snapshot fence＋兩次穩定觀測）。

## Updates (2026-09-28)

- 由 [2026-09-28-ledger-journal-and-venue-mirror-replace-event-sourcing](2026-09-28-ledger-journal-and-venue-mirror-replace-event-sourcing.md) 取代；現行程式在該 ADR 的遷移 S3 完成前仍依本 ADR 運作。
