---
title: 後端重構為依能力劃分的 modular monolith——門面＋_internal、import-linter ratchet、策略核心獨立、組裝入口移出 marketfeed；帳本狀態模型另案
date: 2026-09-28
status: active
tags: [bfx-funding-bot, decision, architecture, refactoring]
---

# 後端重構：依能力劃分的 modular monolith，邊界由 import-linter 強制

## Context

`backend/src/bfx_funding_bot` 51,416 行（`find src -name '*.py' | xargs wc -l`），依賴方向沒有任何機械檢查，已經長成一團循環：

- 跨模組雙向 import：marketfeed↔execution（44／24 處）、accounts↔execution（13／13）、observability↔execution、admin↔marketfeed、backtest↔marketfeed、backtest↔live_validation、candles↔backfill。AST 分析顯示除了 api、external_signals，其餘 component 全在同一個 SCC；排除 daemon 後仍有 13 個。
- bot 的 composition root `build_daemon` 在 `modules/marketfeed/daemon.py`（2,126 行，其中約 1,000 行是組裝），compose 以 `python -m ...marketfeed.daemon` 啟動。
- live 用的策略住在 `modules/backtest/strategies`，`marketfeed/strategy_registry.py` 反向 import research 模組；`execution/contracts.py` 與 `marketfeed/schemas.py` 各有一個語意不同的 `DecisionOutcome`（READY/BLOCKED 與 POST/SKIP），同名易混用。
- `modules/execution` 23,084 行、佔 45%，混著 event store、capital、deployment reconciler、safety、boot recovery、projection_cutover。
- `core/writer_lock.py` 與 `external/bitfinex/*` 反向 import modules；無 import-linter、tach 或 AST 邊界測試。

約束：

- `external` 真錢運作中：每一步都要能部署、行為不變；允許計畫性 halt，但不做 big-bang 重寫。
- `external` 人力是 Will 一人加 coding agents（Codex 實作、Claude review）：邊界必須在 CI 機械判定，不能只靠 review 抓。
- `external` 前端 BFF 依賴 webapi 的 HTTP 合約，重構期間 HTTP 介面不變。
- `inherited` [2026-08-31-account-isolated-execution-target-architecture](2026-08-31-account-isolated-execution-target-architecture.md) D2/D3/D4'（同 repo modular monolith、bot 與 webapi 分 process、operator 動作走 outbox、webapi 對 ledger 零寫權限）。仍成立：operator-only、規模未知，該 ADR 的撤銷條件都未觸發。
- `inherited` Python 3.13＋SQLAlchemy async、單 VM 共用 Postgres。仍成立：沒有換語言或拆庫的新外部理由。
- `inherited` backtest 與 live 共用同一份 `Strategy.observe/decide`、candle 不可變（[2026-07-27-candle-immutability-bitemporal](2026-07-27-candle-immutability-bitemporal.md)）。仍成立：研究可信度靠它。

## Options Considered

### 模組邊界

- **A. 依能力劃分、有公開 API 與 internal 的 modular monolith，依賴為 DAG、組裝在獨立 app 層（業界基準；Spring Modulith 的 API／internal 驗證 https://docs.spring.io/spring-modulith/reference/verification.html ，kgrzybek/modular-monolith-with-ddd）**。代價：一次性大量搬檔與改 import。
- **B. 保留現有模組，只修掉循環**：改動最少；但模組是依管線階段切的（marketfeed 同時收資料、跑訊號、組裝 daemon），修完會再長回來。
- **C. 每個 context 一個 uv workspace package**：實體強制最強；多出版本、wheel、Docker layer 管理，且要先清光循環才能拆。
- **D. 拆服務**：違反上游 D3，solo 維運成本過高。

### 模組內部

- **A. 平鋪（現況）**：公開 API 與實作混在一起，無法機械判定哪些是刻意公開的。
- **B. 每個模組都分 domain／application／adapters（hexagonal／Cosmic Python）**：簡單模組的樣板比邏輯多。
- **C. 根套件只當門面，實作依子領域放 `_internal/`；預設 transaction script，有狀態機的子領域抽出不碰 I/O 的純邏輯**。

### 邊界強制

- **A. import-linter**（https://import-linter.readthedocs.io/ ）：pyproject 設定，layers／forbidden／independence contracts，`ignore_imports` 可當 baseline。
- **B. tach**：原生宣告 interface，成熟度與維護狀態未確認。
- **C. 自寫 AST pytest**：最彈性，規則引擎要自己維護。

## Decision

- **D1**：模組邊界採 A。目標 context 與方向：`platform`（原 core）← `venue`（原 external/bitfinex，只實作上層定義的 port）← `market`（book、candles、funding_stats、backfill、external_signals）← `strategy`（純策略、決策合約，無 I/O）← `trading`（帳本、capital、deployment、safety、reconcile、submit）← `control`（accounts、operator requests、trading control、policy）← `reporting`（live_validation、attribution、metrics 讀取端）。`research`（原 backtest）只能 import `strategy`、`market`，runtime 模組禁止 import `research`。
- **D2**：每個 process 一個薄 composition root：`apps/bot`、`apps/webapi`、`apps/jobs`；只有 apps 可以跨所有模組組裝，HTTP routers 屬於 `apps/webapi`。
- **D3**：模組內部採 C。其他模組只能 import 門面（`<module>/__init__.py`），禁止 import `_internal`。
- **D4**：邊界強制採 import-linter，進 lefthook 與 CI。既有違規列入 `ignore_imports` 當 ratchet，**只許減不許加**；另以 pytest 斷言 ignore 數量不增加，並鎖住 function-local import 的 allowlist。
- **D5**：遷移採原地 strangler。**純搬移的 PR 與行為變更的 PR 分開**：搬移 PR 只能改路徑與 import，由全套 pytest＋import-linter 判定。
- **D6**：`trading` 帳本的狀態模型（完整 event sourcing＋Decider／current-state＋append-only journal＋reconcile／snapshot 錨定的 event sourcing）**不在本 ADR 決定**，另寫 ADR；D1–D5 的遷移步驟不依賴它。

## Rationale

- **D1 選 A 而非 B**：循環是切錯邊界的症狀；依能力切分後 `strategy` 成為葉節點，backtest 與 live 共用同一份程式，依賴方向自然正確。**選 A 而非 C**：清完循環前實體 package 拆不動，import-linter 已足以擋 agent 越界；清完後若還要更強隔離再升級。代價是一段時間的大量搬檔，靠 D5 的純搬移 PR 控制風險。
- **D2**：組裝程式住在 marketfeed，是讓 marketfeed 看起來依賴全世界的主因；移出後可以直接拆掉一半的假循環。
- **D3 選 C 而非 B**：帳本、capital 這類狀態機才值得抽純邏輯，其他模組分層只是樣板。代價是門面每個對外符號多一行 re-export。
- **D4 選 import-linter**：零程式碼、輸出 agent 看得懂，ratchet 讓「Codex 引入新的跨界 import」在 CI 直接失敗，review 不再負責抓依賴方向。放棄 tach 原生的 interface 宣告，改以 `_internal` 命名加 forbidden contract 表達。
- **D5**：真錢路徑的回歸風險才是主要成本；純搬移 PR 可以用「測試全綠＋diff 只有 import」機械判定，行為變更的 PR 才需要逐行 review。
- **D6 另案**：兩份獨立草稿對此分歧。一方主張保留 event sourcing 並把帳本規則萃取成 Decider，以 replay prefix hash 做重構的回歸 oracle；另一方主張改為 current-state＋journal，理由是 venue snapshot 每 90 秒絕對覆寫曝險、Halt 2 顯示舊事件曾不足以重建現況（[2026-09-10-historical-replay-and-projection-audit-cutover](2026-09-10-historical-replay-and-projection-audit-cutover.md)），且 event_store＋projection_cutover 約 7.5k 行。這是真 trade-off、牽涉「帳本要可決定性重建」這項產品需求，需由 Will 拍板；D1–D5 不論哪個結果都成立，所以先行。

## Expected Outcome

- import-linter 在 CI 綠燈，`ignore_imports` 從 baseline 逐 PR 縮減到 0；模組間循環歸零。
- `apps/bot` 是 bot 唯一的 composition root，`marketfeed`（改名 `market`）不再 import trading、control、research。
- `strategy` 沒有 SQLAlchemy、httpx 或環境變數 import，backtest 與 live 用同一份程式。
- Codex 越界時 CI 失敗；Claude review 聚焦在行為變更 PR。

## Followup

遷移順序（每步一個 PR，細節與驗收寫在實作計畫）：

1. ✅ 加 import-linter，以現況產生 baseline contract 與 ignore 清單（PR #34）。
2. ✅ 組裝入口移到 `apps/bot`、`apps/webapi`（PR #35）。
3. ✅ 建立 `strategy`：搬入 `backtest/strategies`、`LendDecision`（PR #36）；`DecisionPayload`、`MarketSnapshot` 改由第 5 步處理。
4. ✅ 切斷最便宜的反向依賴：`core → accounts`、`Credentials` 與送單 wire helper 下沉到 venue、`gap_fill` 歸 candles（PR #37）；其餘延到第 5、6 步。
5. 建立 `market` context：把 marketfeed 混放的市場資料、共用 telemetry 詞彙、決策合約、health 監控與 daemon runtime 各歸其層，消除 `execution → marketfeed` 的例外，並解開 `ws_dispatcher`、`live_executor`、`fill_tracker` 與 observability 的延後項。
6. 拆 `execution` 為 `trading` 門面＋`_internal/…`，operator 相關搬到 `control`；先決定 `REQUEST_STATES` 與三個混合 tables 檔的歸屬。
7. ✅ 帳本狀態模型已決（[2026-09-28-ledger-journal-and-venue-mirror-replace-event-sourcing](2026-09-28-ledger-journal-and-venue-mirror-replace-event-sourcing.md)）：bot 事實 journal＋venue 觀測，取代 event sourcing；遷移 S0–S3 另排。
8. 其餘模組補門面，ignore 清零，同步改 `backend/ARCHITECTURE.md`。

## Updates (2026-09-28)

- 原第 5 步（拆 execution）排到 market context 之後：盤點顯示 execution 牽涉的 59 條例外全是它與外部模組的依賴（最多的是 `execution → marketfeed` 24 條），只在 execution 內部重新分組一條也消不掉，還會產生新的 trading ↔ control 循環（`uncertainty_tables → operator_requests.REQUEST_STATES`）。

## Amendment (2026-09-28)：D3' 合約／實作／組裝三分，跨模組只走合約＋注入

### Amendment Context

做 market context 的 pre-flight 時發現，market 同時有 strategy 需要的純型別與 repository、service 等 I/O；單一門面會讓 `strategy` 經由 import 間接帶進 SQLAlchemy、httpx。更根本的是，61 條循環例外幾乎都是「A 直接 import B 的實作」。系統尚無外部使用者，Will 要求治本、不留技術債。

### Amendment Options

- **A. 單一門面＋逐步搬移（原 D3）**：搬到對的位置能減例外，但模組仍可直接 import 別人的實作，新循環會再長。
- **B. 每模組開多個公開入口（Spring Modulith `@NamedInterface`）**：解 market 的純／I/O 衝突，但沒規範跨模組執行期依賴怎麼走。
- **C. 合約／實作／組裝三分＋Dependency Inversion（採用；Grzybek modular-monolith-with-ddd 的 Contracts／Infrastructure 分離、Shopify Packwerk 的 public 目錄、Seemann 的 Composition Root）**。

### Amendment Decision（D3'，supersedes D3）

- `<module>/__init__.py` 只放合約：型別、純函式、`Protocol` port、事件；import 時不得帶進 I/O。
- `<module>/_internal/` 放實作（含 repository、HTTP、背景 loop），其他模組一律不可 import。
- `<module>/wiring.py` 建構實作，**只有 `apps/` 可 import**。
- 跨模組執行期能力一律透過對方合約的 `Protocol`，由 `apps` 注入；import-linter 強制上述三條，`modules-acyclic` 目標 0 條例外。
- Followup 改為逐模組直接做到目標形狀，不再分「先搬、後補 port」兩輪。

### Amendment Rationale

選 C 而非 A：A 處理的是循環這個症狀，C 移除成因（依賴實作）。選 C 而非 B：B 只回答「開哪些入口」，C 同時規定執行期依賴的方向，且合約無 I/O，`strategy-is-pure` 類規則自然成立。代價：跨模組呼叫改成建構子注入屬行為層級改動，每個 PR 需逐行 review；`apps/bot.py` 變長（組裝本來就該在那裡）；每個 port 需一個 `Protocol`，換得測試可用 fake。

## Revocation Triggers

- 多帳戶 worker 需要獨立部署或 scale，而 import-linter 擋不住跨模組共用狀態：改評實體 package（邊界選項 C）。
- 門面 re-export 樣板明顯超過實作本身：放寬 D3，小模組回到平鋪。
- import-linter 無法表達 `_internal` 規則或停止維護：改 tach 或 AST 測試。
- `ignore_imports` 連續兩個月沒有縮短：重評該 context 是否改局部重寫。

## Related

- 本 ADR 來自 2026-09-28 Will 與 coding agent 的架構討論，並對照兩份獨立 subagent 草稿；無外部來源文件。
- [2026-08-31-account-isolated-execution-target-architecture](2026-08-31-account-isolated-execution-target-architecture.md) — process 邊界與 per-account worker 的上游決策。
- [2026-05-27-reconcile-as-correctness-backbone](2026-05-27-reconcile-as-correctness-backbone.md) — reconcile 為正確性骨幹，D6 的前提之一。
- [2026-09-10-historical-replay-and-projection-audit-cutover](2026-09-10-historical-replay-and-projection-audit-cutover.md) — Halt 2 replay 證據，D6 的爭點來源。
- Decider pattern：https://thinkbeforecoding.com/post/2021/12/17/functional-event-sourcing-decider ；Event Sourcing when-not-to-use：https://learn.microsoft.com/en-us/azure/architecture/patterns/event-sourcing
