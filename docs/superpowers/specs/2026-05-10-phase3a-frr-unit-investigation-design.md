# v2 Phase 3a — FRR Unit Investigation Design

**Status**: design (pending implementation plan)
**Date**: 2026-05-10
**Owner**: Will（solo）
**Brainstorm session**: 2026-05-10
**Parent**: v2 strategy iteration phase（Phase 2 backfill PASS 後的 single blocker）
**Predecessors**: `2026-05-10-engine-upgrade-design.md`、`2026-05-10-phase2-data-backfill-design.md`、`2026-05-10-phase2-result.md`

---

## Why

Phase 1 引擎已預留 `BacktestConfig.market_rate_source: Literal["candle_close", "frr"]` 接口。Phase 2 已 backfill 547,278 rows 進 Neon（fUSD funding_stats 83,904、fUST funding_stats 64,708、4 個 candles series 共 398,666）。但 Phase 2 抽樣 ratio 檢查 `frr × 86400 / candle.close = 668.98` 推翻 5/9 「FRR=秒利率」的推測。FRR 單位含義未解，導致：

- `_resolve_market_rate(candle, "frr")` 不可通電
- 2🟡 候選策略（FRR-trend、SpikeDetect）的閾值無法設計
- Phase 3b 完整 36-run backtest matrix 中，依賴 FRR 絕對數值的策略全部 blocked

3a 是切出來的 single-purpose mini-phase：**只解 FRR 數值到日利率的單位轉換**，不碰策略矩陣設計（推遲到 3b）。

## What

走 reference-first → empirical-confirmation 流程，產出五個交付物：

1. `frr_to_daily_rate(frr, avg_period=None)` 純函式，constant 由 Stage 2 empirically derive
2. `FundingCandleWithStats` 新型別 + `get_candles_with_funding_stats` repository variant
3. `BacktestConfig.market_rate_source="frr"` 引擎通電（含型別 gate）
4. `docs/research/2026-05-XX-frr-unit-investigation.md` 研究 note
5. Phase 2 backfill 的 `check_frr_unit` 改為 `check_frr_unit_stability`（diagnostic-only，永不阻塞）

3a 「done」= Stage 2 Gate 1 結果決定（PASS → 五件全交；FAIL → 只交 research note + 第 5 項，code 不 commit）。

## Out of Scope

| 項目 | 推遲到 |
|---|---|
| 2🟡 策略（FRR-trend / SpikeDetect）設計 | Phase 3b |
| 4🟢 策略（RatePercentile / DynamicPeriod / WeekendPremium / MeanReversion）實作 | Phase 3b |
| 36-run backtest matrix 跑數字 | Phase 3b |
| H1-H5 之外的 multivariate hypothesis（e.g. utilization-weighted reconstruction） | 留 Stage 2 script 擴展空間，本 phase 不主動跑 |
| Conversion factor 敏感度分析（±10% factor 對策略影響） | Phase 3b 跑出策略結果再決定值不值得 |
| fUSD / fUST 以外幣種 | 547K rows 只覆蓋這兩個，超出範圍 |
| FRR live WebSocket feed | v3+ |
| `funding_stats.funding_amount` / `funding_amount_used` 等其他欄位的策略意義 | Phase 3b 或更晚 |

## Architecture

### 三階段 gated flow

```
Stage 1 (reference, 1.5 hr) ──┐
                              ├─→ winning hypothesis candidate(s)
Stage 2 (empirical, 3-4 hr) ──┘
                              │
                       ┌──────┴──────┐
                  Gate 1 PASS    Gate 1 FAIL
                       │              │
                       ▼              ▼
              Stage 3 (impl, 3 hr)   research-note-only
              · conversion code      · finding：未解
              · new type + repo      · check_frr_unit
              · engine wiring        改 diagnostic
              · check_frr_unit       (code 不 commit)
              改 diagnostic
```

### Stage 1 — Reference

不寫 code。三條 source 並行，每條 30-min hard timeout：

1. **`bitfinex-api-py` SDK** — Project schema docstring 已引用過 `FundingStatistic.serializer`；Stage 1 深挖該 class 的 unit comments / docstring / example。PyPI source via `gh api` 或 `pip download && unzip`。
2. **`ccxt` Python source** — `python/ccxt/bitfinex.py` master，grep `frr|FRR_AMOUNT|LAST_APR` 看 parser normalization。
3. **Bitfinex Help Center** — 兩篇 article（"What is FRR"、"What is FRR Delta"）。WebFetch 拿到 403；Stage 1 改用 `curl -A "Mozilla/5.0 ..."` 或 `gh api` proxy。

每條 source 寫一段進 research note。**Gate**: 任一 source 明確說出 unit + formula → Stage 2 進入 confirm-only mode（只跑 winning hypothesis 的 regression 驗證、跳過其他 4 個）。

### Stage 2 — Empirical hypothesis testing

5 個命名 hypothesis（Stage 2 script 寫成 `dict[hypothesis_id, predictor_fn]`，新增只需加 entry）：

| ID | Predictor | Hypothesis 對應 |
|---|---|---|
| H1 | `frr` | FRR 已是 per-day rate |
| H2 | `frr × 86400` | FRR 是 per-second rate |
| H3 | `frr × avg_period` | FRR 是 per-period rate（period = avg_period 天）|
| H4 | `frr × avg_period × 86400` | FRR 是 per-period-second hybrid |
| H5 | `frr / 365` | FRR 是 annualized rate |

對每個 H，用全歷史 ≥10000 對 (frr, avg_period, close) sample（fUSD + fUST funding_stats × candles 1h p2 point-in-time JOIN），跑：

```python
# 純 numpy / pandas，不用 Decimal
fit close = a × predictor + b           # OLS regression
report:
  - r2: float
  - slope (a): float
  - intercept (b): float
  - median_rel_err: float                # median(|close - predicted|/close)
  - per_year_slopes: dict[year, float]   # split by mts year, fit per group
  - per_year_slope_cv: float             # std(per_year_slopes) / mean(per_year_slopes)
  - per_symbol_slopes: dict[symbol, float]   # fUSD vs fUST
  - per_symbol_slope_diff: float         # |fUSD_a - fUST_a| / mean(...)
  - pass_gate: bool                      # all 4 conditions
```

### Gate 1 PASS conditions（**每條皆需滿足**）

1. R² > 0.99
2. `per_year_slope_cv < 0.05`（regime-invariant slope）
3. `per_symbol_slope_diff < 0.05`（cross-symbol invariant）
4. `median_rel_err < 0.05`（reconstruction quality）

任一條件不滿足 → Stage 2 retry 換 H；五個 H 全部不過 → Gate 1 FAIL，3a 進 research-note-only 收尾。

多個 H 同時 pass（例：H1 與 H4 在 `avg_period` 變異小時數學上會看起來等價）→ 取 R² 最高者；同 R² 取 simpler predictor（H1 > H3 > H4）；研究 note 列舉所有 pass 的 H 與最終決選依據。

### Stage 3 — Implementation（Gate 1 PASS 才走）

詳見 Components / Commit Plan。

## Components

### `modules/funding_stats/conversion.py`（新增）

```python
"""FRR → daily rate conversion. Constant derived empirically by Stage 2.

See `docs/research/2026-05-XX-frr-unit-investigation.md` for derivation,
hypothesis comparison, and cross-source confirmation (bitfinex-api-py SDK,
ccxt, Bitfinex Help Center).

Verified valid as of 2026-05-10 (sample mts ≈ 1778411100000). If Bitfinex
changes FRR semantics (rare but possible — see backfill_phase2.py
check_frr_unit_stability for ongoing diagnostic), this constant must be
re-derived.
"""
from decimal import Decimal
from typing import Final

# Empirically derived in Stage 2; placeholder until Stage 2 ships
FRR_TO_DAILY_RATE_FACTOR: Final[Decimal] = Decimal("...")
FRR_REQUIRES_AVG_PERIOD: Final[bool] = False  # True if H3/H4 wins


def frr_to_daily_rate(
    frr: Decimal,
    avg_period: Decimal | None = None,
) -> Decimal:
    """Convert raw FRR value (from /v2/funding/stats) to per-day decimal rate.

    If FRR_REQUIRES_AVG_PERIOD is True, raises ValueError on None avg_period.
    Otherwise avg_period is ignored.
    """
```

### `modules/candles/schemas.py`（增量）

```python
class FundingCandleWithStats(FundingCandle):
    """Candle joined with point-in-time funding_stats (frr, avg_period at the
    latest funding_stats row with mts <= candle.mts).

    Use this type ONLY when frr / avg_period are needed; backtest engine
    enforces this via isinstance check on market_rate_source='frr'.
    """
    frr: Decimal | None = None      # None when no funding_stat exists ≤ mts
    avg_period: Decimal | None = None
```

Base `FundingCandle` 不動，避免污染既有 caller。

### `modules/candles/repository.py`（增量）

```python
async def get_candles_with_funding_stats(
    session: AsyncSession,
    *,
    symbol: str,
    timeframe: str,
    period_agg: str,
    start_mts: int,
    end_mts: int,
) -> list[FundingCandleWithStats]:
    """Like get_candles_in_range but LEFT LATERAL JOIN funding_stats on
    `funding_stats.mts <= candle.mts ORDER BY mts DESC LIMIT 1`
    (point-in-time, no lookahead).

    See also: `get_candles_in_range` for plain candle queries (cheaper, no JOIN).
    """
```

既有 `get_candles_in_range` 一字不改，docstring 加 "See also" 一行指向新方法。

### `modules/backtest/engine.py`（修改）

```python
def _resolve_market_rate(
    candle: FundingCandle | FundingCandleWithStats,
    source: str,
) -> Decimal | None:
    if source == "candle_close":
        return candle.close
    if source == "frr":
        if not isinstance(candle, FundingCandleWithStats):
            raise ValueError(
                "market_rate_source='frr' requires FundingCandleWithStats; "
                "use candles.repository.get_candles_with_funding_stats(...)"
            )
        if candle.frr is None:
            return None  # caller will fall through to instant-fill
        return frr_to_daily_rate(candle.frr, candle.avg_period)
    raise ValueError(f"unsupported market_rate_source: {source!r}")
```

`run_backtest` signature 不動（型別已是 `list[FundingCandle]`，subclass 自動相容）。

### `scripts/investigate_frr_unit.py`（新增）

一次性 research script。輸入 `DATABASE_URL`（從 `.env`），輸出 `data/research/frr_hypothesis_results.json`。

JSON schema：

```json
{
  "generated_at_utc": "2026-05-XX...",
  "sample_size": 12345,
  "symbols_used": ["fUSD", "fUST"],
  "hypotheses": [
    {
      "id": "H1",
      "predictor": "frr",
      "r2": 0.xx,
      "slope": x.xx,
      "intercept": x.xx,
      "median_rel_err": 0.xx,
      "per_year_slope_cv": 0.xx,
      "per_symbol_slope_diff": 0.xx,
      "pass_gate": false
    }, ...
  ],
  "winning_hypothesis": "H3" | null,
  "winning_factor": "x.xxxxxxx" | null,
  "winning_requires_avg_period": true | false
}
```

Exit 0 = 至少一個 H pass gate；exit 1 = 全 fail（→ note-only 路徑）。

純 numpy / pandas 計算（不用 Decimal）。Decimal 只在最終把 `winning_factor` 寫進 `conversion.py` 時用 `Decimal(str(float_value))` 轉換。

### `docs/research/2026-05-XX-frr-unit-investigation.md`（新增）

結構：

```
1. Problem statement (refer Phase 2 result)
2. Stage 1 reference findings
   2.1 bitfinex-api-py SDK
   2.2 ccxt
   2.3 Bitfinex Help Center
3. Stage 2 empirical results
   3.1 5 hypotheses table (R², slope, invariance, err)
   3.2 Winning hypothesis
   3.3 Per-year stability plot data (numerical, no PNG)
4. Conversion factor with provenance
5. Caveats
   - mts-bound validity
   - bitfinex-api-py / ccxt cross-source agreement status
   - Failure modes if Bitfinex changes API
6. Next: Phase 3b
```

### `scripts/backfill_phase2.py`（修改）

`check_frr_unit` 改名 `check_frr_unit_stability`：

- 從 single-sample range check 改成 ≥1000 sample 的 winning-hypothesis residual 檢查
- 計算 `r2_drift`：當前 sample 對 conversion.py 的 `FRR_TO_DAILY_RATE_FACTOR` 做 reconstruct，比對 R²
- **Diagnostic only**：log 印 `"R² drift = X% (conversion factor still holds)"` 或 `"R² dropped from X to Y — conversion may need re-derivation"`，狀態永遠 PASS、不影響 exit code
- 若 3a Gate 1 FAIL（無 conversion factor）→ check 改成只算 raw `r2(close ~ frr)` 當 health metric

## Testing

### `tests/modules/funding_stats/test_conversion.py`（新增）

- `frr_to_daily_rate_uses_correct_factor` — deterministic：given known frr + factor, output matches
- `frr_to_daily_rate_handles_none_avg_period` — H1/H2 win 時 avg_period 參數忽略
- `frr_to_daily_rate_requires_avg_period_when_flagged` — H3/H4 win 時 None avg_period 應 raise
- `frr_to_daily_rate_zero_returns_zero`
- `frr_to_daily_rate_uses_sample_row_from_schema_docstring` — 用 `funding_stats/schemas.py` docstring 的真 sample row（mts=1778411100000, frr=1.12e-06, avg_period=94.98）做 golden test

### `tests/modules/candles/test_repository_with_funding_stats.py`（新增）

- `joins_latest_funding_stat_at_or_before_candle_mts` — 多筆 funding_stats 取 mts ≤ candle.mts 的最新一筆
- `returns_none_frr_when_no_funding_stat_exists_yet` — 早期歷史 candle 比 funding_stats 起始時間早 → frr / avg_period 為 None
- `returns_funding_candle_with_stats_subclass` — `isinstance(result[0], FundingCandleWithStats) is True`
- `empty_candle_range_returns_empty_list`

### `tests/modules/backtest/test_engine.py`（增量）

- `run_backtest_with_frr_source_uses_converted_rate` — frr branch happy path：固定 frr / avg_period → 預期 effective_rate 可手算
- `run_backtest_with_frr_source_raises_on_plain_candle` — 餵 plain `FundingCandle` 給 `market_rate_source="frr"` → ValueError

### `tests/modules/backtest/strategies/test_always_frr.py`

不動。AlwaysFRR 出價等於 candle.close → spread=0，friction 仍主要來自 fee + gap。

### Coverage 目標

- `test_conversion.py`：100%（純函式）
- `test_repository_with_funding_stats.py`：JOIN 主路徑 + None edge 都覆蓋
- `test_engine.py`：frr branch happy path + 型別 gate

### `scripts/investigate_frr_unit.py`

無 unit test（一次性 research script）。正確性靠：
- JSON output schema 在本 spec 列明、可機械 parse
- Stage 2 commit 寫進 PR description 時人眼 review JSON 數字合理性
- Stage 3 conversion module 用實際 sample row 反向 cross-check

## Verification（Phase 3a PASS conditions）

1. `cd backend_py && uv run pytest -m "not integration"` 全過（既有 + 新增 11 tests：conversion 5、repo with stats 4、engine frr branch 2）
2. `cd backend_py && uv run mypy src/ && uv run ruff check` 無錯
3. `cd backend_py && uv run python scripts/investigate_frr_unit.py` exit 0 + 產出 `data/research/frr_hypothesis_results.json` schema-valid
4. **Gate 1 決策點**：根據 JSON 的 `winning_hypothesis` 是否非 null，分支：
   - PASS → 走 Stage 3 commits 3-6
   - FAIL → 只 commit research note（含 finding）+ check_frr_unit_stability raw-r² 版
5. Stage 3 PASS（如進）：`uv run python scripts/run_backtest.py --market-rate-source frr` 在 fUSD / fUST 各跑一次，`run_backtest.py` 加 `--market-rate-source` CLI 參數（default `candle_close` 不破既有行為），exit 0 + 印出 fill_rate / net_monthly_return_pct 數字（不檢驗水位、只 sanity 不爆）
6. Wiki `~/second-brain/wiki/projects/bfx-funding-bot.md` Lessons Learned 一行：
   - PASS：`FRR unit resolved: H<X>, factor=<Y>, requires_avg_period=<Z>（per-year CV<5%, fUSD/fUST diff<5%）`
   - FAIL：`FRR unit unresolved: best H<X> R²=<Y>, ship research-only; 2🟡 策略 blocked to 3c`

## Commit Plan

**Stage 2**（無條件執行）：

```
1. ✨ Feat: scripts/investigate_frr_unit.py — 5-hypothesis OLS regression + JSON output
   - new: scripts/investigate_frr_unit.py
   - new: data/research/.gitkeep
   - 跑通對 Neon DB 產生 frr_hypothesis_results.json
   - exit 0 / 1 行為符合 Gate 1 規範

2. 📝 Docs: research note + Stage 1/2 findings
   - new: docs/research/2026-05-XX-frr-unit-investigation.md
   - 記錄 Stage 1 三 source（含 timeout / 找不到狀態）+ Stage 2 五 hypothesis 對照表 + winning verdict
   - JSON 結果嵌入 note 對應段落
```

**Stage 3**（Gate 1 PASS 才執行）：

```
3. ✨ Feat: funding_stats/conversion — frr_to_daily_rate + tests
   - new: modules/funding_stats/conversion.py（with mts-bound provenance docstring）
   - new: tests/modules/funding_stats/test_conversion.py（5 tests）
   - constant 從 Stage 2 JSON 抄進，附 commit body 引用研究 note 路徑

4. ✨ Feat: candles repository — get_candles_with_funding_stats variant + new type
   - modify: modules/candles/schemas.py（加 FundingCandleWithStats subclass）
   - modify: modules/candles/repository.py（新方法 + LATERAL JOIN）
   - new: tests/modules/candles/test_repository_with_funding_stats.py（4 tests）
   - 既有 get_candles_in_range docstring 加 "See also" 一行

5. ♻️ Refactor: engine market_rate_source="frr" 通電
   - modify: modules/backtest/engine.py（_resolve_market_rate frr branch + 型別 gate）
   - modify: tests/modules/backtest/test_engine.py（2 new tests）
   - modify: scripts/run_backtest.py（加 --market-rate-source CLI flag，default 不變）
   - 跑一次 fUSD + fUST sanity，數字寫進 wiki Lessons Learned

6. 🔧 Chore: backfill_phase2 check_frr_unit → check_frr_unit_stability (diagnostic)
   - modify: scripts/backfill_phase2.py（rewrite check 用 winning H residual r²）
   - 改成永遠 PASS、純 log 印 metric 不影響 exit code
   - spec：`2026-05-10-phase2-result.md` 加附錄一段「3a 後 PASS check 演進」
```

**Stage 3 Gate 1 FAIL 路徑**（替代 commits 3-6）：

```
3'. 🔧 Chore: backfill_phase2 check_frr_unit → diagnostic raw r²
    - modify: scripts/backfill_phase2.py（check 改成只 log raw r²(close ~ frr)，永遠 PASS）
    - spec 附錄記「3a Gate 1 FAIL，等 3c 重啟」
```

每 commit 跑 `cd backend_py && uv run pytest -m "not integration" && uv run mypy src/ && uv run ruff check` 全綠才推。

## Risks

| Risk | 緩解 |
|---|---|
| 5 hypotheses 不夠覆蓋（e.g. utilization-weighted reconstruction）| Stage 2 script 設計成 dict-extensible；spec 不限 H1-H5；研究 note 段「Stage 2 expanded H6+」記下加新 H 的過程 |
| Bitfinex 未來改 FRR 計算 | conversion 模組 docstring 標 mts-bound + 引研究 note；commit 6 的 `check_frr_unit_stability` 持續 log R² drift，下降會在 backfill log 看到 |
| LATERAL JOIN 對 ~150K funding_stats × 240K candles 的 plan 成本 | EXPLAIN ANALYZE 在 commit 4 跑一次寫進 commit body；funding_stats 已有 `idx_funding_stats_mts` index 可吃 |
| `avg_period` 早期歷史 None 但 winning H 需要它 | conversion fn 在 H 需要 avg_period 時對 None raise；新 type 預設 None；`_resolve_market_rate` 拿到 frr=None 或 raise → 回 None 讓 caller 走 instant-fill fallback（已存在的 path） |
| bitfinex-api-py / ccxt 也 outdated 或彼此矛盾 | Stage 1 三條 source 並行；任一不一致就降為 empirical-only verdict、不阻塞；研究 note 記矛盾點 |
| Stage 2 全 fail 但專案進度焦慮想 ship | spec hard rule：Gate 1 fail → conversion code & engine wiring **不 commit**，這是 verifier `winning_hypothesis is null` 自動分支 |
| 多個 H 同時 pass（高度共線）| spec 規定取 R² 最高 → simpler predictor（H1 > H3 > H4）；研究 note 列所有 pass H 透明 |
| Stage 3 commit 5 加 `--market-rate-source` CLI flag 改了 `run_backtest.py` 既有行為 | default 維持 `candle_close`，既有 callers 零影響；commit message 註明 |

## Time Estimate

~1 working day（6-8 hr）：

| Step | 估時 |
|---|---|
| Stage 1 reference（3 sources × 30 min hard timeout）| 1.5 hr |
| Stage 2 script 寫 + 跑 + 研究 note | 3-4 hr |
| Stage 3 commits 3-6（Gate 1 PASS 才走，~30 LOC × 4 + ~150 LOC tests）| 3 hr |
| Buffer / 預期 dependency-assumption surprise（per Phase 2 lesson）| 0.5-1 hr |

Stage 1 + 2 完不過就 fall through 到 note-only，總時間壓在 5-6 hr 內。

## Next Phase

Phase 3a PASS（任一路徑）後 → Phase 3b brainstorm：

- 6 候選策略（4🟢 + 2🟡 視 3a 結果）詳細設計
- 2 symbols × 3 period_agg × 6 strategies = 36-run matrix
- 排名 metric / tie-breakers / 統計顯著性檢定的選擇
- 落地 `scripts/run_backtest_matrix.py` 一鍵跑出比較表

3a Gate 1 FAIL 的話 → Phase 3b 只跑 4🟢 × 24 runs，2🟡 推遲到 Phase 3c（FRR 重新調查 + 可能須加 multivariate hypothesis）。
