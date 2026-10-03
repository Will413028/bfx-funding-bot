---
title: 歷史資料回填走到 Bitfinex 最早一筆，以 DB 最舊 mts 當續跑游標，不設固定時間窗
date: 2026-05-10
status: active
tags: [bfx-funding-bot, decision, spec-compression, backfill, bitfinex, data-quality]
related-commits:
  - "4f2f58c^..52bda30"
  - d64fb26
---

# 歷史資料回填：walking-back 到最早一筆＋DB 衍生續跑游標

## Context

Phase 1 回測引擎用 candle close 當市場利率；Phase 3 的候選策略矩陣（2 幣 × 3 period_agg × 6 策略）需要全歷史 1h candle，其中 FRR-trend／SpikeDetect／bid-relative-to-FRR 三個還需要真的 FRR。**Prior state**：2026-05-09 Go 後端的 Stage 1.2 plan（`d64fb26`）只抓固定 3 年窗（`--start 2023-05-09 --end 2026-05-09`）、1h＋1D、`INSERT … ON CONFLICT DO NOTHING`，另附 `--verify` 用單一比值分桶推 FRR 單位（~169 → per-second）。同日決定整個後端改寫成 Python，該 plan 沒有執行；Phase 2 在 Python 端重新設計回填。

**約束**：

- `external` Bitfinex public REST 限流（與 live 共用同一 IP；當時用 30 req/min limiter）；`/v2/funding/stats/f{sym}/hist` 的 `limit` 上限實測 **250**（500 以上回 HTTP 500），candles 是 10000。
- `external` `a30` candle 的 key 必須寫成 `trade:1h:fSYM:a30:p2:p30`；單寫 `a30` 回空陣列。
- `external` funding stats 回 12 元素陣列，只有 index 0/3/4/7/8/11（MTS／FRR／AVG_PERIOD／FUNDING_AMOUNT／FUNDING_AMOUNT_USED／FUNDING_BELOW_THRESHOLD）有值；官方文件只列欄名、不列 index，位置以實測＋官方 `bitfinex-api-py` serializer 交叉確認。
- `inherited` 資料庫當時是 Neon free tier（容量上限）；**現在已不成立**（改自托 Postgres），不影響本決策。
- `inherited` 回測需要「一個 series 從最早到現在沒有中間空段」；現在仍成立（研究與 walk-forward 都吃全歷史）。

## Options Considered

- **基準. 固定區間＋外部 checkpoint 狀態**：ETL 慣例是設定抓取區間，進度寫進獨立 state（[Singer spec 的 STATE message](https://hub.meltano.com/singer/spec#state)、Airbyte incremental cursor），中斷後從 state 續跑。
- **A. 固定 3 年時間窗、DO NOTHING、一次跑完（5/9 Go plan）**：範圍可預估（~110K candle＋~52K stats），中斷只能整段重跑或手調 `--start`。
- **B. walking-back 到最早一筆，游標從 DB 推導（選用）**：每個 series 從 `min(mts) - 1`（空表則 now）往回翻頁，直到 Bitfinex 回空頁或短頁；UPSERT 可重跑；跑完做資料品質檢查。

## Decision

- **D1 範圍與游標**：選 B。每個 series 回填到 Bitfinex 最早一筆，不設時間窗；續跑游標 = DB 的 `min(mts) - 1`，不另存 checkpoint。
- **D2 終止與防呆**：空頁或 `len(page) < limit` 即視為到底；`oldest_mts >= end_ms`（游標沒前進）直接 raise `BackfillCursorStuck`，不靜默重試。
- **D3 交易邊界與失敗隔離**：每頁 `flush`（讓 `min(mts)` 即時反映），每個 series 一個 transaction；8 條 series 循序執行，單條失敗記錄後繼續其他條。exit code 0 PASS／1 檢查失敗／2 有 series 失敗／3 運作錯誤；`--dry-run` 不打 API 不寫 DB。
- **D4 冪等寫入**：`ON CONFLICT DO UPDATE`，而非 A 的 DO NOTHING——重跑要能更新最後一根還在變動的 candle。
- **D5 回填後檢查**：(1) 每條 series row_count > 0；(2) 抽 **7 天前**的已定稿資料與 API 逐欄比對（容差 1e-15）；(3) FRR 單位 sanity：`frr × 86400 / close` 須落在 [0.1, 10]；(4) 連續性：candles 相鄰最大間隔 < 60 天、funding_stats < 24 小時。
- **D6 範圍**：fUSD／fUST × 1h × p2／p30／a30 candles ＋ 兩幣 funding_stats（只存上述 5 個欄位）；排程增量同步、多 timeframe、其他幣種、進度 dashboard 都不做。

## Rationale

- **D1**：B 不需要知道 Bitfinex 各 series 的上線日，空頁就是天然訊號；續跑狀態就是資料本身，不會有「state 說做完了、資料其實缺」的分歧。**不選基準**：外部 state 對一次性、單人維運的 runner 是多一份要同步的真相。**不選 A**：3 年窗讓 walk-forward 的樣本被資料量卡住，而且 fUSD 實際可追溯到 2016-07。代價：游標只看最舊一筆，**中間的空段它看不到**，所以 D5(4) 的連續性檢查是這個選擇的必要配套，不是可有可無。
- **D2**：cursor-stuck 選擇 raise 而非重試，因為 API 行為一旦改變，靜默重試就是無限迴圈。空頁即到底是對 funding candles／stats 端點的假設，換成別的端點不一定成立（見 Revocation Triggers）。
- **D3**：per-series transaction 讓失敗的 series 整段 rollback，下次從乾淨的 `min(mts)` 續跑；相較整批一個 transaction，一條失敗不會拖垮其他七條。service 只負責「把一條 series 走完」，series 矩陣只有 orchestrator 知道。
- **D5**：(2) 原本抽最新一筆，會撞到還在形成中的 candle 造成假 drift，所以改抽 7 天前。(4) 原設計用「實際筆數／期望筆數 ≥ 95%」，但 p30、a30 天生稀疏（fUSD a30 有 47 天的合法空窗、fUST p30 筆數只有 p2 的 49%），筆數比會誤報；改看最大間隔，60 天仍抓得到續跑邏輯漏掉幾個月的 bug。(3) 容差故意寬一個數量級，只抓差兩三個數量級的單位錯誤。

## Result

- `git log --oneline 4f2f58c^..52bda30`：spec、plan、5 個計畫內 commit，加上 2 個計畫外修正：`1065be2`（asyncpg URL、stats limit 250、a30 path）和 `b3330fb`（D5 的抽樣與連續性改法）。
- 首次回填 25.2 分鐘、547,278 列（fUSD candles 239,551、fUST candles 159,115、fUSD stats 83,904、fUST stats 64,708）；最早 fUSD candle 2016-07-31、fUST 2018-12-21；限流全程沒有觸發 429。重跑只花 42.5 秒、0 頁，證明續跑收斂。
- D5：(1)(2)(4) 通過；(3) **失敗**（ratio 669），推翻「FRR 是秒利率」的推測，後續見 [2026-05-10-frr-unit-gated-investigation](2026-05-10-frr-unit-gated-investigation.md)。這項檢查之後先降級成診斷，再由 [2026-05-28-frr-not-a-market-rate-proxy](2026-05-28-frr-not-a-market-rate-proxy.md) D3 移除；現行 `modules/backfill/checks.py` 只剩三項。
- D4 後來被 [2026-07-27-candle-immutability-bitemporal](2026-07-27-candle-immutability-bitemporal.md) 收窄：UPDATE 只作用在 `is_final = false` 的列，已定稿 candle 不再被回填改寫。
- 重跑：`cd backend && uv run python scripts/backfill_phase2.py`（UPSERT 冪等）。funding_stats 往前補齊由 `backfill_funding_stats_to_latest`（weekly chain 的 `ingest_funding_stats`）負責；429 退避在 `0f80647` 補上。

## Revocation Triggers

- 某個端點出現「空頁但更早還有資料」→ D2 對該端點不成立。liquidations feed 已經發生過（要往前探一段才能確認到底），walking-back 套到新端點前要先實測。
- 回測或研究開始需要 1h 以外的 timeframe，或 fUSD／fUST 以外的幣種 → 重評 D6。
- 回填改成常駐排程 → D3 的循序＋一次性 runner 假設要重評（鎖、與 live 共享限流額度）。

## Lessons

### Observations

- **O1**：計畫寫得再細，真實 API 的 limit、URL 形式、未定稿資料的行為還是會多出 1–2 個修正 commit（Plan deviation 是常態）。

## Related

- 來源（壓縮自 SDD，目錄有追蹤）：
  - `2026-05-10-phase2-data-backfill-design.md`（原文已不在 repo，本 ADR 即紀錄）
  - `2026-05-10-phase2-data-backfill.md`（原文已不在 repo，本 ADR 即紀錄）
  - `2026-05-10-phase2-result.md`（原文已不在 repo，本 ADR 即紀錄）
  - Prior state（Go plan，未執行即被改寫取代）：`2026-05-09-bitfinex-funding-historical-backfill.md`（原文已不在 repo，本 ADR 即紀錄）
- Go 時代 backfill openspec 的處置（兩表分開、複合 PK、one-shot runner、循序抓取延續）：[2026-09-23-go-era-openspec-design-disposition](2026-09-23-go-era-openspec-design-disposition.md) D6
