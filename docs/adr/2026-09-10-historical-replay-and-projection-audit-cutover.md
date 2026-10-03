---
title: Halt 2 recovery — 歷史 CID cycle 只在 stored-row replay 相容、舊投影封存後 event-only 切換、證據串流化、fUST-only scope
date: 2026-09-10
status: "superseded-by: [2026-09-28-ledger-journal-and-venue-mirror-replace-event-sourcing](2026-09-28-ledger-journal-and-venue-mirror-replace-event-sourcing.md)"
tags: [bfx-funding-bot, decision, event-sourcing, migration, disaster-recovery, execution]
related-commits:
  - "528dbe6^..3f86511"
---

# Halt 2 recovery — 歷史 replay 相容、投影封存切換、證據串流化

## Context

[2026-08-31-production-integrity-staged-clean-cutovers](2026-08-31-production-integrity-staged-clean-cutovers.md) 的 Halt 2 要求 empty projection 由 event chain 重建，
但 isolated restore drill 連兩道擋住：(1) production stream 有**歷史 CID 重用**——同一 CID 先 intent/claim/fill 一個
venue offer，之後又 intent/claim/fill 另一個；CID-keyed claim projector 視 venue binding 對整個 CID 不可變，verifier
報 `claim identity conflict`。(2) 修掉後 7,545 筆事件 replay 成功、hash 不變，但 `projection_replay_mismatch`：
`reconcile_observation` 還原來源 148,768 列、replay 0 列；`position_state` 2→1；`offer_claims`／`projection_heads` 同筆數但 content hash 不同。
舊 reconcile 把完整對帳結果存在 append-only checkpoint、`CREDIT_CLOSED` 為 audit-only，舊事件**本來就不足以**重建現況。

**約束**：

- `external` live 資料：歷史事件已落地且不可改寫（hash 被 DR 驗證綁定）；舊 checkpoint 含事件裡沒有的資訊。
- `external` 交易停在 persistent halt，恢復前需 Halt 2 RPO ≤300s／RTO ≤3600s 與 canary gates（見 [2026-09-04-offsite-dr-cloudflare-r2](2026-09-04-offsite-dr-cloudflare-r2.md) 09-12 amendment）。
- `inherited` event-only projector 與 live identity uniqueness（上游 ADR D4）；仍成立：放寬它等於重新打開 lost/duplicate 曝險。
- `inherited` 當時 fUSD 無資金、未啟用（[2026-06-01-per-currency-allocation-phase-2](2026-06-01-per-currency-allocation-phase-2.md)）；仍成立於 09-11。

## Options Considered

### 歷史 CID 重用

- **A. 只在 stored-row rebuild 解讀可證明的 cycle 邊界（選用）**。
- **B. 引入 claim-cycle identity，遷移 claim schema 與所有 consumer**。
- **C. 改寫 CID／payload、丟棄舊 fill 或 skip 失敗事件**。

### 舊投影與新基準

- **基準. 投影是可丟棄 cache，直接清空由事件重建**：event sourcing 的標準做法（[Fowler — Event Sourcing](https://martinfowler.com/eaaDev/EventSourcing.html)）。
- **D. 舊投影完整封存到獨立 audit schema，以新的 full-account venue snapshot 建基準，active 投影 event-only（選用）**。
- **E. 把舊 checkpoint 重新納入 live replay 輸入**。

### 差異證據容量（09-11）

逐欄 `Difference` 對 148,768 缺列至少 578,112,502 bytes（551 MiB），`read_private` 上限 64 MiB。

- **F. 提高單檔上限**。
- **G. 缺列收斂成單一 sentinel**。
- **H. v2 分塊證據目錄＋串流 writer/verifier，lock 內只驗 compact digest（選用）**。

## Decision

- **D1（claim replay spec，Option A）**：相容只授權給持久化 `event_log` row 經既有 stored-event provenance 邊界，不接受 boolean／env flag／caller event。
  前一 cycle 須由自身歷史事件證明 terminal（今日 snapshot 缺席不算），新 `RESERVATION_INTENT` 才開新 cycle，只在該點重設衍生 claim；兩個 cycle 都各計一次。重疊、UNKNOWN 前驅、缺邊界、跨 scope 一律 fail closed；live append 與 v3 identity 不變，無 migration、無新 projector version。
- **D2（projection audit spec，Option A）**：採 D。archive 不是 replay 輸入但納入 DR 驗證；無損編碼（Decimal 不經 float）、逐列與整表 digest 含原始 timestamp／surrogate id；runtime roles 無 archive 寫刪權；已完成 run 不可覆寫；不設自動 retention。
  新 snapshot 走 validated append 路徑，不直接 SQL；apply 在同一 transaction＋account lock 內 append snapshot → full scoped rebuild → parity → receipt。任何未分類差異阻擋 apply，不加 blanket allowlist；不改舊 baseline 冒充通過。
- **D3（rollback 邊界）**：commit 前失敗全 rollback；commit 後恢復交易前維持 halt、forward repair，不讓舊投影配已前進的 head；已有 venue 寫入後不自動回舊 DB／image。
- **D4（streaming evidence spec）**：採 H。`manifest/COMPLETE/chunks` 目錄、`0700/0600`、拒 symlink；record 1 MiB／part 4 MiB／artifact 2 GiB／4096 parts；classification 須與 diagnostic 逐筆同序（不用 set 比對）；新 cutover v2-only，v1 只供檢視。
- **D5（scoped fUST recovery plan）**：`--managed-symbols fUST` 為明確 operator scope，pin 進 prepared artifact；scope 外任何非零曝險／position fail closed，全零 legacy scaffold 由 atomic rebuild 移除；canary fUSD cells 移除、cap 0、fUST buffer 0。

## Rationale

- **D1**：A 把相容性限縮在已不可變的歷史 row，live 合約不動；不選 B：改 runtime claim 合約與 consumer 的遷移面遠大於問題本身，除非歷史證據無法以 A 安全表達；不選 C：改寫或丟棄事件會破壞 hash 與曝險。代價：projector 多一條歷史專用路徑，需證明它不洩漏到 live append。
- **D2**：不選基準——它假設投影可由事件完整推導，本例被證偽，清空等於銷毀 148,768 列唯一的對帳歷史；不選 E：重新引入第二個權威來源，推翻已核准的 event-only 驗收契約。代價：新增封存完整性工具、DR verifier 多驗 archive、restore 變慢。
- **D4**：F 仍整份 materialize 成 list，記憶體無界；G 違反「不省略欄位」的分類覆蓋；H 的代價是新 wire format 與 v1/v2 並存，但換得有界記憶體且 account lock 不持有數百 MiB 檔案 I/O。
- **D5**：相較等待 fUSD 資金再做雙幣別 snapshot，明確 scope 讓 fUST 先恢復；不選「缺幣別視為 0」，因遺漏資料不能冒充零曝險。

## Result

- `git log --oneline 528dbe6^..3f86511`（claim cycle `2595041`、archive/prepare/apply、streaming evidence、scoped fUST `3f86511`；區間含同期 DR 修補）。
- 2026-09-12：identity migration／quiescence、isolated restore 通過；fUST-only projection apply 可重跑且 idempotent，fUSD 維持 dark（見 [2026-09-05-offsite-dr-terraform-runtime-ownership](2026-09-05-offsite-dr-terraform-runtime-ownership.md) Result）。實體 restore benchmark 88.7s（pgBackRest 78.8s），非含 verifier 的 RTO。
- **O1**：D5 的「本輪不需 fUSD coverage」已被 [2026-09-19-dynamic-capital-and-release-canary](2026-09-19-dynamic-capital-and-release-canary.md) 改為 full-account snapshot 仍觀察所有持倉；`--managed-symbols` 仍是 cutover CLI 契約。

## Revocation Triggers

- 發現不符合 D1 邊界的歷史 CID 衝突 → 維持 blocker，重評 Option B，不加 ad-hoc 例外清單。
- 需要第二次 projection cutover（新帳戶或 schema）→ 沿用 D2／D4 工具，不直接清投影。

## Related

- 來源：`2026-09-10-historical-claim-replay-compatibility-design.md`（原文已不在 repo，本 ADR 即紀錄）
- 來源：`2026-09-10-historical-claim-replay-compatibility.md`（原文已不在 repo，本 ADR 即紀錄）
- 來源：`2026-09-10-projection-audit-cutover-design.md`（原文已不在 repo，本 ADR 即紀錄）
- 來源：`2026-09-10-projection-audit-cutover.md`（原文已不在 repo，本 ADR 即紀錄）
- 來源：`2026-09-11-projection-cutover-streaming-evidence-design.md`（原文已不在 repo，本 ADR 即紀錄）
- 來源：`2026-09-11-projection-cutover-streaming-evidence.md`（原文已不在 repo，本 ADR 即紀錄）
- 來源：`2026-09-11-scoped-fust-recovery.md`（原文已不在 repo，本 ADR 即紀錄）
- 上游：[2026-08-31-production-integrity-staged-clean-cutovers](2026-08-31-production-integrity-staged-clean-cutovers.md)；DR gate：[2026-09-04-offsite-dr-cloudflare-r2](2026-09-04-offsite-dr-cloudflare-r2.md)；幣別 scope：[2026-06-01-per-currency-allocation-phase-2](2026-06-01-per-currency-allocation-phase-2.md) 09-12 amendment。
- Repo 現行規範：`backend/ARCHITECTURE.md` §7「Projection audit cutover release boundary」；runbook `docs/runbooks/projection-audit-cutover.md`。

## Updates (2026-09-28)

- 由 [2026-09-28-ledger-journal-and-venue-mirror-replace-event-sourcing](2026-09-28-ledger-journal-and-venue-mirror-replace-event-sourcing.md) 取代；現行程式在該 ADR 的遷移 S3 完成前仍依本 ADR 運作。
