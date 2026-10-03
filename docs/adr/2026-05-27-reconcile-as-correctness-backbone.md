---
title: 週期 venue 對帳為「正確性骨幹」，WS stream 為「可失敗的延遲最佳化」
date: 2026-05-27
status: active
tags: [bfx-funding-bot, decision, reconciliation, event-sourcing, bitfinex, resilience]
related-commits:
  - "56eacf6^..7e37de5"
  - "d1fdbd7..43f90f8"
---

# 週期 venue 對帳為正確性骨幹，WS stream 為可失敗的最佳化

## Context

First real money（2026-05-26）暴露：送單後生命週期從不收斂。bot 07:00 掛的 2 筆（各接近 venue 最低下單額）在 07:10 被借走（變 funding credit），但 WS 事件全部解析失敗 → 從未產生 release/fill → ledger 卡在 canary cap、`AllocationCapGuard` 自 08:00 起擋掉所有新單、該 2 筆對應的資金閒置 ~7h。

三個 root cause 疊加：(1) `auth_ws._parse_foc` 索引錯位（用 `d[7]/d[11]`，真實 Bitfinex funding-offer 是 `d[10]/d[14]`）→ 對真實 payload `float(None)` TypeError → 整個 `foc` 事件被丟；(2) `fcn`（credit new）不帶原始 offer id（`offer_id_meta=d[14]` 實為 mts_opening）→ 成交無法映射回 offer；(3) reconcile 只在開機 `BootRecovery.run()` 跑一次，而 daemon 自掛單後未重啟 → drift 從未被對帳。

更根本的設計缺陷：**系統的正確性依賴了 WS stream**。stream 一壞，就沒有任何機制把 ledger 收斂回 venue 真實狀態。這違反交易所整合的業界通則（venue = source of truth；stream 是最佳化、不是正確性保證）。

延續本專案既有教訓（5/23 就標記「fcn offer_id_meta / foc 索引切 live 前需驗」）—— 風險已知，但當時只當「parser 待驗」處理，沒上升到「架構不能靠 stream 保證正確」的層次。

## Options Considered

**A. 最小 WS 補丁（WS-only）**：只修 `_parse_foc` 索引 + None-guard、用 `foc EXECUTED` 當成交訊號、重建 fixtures。不加週期對帳。
- 改動最小、最快上線。
- 但正確性**仍是 WS 單點**：未來 WS 斷線 / 漏事件 / 又一個 parser bug，ledger 會再次 drift 卡 cap，且如本次般無聲。治標不治本。

**B. 分層對帳（選用）**：以**週期 REST snapshot reconcile 為正確性骨幹**（重用既有純函式 `compute_recovery_actions` + `BootRecovery` 的 fetch/persist/publish，包成 interval loop），WS `foc` 修復**降級為可失敗的延遲最佳化**。加 in-flight grace window（避免對帳與剛掛的單競爭）、fail-safe（venue 拿不到 → executor DOWN 擋新單）、divergence 可觀測（穩態對帳發現 drift = stream 無聲壞了）。拆 Plan 1（骨幹，先 ship 且自動解卡）/ Plan 2（WS parser）。
- 縱深防禦：WS 全死系統仍正確收斂（延遲 ≤ interval）。
- 與既有 event-sourced 架構一致（event_log SoT + registry/ledger 投影 + 既有 boot reconcile）。
- 代價：要設計對帳節奏、idempotency、與並發掛單的 race 防護（grace window）；比 A 多工。

**C. 整個 WS 路徑重寫**：從零重建 `auth_ws` + `ws_dispatcher`。
- 一次到位、乾淨。
- 但風險最高、且**丟掉本來就正確的 dispatch table / OOO staging / dedup 邏輯**（這些不是 root cause）。過度工程，違反 YAGNI。

## Decision

選 **B**。週期 REST snapshot reconcile 成為常駐正確性骨幹（`PeriodicReconcile` sub-task，`BFX_RECONCILE_INTERVAL_S=90`），WS `foc` 修復獨立成 Plan 2 的延遲最佳化、且明確允許失敗。Plan 1（骨幹）先實作、ship、deploy —— 它本身就會在重啟/對帳時自動釋放卡住的 reservation。

## Rationale

- **A 不選**：它沒有解決「正確性依賴 stream」這個 root cause，只是換一批 parser bug 再等下次爆。對真錢系統，治標不可接受。
- **C 不選**：重寫的風險與成本遠超收益；dispatch / OOO / dedup 邏輯本來就對，重寫等於把對的東西也賭進去（代價過高），是 over-engineering。
- **B 是業界標準**：snapshot + incremental stream sync（交易所訂單簿同步法）、FIX **drop-copy reconciliation**（OMS 從不盲信自己的 fill stream，必對 venue 權威快照對帳）。心態是「stream 最佳化、snapshot 保證收斂」，而非把兩者並列或讓 stream 扛正確性。
- **既有資產讓 B 不貴**：`compute_recovery_actions` / `BootRecovery` / registry RELEASED dedup / 合成 cid 早已存在且測過（見 [2026-05-24-event-store-3a-recovery-reconcile-by-venue-id](2026-05-24-event-store-3a-recovery-reconcile-by-venue-id.md)），B 主要是「把已寫好的對帳邏輯改成週期跑 + 接上 fail-safe/divergence」，相較重寫風險低很多。
- **未上線前做對的事**：延續本專案 [2026-05-23-phase4.4a-bitfinex-live](2026-05-23-phase4.4a-bitfinex-live.md) 與 Lessons「pre-prod pivot 比 post-prod refactor 便宜」原則 —— canary 真錢但金額小（小額 canary cap），現在把骨幹做對的成本 << 規模放大後再回頭補。

實作細節經 review 校正兩處（與初版設計不同）：fail-safe 用 `EXECUTOR DOWN` **而非** DEGRADED（`AuthHealthGuard` 只有 DOWN 會擋單）；divergence 用**自清的專屬 `HealthTarget.RECONCILE`** 而非 `BITFINEX_REST`（放棄共用 target，避免與 daemon REST health poller 互搶）。`periodic_reconcile` 註冊進 `LIVENESS_THRESHOLDS=270s`（proactive 任務 → liveness，與 reactive executor heartbeat 反模式區分）。

## Consequences / Follow-ups

- **已驗證**：canary deploy 後開機 `boot_recovery released=2` 自動釋放卡住的 2 筆 reservation，bot 下個 tick 重新掛 2 筆真單，週期對帳每 90s 收斂、無假 DEGRADED。697 unit + 9 integration（含復現 incident 的整合測試）pass。
- **Plan 2（2026-05-27 已完成，見 [2026-05-27-ws-fill-path-single-source-wire-parser](2026-05-27-ws-fill-path-single-source-wire-parser.md) 與下方 Amendment）**：WS `foc` parser 索引修正 + 容忍解析 + `fcn` 降級 + dispatcher `EXECUTED→OrderFilled` + 真實 payload 重建 fixtures；可一併做 WS reconnect/seq-gap 觸發即時對帳。把成交追蹤延遲從 ≤90s 降到秒級。
- **待查**：`position_state` 快照表脫節（停在 `reserved=<cap> / updated_at=06:00:30`，未隨 release/新單更新；runtime 用 in-memory ledger + event_log SoT 仍正確，屬獨立可觀測性缺口）。
- **單幣別前提**：對帳目前用 `first_cell.symbol`（fUST-only canary 安全）；加第二幣別前需先做 per-symbol 對帳。

## Amendment (2026-09-27): 補記 grace／偵測層取捨與 resync 觸發

壓縮 spec/plan 時補上原 ADR 未記的 ruling（決策不變）：

- **D-grace**：`compute_recovery_actions` 加 `action_grace_ms`，同時管 orphan→claim 與 missing→release 兩方向；boot 傳 `0`（沒有並行下單，立即完整對帳，行為不變），periodic 傳 `120_000`（2 分鐘內的 offer／claim 不動，避免與正在掛的單競爭）。`PENDING→FAILED` 原本就有 `grace_ms`。fail-safe 門檻 `max_consecutive_failures=3` → `EXECUTOR DOWN`。
- **D-snapshot-diff**：periodic 用完整 registry-vs-venue snapshot diff，**不選** `RestPollingFillTracker` 的 `last_state` diff——後者只看得到開始觀察之後的變化，放不掉在觀察前就消失的 offer（正是卡住的那 2 筆）；fill_tracker 仍 gated off。
- **D-resync（2026-05-27 (d) spec/plan）**：timer 之外加兩個偵測器，同一個恢復動作。`auth_ws` 連線後送 `{"event":"conf","flags":65536}`（`SEQ_ALL`），以純函式 `SequenceTracker` 追 **public seq**（每個 packet 含 hb 都 +1，最連續），跳號 → `on_resync_needed("seq_gap")`；非首次連線建立 → `("reconnect")`（首次不觸發，boot 已對帳）。`PeriodicReconcile.request_resync` 只 set `asyncio.Event`：多次呼叫合併、與 timer 同一個 loop 不會並行 `_tick()`、`_tick()` 本體不變（fail-safe／divergence 保留），debounce `BFX_RESYNC_MIN_INTERVAL_S=10` 防 reconnect flapping。seq 放在 outer frame、不移動 `foc/fcn` payload 索引；取 seq 用「frame 尾端」heuristic（channel-0 資料 frame 尾端是 `[…, MSG_SEQ, AUTH_SEQ]`、hb 只有 `MSG_SEQ`），缺值一律視為「沒資訊」，錯的假設只會退化成「不偵測」，不會誤觸發。
  - 不選 **gap 時強制重連**（snapshot reconcile 已是權威恢復，重連只是雙保險；production 反覆見 gap 才加）；不選 **auth seq 追蹤**（public seq 已涵蓋「漏了任何 packet」）；不選只靠 timer（WS 漏事件時延遲是完整 interval）。代價：`auth_ws` 多一個 callback 依賴，且 seq 位置要靠 gated live test（`test_auth_ws_seq_live.py`）對真 frame 確認。
  - Result：`git log --oneline d1fdbd7..43f90f8`（tracker、request_resync、public seq 抽取、auth_ws 觸發、daemon wiring、off-interval 整合測試、gated live）。
- **liveness 附註**：同期 2026-05-26 executor liveness 修正（`executor`/`safety_chain` 移出 `LIVENESS_THRESHOLDS`、`/healthz` 只看 liveness 白名單、`HeartbeatGuard` 改 watch `ws`；`3092d02..cdab352`）屬 bug fix，現行規則與 why 已寫在 `backend/ARCHITECTURE.md` §6，不另立 ADR。

## Related

- 來源（Provenance）：
  - `2026-05-27-live-venue-reconcile-backbone-design.md`（原文已不在 repo，本 ADR 即紀錄）
  - `2026-05-27-periodic-reconcile-backbone.md`（原文已不在 repo，本 ADR 即紀錄）
  - `2026-05-27-resync-on-reconnect-seq-gap-design.md`（原文已不在 repo，本 ADR 即紀錄）
  - `2026-05-27-resync-on-reconnect-seq-gap.md`（原文已不在 repo，本 ADR 即紀錄）
  - `2026-05-26-executor-liveness-health-refactor.md`（原文已不在 repo，本 ADR 即紀錄）
- [2026-05-27-ws-fill-path-single-source-wire-parser](2026-05-27-ws-fill-path-single-source-wire-parser.md) — Plan 2（WS 層）。
- [2026-05-29-credit-aware-reconcile-v2](2026-05-29-credit-aware-reconcile-v2.md) — 把本骨幹延伸到 credit 維度並修正 offers-only 過修。
- [2026-05-24-event-store-3a-recovery-reconcile-by-venue-id](2026-05-24-event-store-3a-recovery-reconcile-by-venue-id.md)、[2026-05-23-phase4.4a-bitfinex-live](2026-05-23-phase4.4a-bitfinex-live.md)
