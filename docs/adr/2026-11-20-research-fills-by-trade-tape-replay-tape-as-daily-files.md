---
title: 研究回測以公開成交紀錄逐筆 replay 判定成交，成交紀錄存成每日檔案而非 Postgres
date: 2026-11-20
status: active
supersedes: "[2026-05-26-g13-candle-path-crossing-fill-model](2026-05-26-g13-candle-path-crossing-fill-model.md)；[2026-09-22-research-infra-tenor-pricing-book-fill-weekly-revalidation](2026-09-22-research-infra-tenor-pricing-book-fill-weekly-revalidation.md) 的 B1（決策當下全額計入）與 C（研究回測的 fill 證據來源）"
tags: [bfx-funding-bot, decision, backtest, fill-model, research-data]
---

# 研究回測以成交紀錄 replay 判定成交，成交紀錄存成每日檔案

> owner 於 2026-11-20 核可。執行計畫：`docs/plans/2026-11-15-trade-tape-research-method.md`（步驟 1 spike 結果在其附件 `spike/report.md`）。

## Context

prior state：回測引擎每根 candle 以 `gross_rate = decision.rate × fill_prob` 記帳（`backend/src/bfx_funding_bot/modules/backtest/engine.py` `_apply_friction`）。`fill_prob` 來自 linear 曲線，或來自依「掛價高於市場幾 bps」分桶的查表 artifact（G13 candle learner、2026-09-22 的 book learner）。這套設計成形時沒有逐筆成交可用：candle 只有成交利率的 OHLC，book 快照 2026-07-19 才開始。

2026-11-15 起確認公開 `GET /v2/trades/f{SYM}/hist` 有 2022-01 以來的全市場逐筆成交，AMOUNT 正負號代表主動成交方。步驟 1 全量抓取：fUSD 1.03 億列、fUST 3,957 萬列，沒有一天無成交。

**約束**：

- `external` Bitfinex 撮合：offer 期限必須 ≥ bid 期限，固定利率單不和 FRR 單撮合（[Margin Funding on Bitfinex](https://support.bitfinex.com/hc/en-us/articles/115003428165-Margin-Funding-on-Bitfinex)）；價格優先、同價先到先撮。公開紀錄的 PERIOD 記的是哪一方的期限：未確認。
- `external` 公開 API 頻率限制。20 秒間隔實測 0 次 429；超限時整個 IP 封鎖 60 秒，VM 與 live bot 共用出口 IP。
- `external` book 快照只從 2026-07-19 起，之前的排隊深度沒有資料。
- `inherited` 正式 DB 由 pgBackRest 整個 cluster 物理備份；帶 migration 或動到 DR 路徑的部署要先備份，再做完整的隔離還原測試（`docs/runbooks/deploy.md`）。仍成立：這是現行 DR 設計，要改成排除單一張表，代價高且與本題無關。
- `inherited` 新增依賴要 owner 同意（`AGENTS.md` 供應鏈）。仍成立。
- 不列為約束：`estimate_fill` 的簽名、引擎採期望值算法、`fill_rate_stats` 的形狀。這些都是自己的程式；live bot 只在未啟用的 `OPTIMIZER_LIVE` 路徑用到 `estimate_fill`。

## Options Considered

**A. 回測怎麼判定成交**

- **A1（選用，業界基準）逐筆 replay**：每個策略決策變成一張假想掛單（利率、期限、掛出時間、金額），對其後的歷史成交逐筆比對。保守規則：借方主動、期限相容、利率高於掛單的成交，依累積量計入成交（含部分成交）；剛好同價的成交，只在有 book 快照時用排隊模型計，其餘不計；報告同時給保守與樂觀邊界。收益照實際成交、閒置時間與成交時間計算。業界例子：hftbacktest 的 queue model、NautilusTrader 以 trade tick 撮合、以 Tardis.dev 逐筆資料做的回測。
- A2 查表 `fill_prob` × 利率（原計畫：從成交紀錄學出查表）：不管成交時間、閒置資金與重新報價，也假設成交與利率互相獨立（放貸單往往在利率飆高時被吃）；還要另外拍一個排隊比例參數。
- A3 成交時間的存活模型（Lo, MacKinlay & Zhang 2002, *Econometric models of limit-order executions*）：做前瞻預測時需要，手上有完整成交紀錄的回測用不到。

**B. 成交紀錄存哪裡**

- **B1（選用，業界基準）不可變的每日檔案**：`tape/<symbol>/<YYYY-MM-DD>.csv.gz`，放在 VM，研究容器唯讀掛載；要不要另存一份到 R2 未決（計畫 D5：既有 offsite 工具 pgBackRest 不能上傳一般檔案，資料又能從公開 API 重抓）。原始逐筆資料放在獨立於交易資料庫的資料區，Tardis.dev 也以每日 gzip CSV 發布。全量 1.27 GB（實測 gzip）；pandas 與標準函式庫可讀，不新增依賴。
- B2 Postgres 新表：約 19.5 GB（每列 137 B 實測後外推），是現有 DB 的 6 倍，每次部署的備份與還原測試都要多處理這些資料。
- B3 Parquet + DuckDB：分析最快，要新增兩個依賴。

## Decision

- **D1**：研究回測以 A1 判定成交。期望值記帳路徑、linear 與 candle learner 從研究路徑移除。book learner 與 `estimate_fill` 只保留給 `OPTIMIZER_LIVE` 的未啟用路徑，研究端不再使用。
- **D2**：成交紀錄以 B1 存放。只有收集工作能寫入；同一天重抓時比對筆數與雜湊，冪等。
- **D3**：A1 開工前先量四件事：公開紀錄 PERIOD 欄位的語意（拿自己成交的 trade id 對照）、我方金額相對各期限成交量的大小、replay 全量一次的耗時與記憶體、公開紀錄是否完整。

## Rationale

- A1 對 A2 的取捨：A1 換掉整個回測的記帳方式，現有研究數字都要重跑，換來的是不靠任何成交機率近似；需要假設的只剩同價時的排隊位置，而且用上下邊界呈現，不用一個參數帶過。A2 的程式改動小，但「成交與利率獨立」這個偏誤正好落在研究要回答的問題上（期限與掛價怎麼選）。
- A3 不選，是因為它回答的是「現在掛單，預期多久成交」，那是 live 決策的問題；回測有實際成交可以對照，不必用模型推估。
- B1 對 B2 的取捨：B1 要另一套存取路徑（檔案掛載），失去 SQL 查詢；B2 讓每次部署的備份與還原測試多處理約 19.5 GB，研究資料的成長直接拖慢上線流程。B3 等 replay 量到讀檔是瓶頸時再升級，避免現在就新增依賴。

## Expected Outcome

- 研究報告的收益來自 replay 的實際成交與閒置時間，並附保守與樂觀兩個邊界。
- 正式 DB 大小與部署時間不受成交紀錄影響。
- `rg -n "linear-baseline|compute_fill_prob|FillRateLearner" backend/src backend/scripts` 無結果。

## Revocation Triggers

- 公開紀錄被證實不完整（例如漏掉某類成交），且無法補齊 → A1 的保守邊界失去意義，重新評估。
- 我方金額相對成交量不可忽略 → A1 要加入市場衝擊模型。
- replay 讀檔成為瓶頸 → B1 升級為 B3（新增依賴需 owner 同意）。

## Followup

- 執行依計畫 `docs/plans/2026-11-15-trade-tape-research-method.md` 的步驟 1b 起。
- 討論開 `OPTIMIZER_LIVE` 時，以 replay 結果訓練 A3 存活模型，取代 book 查表（屆時另寫 ADR）。

## Review Notes

- 2026-11-20 brief 經獨立 subagent 審查，13 項 finding 已修進選項與約束，其中撮合期限規則推翻了原本的「同天期桶」定義。
- 獨立草稿：n/a，本決定只影響研究路徑，可用 revert 撤回。

## Related

- [2026-05-26-g13-candle-path-crossing-fill-model](2026-05-26-g13-candle-path-crossing-fill-model.md)（被取代）
- [2026-09-22-research-infra-tenor-pricing-book-fill-weekly-revalidation](2026-09-22-research-infra-tenor-pricing-book-fill-weekly-revalidation.md)（B1、C 被取代）
- [2026-08-23-funding-strategy-execution-integrity](2026-08-23-funding-strategy-execution-integrity.md) D3：live 的 fill artifact 契約不變
