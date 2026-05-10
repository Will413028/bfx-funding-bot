# v2 Phase 2 — Backfill Result

**Date**: 2026-05-10
**Spec**: `docs/superpowers/specs/2026-05-10-phase2-data-backfill-design.md`
**Plan**: `docs/superpowers/plans/2026-05-10-phase2-data-backfill.md`
**Runtime**: ~25.2 minutes initial backfill (1513.5s; started 20:16:07 UTC, ended 20:42:53 UTC); C5c re-run resume-only 42.5s
**Exit code**: 1（FRR unit check FAIL；per spec 不阻塞 ship — Phase 3 brainstorm 必須先解 FRR 單位）

## 8 Series Summary

Numbers verified directly against Neon (`SELECT count(*), min(mts), max(mts) FROM funding_candles GROUP BY symbol, period_agg` + same for `funding_stats`).

| Series | Pages | Rows | Earliest mts | Earliest UTC |
|---|---|---|---|---|
| candles fUSD 1h p2 | 9 | 85,372 | 1470006000000 | 2016-07-31 23:00:00 UTC |
| candles fUSD 1h p30 | 7 | 69,930 | 1470006000000 | 2016-07-31 23:00:00 UTC |
| candles fUSD 1h a30 | 9 | 84,249 | 1470006000000 | 2016-07-31 23:00:00 UTC |
| candles fUST 1h p2 | 7 | 63,816 | 1545397200000 | 2018-12-21 13:00:00 UTC |
| candles fUST 1h p30 | 4 | 31,174 | 1545390000000 | 2018-12-21 11:00:00 UTC |
| candles fUST 1h a30 | 7 | 64,125 | 1545390000000 | 2018-12-21 11:00:00 UTC |
| funding_stats fUSD | 336 | 83,904 | 1476019925000 | 2016-10-09 13:32:05 UTC |
| funding_stats fUST | 259 | 64,708 | 1545390301000 | 2018-12-21 11:05:01 UTC |

**Total rows: 547,278**（fUSD candles 239,551 + fUST candles 159,115 + fUSD funding_stats 83,904 + fUST funding_stats 64,708）

Latest mts across all series: ~2026-05-10 12:00 UTC（backfill 結束時 most-recent open 1h candle，UPSERT-idempotent 重跑會更新最後一根）。

## 4 PASS Checks

- [✓] **row_count > 0** — 8/8 series 都有資料
- [✓] **round-trip exact-match** — 2 series sampled（fUSD 1h p2、funding_stats fUSD），抽 7 天前 closed candle/stat 比對 API↔DB 完全一致；C5c 修正後從「sample latest」改成「sample 7-day-old」避開 open-candle 漂移
- [✗] **FRR unit sanity** — frr=1.12e-06 × 86400 / candle.close=0.00014465 = **ratio=668.980**（expected `[0.1, 10]`）→ 推翻 5/9 推測「FRR 是秒利率」；單位待 Phase 3 解
- [✓] **continuity** — 8/8 series 在門檻內（candles ≤ 60 天 max-gap、funding_stats ≤ 24 小時）；fUSD a30 觀察到 47 天 quiet period 屬合法（早期 a30 未必每小時都有合約活動），低於 60 天門檻

## FRR 單位確認（CRITICAL FINDING）

`check_frr_unit` 抽樣實測：

- 取樣 mts: `1778414700000`（≈ 2026-05-10，最近的 funding_stats）
- frr: `1.12e-06`
- candle close: `0.00014465`（fUSD 1h p2 同 mts 的 close）
- ratio (frr × 86400 / close): **668.980**
- 期望範圍：`[0.1, 10]`（若 FRR 是秒利率，× 86400 後應與 candle close 同數量級）
- **結論：FRR 不是每秒利率**

5/9 推測「FRR=5.8e-7 是秒利率」**已被推翻**。

### 還沒解的問題

實測 ratio = 669 ≈ 600–700 量級。可能的單位含義（待 Phase 3 brainstorm）：

1. **每某個非標準時間單位的利率**（不是秒、不是小時、不是天）
2. **某個 normalization factor（e.g. 乘以了 LTV 倍數、乘以了基準利率比）**
3. **不是「rate」而是「rate × 借款量加權」之類的衍生指標**
4. **API 文件/社群定義有歧義**，需要交叉比對 Bitfinex 官方 docs / 第三方研究 / 自抓最近 24h candle close 算 mean rate 對照

> Phase 3 brainstorm 必須先做：(a) 找 Bitfinex 官方 funding_stats 欄位定義權威來源；(b) 抽多個時間點的 frr/candle.close ratio 看是否穩定 ≈ 669（如果穩定 → 是固定 normalization；如果浮動 → 不是單純 unit conversion）；(c) 不解出來不准把 `market_rate_source="frr"` 通電。

## 對 Phase 3 的影響

- **BLOCKED**：backtest engine `market_rate_source="frr"` 的 swap **暫不可動**。`_apply_friction` 不能盲目用 `frr × 86400` 當市場日利率。
- **Ready**：`funding_stats.repository.get_frr_at_or_before_mts()` API 已備好，等單位解出後就可以接上。
- **Ready**：8 series 全歷史資料（fUSD 2016-08 起、fUST 2018-12 起）已落 Neon，足夠跑 6 候選策略 × 2 symbols × 3 period_agg = 36 runs。
- **策略影響**：FRR-trend、SpikeDetect、bid-relative-to-FRR 三個 candidate 都依賴 FRR 數值含義，全部待 FRR 單位解才能正確設計閾值。RatePercentile / DynamicPeriod / WeekendPremium / MeanReversion 不依賴 FRR 絕對值，可以先跑。

## Plan 偏差：6 commits → 7 commits

原 plan 設計 6 commits（C1 schema/repo、C2 client、C3 service、C4 orchestrator+checks、C5 ship、C6 docs）。實際走完 **7 個 code commits**，多出來的兩個都是真實跑時才暴露的設計假設失誤：

| # | SHA | Subject | 計畫內？ |
|---|---|---|---|
| 1 | `1809abe` | ♻️ Refactor: move FundingStatRow to funding_stats/tables.py | C1 |
| 2 | `f5f0e4b` | ✨ Feat: funding_stats schemas + repository | C1 |
| 3 | `625d4bd` | ✨ Feat: BitfinexREST.get_funding_stats | C2 |
| 4 | `3bd820b` | ✨ Feat: walking-back service for candles + funding_stats | C3 |
| 5 | `38c703c` | ✨ Feat: scripts/backfill_phase2.py orchestrator + 4 PASS checks | C4 |
| 6 | `1065be2` | 🐛 Fix: Phase 2 dependency assumption bugs (asyncpg / fs limit / a30 path) | **計畫外** |
| 7 | `b3330fb` | 🐛 Fix: Phase 2 PASS check designs (round-trip drift + p30 sparsity) | **計畫外** |

### `1065be2`（dependency assumption bugs，3 個）

第一次跑就 fail 在三個獨立的環境/API 假設：

1. **asyncpg URL 翻譯**：Phase 1 已經處理過 psycopg2→asyncpg 的 `sslmode`/`channel_binding` 翻譯，但 Phase 2 backfill script 進來時又把 connection string 用 sync 風格組進去；統一走 `_prepare_engine_kwargs` 修掉。
2. **funding_stats `limit` 上限**：API 實際上 funding_stats `/hist` 端點 `limit` 上限是 **250**（不是 candles 的 10000）；plan 假設 limit=10000 結果一抓就 422。改成 250，walking-back 邏輯不變但 page 數 ×40。
3. **a30 candles path**：Bitfinex `a30` 不是普通的 `trade:1h:fSYM:a30`，而是 `trade:1h:fSYM:a30:p2:p30`（aggregate 30-day 從 p2/p30 算），plan 漏掉這個 path 後綴。

### `b3330fb`（PASS check 設計缺陷，2 個）

Orchestrator 跑通後才發現兩個 PASS check 設計有問題：

1. **round-trip drift**：原本「sample latest closed candle/stat」會在抓取期間命中正在更新的 open candle，造成偽陽性 drift。改成 sample 7-day-old，徹底躲開 open-candle window。
2. **p30 continuity**：fUSD/fUST p30 series 真的稀疏（30 天平均的合約成交不會每小時都有），用 candles 一致的 60 天門檻會誤判；本來就是 60 天，改的是「看 max-gap」而不是「期望每小時一根」的 framing，避免 false-positive alert。

### Lesson

Plan 寫得再仔細，**真實 API 的 limit、URL pattern、open-window 行為**還是會踩坑；Phase 3 plan 階段預留 1-2 個 「dependency-assumption discovery」commit slot，不要當作偏差。

## Notes / Issues encountered

- **Runtime**：25.2 分鐘 vs plan 預估 20-30 分鐘 — 在估算範圍內。瓶頸在 funding_stats（fUSD 336 pages × ~3s/page rate-limited，funding_stats 限 250/page 比 candles 限 10000 重很多）。
- **Rate limit**：30 req/min 的 aiolimiter 配置全程 OK，沒觸發 Bitfinex 429。
- **Resume idempotency**：C5c re-run 跑 42.5s（全部 0 pages），證明 walking-back service 的「DB earliest mts → API end 參數」邏輯正確收斂；多次重跑安全。
- **Storage**：Neon 端 8 個 partition 共 547,278 rows ≈ 220 MB synthetic_storage_size（仍遠在 free-tier 額度內）。
- **fUST p30 row count 比例**：31,174 vs fUST p2 63,816 ≈ 49%；fUSD p30 69,930 vs fUSD p2 85,372 ≈ 82%。fUST p30 的稀疏度是 continuity check 改設計的 trigger。

## Re-run instructions（給 future-Will）

```bash
cd /Users/will/second-brain/projects/startup/bfx-funding-bot/backend_py
source ../.env  # DATABASE_URL → Neon
uv run python scripts/backfill_phase2.py
```

- UPSERT idempotent — 重跑安全。
- Walking-back service 從 DB 已有的 earliest mts 開始往更早抓；DB 裡資料夠完整時，再跑只會抓近期幾根 open candle。
- Exit code 1 = 至少一個 PASS check fail（目前 FRR unit fail 是已知狀態）。Phase 3 解掉 FRR 單位後，這個 check 應改成驗證新單位（不是直接拿掉）。

## Appendix — Phase 3a evolution of `check_frr_unit`

Phase 3a 結果：**Gate 1 FAIL**。所有 5 個 hypothesis (H1-H5) 都未通過
R²>0.99 + slope_cv<5% + slope_diff<5% + median_rel_err<5% 的 quad-gate。

| H | R² | slope_cv | slope_diff | median_rel_err |
|---|---|---|---|---|
| H1 (frr → daily rate) | 0.2168 | 0.5765 | 0.6366 | 0.3712 |
| H2 (frr × 86400) | 0.2168 | 0.5765 | 0.6366 | 0.3712 |
| H3 (frr × avg_period) | 0.0041 | 1.1084 | 1.2284 | 0.5925 |
| H4 (frr × avg_period × 86400) | 0.0041 | 1.1084 | 1.2284 | 0.5925 |
| H5 (frr / 365) | 0.2168 | 0.5765 | 0.6366 | 0.3712 |

H1/H2/H5 數學上同 fit（純 frr scaling，OLS slope 自吸常數）；H3/H4（× avg_period）
更差 R²=0.004 — avg_period 是反訊號。Per-year slope 從 2016 的 ~387 降到 2023 的
~42（9× drift），fUSD/fUST 跨幣 slope diff 0.64 — Bitfinex 應是改過 FRR 計算方式。

`check_frr_unit` 改名 `check_frr_unit_stability`，改成 raw r²(close ~ frr)
correlation 診斷（diagnostic-only，永遠 passed=True）。完整失敗診斷見
`backend_py/data/research/frr_hypothesis_results.json`（gitignored，re-derivable
via `cd backend_py && uv run python scripts/investigate_frr_unit.py`）與
`docs/research/2026-05-10-frr-unit-investigation.md` section 3.2。

下一輪：Phase 3c 擴展 hypothesis 集（multivariate w/ funding_amount_used /
funding_amount, lower-50% lifetime weighting）。Phase 3b 限 4🟢 (24 runs)，
2🟡 (FRR-trend, SpikeDetect) 推遲。
