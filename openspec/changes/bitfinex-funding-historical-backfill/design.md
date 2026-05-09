## Context

bfx-funding-bot 目前 lending engine 只用 live websocket feed 做即時決策；要 Stage 1 backtest（驗證策略賺不賺錢）必須有歷史資料落地。Stage 1.1 已決定資料來源是 Bitfinex public REST API（詳見 `~/second-brain/wiki/tech/bitfinex-funding-rate-data-sources.md`）。

此 design 經過 superpowers brainstorming 細化（2026-05-09）後 v1，定案下列要點：
1. **資料粒度走 candles + funding_stats，暫不取 trades / order book** —— 走「金字塔策略」，先用 coarse fill 模擬（my rate ≤ candle high 就視為填到）跑 Stage 1.5 backtest，看數字可信度再決定是否擴展到 trades。
2. **Scope = fUST + fUSD × (1h, 1D) × `a30:p2:p30`** —— 1h 給策略決策、1D 給宏觀趨勢；fUSD 當策略對照 + 為未來 10 年回測備料。
3. **Architecture 走 Approach A**：方法直接掛 `bitfinex.Client`、cmd 直接用 `pgxpool`，不開新 `internal/marketdata/` package（YAGNI；未來真要長期 ingestion 再抽）。
4. **Error handling 含 rate-limit 自動指數退避**：60→120→240→240（cap），最多 4 次後 abort。
5. **Backfill runner 內建 `--verify` 模式**：跑完直接做 gap 檢查 + FRR 單位驗證，避免日後忘了驗。

**現況：**
- `internal/bitfinex/client.go` 已有 HTTP 基礎設施（rate limiter / circuit breaker / retry / parseError），目前只實作 `/v2/auth/...` 私有端點。
- Atlas declarative schema（`schema/schema.hcl`）+ atlas migrate diff/apply workflow 已就位。
- sqlc 已就位，generated code 在 `internal/repository/postgres/sqlc/`。
- DATABASE_URL 指向 Neon serverless PostgreSQL（dev/prod 共用）。
- **`funding_candles` + `funding_stats` 兩張表 5/9 已 apply 到 Neon**（migration `20260509100512_add_funding_history_tables.sql`），尚未寫入任何 row。

**約束：**
- Bitfinex `/v2/candles` rate limit = **30 req/min**；超過 → IP 封 60 秒
- Bitfinex candle endpoint 單次 max limit = **10000**（預設 100，必填滿省 request）
- Bitfinex funding/stats endpoint 單次 max limit = **250**（小於 candles，stats 抓會多得多）
- USDT funding 符號 = `fUST`（**非 fUSDT**，5/9 已驗證）
- fUST 資料最早 2018-12-21；fUSD 最早 2016-07-31
- Atlas migration 必須走 `atlas migrate diff` + `atlas migrate apply --env neon`，不可手寫 SQL 或用 MCP 直接 exec
- Project 規約：repository 層不寫 _test.go，**用 in-memory mock 測 consumer**，DB 互動由 end-to-end smoke 涵蓋（見 `backend/CLAUDE.md`）

## Goals / Non-Goals

**Goals:**
- 建立可重複執行（idempotent）的歷史 funding 資料抓取流程
- 一次 backfill 3 年 fUST + fUSD × (1h + 1D) candles + 對應 funding_stats（共 ~110K rows）
- bitfinex client 加 public 端點封裝（`GetFundingCandles` / `GetFundingStats`），未來其他 caller 可重用
- 所有 HTTP 呼叫對 rate limit 自動退避，避免人為干預
- Backfill 跑完內建驗證（gap detection + FRR 單位比對），確認資料可信再進 Stage 1.3
- 支援 `--resume-from-earliest`（nice-to-have，first cut 沒有也可：重跑 ~7 分鐘）

**Non-Goals:**
- 不抓 trades / 不存 order book（Stage 1.5 跑完 backtest 看數字再決定是否擴展）
- 不做 backtest engine 本身（Stage 1.3）
- 不做即時增量同步 cron / scheduler（先一次性 backfill；Stage 2 paper trading 才考慮增量）
- 不做策略邏輯（Stage 1.4）
- 不做 admin UI 顯示歷史資料（驗證階段不需要 UI）
- 不抓 5m / 1m 高頻 candles（先 1h+1D 足夠驗證；高頻需要時再 backfill）
- 不抓 BTC / ETH（只 USDT + USD）
- 不開 `internal/marketdata/` package（cmd 直接用 client + pgxpool）
- 不做 dead-letter queue / 失敗 row retry（abort 重跑就好）
- 不寫 repository 層 sqlc 測試（順 project 慣例，DB 互動由 smoke 涵蓋）

## Decisions

### D1：Schema 兩張表分開，不合併

**決策**：`funding_candles` 與 `funding_stats` 分兩張表。

**Rationale**：
- 兩端點時間粒度不同：candles 可指定 `1h` `1D` 等任意 timeframe，stats 是 raw 時序（每隔 ~1 小時一筆）
- candles 有 OHLCV 五維、stats 有 FRR / amount / used / threshold 四維，欄位完全無交集
- 合併會導致大量 NULL，未來欄位增減互相干擾

**Alternatives considered**：
- 單一 `funding_data` 表 + `data_type` discriminator → 大量 NULL、查詢必須 filter，不划算

### D2：Candle 主鍵採複合鍵 `(symbol, timeframe, period_agg, mts)`

**決策**：PK = `(symbol, timeframe, period_agg, mts)`，不用 surrogate `id`。

**Rationale**：
- Bitfinex candle 對「相同 symbol + tf + period_agg + 時間」是唯一的，自然主鍵就是業務鍵
- INSERT ON CONFLICT DO NOTHING 直接吃複合 PK，不需另外建 unique index
- 避免 surrogate id 的 sequence 在大批 backfill 時變成熱點

**Alternatives considered**：
- 加 surrogate `id BIGSERIAL` PK + unique index 包前述四欄 → 多一個 index、寫入慢、無 query 優勢

### D3：sqlc INSERT 用 `ON CONFLICT DO NOTHING` 而非 UPSERT

**決策**：發生衝突直接跳過，不更新既有 row。

**Rationale**：
- Bitfinex historical candle 一旦封閉就不會變，重抓得到的值 byte-for-byte 一樣
- DO NOTHING 比 DO UPDATE 快（不寫入），重複跑 backfill 0 cost
- 若未來發現上游修正資料，可手動 TRUNCATE 重抓而非 UPSERT

**Alternatives considered**：
- DO UPDATE SET ... → 更慢、語意上錯（資料不該變）

### D4：Backfill runner 用 `cmd/backfill/main.go` 而非 HTTP endpoint

**決策**：獨立 binary，吃 CLI flags。

**Rationale**：
- 一次性 / 偶爾觸發的維運任務，不該污染 prod HTTP server
- 跑時可獨立看 log、kill、resume
- 未來改 cron job / GitHub Action 都直接 `go run ./cmd/backfill/...`

**Alternatives considered**：
- 加 admin HTTP endpoint → auth / route / handler 一堆樣板，不值得
- 寫成 fx lifecycle hook 開機自動跑 → 開發階段每次重啟都跑、無法控制時間

### D5：Idempotent backfill 採「順序遞推 + start/end 推進」

**決策**：runner 接受 `--start` 與 `--end`，內部用 `limit=10000` 循環，每次以上一批最早 mts - 1 ms 當下一批 end。

**Rationale**：
- Bitfinex API 預設新到舊回；用 `end` 推進省一次 sort 邏輯
- ON CONFLICT DO NOTHING 已防重，重跑不會炸
- 若中途斷線，可用 sqlc query `GetEarliestCandleMTS(symbol, timeframe, period_agg)` 接續（`--resume-from-earliest`，nice-to-have）

**Alternatives considered**：
- 用 `sort=1` 從舊到新推進 → 同樣可行；選新到舊純粹因為 API 預設方向，少一個 query 參數

### D6：Rate limit 控制用既有 `WithRateLimit` ClientOption + 自動 backoff retry

**決策**：
- 主限速：bitfinex client `NewClient(WithRateLimit(0.5, 1))` → 0.5 req/sec = 30 req/min
- 超限保險：`doHTTP` 偵測 HTTP 429 / Bitfinex error code 11010 / body 含 `"ratelimit:"` → 進入指數退避（60s → 120s → 240s → 240s，cap 4 次）
- 加 `WithRateLimitBackoffBase(d)` ClientOption（default 60s，test 用 50ms）讓 backoff 可注入測試

**Rationale**：
- 既有 client 已有 rate limiter；多一層 backoff 處理 limiter 失效或多人共用 IP 撞限
- 中央化在 `doHTTP`：所有 caller（含 lending engine）受惠
- backoff 4 次後仍失敗才 abort：避免無限重試 hang job；用戶看到「請稍候重跑」訊息明確

**Alternatives considered**：
- 視為 fatal 立即 abort → 短暫網路抖動就要手動介入，不友善
- 在 cmd 層 wrap → lending engine 撞到一樣痛
- 用 `cenkalti/backoff/v4` 套件 → 多依賴；既有 client 沒用 backoff lib（自刻 retry），保持風格一致

### D7：USDT 與 USD 並行 backfill

**決策**：runner 跑兩次 currency × 兩次 timeframe（4 combos），不平行。

**Rationale**：
- bitfinex rate limit 是 IP-based，平行也省不了時間（共享 quota）
- 序列簡化錯誤處理；一個失敗另一個不受影響
- 4 combos × ~少 requests/combo + stats，總共 ~7 分鐘，沒平行的必要

**Alternatives considered**：
- goroutines 平行 → 對 IP-shared limit 沒幫助、複雜化錯誤處理

### D8：cmd/backfill 內建 `--verify` 模式

**決策**：runner 跑完 backfill 若帶 `--verify`，自動執行兩件事：
1. **Gap detection**：對每個 (currency, tf) 跑 `lead(mts) OVER (ORDER BY mts) - mts` 檢查連續性，列出 > expected_interval × 1.5 的 gap
2. **FRR 單位驗證**：抽 5 對「同期 candle close × funding_stat FRR」，算 ratio，推測 FRR 單位（per-second / per-day / per-period），結果 echo 給使用者更新 wiki

**Rationale**：
- 資料層的「資料完整性」很容易在 backfill 完忘了驗，半年後 backtest 跑出怪結果才發現有 gap
- 內建到 cmd 不增加新工具，使用者一條 command 順手做掉
- FRR 單位驗證是 5/9 spot test 留下的明確 TODO，不順手解掉容易爛尾

**Alternatives considered**：
- 寫獨立 cmd/verify → 兩個 binary 維護成本高、容易漏跑
- 寫 SQL view → SQL 跑不了 print / FRR 比例分析

### D9：Repository 層不寫 _test.go

**決策**：順著 project 既有慣例（`backend/CLAUDE.md`：「每層用 in-memory mock repo 測試，不依賴 DB」），新增的 `FundingMarketDataRepo` 不寫 sqlc 整合測試。DB 互動由 task #5.3 smoke backfill + SQL spot check 涵蓋。

**Rationale**：
- 跟既有 5 個 repo（apikey/billing/config/execution/user）一致 —— 它們也沒 _test.go
- DB 互動測試需要 testcontainers / docker postgres 啟動成本，project 沒裝
- ON CONFLICT DO NOTHING 行為由 PostgreSQL 保證，不是 application bug 風險
- Smoke backfill 跑兩次同 command（task #5.3）天然驗證 idempotency

**Alternatives considered**：
- 加 testcontainers 寫 repository 測試 → 違背 project 慣例、增加 CI infra
- 加 sqlmock 模擬 → 只測「query 字串對不對」，無實質價值

### D10：Pagination cursor 邏輯抽純函數

**決策**：把分頁迴圈邏輯抽成 `paginateBackward(ctx, fetch fetchFunc, startMs, endMs, batchSize) error`，cmd 內部 helper（不對外 export）。

**Rationale**：
- 純函數可純單元測試（mock fetchFunc 餵假資料），覆蓋「正常 / 空回應 / range 早於資料 / cursor 不能跨過 startMs」四 scenario
- 業務邏輯（fetch + persist）與分頁邏輯分離

**Alternatives considered**：
- 寫成 method on cmd struct → 測試需要 setup struct 比較煩
- 不抽函數寫 inline → 「Range earlier than fUST history start」這個 scenario 測不到

## Risks / Trade-offs

- **[risk] Coarse fill 模擬可能讓 Stage 1.5 backtest 結果失真** → 「rate ≤ candle high 視為填到」高估 fill rate（實際 Bitfinex 用 lowest-ask 撮合，我可能總被低 rate 對手搶單）。**Mitigation**：Stage 1.5 跑完後評估 backtest 結果可信度；若 P&L 看起來「太樂觀」，加 trades layer 重跑（這是 brainstorming 選的金字塔策略明確接受的 trade-off）
- **[risk] Bitfinex rate limit 60 秒封鎖** → backfill 在 dev/prod 共用 IP 跑時，可能短暫卡到 lending engine 的 live API 呼叫。**Mitigation**：用 `WithRateLimit(0.5, 1)` 主動限速，留 50% buffer；新加的 backoff 自動處理短暫超限；backfill 預估 7 分鐘總耗時，影響極短
- **[risk] Bitfinex API 不同 timeframe 的歷史深度可能不一致** → 5/9 spot check 只測 1D，1h 真實深度可能更短或一樣。**Mitigation**：runner 接受 start/end，發現空回應自然停；`--verify` gap detection 會捕捉不正常缺漏
- **[risk] FRR 單位推錯讓策略誤算數量級** → 入庫後策略以日利率算實際是秒利率（差 86400 倍）。**Mitigation**：`--verify` 內建 FRR vs candle close 比例分析，推測單位後印出，使用者決定是否更新 `~/second-brain/wiki/tech/bitfinex-funding-rate-data-sources.md` 的「FRR 單位待確認」段
- **[risk] Neon dev/prod 同 DB，backfill 大量 INSERT 可能撞到 free tier 寫入限制** → 影響 lending engine prod 操作。**Mitigation**：總 row 數 ~110K 遠低於 Neon free tier；backfill 跑時段選 startup 工時（非 dailyfresh ops 高峰）
- **[trade-off] 不做即時增量同步** → 將來活用歷史資料做策略時要每天手跑 backfill。**Acceptance**：Stage 1 階段不需要 daily fresh 資料；Stage 2 paper trading 才考慮增量同步
- **[trade-off] 不開 marketdata package** → 未來真要長期 ingestion service 時 cmd 內 helper 要重抽到 package。**Acceptance**：YAGNI；現在 110 行 cmd 內部 helper 足夠

## Migration Plan

1. ~~改 `schema/schema.hcl` 加 `funding_candles` + `funding_stats` 兩 table block~~（5/9 已完成）
2. ~~`cd schema && atlas migrate diff add_funding_history_tables --env neon` → 產生 `migrations/20260509100512_add_funding_history_tables.sql`~~（5/9 已完成）
3. ~~`cd schema && source ../../.env && atlas migrate apply --env neon` → 套用到 Neon~~（5/9 已完成）
4. 實作 `internal/domain/funding_market_data.go` + `internal/bitfinex/client.go` 新 method + `internal/repository/postgres/query/funding.sql` + `cmd/backfill/main.go`（含 `paginateBackward` 純函數）
5. 跑 unit tests：`cd backend && go test ./internal/bitfinex/... ./cmd/backfill/...`
6. 跑 smoke backfill：`go run ./cmd/backfill -currencies UST -timeframes 1h -start 2026-05-01 -end 2026-05-09`
7. 跑完整 backfill：`go run ./cmd/backfill -currencies UST,USD -timeframes 1h,1D -start 2023-05-09 -end 2026-05-09 -verify`
8. 根據 `--verify` 輸出更新 `~/second-brain/wiki/tech/bitfinex-funding-rate-data-sources.md` 的 FRR 單位段

**Rollback**：因為新表獨立、不影響既有 5 表，rollback 只需要 `DROP TABLE funding_candles; DROP TABLE funding_stats;` + `atlas migrate down 1` 還原 atlas.sum

## Open Questions

- **`--resume-from-earliest` 是否 first cut 就有？** 傾向：略過。重跑 ~7 分鐘可接受。如果中途常斷再加。
- **`--verify` FRR 抽樣窗口找不到同期 candle 怎辦？** 設計：先 ±1 分鐘找；找不到 → 擴到 ±1 小時；仍找不到 → 印 "inconclusive"，不阻擋整個 verify 流程
