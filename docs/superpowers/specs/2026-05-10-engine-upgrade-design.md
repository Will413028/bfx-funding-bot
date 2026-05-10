# v2 Phase 1 — Backtest Engine Upgrade Design

**Status**: design (pending implementation plan)
**Date**: 2026-05-10
**Owner**: Will（solo）
**Brainstorm session**: 2026-05-10
**Parent**: v2 strategy iteration phase（Checkpoint 2 PASS 後）

---

## Why

Checkpoint 2 PASS 的數字 0.4028%/月 是 instant-fill / 0% drawdown / 無手續費 / 無 gap cost 的 toy 引擎產出。在比較 v2 策略前，必須先讓引擎能反映真實 friction，否則任何策略間的 A/B 數字都不可信。

引擎升級是 v2 整體三個 workstream 中第一個完成的（順序：引擎 → 資料 → 策略）。本 spec 只覆蓋 Phase 1 引擎升級；Phase 2（資料層）與 Phase 3（策略迭代）之後各自 brainstorm。

## What

加入四種 friction，集中由 `BacktestConfig` 管理：

1. **Bitfinex 15% fee** — 平台抽利息 15%
2. **Gap cost** — credit 到期到下一筆 offer 成交平均 gap（預設 30 min）
3. **Fill probability model** — 出價高於 market 時降低成交機率（EV-based）
4. **Bid-ask spread** — strategy 出價 vs market_rate 的偏離量；Phase 1 用 candle close 當 FRR proxy，Phase 2 補完 funding_stats 後 Phase 3 再 swap 真 FRR

## Out of Scope

| 項目 | 推遲到 |
|---|---|
| 真 FRR 接入（`market_rate_source="frr"`） | Phase 2 補資料、Phase 3 啟用 |
| Order book depth model（queue position） | v3+（需 WebSocket 資料）|
| Liquidation cascade detection | v3+（需 volume spike + 跨市場資料）|
| Multi-instrument allocation（資金分層）| v3+（單 instrument 已可比較策略相對表現）|
| Walk-forward validation / overfit guard | v3+（先看 6 策略絕對差異）|
| 多 timeframe（5m / 1D） | v3+（`gap_candles` 假設 1h，加 `candle_minutes` 參數即可擴展）|
| `BacktestConfig.no_friction()` preset | YAGNI；想要 zero-friction 自己構造 |

## Architecture

當前 signature：

```python
run_backtest(candles: list[FundingCandle], strategy: Strategy) -> BacktestResult
```

升級後：

```python
run_backtest(
    candles: list[FundingCandle],
    strategy: Strategy,
    config: BacktestConfig | None = None,  # default = realistic friction
) -> BacktestResult
```

Per-decision 邏輯（在 engine 內部）：

```text
for each candle (post-cooldown):
    decision = strategy.decide(candle)
    if decision is None: continue

    # 1. spread → 2. fill probability
    market_rate = candle.close   # Phase 1: candle close 當 FRR proxy
    spread_pct = (decision.rate - market_rate) / market_rate
    fill_prob = compute_fill_prob(spread_pct, config.fill_alpha)

    # 3. fee → effective rate
    effective_rate = decision.rate × fill_prob × (1 - config.fee_rate)

    # 4. gap cost → cooldown 拉長（不扣 effective_period）
    gap_candles = ceil(config.gap_minutes / 60)

    # equity update + cooldown
    equity ×= 1 + effective_rate × decision.period_days
    cooldown_until_idx = i + decision.period_days × 24 + gap_candles
```

**關鍵設計選擇**：

1. **`BacktestConfig` 集中管理 friction** — 而非分散 kwargs。便於 Phase 3 strategy 比較共用同一份 config
2. **EV-based fill_prob** — `equity *= 1 + rate × prob × period`，deterministic、可重現、testable。蒙地卡羅版留 v3
3. **Gap 進 cooldown，不進 effective_period** — equity 在 lend 期間長滿；gap 透過拉長 cooldown 自然減少 cycle 數，避免 double-count
4. **`market_rate is None` 退回 instant-fill** — 不 raise；strategy 層應該已濾掉 None close（AlwaysFRR 已經如此）
5. **EV-based fill 的 conservative bias** — fill_prob < 1 時 cooldown 照進，傾向低估 high-spread 策略。可接受作為 v2 簡化
6. **預設值都是「保守的真實值」** — 不提供 zero-friction preset；v1 結果已記錄在 Checkpoint 2 doc

## Components

### `BacktestConfig`（新增 `modules/backtest/config.py`）

```python
from dataclasses import dataclass
from decimal import Decimal
from typing import Literal


@dataclass(frozen=True)
class BacktestConfig:
    """Backtest engine friction parameters.

    Defaults model realistic Bitfinex funding market conditions.
    """

    # Bitfinex 抽 15% 利息（強制平台費）
    fee_rate: Decimal = Decimal("0.15")

    # Credit 到期到下一筆 offer 成交的平均間隔
    # journal §M1.2 估 ~20min；保守取 30min
    gap_minutes: int = 30

    # Fill prob slope: fill_prob = max(0, 1 - fill_alpha × spread_pct)
    # alpha=5 → 10% above market = 50% fill；20% above = 0% fill
    fill_alpha: Decimal = Decimal("5.0")

    # Phase 1 = candle_close (FRR proxy)；Phase 3 = frr (real, after Phase 2 backfill)
    market_rate_source: Literal["candle_close", "frr"] = "candle_close"

    def __post_init__(self) -> None:
        if not (Decimal("0") <= self.fee_rate <= Decimal("1")):
            raise ValueError(f"fee_rate must be in [0,1], got {self.fee_rate}")
        if self.gap_minutes < 0:
            raise ValueError(f"gap_minutes must be non-negative, got {self.gap_minutes}")
        if self.fill_alpha < 0:
            raise ValueError(f"fill_alpha must be non-negative, got {self.fill_alpha}")


def compute_fill_prob(spread_pct: Decimal, fill_alpha: Decimal) -> Decimal:
    """spread_pct <= 0 (offer 等於或低於 market) → 1.0；
    spread_pct > 0 → linear decay to 0 at spread_pct = 1/fill_alpha."""
    if spread_pct <= 0:
        return Decimal("1.0")
    return max(Decimal("0"), Decimal("1") - fill_alpha * spread_pct)
```

### `BacktestResult`（修改 `modules/backtest/schemas.py`）

新增欄位：

```python
class BacktestResult(BaseModel):
    # ... 現有欄位 ...
    fill_rate: Decimal           # n_filled / n_trades （EV 累積平均）
    gross_monthly_return_pct: Decimal  # 不扣 fee
    # 現有 monthly_return_pct rename 為 net_monthly_return_pct
    net_monthly_return_pct: Decimal    # 扣 fee + gap 後
```

### `_apply_friction`（engine.py 內部 helper）

```python
def _apply_friction(
    decision: LendDecision,
    candle: FundingCandle,
    config: BacktestConfig,
) -> tuple[Decimal, Decimal]:
    """Compute (effective_rate, fill_prob) for a strategy decision.

    effective_rate = decision.rate × fill_prob × (1 - fee_rate)
    Caller multiplies by period_days to grow equity.
    """
    market_rate = _resolve_market_rate(candle, config.market_rate_source)
    if market_rate is None or market_rate == 0:
        # Can't resolve market — instant fill at decision rate (no spread penalty)
        return decision.rate * (Decimal("1") - config.fee_rate), Decimal("1")

    spread_pct = (decision.rate - market_rate) / market_rate
    fill_prob = compute_fill_prob(spread_pct, config.fill_alpha)
    effective_rate = decision.rate * fill_prob * (Decimal("1") - config.fee_rate)
    return effective_rate, fill_prob
```

### `run_backtest`（修改 `modules/backtest/engine.py`）

- 加 `config` 參數（default `None`，內部 fallback `BacktestConfig()`）
- 主迴圈用 `_apply_friction` 計算 effective_rate
- `cooldown_until_idx = i + period_days × 24 + gap_candles`
- 新累積 `n_filled` 算 fill_rate
- 同時計算 gross 與 net monthly return

### `scripts/run_backtest.py`

- 不加新參數（用 default config）
- log 多印 `fill_rate`、`gross_monthly_return_pct`、`net_monthly_return_pct`

## Testing

### `tests/modules/backtest/test_config.py`（新增）

```python
# BacktestConfig validation
def test_config_default_is_realistic_friction()
def test_config_rejects_negative_fee()
def test_config_rejects_fee_above_one()
def test_config_rejects_negative_gap_minutes()
def test_config_rejects_negative_fill_alpha()

# compute_fill_prob — pure function
def test_fill_prob_at_market_is_one()         # spread=0 → 1.0
def test_fill_prob_below_market_is_one()      # spread<0 → 1.0
def test_fill_prob_linear_decay()             # spread=10% alpha=5 → 0.5
def test_fill_prob_clamps_at_zero()           # spread=30% alpha=5 → 0
```

### `tests/modules/backtest/test_engine.py`（修改 + 新增）

| 測試 | 動作 |
|---|---|
| `test_run_backtest_constant_rate_produces_expected_monthly_return` | 預期 `0.3 ± 0.01` → `0.255 ± 0.01`（15% fee adjustment）+ docstring 解釋 |
| `test_run_backtest_handles_empty_candles` | 不動 |
| `test_run_backtest_skips_candles_with_no_close` | 不動 |
| `test_run_backtest_applies_15pct_fee` | 新增 — constant rate, default config，net = gross × 0.85 |
| `test_run_backtest_gap_minutes_extends_cooldown` | 新增 — 比較 `gap_minutes=0` 與顯著大於 candle 寬度的 gap（例 `gap_minutes=180`，3 candles），驗證後者 cooldown 拉長、n_trades 相對減少。Implementer 自選 candle 集大小使效應觀察得到 |
| `test_run_backtest_spread_above_market_reduces_fill` | 新增 — mock strategy 寫死 decision.rate = candle.close × 1.10, alpha=5 → effective rate × 0.5 |

不寫 `test_run_backtest_zero_friction_matches_old_behavior`（YAGNI；v1 數字已在 Checkpoint 2 doc）。

### `tests/modules/backtest/strategies/test_always_frr.py`

不動。AlwaysFRR 出價等於 candle close → spread=0 → fill_prob=1，friction 從 fee + gap 進入。

### Coverage 目標

- `test_config.py`：100%（純函數 + dataclass）
- `test_engine.py`：所有 friction 路徑都有測試打到（fee=0、fill_prob=0、gap=0）

## Verification（Phase 1 PASS 條件）

1. `cd backend_py && uv run pytest -v -m "not integration"` 全過（28 既有 + 9 新 + 3 新 = 40 個）
2. `cd backend_py && uv run mypy src/ && uv run ruff check` 無錯
3. `cd backend_py && uv run python scripts/run_backtest.py --days-back 30` exit 0，log 印出新 baseline 數字
4. 新 baseline 數字寫一行進 wiki Lessons Learned（`~/second-brain/wiki/projects/bfx-funding-bot.md`），格式：「AlwaysFRR baseline post-friction: X.XX%/月（was 0.4028 pre-friction）」

不另寫 `credible-baseline.md`（spec 太重）；wiki 一行紀錄即可。

## Commit Plan（4 commits，獨立可 revert）

```
1. ✨ Feat: backtest/config — BacktestConfig + compute_fill_prob
   - new: modules/backtest/config.py
   - new: tests/modules/backtest/test_config.py
   - 9 tests pass

2. ✨ Feat: backtest/engine — apply config-driven friction
   - modify engine.py: add config arg, _apply_friction helper
   - modify schemas.py: BacktestResult adds fill_rate, gross/net split
   - update test_engine.py: 1 existing assertion (0.3 → 0.255), 3 new friction tests
   - all tests pass

3. ♻️ Refactor: run_backtest CLI — log gross/net + fill_rate
   - modify scripts/run_backtest.py
   - re-run against Neon, capture new baseline number

4. 📝 Docs: wiki Lessons Learned — engine v2 new baseline
   - modify ~/second-brain/wiki/projects/bfx-funding-bot.md
   - one-line: "AlwaysFRR baseline post-friction: X.XX%/月 (was 0.4028 pre-friction)"
```

## Risks

| Risk | 緩解 |
|---|---|
| `gap_minutes=30` 是猜測值，敏感度未知 | Phase 3 收尾時 sensitivity sweep（gap=15/30/60）寫進 strategy 比較表 |
| Linear fill_prob 過於粗糙 | 文件記為「v2 簡化」；Phase 3 觀察到 high-spread 策略結果跟直覺差太多再升 sigmoid |
| EV-based fill 對 high-spread 策略偏 conservative | 同上；spread=10% 結果是「真 EV 的 lower bound」 |
| 改現有 test 預期值（從 0.3% → 0.255%）會破壞 git history 對照 | commit message 註明「engine friction added; baseline expectation updated to 15%-fee-adjusted」 |
| Phase 1 `market_rate_source="candle_close"` 跟 Phase 3 真 FRR 行為不同 | spec 明寫；`Literal` type 強迫 Phase 3 補完才能 swap |

## Time Estimate

~1 working day（4-6 hours）。

| Step | 估時 |
|---|---|
| `BacktestConfig` + 9 tests | 1.5 h |
| `engine.py` 改 + 3 new tests + update 1 existing | 2.5 h |
| `run_backtest.py` log 升級 | 0.5 h |
| 跑一次 + 寫 wiki Lessons | 0.5 h |
| Buffer / 預期問題 | 0.5-1 h |

## Next Phase

Phase 1 PASS 後 → Phase 2 brainstorm（資料層：fUSD/fUST 全歷史 1h × p2/p30/a30 + funding_stats 全欄位回填）。
