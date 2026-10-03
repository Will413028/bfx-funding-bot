---
title: Candle 不可變 + bitemporal 資料層，而非重建 live 策略狀態
date: 2026-07-27
status: active
tags: [bfx-funding-bot, decision, market-data, event-sourcing, bitemporal, backtesting]
---

# Candle 不可變 + bitemporal 資料層，而非重建 live 策略狀態

## Context

追查 `divergence_detected` 每小時 4/4 cells 100% 觸發（長期被當噪音）時發現：live MR EMA 偏離 replay
基準 **4.1e-2**，是容忍帶 `_REL_TOL=1e-4` 的 400 倍，也大於 code 自定的「real drift ~1e-2」。

根因鏈：`funding_candles` 唯一 live 寫入者是 WS `CandleWriter`，對同一 `mts` **in-place upsert 覆蓋**
`close`；scheduler 在 `T+30s` 讀 `mts=T-1h` 的**當下快照**餵 `strategy.observe()`，live EMA 單向累積，
之後的修正只進 DB **永不回饋**；`DivergenceReporter` 每 tick 從 DB **全量重建** → 必然分歧。
實測 132 筆 live 觀察值對 DB 現值 **23 筆（17.4%）不符，最大 -35.3%**。

影響不只告警：`LendDecision.rate` 直接取 `candle.close`，失真值**直接成為掛單利率**
（07-25 02:00 live `0.00023193` vs DB `0.00014999` = **+54.6%**）。

Prior state：Phase 4.1 建 dual-compute reporter（design Q4）；Phase 4.3 LOCF 把 warmup 與 replay 統一到
`build_strategy_at_boundary`，其 docstring 宣稱「given (history, ref_mts, budget_hours) 即 deterministic」
——**該前提在 candle 可被事後修正時從一開始就不成立**，容忍帶只是把破裂壓到看不見。

## Options Considered

- **A. live 每 tick 從 DB 重建策略狀態**：放棄增量累積，live 直接用 `build_strategy_at_boundary` 結果。
- **B. 延後 observe 到 candle 定稿**：加大 `scheduler_buffer_s` 或等下一根 bar 出現才處理前一根。
- **C. bar 不可變 + 定稿判定 + bitemporal 資料層（選用）**：資料層加 revision / knowledge time，
  只有定稿 bar 能進策略；live 維持增量。

## Decision

- **D1**：選 C。分層落地 L1 schema（`revision` + `observed_at_ms`，append-only）→ L2 `CandleWriter`
  偵測 mts 前進即標前一根 final、策略只 observe `is_final` → L3 驗 live 增量與 replay 重建 byte-match。
- **D2**：既有 WFO / OOS 結論全部標記**未確認**。但**不 gate 在「重跑」上——歷史 point-in-time 值已
  永久遺失**（見 Rationale），只能做敏感度分析 + 從今起累積 revision。
- **D3**：L2 落地前 canary 暫停下新單（`BFX_ALLOCATION_CAP_USDT=0`，已執行）。**⚠️ 此手段無效，當日即推翻——見 Updates。**

## Rationale

- **D1**：C 修的是被破壞的不變式本身（輸入不可變是決定性重放的前提），修好後 A 的重建與現行增量會收斂
  到同一答案，divergence 告警自然恢復鑑別力。**不選 A**：A 把 live 對齊到「as-of-now」基準，但 live 在真
  實世界永遠拿不到未來的修正——等於對齊一個不現實的參考；且歷史 bar 被修正時 EMA 會跳變，策略狀態不連續。
  **不選 B**：只降低機率不消除（修正可能數分鐘後才到），且延遲決策時效；`scheduler_buffer_s` 5s→30s 已經
  是這條路走過一次的失敗嘗試。**代價**：C 要改 schema + 寫入路徑，工作量最大。
- **D2**：backtest fixture 用已修正值、live 用未修正值 → 結論建立在 live 拿不到的資訊上，系統性樂觀。
  **關鍵限制**：`funding_candles` 無 revision 也無時間戳，歷史每根 bar「當時的值」已被覆蓋、不可回復；
  REST 重抓拿到的仍是最終值。相較「等重跑再說」（永遠等不到），選擇承認不可重跑、改用敏感度分析回答
  「結論脆不脆弱」；代價是拿不到真實的 live 可實現績效，cap 加碼與 B3 排程都得延後。
- **D3**：相較「邊修邊跑」，暫停的代價只是放棄一段放貸收益；不暫停的代價是繼續用失真價格掛單。
  選 cap=0 而非停容器：`allocate_gap` 見 `gap < min_fill` 直接 `return {}`，零新單，而 daemon 續跑
  保住 reconcile 與可觀測性；已借出 credit 不受影響，自然到期退場。
  **⚠️ 這段推理當日即被證偽**：reconciler 解析的是 `caps[symbol]`（仍為原 cap），env 的 0 從未進入
  `allocate_gap`，gap 也就從未 `< min_fill`。「daemon 續跑保住可觀測性」的目標仍成立，但要靠
  `BFX_KILL_SWITCH` / `trading_halt` 達成，不是靠 cap。見 Updates。

## Expected Outcome

- `divergence_detected` 觸發率由 100% 降至接近 0；殘餘觸發即真分歧（告警恢復鑑別力）
- 掛單利率不再出現 +40~55% 的離譜偏離 → E2 `reprice_cancelled` 與 unfilled 應同步下降
- 敏感度分析若顯示結論對失真不敏感 → 原 WFO/OOS 結論可回升為「已驗證」；若敏感 → 需重新設計研究基準

## Followup

- ✅ L1 schema migration（最小版 `is_final` + `finalized_at_ms` + `first_seen_at_ms` + append-only `funding_candle_revisions`）—— `f9b994f`，migration `c5a7e2d94f18`
- ✅ L2 定稿判定；策略只 observe `is_final` —— 實作需**雙路徑**（寫入即判定 ＋ 期轉換補封 ＋ 讀取前 `seal_closed_periods`），見 Updates
- ✅ L3 驗收 —— `39c8d50`；28 tick 中期 `divergence=0` 且 LOCF 補格消失。完整 24h 版 07-28 22:52 UTC PASS（`divergence=0/tick=96`，2026-08-05 查核確認）
- ✅ L4 敏感度分析 —— v2 N=500（`db076de`）+ 2× 頻率壓測（`f633a6e`）
- 恢復交易（gate：24h L3 驗收通過）。**恢復手段是 `POST /admin/resume?confirm=true`，不是改 cap**——原文「恢復 canary cap」建立在 cap=0 有效的錯誤前提上
- ✅ 直接觀測 WS 對已收盤 bar 的 upsert —— 不再是推論：`funding_candle_revisions` 243 筆即直接證據

## Invariants

- 進入 `strategy.observe()` 的 candle 必須已定稿，且此後不再變動
- 同一 `(symbol, timeframe, period_agg, mts, revision)` 的值永不覆寫
- L1 之後產生的 fixture 必須用 point-in-time 值；L1 之前的歷史只能標為 as-of-now 並註明限制

## Revocation Triggers

- ~~當直接觀測證實 Bitfinex WS 對已收盤 bar 永不修訂 → L1 可從 bitemporal 降級為單一 `is_final` 旗標~~
  **已否證（2026-07-27 14:20 UTC）**：`funding_candle_revisions` 累計 **243 筆**（03:00~11:00 UTC，幅度 −13.8%~+1.7%，非浮點誤差）。WS 確實會對已收盤 bar 推不同的值，`where is_final = false` 正在攔截並留痕。**bitemporal 不可降級。**
- 當敏感度分析顯示績效結論對失真不敏感 → D2 的「全部未確認」可解除，回到原 gate 節奏

## Lessons

### Rules

- **R1**：`divergence_detected` 長期 100% 觸發被當成噪音，實際上它一直在正確地叫——真訊號被自己的滿觸發率
  淹沒。`Rule: 告警 100% 觸發時，先驗「它是不是在正確地叫」再調容忍帶；滿觸發的 parity 檢查等於已經關掉，
  不是待調校。`
- **R2**：有狀態策略 + in-place 可變的市場資料 = 靜默分歧，且無時間戳時事後不可證。
  `Rule: 餵給有狀態計算的資料一律不可變（append-only + knowledge time）；輸入不可變是決定性重放的前提，
  不是效能最佳化。`

### Observations

- **O1**：用 EMA 衰減反推（`alpha=0.08`，`0.92^n = 4e-2 → n≈38`）在零 instrumentation 下就定出了問題規模，
  與後續實測「23/132 slot 被修正」量級一致
- **O2**：`funding_candles` 無任何時間戳，證據只能靠比對 7 天容器 log 取得——再晚幾天 log rotation 後，
  這個 bug 將無法被證明，也是 D2「不可重跑」的同一個根源

## Updates (2026-07-27)

- ✅ **L3 驗收通過**（`39c8d50`，2026-07-27 02:05 UTC）：`divergence=0 / tick=4`，軌跡 `4.1e-2 → 9.4e-3 → 6.8e-3 → 5.2e-3 → 0`。共四個根因，**③④ 都是引入 final-only 讀取自己造成的**：③ `get_candles_in_range` 漏改（warmup 走它）④ **封存時序競態**——`CandleWriter` 只在「更晚 mts 到達」時封存，但 scheduler 在 `T+30s` 讀 `mts=T-1h`，venue 稍慢就讓該列在需要的當下仍未封存，final-only 讀取跳過它、LOCF 從前一根補格（live 證據：`scheduler_tick` 恆 `raw=3 filled=4`），live 序列出現 replay 沒有的缺口。修法 `seal_closed_periods`：**時間本身即充分證據**，已過完的期不可能再有成交，故每次策略讀取前先封存，不依賴 venue 推送時機。
- **離線重現是轉折點**（`34e1ab4`）：序列連續時兩臂 gap 僅 `1e-9`，低於容忍帶五個數量級 → 架構與 `_REL_TOL` 皆無問題，分歧純來自序列缺口。腳本自身的 off-by-one（從 `boot+step` 起 walk）反而有用——它證明「漏一根 ≈ 1.5e-2」與生產殘差同量級，直接指向漏根。
- **D3 暫停維持**：cap 仍為 0。gate 已部分達成但樣本僅 4 tick，等 24h 穩定性驗收（`bfx-l3-verify-24h.timer`，07-28 10:13 CST）+ L4 v2 完整結果。
- **D1 的 C 方案在實作中補了一條**：封存需要**雙路徑**——寫入即判定（`mts` 早於當期即落地為 final）＋期轉換補封（`CandleWriter`）。只有後者時 REST backfill 寫的歷史永不封存，final-only 讀取看不到、`fetch_and_store` 寫後讀回回空。原 Decision 只寫了後者，是不完整的。
- **D2 的 L4 第一版作廢，v2 引擎已修**（`6ac23a7` → `052e81c`）：擾動單一 close 序列使誤差在決策端與收益端互相抵消，實驗幾乎不可能失敗故不構成證據。v2 讓 `run_backtest` / `evaluate_oos_windows` 收 optional `market_candles`（按 mts 對應、預設等於觀察序列），策略觀察失真序列、fill/PnL 用真實序列。v1 擾動後「變好」、v2 如預期受損（smoke N=2：−3.9%~−7.4%）——這個反轉本身就是修正生效的驗證。**N=500 完成前 WFO/OOS 維持未確認。**
- **原記「revisions=0、trigger 傾向成立」已被推翻**：那是部署後數小時的樣本，14:20 UTC 實測 243 筆（見 Revocation Triggers）。`Rule: 用「至今沒發生」論證一個保護機制可以拿掉時，樣本長度必須先跨過該機制的觸發週期——否則證明的只是自己看得不夠久。`
- **D3 的暫停手段當日即推翻，最終落在 DB**：`BFX_ALLOCATION_CAP_USDT=0`（`3ad142c`）是 **no-op**——它只是 symbol 缺席於 `caps` 時的 fallback，而 fUST/fUSD 都在其中，bot 續掛數小時（3 筆完整 INTENT→CLAIMED→ORDER_FILL）。當時「驗證」是 `docker exec printenv` 讀回 0 = 驗證輸入而非結果。改用 `BFX_KILL_SWITCH`（`3481f5b`）後，同日再移入 DB `trading_halt`（`ddaf792`/`fe6de1d`，append-only、fail-closed），env 已從 `canary.env` 移除並實測「env 不在、交易仍停」。現況可查：`GET /admin/trading-status`、`POST /admin/dry-evaluate`（`25b7d5e`）。
- **L4 v2 完成 → D2 可部分解除**：N=500（`db076de`）+ 2× 頻率壓測（`f633a6e`）。「每月皆正」在兩組各 2000 次擾動下 **100% 維持**；月中位數 1× 打 3~7% 折、2× 打 8~12%，`share_of_runs_below_baseline_median` = 1.0（損害嚴格單向，反證 v1 的「擾動後變好」是引擎缺陷）。建議 WFO/OOS 改標「穩健性已驗證、數值下修 8~12%」，**不回復原值**——失真頻率只知下限。
- **L3 中期驗收（14:20 UTC，28 tick）**：`divergence=0`、**LOCF 補格消失**（`raw=3 filled=3`，先前恆 `filled=4`）→ `seal_closed_periods` 確認生效。完整 24h 版 07-28 02:13 UTC。

## Amendment (2026-09-27): L4 唯一原始證據內嵌

### Amendment Decision

- 失真樣本是 L4 唯一倖存的輸入（container log 已在 07-26 recreate 時消失，`funding_candles` 在 L1 前沒有時間戳，**不可重新取得**），全文內嵌如下。範圍：只有 `fUSD`/`a30`/`1h`，07-19~07-26，比對 132 筆、23 筆不符（17.4%，只算被 divergence 偵測到的，所以是下限）；`LIMIT 20` 截掉了最舊 3 筆。`pct=(db_final−live)/live×100`，反推 `live = final/(1+pct/100)`：
  `-0.068 -33.924 -1.024 13.682 -0.191 -22.241 0.111 -4.547 -35.330 2.602 0.508 0.862 5.532 0.032 -0.673 0.481 0.021 3.671 -9.792 1.443`
- L4 v2 各 cell 月中位數（baseline → 擾動 p50）。1×（rate 0.174）：fUST_a30 0.5725→0.5500、fUST_p2 0.5701→0.5427、fUSD_a30 0.6218→0.6021、fUSD_p2 0.6224→0.5761；2×（0.3485）：0.5255／0.5174／0.5712／0.5449。重現：`cd backend && uv run python scripts/run_distortion_sensitivity.py --runs 500`（先用 `--runs 1` 量單輪耗時）。

## Related

- **來源（Provenance）**：本 ADR 即原始紀錄，與 Claude Code 討論當場拍板，無外部來源文件
- L4 報告（原始 `61e6db0`、`db076de`、`f633a6e`）：研究報告 2026-07-27-candle-distortion-sample、2026-07-27-l4v2-distortion-sensitivity、2026-07-27-l4v2-distortion-2x（原文不在本 repo）
- 暫停 commit：`3ad142c`（`deploy/vm/canary.env` cap 改為 0，2026-07-27 deployed 並驗證 bot 進程讀到 0）
- 相關 ADR：[2026-07-06-profit-review-execution-layer-first](2026-07-06-profit-review-execution-layer-first.md)（「bot 輸 AlwaysFRR 主因是價格面」的歸因受本決策影響）、[2026-07-10-strategy-review-e2-enforce-and-chunking-verdict](2026-07-10-strategy-review-e2-enforce-and-chunking-verdict.md)
