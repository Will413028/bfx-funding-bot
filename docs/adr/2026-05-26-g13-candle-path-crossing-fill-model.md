---
title: G13 回測 fill model 改用 funding trade candle 的 path-crossing 實證模型，而非 order-book queue 或自有成交回饋
date: 2026-05-26
status: active
tags: [bfx-funding-bot, decision, backtesting, fill-model, bitfinex]
related-commits:
  - 082a3ac                # spec
  - 22dabb3                # plan
  - "332caf2^..8074d70"    # 8-task 實作 + 收尾
---

# G13 回測 fill model：candle path-crossing 實證模型

## Context

ROADMAP Phase G 的 G13（數據驅動定價取代靜態常數）。當時所有 fill 假設都是無回饋的靜態猜測：回測引擎唯一的 fill model 是 `compute_fill_prob(spread_pct, fill_alpha=5.0)` 的合成線性曲線，任意常數卻驅動 strategy selection 與 WFO。要換成「每個 rate bucket 實際成交機率＋time-to-fill」的實證模型。prior state：線性模型是 v2 Phase 1 engine upgrade（`0c7976e`）加的摩擦假設。

**約束**：

- `external` 手上只有 `funding_candles`（**已成交**的 funding trade rate OHLC，shadow 累積數週＋REST backfill 1–2 年）與離線 `funding_stats.frr`：Bitfinex 公開資料的取得現況。
- `external` 沒有 order book depth／bestAsk／ticker／trades feed，也沒有自有真實成交（canary 閒置 = 0 fill，shadow 走 paper executor）：當時的 ingestion 與營運狀態。
- `external` Bitfinex funding book 依 rate 升冪、FIFO 撮合：借方先吃最便宜的 offer，需求把 book 吃深時成交利率上升（venue 撮合規則）。
- `inherited` 回測以 `candle_close` 為市場利率（[2026-05-28-frr-not-a-market-rate-proxy](2026-05-28-frr-not-a-market-rate-proxy.md) 前身的 `market_rate_source="candle_close"`）；仍成立：至今唯一的 `market_rate_source`。

## Options Considered

- **基準／A. candle path-crossing（選用）**：掛在 `R` 的 offer 在窗內成交 iff 成交利率路徑碰到 `R`（`max(candle.high) ≥ R`）。沒有 L2／queue 資料時，限價單回測的標準做法就是 price-touch／crossing fill（backtrader 等 bar-based 回測框架的 limit order 撮合即此規則）。
- **B. order-book queue model**：用 book 深度與 queue position 估成交（Lo, MacKinlay & Zhang 2002《Econometric models of limit-order executions》一類）。需要新的 ticker/book ingestion，與未實作的 S1/G16 重疊。
- **C. 自有成交回饋＋Bayesian blend**：以真實 fill 學習。cold-start 為空，canary 0 fill。

## Decision

- **D1 = spec §3** — 選 A path-crossing，不選 B（無資料）、C（無樣本）。
- **D2 = spec §3.2–3.5** — 參考價 `ref = candle.close(t)`；spread 以相對 bps 表示；非均勻 bucket grid `[-500…2000]` bps（近 0 較密），learner 在每個 bucket 中點放**假想 offer**對實際路徑判定，而非對觀察到的 spread 分箱；horizons `[1, 4, 24]` h；窗內成員依實際 `mts`（gap-safe）；查詢時兩相鄰 bucket 線性內插、端點 clamp、`[0,1]` clamp；任一端 `n_samples < 30` 標 `low_confidence`。
- **D3 = spec §4/§6** — 結果寫 `fill_rate_stats`（PK = source+symbol+period_agg+horizon+bucket，`source='candle'`，預留 `'book'`／`'own_fill'`）；**不依 `deployment_environment` 分區**（衍生自公開市場資料，與 `funding_candles` 同屬 realm-agnostic）；learner 是離線 batch（`scripts/learn_fill_rate.py`），不接 live daemon。
- **D4 = spec §5** — 回測預設 `fill_model="empirical"`，缺 stats 或 low-confidence 時**逐次自動退回線性模型**；`linear` 保留給要求精確數字的 unit test。
- **D5 = spec §8** — time-to-fill 學習並存下，但**不**餵進回測報酬模型。

## Rationale

- **D1**：candle 是成交利率，「路徑碰到 R 才成交」正對應 FIFO 升冪撮合，是當下唯一有資料支撐、又能延伸的做法；相較 B 要先建 book ingestion（另一個 scope），相較 C 在 0 fill 下無從學起。代價：這是 proxy，忽略 queue position、前方深度、partial fill 與 15% fee。
- **D2**：假想 offer 法讓每根 candle 對每個 bucket 都產生一個樣本，而非只在有人掛該價時才有樣本；bucket 近 0 較密是因為決策集中在市場價附近。不選均勻 grid：遠端 bucket 會稀疏到無意義。
- **D3**：市場衍生量與帳戶 realm 無關，分區只會複製相同資料；離線 batch 與 `backfill_phase2.py` 同模式，不把學習失敗帶進真錢路徑。
- **D4**：預設 empirical 是最佳實務，但 stats 表空的環境下既有回測必須維持原數字（仍綠）。代價：模型缺席時靜默變回線性，結果來源不透明——這正是後來被推翻的部分（見 Result）。
- **D5**：ttf 是「掛單前的等待時間」，與引擎的 `gap_minutes`（成交後重投延遲）語意不同；要做對需新的 idle-cost 項與獨立驗證，不選「先塞進 gap_minutes」以免混淆。

## Result

- `git log --oneline 332caf2^..8074d70`；unit + mypy + ruff + `alembic check` 全綠。收尾時已知：引擎能吃 empirical，但 matrix runner 尚未載入 stats → 實際仍跑 linear（`8074d70` 記錄啟用步驟）。
- **Pivot 1（2026-07-06）**：[2026-07-06-profit-review-execution-layer-first](2026-07-06-profit-review-execution-layer-first.md) 發現 fill model 對 `spread=0` 全回 1.0——部署策略都掛 `candle_close`，於是「利率不是槓桿」的結論是循環論證（該 ADR R1）。
- **Pivot 2（2026-08-23）**：[2026-08-23-funding-strategy-execution-integrity](2026-08-23-funding-strategy-execution-integrity.md) D3 推翻 D4 的自動退回：empirical 必須是有 scope/version/hash 的 artifact，缺模型 `BacktestIncomplete("fill_model_missing"|"…_low_confidence"|"…_scope_mismatch")`；線性模型改名 `linear-baseline`，只能由明示的離線指令使用。
- **延伸（2026-09-22）**：[2026-09-22-research-infra-tenor-pricing-book-fill-weekly-revalidation](2026-09-22-research-infra-tenor-pricing-book-fill-weekly-revalidation.md) 補上 D3 預留的 `source="book"`（book-replay learner），部分實現 B。

## Revocation Triggers

- 取得可回放的 L2 book 歷史且其 fill 證據與 candle path-crossing 系統性分歧 → 以 book source 為準（已部分發生，見上）。
- 自有真實成交累積到足以按 bucket 估計 → 評估 `source='own_fill'` blend（D3 已預留）。
- 策略開始掛在 `close` 以下或恰好 `close`（spread ≤ 0）時，path-crossing 恆近 1，不能作為定價證據。

## Lessons

### Observations

- **O1**：path-crossing 的判準 `high ≥ R` 對 `R ≤ close` 幾乎必然成立，所以它只對「掛在市場價之上多少」有鑑別力；拿它評估掛市場價的策略時，fill 摩擦被系統性低估（後由 07-06 review 證實）。

## Related

- 來源 spec：`2026-05-26-g13-fill-rate-learning-design.md`（原文已不在 repo，本 ADR 即紀錄）
- 來源 plan：`2026-05-26-g13-fill-rate-learning.md`（原文已不在 repo，本 ADR 即紀錄）
- 後續 ADR：[2026-07-06-profit-review-execution-layer-first](2026-07-06-profit-review-execution-layer-first.md)、[2026-08-23-funding-strategy-execution-integrity](2026-08-23-funding-strategy-execution-integrity.md)、[2026-09-22-research-infra-tenor-pricing-book-fill-weekly-revalidation](2026-09-22-research-infra-tenor-pricing-book-fill-weekly-revalidation.md)
