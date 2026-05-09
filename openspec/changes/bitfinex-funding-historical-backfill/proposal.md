## Why

Stage 1 validation 需要回測（backtest）才能驗「策略賺不賺錢」這個 go/no-go 假設（見 `~/second-brain/wiki/startup/products/bfx-funding-bot.md` Validation Plan）。回測前提是手上要有 ≥ 3 年歷史 funding 利率資料；目前 repo 沒有任何歷史資料儲存或抓取機制——bitfinex client 只實作了 authenticated trading endpoints（offer / credit / earnings），缺 public market data。

Stage 1.1 已完成資料來源評估（見 `~/second-brain/wiki/tech/bitfinex-funding-rate-data-sources.md`）：第三方 dataset 對 Bitfinex margin lending 接近全空，唯一路徑是 Bitfinex public REST API（`/v2/candles/...` + `/v2/funding/stats/...`）。本 change 落地 Stage 1.2：建表 + 抓資料 + 入庫。

## What Changes

- 新增兩張 PostgreSQL 表：`funding_candles`（市場成交利率 OHLC）與 `funding_stats`（FRR + 流動性深度）
- bitfinex client 新增兩個 public method：`GetFundingCandles`（呼叫 `/v2/candles/trade:{tf}:f{currency}:{period}/hist`）與 `GetFundingStats`（呼叫 `/v2/funding/stats/f{currency}/hist`）
- 新增 `cmd/backfill/main.go` runner：吃 currency / timeframe / start / end 參數，順序遞推呼叫 bitfinex client，使用 INSERT ON CONFLICT DO NOTHING idempotent 寫入
- 新增 sqlc query：`InsertFundingCandle`、`InsertFundingStat`、`GetLatestCandleMTS`（恢復 backfill 用）
- backfill 預設目標：3 年（2023-05-09 ~ 2026-05-09）`fUST` + `fUSD` 1h aggregated candles + 對應 funding_stats

## Capabilities

### New Capabilities

- `funding-history-storage`: PostgreSQL schema 與 query 介面，儲存 Bitfinex margin lending 歷史 candles 與 funding stats，支援 idempotent 重複寫入
- `bitfinex-public-market-data`: bitfinex client 的 public（無需 auth）endpoint 封裝，涵蓋 funding candles 與 funding stats 兩個 endpoint，符合 30 req/min rate limit
- `funding-data-backfill`: 命令列工具，用於從 Bitfinex public API 批次抓取歷史 funding 資料並寫入 PostgreSQL，支援指定 currency / timeframe / 時間區間 / resume from latest

### Modified Capabilities

（無）

## Impact

- **新增 schema**：`schema/schema.hcl` 加 2 張表 → atlas migrate diff 產生新 migration → atlas migrate apply 到 Neon
- **新增 code**：`internal/bitfinex/client.go` 加 2 個 public method + 對應 unit tests；`cmd/backfill/main.go` 新檔；`internal/repository/postgres/query/funding.sql` 新檔
- **不影響現有**：lending engine / market feed / handler / service 全部不動；新表獨立，不跟既有 5 表（users / api_keys / user_configs / executions / billing_records）發生 FK 關係
- **Neon DB 容量**：3 年 1h candles × 2 currencies = ~52,560 rows；funding_stats 假設 1h 一筆 = 同量級。總共 ~150k rows，遠低於 Neon free tier 限制
- **rate limit 風險**：bitfinex 30 req/min，limit=10000 一次抓 → 3 年僅需 ~3 requests / currency，完全在限制內
- **無 BREAKING changes**
