---
title: 模擬模式改跑 ledger 全鏈——獨立 DB、httpx 層模擬 venue、S1-7 前以 soak 為閘門
date: 2026-10-03
status: active
tags: [bfx-funding-bot, decision, ledger, simulation, paper, shadow, testing]
---

# 模擬模式改跑 ledger 全鏈

## Context

帳本切換（[母 ADR](2026-09-28-ledger-journal-and-venue-mirror-replace-event-sourcing.md)）完成 S1-3 後，paper／shadow 仍只能組 legacy：非 live 不讀 authority epoch，`select_bot_ports` 直接拒絕 ledger 的模擬組裝。2026-10-03 唯讀盤點的現況如下：

- 模擬模式穩態一筆都不送。`DeploymentReconciler` 只在 live 組裝，signal engine 只寫 StandingQuote。boot smoke 的 submit 無條件 raise，所以每次模擬開機都會 smoke 失敗並發出 `SAFETY_TRIGGER`。
- `EchoPaperExecutor`（[4.2 ADR](2026-05-21-phase4.2-safety-harness-and-executor-port.md) D6）送出即全額成交，沒有掛單、撤單、credit 或到期。
- compose、CI、systemd 都沒有跑 paper／shadow。prod DB 只有 realm `prod` 的資料列（VM 唯讀查詢）。

Will 已決定模擬要改接 ledger（同一套 ledger port 加模擬 venue），並在 S1-8 刪除 legacy 前完成。本 ADR 記錄怎麼接。

**約束**：

- `external` 系統未上線、允許計畫性 halt（Will 2026-09-29，10-03 仍成立）。
- `external` Bitfinex 沒有流動性接近 live 的 funding 測試場。Paper Trading 子帳戶有 funding，但官方說明它不重現 live。這一條修正 [Phase 4 ADR](2026-05-18-phase4-staged-rollout-paper-shadow-canary.md)「沒有 funding testnet」的說法。
- `external` 公開資料只有 top-25 book snapshot 與小時 K。成交模型的解析度上限由這一條決定，與選哪個接縫無關。
- `inherited` authority epoch 是每個 DB 一份，dormancy trigger 讀的是本地 epoch 表（[S1 契約 ADR](2026-10-02-ledger-s1-port-and-wire-contracts.md)）。這條仍成立，它保護 prod 在 S1-7 前不被 ledger 寫入；因為理由是自設的，歸為 SELF-IMPOSED。
- `inherited` realm `{prod, shadow, ci}` 共用一個 DB 的寫法（[realm ADR](2026-05-23-deployment-environment-realm-and-backend-ci-gate.md)）。這是為當年 Axiom dataset 方案妥協而來的，現在只剩「與既有寫法一致」這個理由，歸為 SELF-IMPOSED。

## Options Considered

**(a) 模擬在哪個 DB 執行**
- **基準：模擬使用自己的資料庫。** Freqtrade 的 `dry_run` 預設用 `tradesv3.dryrun.sqlite`，open orders 跨重啟保留（https://www.freqtrade.io/en/stable/configuration/）。
- **A. 獨立 simulation DB**，epoch 設為 `ledger`、realm 設為 `shadow`（選用，即基準）。
- **B. prod DB 內的 shadow realm。** 在 S1-7 把 prod 的 epoch 切到 ledger 之前，這個方案無法開機。
- **C. 改成 per-scope epoch。** 會重新打開 S1 契約 ADR 已定案的「epoch 全域、DB 強制休眠」。

**(b) 模擬 venue 接在哪一層**
- **基準：行程內模擬交易所，藏在與 live 相同的介面後面，吃 live 行情。** 例如 NautilusTrader sandbox、Hummingbot `paper_trade`、QuantConnect paper brokerage、Freqtrade `dry_run`。但 Nautilus sandbox 的 venue report 一律回空（`crates/adapters/sandbox/src/execution.rs`，https://nautilustrader.io/docs/latest/concepts/reconciliation/），也就是它不經過對帳路徑。
- **T. 行程內 httpx transport**，模擬實際用到的 Bitfinex auth endpoint，包含 paging（選用）。
- **P. 在 port 層直接實作 `VenueObservation`／`ExecutorPort`。**
- **W. 獨立的 wire emulator 行程**（HTTP 加 WS）。
- **S. Bitfinex Paper Trading 子帳戶。**

**(c) 進 S1-7 前要不要先跑模擬 soak**
- **基準：階段式上線，每段有觀察期。** 見 Google SRE Workbook「Canarying Releases」（https://sre.google/workbook/canarying-releases/）。
- **a. 只靠 CI e2e。**
- **b. CI e2e 之外，在 simulation DB 跑一段限時 soak，作為進 S1-7 的入場條件**（選用）。
- **c. 常駐 shadow service。**

## Decision

- **D1 = A。** 模擬固定在獨立 DB 執行：同一個 Postgres cluster 裡另開一個 database，epoch 設為 `ledger`、realm 設為 `shadow`，S1-7 前就能開機。sim bot 沿用 `bfx_bot` 登入。simulated venue 只接受 epoch `ledger`；realm 為 `prod` 時 config 會拒絕。DB 層的保護是每個 database 存一列 `database_realm`，由 trigger 拒絕與本 DB realm 不符的寫入。之所以不用表 CHECK，是因為同一條 migration chain 在 sim DB 也必須接受 `shadow`。prod DB 永遠不會出現 `shadow`／`ci` 資料列。
- **D2 = T。** venue 的狀態以 event-sourced 方式存在 simulation DB，ledger journal 一律不讀。CI 與 unit 改用同一介面的記憶體實作。成交模型採 book-queue 加成交量的決定性規則，從 `lending/tracking/book_replay.py` 抽出純函式，研究與模擬共用。模擬吃公開 funding trades 的逐筆成交量；研究端仍用小時 K。逐筆 trades 依小時加總後應等於 K 線成交量，以測試對齊。利息依 Bitfinex 付息節奏直接記入 wallet，attribution 不在本次範圍。venue 故障注入（UNKNOWN，包括 venue 拒絕卻回 5xx 加 `["error",…]` 的形狀、REJECTED、history 不完整）預設關閉，CI 打開。NOT_SENT 不是 venue 故障：executor 在 HTTP 送出前就標記 transport 已開始，transport 拋出的例外一律判為 UNKNOWN。NOT_SENT 改在 executor 之前，以本地故障注入。模擬器回應的形狀取自 Bitfinex 文件或 live 擷取，不照 client parser 抄。
- **D3 = b。** soak 門檻草案：
  - 至少 72 小時，期間至少 2 次部署重啟與 1 次 kill；
  - `unexplained_lending` 為 0；
  - 非注入的 quarantine／UNKNOWN 為 0；
  - accepted cycle 比例至少 99%；
  - 注入的 UNKNOWN 全數自動結案。

  部署 pipeline 不擴充時，改用 one-shot job 執行（`docs/runbooks/research-one-shot-jobs.md`）。
- **D4 刪除，取代 4.2 ADR D6 的 echo-only paper。** 刪除 `EchoPaperExecutor`、`AllocationCapGuard`／`BFX_ALLOCATION_CAP_USDT`、模擬模式的 guard 放寬、`BFX_EXECUTOR`、`ExecutionPolicy.PAPER`，以及 SmokeRunner、boot smoke、`/admin/smoke-test`。venue 由 phase 推導；`rg` 找不到 consumer 的話，`paper` 併入 `shadow`。所有 `is_simulated=True` 的程式分支在本次刪除，只有 legacy archive 格式裡的 `is_simulated` event 欄位留到 S1-8。

## Rationale

- **D1**：獨立 DB 的隔離是物理上的，不必靠查詢過濾。S1-7 前就能取得 ledger 全鏈證據，也不必放寬 epoch 的保護。**不選 B**：S1-7 前開不了機，切換前拿不到證據。**不選 C**：為了模擬去削弱 prod 的休眠保護，得不償失。**代價**：部署工具必須能 migrate 第二個 database；default privileges 以 database 為單位，建立 sim DB 時要重新設定。VM 資源已量過（2026-10-04：可用記憶體 18 GB，bot 265 MiB、Postgres 179 MiB），足以多跑一個 bot 和一個 database。
- **D2**：本系統的正確性骨幹是對帳。`BitfinexVenueObservation` 的 symbol 集合、時間窗、coverage 判定（G1 缺陷就出在這裡），以及 executor 對 HTTP 回應的 ACK／UNKNOWN 分類，都應該留在迴圈內。只有 T 能做到。**不選 P**：會繞過上述兩層，而 P 的優勢只有工作量（SELF-IMPOSED）。**不選 W**：要另寫簽章、nonce 與 WS，成本約為 T 的 2 到 3 倍。**不選 S**：不重現 live，只能當臨時的傳輸一致性檢查。**狀態存 DB 不存記憶體**：模擬 venue 的耐久度不能低於 bot 自己的耐久度，否則每次重啟都會製造真 venue 不會有的 quarantine，測到的是模擬器失憶，而不是 bot。**代價**：成交模型偏樂觀（不模擬後來者削價），模擬 P&L 校準前不能拿來做決策。
- **D3**：母 ADR D7'' 放棄切換前的持續觀測，理由之一是 shadow 基礎設施用完即丟。現在模擬器是永久保留的，這個理由不再成立；soak 是用最便宜的方式補回這段證據。**不選 a**：CI 只跑單一行程的短情境，抓不到重啟、長時間漂移與部署互動。**不選 c**：要多一條部署路徑，在目前沒有實驗需求的情況下只是成本。**代價**：模擬 venue 無法驗證 Bitfinex 本身的語意，soak 證明的範圍只到 ledger、組裝與 runtime。
- **D4**：系統未上線，沒有相容負擔。保留沒有 producer 的 `is_simulated` 分支只是技術債。

## Expected Outcome

- 模擬與 live 走同一條鏈：writer lock、CapitalPolicy 與 pre-trade guards、reconciler、workers、periodic reconcile。兩者唯一的差別是 venue transport。
- 在模擬 venue 上隨機產生操作序列時，每個 accepted basis 都是 `conserved`；模擬器重啟後，不會因此產生 quarantine 或 UNKNOWN。
- S1-7 開始前，有一份達到 D3 門檻的 soak 紀錄。

## Followup

- [ ] P1：simulated venue 模組、simulation DB 的 migration 路徑與故障注入，先以 dormant 狀態合併。
- [ ] P2：組裝改為 venue 軸並執行 D4 的刪除；把 SignalEngine 與 `date_provider` 的 wall time 改接 composition clock。
- [ ] soak 前單獨一個 PR：每個 database 一列 `database_realm` 加 trigger。
- [ ] 定案 soak 的執行方式（常駐第二個 compose service 或 one-shot job）與門檻。

## Revocation Triggers

- T 抓不到的 WS 或簽章缺陷在 prod 出現 → 加做 W。
- 模擬 P&L 要拿來當上線或策略的決策依據 → 先以 live submit→fill 延遲校準成交模型。
- 要做 p14 optimizer 實驗，或 S1-7 後需要持續的回歸 soak → 改為 c。

## Related

- 來源：2026-10-03 唯讀 pre-flight 與獨立 critique（兩份 subagent 報告，未進 repo，結論與證據已收進本檔）；Will 2026-10-03 選定 D1–D3。
- 取代 [2026-05-21-phase4.2-safety-harness-and-executor-port](2026-05-21-phase4.2-safety-harness-and-executor-port.md) D6（echo-only paper）；修正 [2026-05-18-phase4-staged-rollout-paper-shadow-canary](2026-05-18-phase4-staged-rollout-paper-shadow-canary.md) 的 testnet 約束；補充 [2026-09-28-ledger-journal-and-venue-mirror-replace-event-sourcing](2026-09-28-ledger-journal-and-venue-mirror-replace-event-sourcing.md) D7''。
