# v2 Phase 3b-WFO — Walk-Forward Strategy Matrix Design

**Status**: design (pending implementation plan)
**Date**: 2026-05-18
**Owner**: Will（solo）
**Brainstorm session**: 2026-05-18
**Replaces**: `2026-05-17-phase3b-strategy-matrix-design.md`（single-split version invalidated by EDA gate failure on all 6 cells — see `docs/research/2026-05-18-phase3b-eda-gate-failure.md`）

---

## Why

Phase 3b (single 70/30 split) was invalidated by 2026-05-18 EDA: per-quarter drift 1.33-2.66× across all 6 cells — within-post-2022 non-stationarity rules out single-split as a credible OOS test. Walk-forward CV is industry-standard for trading strategy backtests on non-stationary data and is what this redesign delivers.

Bonus: EDA showed weekend funding rates are actually **lower** than weekday rates in 4 of 6 cells (effect -6.8% to -10.96%), refuting the WeekendPremium hypothesis entirely. WeekendPremium is **permanently dropped** from the candidate set.

## What

- Replace single 70/30 split with rolling **walk-forward optimization (WFO)**: 3-month train + 1-month test + 1-month walk step.
- 2 candidate strategies: RatePercentile, MeanReversion. (WeekendPremium dropped.)
- ~48 WFO windows per cell × 6 cells = ~288 OOS test windows × 2 strategies = ~576 OOS observations total.
- New stability diagnostics: optimal-param drift across windows + rolling OOS Sortino.
- New decision rule based on per-window outperformance consistency, not single-shot OOS magnitude.
- Reuse Phase A/B/C foundations (engine record window, observe routing, Sortino, EDA, strategies) unchanged.

## Out of Scope

| 項目 | 推遲到 |
|---|---|
| FRR 單位解碼、`market_rate_source="frr"` 通電 | Phase 3c |
| FRR-trend、SpikeDetect、bid-rel-to-FRR 等 2🟡 策略 | Phase 3c |
| WeekendPremium strategy | **permanently dropped**（EDA evidence refutes hypothesis） |
| DynamicPeriod strategy | dropped（concept overlap with MeanReversion, decided in 5/17 design） |
| Bootstrap CI / Deflated Sharpe / Bonferroni | Phase 4 上線前 |
| Live trading / safety flags | v2 全 phase 完才解凍 |
| Per-trade raw data 持久化 | 暫不需要 |
| Pre-flight regime drift gate | **REMOVED** — WFO inherently handles non-stationarity; pre-flight gate was redundant and over-strong (decided in this design) |

## Methodology

### Walk-Forward Optimization (WFO)

For each cell (symbol × period_agg), slide a (train, test) pair across the full post-2022 candle stream:

```
| train 3mo | test 1mo |                                  → window 1: train [t0, t0+3mo], test (t0+3mo, t0+4mo]
            | train 3mo | test 1mo |                       → window 2: train [t0+1mo, t0+4mo], test (t0+4mo, t0+5mo]
                        | train 3mo | test 1mo |           → window 3
                                    | ... |
```

- **Train window length**: 3 months (90 days) — chosen to match per-quarter drift periodicity from EDA. Long enough to fill the 30-day rolling lookback (RatePercentile P50/N=720h) ~3× over and accumulate enough trades for stable param selection. Short enough that within-train regime is approximately stable.
- **Test window length**: 1 month — enough trades for meaningful Sortino, short enough to stay in a single regime, gives ~48 windows over post-2022.
- **Walk step**: 1 month, non-overlapping test windows. Test windows are independent OOS observations.
- **Total windows per cell**: ⌊(span_months − 4) / 1⌋ + 1 ≈ 48 for post-2022 cells (∼52 months span).

### Window definition (precise)

For each cell:
1. Load all candles where `mts >= 2022-01-01 UTC`.
2. Sort ascending by mts. Compute series start = first candle mts, end = last candle mts.
3. Compute month boundaries within [start, end] using UTC month-end timestamps (reuse `month_end_timestamps_within` from sortino module).
4. For each contiguous 4-month group (train_months=3 + test_months=1) starting at month-boundary `M_i`:
   - `train_start_mts = M_i.start` (first ms of month M_i)
   - `train_end_mts = M_{i+3}.start - 1` (last ms before month M_{i+3} starts)
   - `test_start_mts = M_{i+3}.start`
   - `test_end_mts = M_{i+4}.start - 1`
5. Skip windows where train or test portion has < 200 candles (~1 week of 1h candles minimum).

This produces deterministic, calendar-aligned windows.

### Per-window flow

For each (strategy, cell, window):

1. **Sweep**: for each param variant from `strategy.param_grid_for_cell(symbol, period_agg, eda_cell)`:
   - Run `engine.run_backtest(candles, strategy(**params), record_start_mts=train_start, record_end_mts=train_end)`
   - Filter by health gates: `fill_rate >= 0.3` AND `n_trades >= 10`
   - Track Sortino + raw return for tie-break
2. **Pick winner**: `matrix.pick_sweep_winner` (reuse from earlier work — Sortino + +inf tie-break by raw return).
3. **OOS eval**: instantiate winning params, run `engine.run_backtest(candles, strategy(**best_params), record_start_mts=test_start, record_end_mts=test_end)`. State warms up automatically because engine processes all candles before `record_start_mts`.
4. **Baseline**: `engine.run_backtest(candles, AlwaysFRRStrategy(period_days=2), record_start_mts=test_start, record_end_mts=test_end)`.
5. **Record window outcome**: `(cell, strategy, window_idx, best_params, oos_metrics, baseline_metrics)`.

### EDA inputs

The 2026-05-18 EDA report (`docs/research/2026-05-18-phase3b-eda-gate-failure.md`) provides per-cell stats used by `param_grid_for_cell`:

- `acf_168h_pass`: per cell, true if ACF(168h) >= 0.3. From EDA:
  - fUSD × p2: 0.227 → false
  - fUSD × p30: 0.049 → false
  - fUSD × a30: 0.242 → false
  - fUST × p2: 0.008 → false
  - fUST × p30: -0.002 → false
  - fUST × a30: 0.012 → false
  - **All cells**: false → RatePercentile uses {168} lookback only (3 variants per cell, not 6).
- `close_over_ema_sigma_24` / `close_over_ema_sigma_168`: per-cell σ values from EDA table. Injected per cell into MeanReversion `ratio_sigma`.

EDA is **not re-run per WFO window** — global EDA on all post-2022 data is acceptable for param-grid structure decisions (lookback choice, sigma scale); per-window param selection happens via sweep within that grid.

## Decision Rules

### Per-(cell, strategy) qualification

A strategy qualifies on a cell when:

1. **Window-win consistency**: in ≥ **60% of WFO windows on that cell**, strategy's OOS `net_monthly_return_pct > baseline's OOS net_monthly_return_pct` for the same window.
2. **Mean magnitude**: across all eligible windows for that cell, `mean(strategy_net) > mean(baseline_net) * 1.05` (i.e., +5% relative).
3. **Health**: in ≥ 80% of eligible windows, strategy's OOS `fill_rate >= 0.3` AND `n_trades >= 5` (per-window — looser than per-cell because each window is 1 month).

"Eligible window" = WFO window where the strategy sweep produced a valid winner (not all variants filtered out).

### Per-strategy qualification (Phase 4 candidate)

Strategy enters Phase 4 candidate pool when it qualifies on **≥ 4 of 6 cells**.

### Stability diagnostics (informational, not gating)

Reported per (cell, strategy):

- **Param drift score**: number of distinct `(percentile, lookback_hours)` (or `(ema_span, threshold_sigma)`) combinations picked as winner across windows. High distinct count = unstable param → strategy fragile to regime drift.
- **Rolling OOS Sortino**: list of per-window OOS Sortino values for visual inspection. Look for "all windows positive" vs "alternating + / −" vs "early positive, late negative" patterns.
- **Pattern flags**: explicit note if (a) all windows positive (very strong), (b) ≥ 80% positive, (c) bimodal, (d) deteriorating trend over time.

## Architecture

### Reused modules (no changes)

- `modules/backtest/engine.py` — `run_backtest(record_start_mts, record_end_mts)` is exactly what WFO needs
- `modules/backtest/sortino.py` — month-end sampling + Sortino formula
- `modules/backtest/eda.py` — pure stat helpers; one-shot EDA already done
- `modules/backtest/strategies/base.py` — observe + param_grid_for_cell hooks
- `modules/backtest/strategies/always_frr.py` — baseline, unchanged
- `modules/backtest/split.py` — `compute_train_end_mts` kept as-is for any future single-split utility; not used by WFO

### New module: `modules/backtest/wfo.py`

Pure-function helpers:

```python
@dataclass(frozen=True)
class WfoWindow:
    train_start_mts: int
    train_end_mts: int
    test_start_mts: int
    test_end_mts: int


def compute_wfo_windows(
    candles: list[FundingCandle],
    train_months: int = 3,
    test_months: int = 1,
    step_months: int = 1,
    min_candles_per_segment: int = 200,
) -> list[WfoWindow]:
    """Generate calendar-aligned WFO windows.
    Filters out windows where train or test has < min_candles_per_segment candles.
    """
    ...
```

### Replaced module: `modules/backtest/matrix.py`

Existing `pick_sweep_winner` is reused. `evaluate_strategy_consistency` is **replaced** by new `evaluate_cell_qualification` (per WFO window list) and `evaluate_strategy_qualification` (across cells).

```python
@dataclass(frozen=True)
class WindowOutcome:
    window_idx: int
    train_start_mts: int
    train_end_mts: int
    test_start_mts: int
    test_end_mts: int
    status: str  # "ok" | "skipped:no_valid_candidate" | "errored"
    best_params: dict[str, Any] | None
    oos_net: Decimal | None
    oos_max_dd: Decimal | None
    oos_fill_rate: Decimal | None
    oos_sortino: Decimal | None
    baseline_net: Decimal | None
    baseline_sortino: Decimal | None


@dataclass(frozen=True)
class CellVerdict:
    qualifies: bool
    windows_eligible: int
    windows_strategy_beats_baseline: int
    pct_windows_won: Decimal
    mean_strategy_net: Decimal
    mean_baseline_net: Decimal
    relative_margin: Decimal  # (mean_strat - mean_base) / mean_base
    health_pct: Decimal       # fraction of windows passing fill+trade floor


@dataclass(frozen=True)
class StrategyVerdict:
    qualifies: bool
    cells_qualifying: int
    cells_played: int


def evaluate_cell_qualification(
    window_outcomes: list[WindowOutcome],
    consistency_threshold: Decimal = Decimal("0.60"),
    margin_threshold: Decimal = Decimal("0.05"),
    health_threshold: Decimal = Decimal("0.80"),
) -> CellVerdict: ...


def evaluate_strategy_qualification(
    per_cell_verdicts: list[CellVerdict],
    cells_required: int = 4,
    total_cells: int = 6,
) -> StrategyVerdict: ...
```

`run_cell` is **replaced** by `run_cell_wfo`:

```python
def run_cell_wfo(
    strategy_class: type[Strategy],
    candles: list[FundingCandle],
    eda_cell: dict[str, Any],
    cell_key: str,
    wfo_windows: list[WfoWindow],
) -> tuple[list[WindowOutcome], list[BacktestResult]]:
    """Run sweep + OOS eval for one (strategy, cell) across all WFO windows.

    Returns:
        (window_outcomes, baseline_per_window): per-window strategy outcomes
        and per-window baseline results (AlwaysFRR(period=2) on test portion).
    """
    ...
```

### New script: `scripts/run_phase3b_wfo_matrix.py`

Wrapper:
1. Load EDA JSON (built from 2026-05-18 EDA report)
2. For each (symbol, period_agg):
   - Load candles from Neon
   - `compute_wfo_windows`
   - Compute baseline per window
   - For each strategy: `run_cell_wfo`
   - Collect window outcomes
3. Aggregate via `evaluate_cell_qualification` per (cell, strategy)
4. Aggregate via `evaluate_strategy_qualification` per strategy
5. Compute stability diagnostics
6. Print summary

## Output Format

**Single markdown report**: `docs/research/2026-05-18-phase3b-wfo-results.md`

```markdown
# Phase 3b-WFO Results

## TL;DR
<one line: strategies qualifying for Phase 4 + best cell × strategy>

## Methodology Snapshot
- Window: post-2022-01-01
- WFO: 3-month train / 1-month test / 1-month walk
- N windows per cell: ~48
- Sweep metric: Sortino + fill_rate>=0.3 + n_trades>=10 floors
- Decision rule: per-cell 60% windows beat + margin >5%; per-strategy 4/6 cells
- WeekendPremium dropped (EDA refuted hypothesis)

## Strategy-Level Verdicts

| Strategy | Cells qualifying | Phase 4 candidate? |
|---|---|---|
| RatePercentile | X / 6 | yes/no |
| MeanReversion | X / 6 | yes/no |

## Per-Cell Detail

### fUSD × p2

| Strategy | Windows beat / eligible | Mean OOS net % | Baseline mean % | Margin | Health % | Qualifies |
|---|---|---|---|---|---|---|
| RatePercentile | XX / 48 (XX%) | 0.42 | 0.31 | +35% | 95% | yes |
| MeanReversion | XX / 48 (XX%) | ... | ... | ... | ... | yes/no |

**Stability diagnostics**:
- RatePercentile param drift: 4 distinct (p=25,n=168), (p=50,n=168), (p=75,n=168), (p=50,n=720) across 48 windows
- Rolling OOS Sortino: [chart-like ASCII or values]
- Pattern: 80% positive, stable mean, no deteriorating trend

(repeat for all 6 cells)

## Phase 3c Decision

- If ≥ 1 strategy qualifies → Phase 3c launched only if extended FRR work adds independent value
- If 0 strategies qualify → Phase 3c becomes mandatory (FRR-trend / SpikeDetect / extended hypothesis are the next levers)
- If qualified strategy shows high param drift / pattern instability → Phase 4 ship blocked pending stability fix
```

## Error Handling

| 情況 | 處理 |
|---|---|
| Cell 缺資料 (< 720 candles post-2022 OR < 5 valid WFO windows) | Log warning, results 表標 n/a, cell 不計入 strategy 分母 |
| WFO window 過短 (train < 200 candles OR test < 200 candles) | compute_wfo_windows 自動 skip; 不報錯 |
| Sweep 在某 window 無 valid candidate | window_outcome.status = "skipped:no_valid_candidate"; 不計入 consistency 分母 |
| Sweep Sortino = +inf 全部 candidate | tie-break by raw net_monthly_return_pct（reuse 既有 pick_sweep_winner） |
| Test portion n_trades == 0 | OOS net = 0%，自動低於 baseline（不會 qualify） |
| Strategy raises in window | catch + log + window_outcome.status = "errored", 不計入 |

## Testing

| 層級 | 涵蓋 |
|---|---|
| Unit | `compute_wfo_windows`: 48-month synthetic input → ~45 windows; min-candle filter; calendar alignment |
| Unit | `evaluate_cell_qualification`: 60% threshold edge cases; margin gate; health gate; skipped windows excluded |
| Unit | `evaluate_strategy_qualification`: 4-of-6 threshold; cells_played adjustment |
| Unit | `run_cell_wfo` orchestration on synthetic candle fixture spanning 6 months (sufficient for 2 WFO windows) |
| Integration | End-to-end on 12-month synthetic candle stream (no Neon dependency) |
| Manual | Full matrix run on Neon → results report |

## Implementation Order

1. `✨ Feat: compute_wfo_windows + WfoWindow dataclass + tests` (`wfo.py`)
2. `♻️ Refactor: matrix.py — split single-shot run_cell + WindowOutcome dataclass + evaluate_cell_qualification + evaluate_strategy_qualification + tests`
3. `✨ Feat: run_cell_wfo orchestration helper + synthetic-fixture integration test`
4. `✨ Feat: Phase 3b-WFO matrix runner script (scripts/run_phase3b_wfo_matrix.py)`
5. `📝 Docs: Phase 3b-WFO results + Phase 3c decision`

Estimated **5 commits** (smaller scope than original Phase 3b because Phase A+B+C foundations + RatePercentile + MeanReversion are reused).

## Risks & Open Questions

| 風險 | 處理 |
|---|---|
| 3-month train 仍跨越 regime boundary (drift 是 per-quarter) | Stability diagnostics (param drift score) will surface; if scoring shows pattern instability we narrow to 2-month train and re-run |
| Param drift across windows is high but strategy still qualifies on aggregate (60% + 5%) | Spec gates Phase 4 entry on stability diagnostics — explicit notation in results report that high param drift blocks ship until investigated |
| fUST p30 has only 17.7k candles (Phase 3b-WFO will produce fewer windows on that cell) | Acceptable; cell qualifies if ≥ 60% of *eligible* windows beat baseline (denominator adjusts) |
| RatePercentile lookback fixed at 168h per EDA — what if WFO shows lookback should be even shorter? | Out of scope for Phase 3b-WFO; if MeanReversion qualifies but RatePercentile doesn't due to lookback, Phase 3c can add lookback to the sweep grid |
| 60% threshold is arbitrary like the old 4/6 was | Documented as a heuristic; results report includes raw distribution so post-hoc sensitivity analysis is possible |

## Reused Phase 3b Artifacts (Already Shipped)

| Commit | Module | WFO status |
|---|---|---|
| `a6ed207` + `c67c40d` | `split.py` | Unused by WFO (kept for future single-split utility) |
| `51b24f5` + `bc8597b` | `sortino.py` | Reused unchanged |
| `7c2c673` | `strategies/base.py` | Reused unchanged |
| `af5f082` + `4ab9282` | `engine.py` + `schemas.py` | Reused unchanged — record window is exactly what WFO needs per-window |
| `0bf9ae4` + `7c02778` | `eda.py` + script | Reused; one-shot EDA already done |
| `f56e6b5` | gate-failure doc | Historical record of why we pivoted |

Implementation Plan: to be written via writing-plans skill.
