# v2 Phase 3b — Strategy Matrix Design

**Status**: design (pending implementation plan)
**Date**: 2026-05-17
**Owner**: Will（solo）
**Brainstorm session**: 2026-05-17
**Parent**: v2 strategy iteration phase（Phase 3a Gate 1 FAIL 後，2🟡 推遲至 3c）

---

## Why

Phase 3a Gate 1 FAIL（FRR 單位 5 hypothesis 全 missed）後，2🟡 策略（FRR-trend / SpikeDetect）依賴 FRR 絕對值含義，推遲到 Phase 3c。Phase 3b 限縮為 3🟢 策略 ×（symbol × period_agg）資料矩陣，全程用 `market_rate_source="candle_close"` proxy，**不阻塞、不踩 FRR 雷**。

目的：在 OOS 上驗證候選策略是否能 beat AlwaysFRR(period=2) baseline，產出進 Phase 4 的候選清單。

## What

- 擴充 Strategy 介面支援 stateful（observe + decide），用以實作有 history dependency 的策略
- 擴充 Engine 支援 record window（warmup 後再開始計入結果）+ Sortino 指標
- 實作 3 個候選策略：RatePercentile / WeekendPremium / MeanReversion
- 跑 Step 0 EDA（per cell train portion）取得 data-informed param grids
- 跑 Phase 3b matrix（3 × 2 symbols × 3 period_agg = 18 OOS runs + 6 baselines）
- 產出 result report + 進 Phase 3c 決策建議

**OOS 結論的有效範圍**：本 spec 採 single train/test 70/30 split，OOS 結論在 test-window 涵蓋的 regime 內有效。**Phase 4 上線前必須做 walk-forward validation 確認 parameter stability**。

## Out of Scope

| 項目 | 推遲到 |
|---|---|
| FRR 單位解碼、`market_rate_source="frr"` 通電 | Phase 3c |
| FRR-trend、SpikeDetect、bid-rel-to-FRR 等 2🟡 策略 | Phase 3c |
| Walk-forward optimization (WFO) | Phase 4 上線前 |
| Bootstrap CI / Deflated Sharpe / Bonferroni | Phase 4 上線前 |
| DynamicPeriod 策略 | 砍掉（與 MeanReversion 概念重疊） |
| Live trading hookup / worker model / safety flags | v2 全 phase 完才解凍 |
| Per-trade raw data 持久化（DB / parquet） | 暫不需要（exploratory） |

## Architecture

### Engine 變更（`modules/backtest/engine.py`）

新 signature：

```python
run_backtest(
    candles: list[FundingCandle],
    strategy: Strategy,
    config: BacktestConfig | None = None,
    record_start_mts: int | None = None,  # default = 全 record
    record_end_mts: int | None = None,
) -> BacktestResult
```

主迴圈邏輯：

```text
for each candle in sorted(candles):
    strategy.observe(candle)              # 每根都 call，更新 state
    if in_cooldown:                       continue
    if outside record window:             continue
    decision = strategy.decide(candle)
    if decision is None:                  continue
    # 套 friction → 更新 gross/net equity → 累 trade metrics
```

**Sortino 計算**：
1. Sample equity curve 在 month-end timestamps（取 record window 內所有 month-end）
2. Compute monthly returns = `(equity[t] / equity[t-1]) - 1`
3. `downside_returns = [r for r in returns if r < 0]`
4. `sortino = mean(returns) / std(downside_returns)`
5. 若 `len(downside_returns) == 0` → `sortino = +inf`（無下行樣本）
6. 若 `len(returns) < 3` → `sortino = 0`（樣本不足，不可信）

**Sweep tie-break（避免 +inf dominate）**：
- 若候選中有任何 sortino == +inf，**先**在 +inf 子集內按 `net_monthly_return_pct desc` 取最大
- 否則按 sortino desc 取最大

**BacktestResult 新增欄位**：`sortino: Decimal`。

### Strategy 介面變更（`modules/backtest/strategies/base.py`）

```python
class Strategy(ABC):
    @property
    @abstractmethod
    def name(self) -> str: ...

    def observe(self, candle: FundingCandle) -> None:
        """Called on every candle (incl. cooldown). Update internal state.

        Default no-op for stateless strategies.
        """
        pass

    @abstractmethod
    def decide(self, candle: FundingCandle) -> LendDecision | None: ...

    @classmethod
    def param_grid(cls) -> list[dict]:
        """Param variants to sweep. Override per strategy.

        EDA-driven values（per-cell train portion）填回此處前必須先跑 Step 0。
        """
        raise NotImplementedError
```

`AlwaysFRRStrategy` 不變（沒 override observe、不參與 sweep）。

## 候選策略（3 個）

### 1. RatePercentile — 「只在高利率時段出手」

```python
class RatePercentileStrategy(Strategy):
    def __init__(self, percentile: int, lookback_hours: int): ...

    def observe(self, candle):
        self._window.append(candle.close)  # rolling deque, maxlen=lookback_hours

    def decide(self, candle):
        if len(self._window) < self._lookback_hours:
            return None  # warmup
        threshold = numpy.percentile(self._window, self._percentile)
        if candle.close >= threshold:
            return LendDecision(mts=candle.mts, rate=candle.close, period_days=2)
        return None
```

**Param grid**（暫定，EDA Step 4 確認後可調 lookback）：

| Param | Variants | 變體數 |
|---|---|---|
| `percentile` | 25, 50, 75 | 3 |
| `lookback_hours` | 168 (1w), 720 (1m) | 2 |

**6 variants per cell**

### 2. WeekendPremium — 「週末加碼鎖較久」

```python
class WeekendPremiumStrategy(Strategy):
    def __init__(self, weekend_period: int): ...

    def decide(self, candle):
        weekday = datetime.fromtimestamp(candle.mts / 1000, UTC).weekday()
        period = self._weekend_period if weekday in (4, 5, 6) else 2  # Fri/Sat/Sun
        return LendDecision(mts=candle.mts, rate=candle.close, period_days=period)
```

**Drop rule**：Step 0 EDA 若顯示 weekend mean rate vs weekday mean rate **effect size < 5% relative**（per cell），該 cell 直接 skip 此策略。Effect size 公式：`(mean_weekend - mean_weekday) / mean_weekday`。

**Param grid**（EDA 通過後）：

| Param | Variants | 變體數 |
|---|---|---|
| `weekend_period` | 7, 14, 30 | 3 |

**3 variants per cell**（若 EDA 通過）

### 3. MeanReversion — 「rate 跌破 EMA 暫停」

```python
class MeanReversionStrategy(Strategy):
    def __init__(self, ema_span: int, threshold_sigma: float): ...

    def observe(self, candle):
        # Incremental EMA update
        if self._ema is None:
            self._ema = candle.close
        else:
            alpha = 2 / (self._ema_span + 1)
            self._ema = alpha * candle.close + (1 - alpha) * self._ema
        # Track close/ema ratio for σ estimation (done offline via EDA, not online)

    def decide(self, candle):
        if self._ema is None or self._ratio_sigma is None:
            return None
        deviation = (candle.close - self._ema) / self._ema
        if deviation < -self._threshold_sigma * self._ratio_sigma:
            return None  # 等 rate 回升
        return LendDecision(mts=candle.mts, rate=candle.close, period_days=2)
```

`ratio_sigma`（close/EMA ratio 的 σ）從 EDA Step 3 注入，**不在 sweep 軸上**（per cell train 固定值）。

**Param grid**：

| Param | Variants | 變體數 |
|---|---|---|
| `ema_span` | 24, 168 | 2 |
| `threshold_sigma` | 0.5, 1.0, 1.5 | 3 |

**6 variants per cell**

**注意**：與已刪除的 DynamicPeriod 概念重疊（同樣賭 mean reversion）。本 spec 選 MeanReversion 因為它更直接（過濾退化 trade vs 變動 period_days），保留單一 hypothesis 測試。

## Step 0 — Exploratory Data Analysis

**目的**：取得 data-informed param grids（避免拍腦袋）+ 驗證後續策略假設。

**範圍**：**Per-cell train portion only**（post-2022-01-01 to train_end_mts）。**禁止用 test portion 做任何 EDA decision**（避免 data snooping）。

**train_end_mts 計算規則**（EDA script 與 matrix runner 必須完全一致）：

```python
def compute_train_end_mts(candles: list[FundingCandle]) -> int:
    sorted_candles = sorted(candles, key=lambda c: c.mts)
    split_idx = int(len(sorted_candles) * 0.7)
    return sorted_candles[split_idx - 1].mts  # inclusive of train portion
```

該 helper 提取為 `modules/backtest/split.py:compute_train_end_mts`，兩處共用。

**腳本**：`scripts/eda_phase3b.py`

**輸出**：`docs/research/2026-05-17-phase3b-eda.md`，每 cell 一節，含下列項目：

| 項 | 用途 | 影響策略 |
|---|---|---|
| Close rate histogram + 分位數 (P25/P50/P75/P90) | 看分布 + 確認 RatePercentile 變體合理 | RatePercentile |
| Mean close rate by weekday | 驗證 weekend premium 假設 | WeekendPremium drop rule |
| Close/EMA ratio σ（span = 24h, 168h） | MeanReversion threshold σ 基準 | MeanReversion `ratio_sigma` 注入 |
| ACF lag {1h, 24h, 168h, 720h} | 確認 RatePercentile lookback 範圍 | RatePercentile lookback 調整（若 lag 168h corr < 0.3 → 砍 720h 變體） |
| Within-window regime drift 檢查（per-quarter mean rate） | 若 `(max_quarter_mean - min_quarter_mean) / min_quarter_mean >= 0.30` → 升級成 mandatory WFO | 全策略；可能延後 Phase 3b |

**完成條件**：EDA report 寫完 + 三策略的 param grid / sigma 注入值定案 + drop rules 觸發或未觸發明確記錄。

## Matrix Runner

**腳本**：`scripts/run_phase3b_matrix.py`

**Pseudocode**：

```python
SYMBOLS = ["fUSD", "fUST"]
PERIOD_AGGS = ["p2", "p30", "a30"]
START_MTS = mts("2022-01-01")
STRATEGIES = [RatePercentileStrategy, WeekendPremiumStrategy, MeanReversionStrategy]

results = []
baselines = {}

for symbol, period_agg in product(SYMBOLS, PERIOD_AGGS):
    candles = load_candles(symbol, period_agg, since=START_MTS)
    train_end_mts = candles[int(len(candles) * 0.7)].mts

    # Baseline: AlwaysFRR period=2, fixed, no sweep
    baseline = run_backtest(
        candles, AlwaysFRRStrategy(period_days=2),
        record_start_mts=train_end_mts + 1,
    )
    baselines[(symbol, period_agg)] = baseline

    for strategy_class in STRATEGIES:
        # WeekendPremium drop rule check (from EDA results)
        if strategy_class is WeekendPremiumStrategy:
            if eda[symbol, period_agg].weekend_effect_size < 0.05:
                results.append((strategy_class, symbol, period_agg, "skipped:eda_drop"))
                continue

        # 1. Sweep params on train portion
        candidates = []
        for params in strategy_class.param_grid_for_cell(symbol, period_agg, eda):
            train_result = run_backtest(
                candles, strategy_class(**params),
                record_end_mts=train_end_mts,
            )
            if train_result.fill_rate < Decimal("0.3"): continue
            if train_result.n_trades < 10: continue
            candidates.append((params, train_result))

        if not candidates:
            results.append((strategy_class, symbol, period_agg, "skipped:no_valid_candidate"))
            continue

        best_params, _ = max(candidates, key=lambda x: x[1].sortino)

        # 2. Eval best_params on test portion (warmup from history start)
        test_result = run_backtest(
            candles, strategy_class(**best_params),
            record_start_mts=train_end_mts + 1,
        )
        results.append((strategy_class, symbol, period_agg, best_params, test_result))

write_report(results, baselines)
```

**設計選擇**：
- `param_grid_for_cell(symbol, period_agg, eda)`：classmethod 接收 EDA 結果，返回 per-cell 的 param dict list。讓 EDA-driven 值（ratio_sigma、lookback 調整）注入流程顯式。
- Warmup：sweep 階段用 `record_end_mts`（從 history_start 到 train_end，全 train 資料 warmup + record）；eval 階段用 `record_start_mts`（從 history_start 跑，只記 test 段）。

## Decision Rules（Phase 3c 候選晉級）

策略 **晉級 Phase 4 候選池** 條件（all-of）：

1. **Consistency**：在 ≥ **4/6** cells 上 OOS `net_monthly_return_pct > baseline (same cell)`
2. **Magnitude**：至少 **1** cell beat by `relative_margin > 5%`（i.e. `(strategy - baseline) / baseline > 0.05`）
3. **Health**：所有 beat-baseline cells 的 OOS `fill_rate >= 0.3` 且 `n_trades >= 10`

若都不達標 → 該策略列「Phase 3c 不採納」。

**多 cell 對 baseline 都贏但 Sortino 各不同**：晉級狀態不 weighted average Sortino，只看 net_monthly_return（為跟 baseline 直接可比）。Sortino 為輔助診斷指標。

**Phase 3c 啟動條件**：

- 若 Phase 3b 有 ≥ 1 策略晉級 → Phase 3c 可推遲（先做 Phase 4 上線準備）
- 若 0 策略晉級 → Phase 3c 必跑（FRR-trend / SpikeDetect / extended hypothesis 是剩下的牌）

## Output Format

**單一 markdown report**：`docs/research/2026-05-17-phase3b-results.md`

結構：

```markdown
# Phase 3b Results

## TL;DR
<一行：晉級策略 + 各 cell 最強表現>

## Methodology Snapshot
- 資料窗：post-2022-01-01
- Train/test split：70/30 per cell
- Sweep metric：Sortino + fill_rate ≥ 0.3 + n_trades ≥ 10 floors
- Decision rule：consistency ≥ 4/6 + margin > 5% + health gates
- Conclusion scope：conditional on test-window regime；Phase 4 須 WFO

## OOS Comparison Table（核心 4 軸）
| Cell | Strategy | Best params | Net %/mo | Max DD % | Fill rate | Sortino | vs Baseline |
|---|---|---|---|---|---|---|---|
| fUSD × p2 | RatePercentile | P=50, N=168 | 0.42 | 0.0 | 0.62 | 1.85 | +35.5% |
| fUSD × p2 | AlwaysFRR (baseline) | period=2 | 0.31 | 0.0 | 1.00 | 1.21 | — |
| ... | ... | ... | ... | ... | ... | ... | ... |

## Per-cell Detail Appendix
- Train sweep table（每 param variant 的 train Sortino）
- 為何選此 winner
- OOS vs baseline 差異 / 解讀

## Decision
- 晉級 Phase 4 候選的策略 + 理由
- 未晉級的策略 + 原因
- Phase 3c 啟動 yes / no
```

## Error Handling / Edge Cases

| 情況 | 處理 |
|---|---|
| Cell 缺資料（`len(candles) == 0` 或 `len(candles) < 720`，即 1 個月 1h candles） | Log warning，results 表標 `n/a`，該 cell 不計入任何策略 consistency 分母 |
| WeekendPremium EDA drop（effect size < 5%） | Cell 標 `skipped:eda_drop`，不計入 consistency 分母（4/6 改為 4/N，N = 該策略未 skip 的 cell 數） |
| Train sweep 無 valid candidate（所有 variant 觸 floor） | Cell 標 `skipped:no_valid_candidate`，同上不計入 consistency 分母 |
| Sortino 分母 0（無 downside） | 回傳 `+inf`，sweep 中當 raw return 退讓決勝 |
| Test portion `n_trades == 0` | OOS net_monthly_return = 0%，自動低於 baseline |
| Strategy raises during observe / decide | Log exception，cell 標 `errored`，不參與 consistency 統計 |

## Testing

| 層級 | 涵蓋項 |
|---|---|
| Unit | `Strategy.observe` default no-op；`AlwaysFRRStrategy` 無 regression |
| Unit | RatePercentileStrategy: warmup（< lookback 不 emit）、threshold 邊界（== 邊界、> 邊界、< 邊界） |
| Unit | WeekendPremiumStrategy: Fri/Sat/Sun 用 weekend_period、Mon-Thu 用 period=2、跨時區邊界（UTC midnight） |
| Unit | MeanReversionStrategy: EMA incremental update 對 batch 計算一致、threshold 觸發 / 不觸發 |
| Unit | Engine warmup window：`record_start_mts` 之前 trade 不計入 result；observe 仍被呼叫 |
| Unit | Engine Sortino computation：月末 sampling + downside std + edge cases（< 3 obs、無 downside） |
| Unit | Matrix runner consistency rule（4/6 + margin > 5% + health gates）on synthetic results |
| Integration | Matrix runner end-to-end on 100-candle synthetic fixture（不打 Neon） |
| Manual | EDA script run on real Neon → EDA report committed |
| Manual | Full matrix run on real Neon → results report committed |

**`pytest -m "not integration"` 必須全綠才 commit。**

## Implementation Order

1. `✨ Feat: engine record window + Sortino metric`（engine.py + schemas.py + tests）
2. `✨ Feat: Strategy.observe + param_grid hooks`（base.py + tests；AlwaysFRRStrategy regression test）
3. `✨ Feat: EDA script + post-2022 analysis`（scripts/eda_phase3b.py + sub-modules）
4. `📝 Docs: Phase 3b EDA results + param grid lock-in`（docs/research/2026-05-17-phase3b-eda.md；含 drop rule 觸發狀態）
5. `✨ Feat: RatePercentileStrategy + tests`
6. `✨ Feat: MeanReversionStrategy + tests`
7. `✨ Feat: WeekendPremiumStrategy + tests`（若 EDA 通過至少 1 cell）
8. `✨ Feat: Phase 3b matrix runner + integration test`
9. `📝 Docs: Phase 3b results + Phase 3c decision`（docs/research/2026-05-17-phase3b-results.md）

**預估 8-9 commits**，留 1-2 個 slot 給 plan deviation（Phase 2 教訓）。

## Risks & Open Questions

| 風險 | 處理方式 |
|---|---|
| Within-post-2022 仍有 regime drift（per-quarter mean rate 變動 ≥ 30%） | EDA Step 5 偵測 → 觸發即升級 mandatory WFO，Phase 3b 結論作廢 |
| Single train/test split 結論不泛化 | Spec 明寫 conclusion 範圍；Phase 4 上線前必跑 WFO |
| MeanReversion 與已刪 DynamicPeriod 概念重疊（殘留偏誤） | 接受；spec 已記錄選擇理由（過濾退化 trade 比變 period_days 更乾淨） |
| RatePercentile lookback 720h 跨 train portion 不夠 warmup（train ~2.4 年，2.4y × 12 mo/y ≈ 28.8 mo train，720h ≈ 1 mo） | 樣本充足，不過 EDA Step 4 ACF 若 lag 168h corr < 0.3，可砍 720h 變體 |
| Sortino + inf 在 sweep 中 tie-break 退讓 raw return 可能誤判 | 接受；exploratory phase 容忍。production 需 robust 量級調整（e.g. clip to 5σ） |
| EDA 完才能填 param grid，違反「先 spec 再 plan」順序 | Plan 階段把 Step 0 EDA 列為前置 commit，commit 後再生成 param grid lock-in commit |

## Phase 3c Punt（明確記錄推遲項）

以下項目延後到 Phase 3c（先看 Phase 3b 結果再決定要不要全做 / 半做 / 跳過）：

- FRR 單位解碼 extended hypothesis（multivariate / regime split / lower-50% weighting）
- FRR-trend、SpikeDetect 策略
- bid-rel-to-FRR 等 FRR-絕對值-依賴策略

Phase 3b 跑完才有資料判斷 Phase 3c 是否仍必要。

---

## Implementation Plan

待 brainstorm 確認 → 由 writing-plans skill 產出 `docs/superpowers/plans/2026-05-17-phase3b-strategy-matrix.md`。
