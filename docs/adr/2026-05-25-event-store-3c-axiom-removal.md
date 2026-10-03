---
title: Event-store 3c — Axiom 全移除（expand-contract 的 contract step）
date: 2026-05-25
status: active
tags: [bfx-funding-bot, decision, event-sourcing, observability, axiom, strangler-fig]
related-commits:
  - "4cafb6d^..6121705"
---

# Event-store 3c — Axiom 全移除

## Context

PG event-store SoT 遷移 Plan 3 拆 3a/3b/3c（expand-contract / Strangler Fig）。3a（write-ahead intent + recovery）把 SoT 寫入移到 `event_log`、3b 把 forensic（DECISION/SAFETY_TRIGGER/CANCEL_AUDIT）移到 PG `diagnostics` 表 —— Axiom 的 emit 路徑此時已全 repoint 走，只剩 **operational telemetry**（SIGNAL/HEALTH_CHECK/lifecycle）+ smoke 驗證鏈 + client/adapter/env 殘留。3c 是 contract step：把這些殘留刪乾淨。觸發點：Axiom（log SaaS）的 wire-level 脆弱 + 30d retention + 「Axiom down=daemon 起不來」耦合（見上游 ADR [2026-05-23-postgres-event-store-sot-migration](2026-05-23-postgres-event-store-sot-migration.md)），pre-launch 一次清掉。

## Options Considered

**operational telemetry（SIGNAL/HEALTH_CHECK/lifecycle）落點：**
- **A. drop-in `StdoutEventSink`（選用）**：與舊 `AxiomClient` 共用 `emit(dict)` port，寫 structured stdout。
- **B. inline `log.info(...)`**：逐 call site 改成字串 logging。
- **C. 保留 buffered AxiomClient**：只是不接 Axiom。

**L3 smoke 驗證後端：**
- **D. PG `event_log` read-your-writes（選用）** vs **E. 保留 Axiom round-trip** vs **F. 砍掉 deploy gate**。

**G1 continuity gate + `paper_smoke_runner`（查 `signal` 事件）：**
- **G. 整個退役（選用，使用者拍板）** vs **H. 改 grep Koyeb structured stdout logs** vs **I. defer：只解 Axiom 耦合、留 stub + TODO**。

**g5 time-travel（`replay_from_axiom(up_to_ms=T)`）：J. drop（選用）** vs **K. 在 PG 重建 point-in-time API**。

## Decision

- **D1**：operational telemetry → drop-in `StdoutEventSink`（A）。
- **D2**：L3 smoke → PG `event_log` read-your-writes via `PostgresEventLogQueryAdapter`（D）。
- **D3**：G1 gate + `paper_smoke_runner` 整個退役（G）；deploy gate 改用 PG-backed L3 endpoint。
- **D4**：g5 time-travel drop（J）；`kind=safety_trigger` 異質 payload 正規化 deferred（anti-gold-plating）。

## Rationale

- **D1**：3b 刻意讓新 sink 與 `AxiomClient` 共用 `emit(dict)` port → 換 sink 只改 daemon wiring 一個 identifier、consumer 的 `.emit()` 呼叫 body 零變更。**相較 B**（inline log）要改每個 call site、且失去結構化 envelope；**相較 C** 留死 client 是技術債。**代價**：stdout sink 無 buffer/retry，但寫的本就是同一個會掛的 PG/平台層、buffer 在 crash 照樣丟，best-effort 合理。
- **D2**：L3 驗的 RESERVATION_CLAIMED + ORDER_FILL 本就進 `event_log`（read-your-writes 直接斷言、無 indexing-latency poll）。**不選 E**：Axiom 要被刪；**不選 F**：deploy-time wiring-drift gate 有真實 catch 價值（lesson：smoke endpoint 第一次跑就抓 bug）。
- **D3**：G1/paper-smoke 查 `signal` 事件，但 3c 後 signal → stdout，**無可查詢後端**；spec §358 明定不為此建表（不做迷你 observability）。**不選 H**（grep stdout）：脆弱、solo pre-launch 不值；**不選 I**（defer）：留 stub + Axiom env 是半套技術債。**代價**：失去 signal-continuity 的 pre-deploy gate，由 PG-backed L3 + Koyeb log 人工查補。**性質是產品決策（capability 取捨），非 bug fix。**
- **D4**：g5 在 PG SoT 無對等（snapshot 是 current state；raw events 仍在 `event_log` 可手工重放，但無 consumer 要 point-in-time API）。`safety_trigger` 異質 payload（smoke-boot vs safety-chain 兩種 shape）lossless 存著、query-time 靠 `event_type` 區分即可，正規化是 gold-plating。**代價**：未來若真要 point-in-time / 統一 payload，得另開。

## Result

- `git log --oneline 4cafb6d^..6121705`（30 commits / subagent-driven 15 tasks / FF merge main，**local 未 push**）。
- 淨 **−3868 行 / 97 檔**；merged main：**655 unit + 8 PG integration（testcontainers）pass / mypy 101 files clean / ruff clean**。
- opus holistic final review **READY TO MERGE**：daemon end-to-end 連貫（StdoutEventSink + PG smoke wired、run-loop 移除 axiom sub-task 無缺口）、SoT persister + diagnostics 路徑零變動、operational emit 確實非 no-op。
- `alembic check`：worktree .env 壞密碼連不上（pre-existing 環境問題）；baseline drift 屬 item 10 記錄的獨立技術債，3c 不觸碰。

## Followup

- **上 live 前必驗**：對 real Bitfinex 帳號跑 venue reconcile（沿用 3a-recovery followup，未因 3c 改變）。
- ~~**Axiom dataset 關閉前**：退役 `scripts/g2_audit_locf.py`~~ — 已於 2026-05-25 刪除 script 與其測試（spec §13.1 item 11，2026-09-27 補記）。原文：退役 `scripts/g2_audit_locf.py` — 另一支查 Axiom 的手動 LOCF 稽核腳本（自帶 `AxiomQueryClient`），不在 G1/paper-smoke 決策範圍、3c out-of-scope。app 已無 Axiom 但此腳本仍會查空 dataset。

## Lessons

### Rules
- **R1**：deletion/contract task 動手前先 `grep -rn <symbol> src/ tests/` 全域盤點 —— plan 的檔案清單是 floor 不是 ceiling（T8/T9/T10 每個都漏列 import）；刪測試前區分「測機制（隨刪）vs 測不變式（須確認 PG/新路徑有對等覆蓋再刪）」。`Rule: 刪 symbol 先全域 grep + 分類其測試`。
- **R2**：subagent-driven 中 deletion/docs 類「done」報告**不可信字面**，reviewer 須 `git show --stat`/`grep` 獨立驗證實際落地。本次 T13 implementer 報「docs 已改」但 git 零變更（編輯誤落 main repo 副本而非 worktree）。`Rule: deletion/docs 任務的完成宣稱一律 git show 驗證`。

### Observations
- **O1**：3b 在 expand 階段讓新 sink 共用舊 `emit(dict)` port 的決策，在 3c contract 階段兌現 —— operational repoint 全是機械 rename、emit payload byte-identical、30 commits 幾乎無介面落差。expand-contract 的「contract 成本前置到 expand 設計」預測命中（同專案驗證）。

## Related

- 原 spec §13.1 item 11（3c shipped 記錄）+ plan `docs/superpowers/plans/2026-05-24-pg-event-store-3c-axiom-removal.md`。spec 為 3a/3b/3c 共用（原文已不在 repo，本 ADR 即紀錄）。
- 共用 spec 取回（§8、§13.1 item 11）：`2026-05-23-postgres-event-store-sot-migration-design.md`（原文已不在 repo，本 ADR 即紀錄）
- 上游 ADR [2026-05-23-postgres-event-store-sot-migration](2026-05-23-postgres-event-store-sot-migration.md)（§8 Axiom Removal 的落地）+ 前序 [2026-05-24-event-store-3a-recovery-reconcile-by-venue-id](2026-05-24-event-store-3a-recovery-reconcile-by-venue-id.md)、[2026-05-24-event-store-3b-forensic-diagnostics-in-postgres](2026-05-24-event-store-3b-forensic-diagnostics-in-postgres.md)（D1 共用 port 的來源）。
- 相關 lesson：「shared port 讓未來刪除乾淨」（3c 驗證）、「deletion task grep-first / 不信 subagent 報告」。
