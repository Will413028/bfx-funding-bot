# v2 Phase 1 — Backtest Engine Upgrade Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add Bitfinex 15% fee, gap cost, fill probability model, and bid-ask spread (with candle close as FRR proxy) to the backtest engine via a new `BacktestConfig` dataclass.

**Architecture:** Extend `run_backtest()` with optional `config: BacktestConfig | None = None` parameter. Engine internally computes spread → fill_prob → fee → cooldown extension. Track gross (pre-fee) and net (post-fee) equity in parallel.

**Tech Stack:** Python 3.13, Pydantic, Decimal, pytest, mypy, ruff. uv for dep management.

**Spec:** `docs/superpowers/specs/2026-05-10-engine-upgrade-design.md`

**Working dir:** `backend_py/` (run all `uv run` commands from this dir).

---

## File Structure

| File | Action | Purpose |
|---|---|---|
| `backend_py/src/bfx_funding_bot/modules/backtest/config.py` | Create | `BacktestConfig` dataclass + `compute_fill_prob` helper |
| `backend_py/tests/modules/backtest/test_config.py` | Create | 9 unit tests for config + helper |
| `backend_py/src/bfx_funding_bot/modules/backtest/schemas.py` | Modify | Rename `monthly_return_pct` → `net_monthly_return_pct`; add `gross_monthly_return_pct`, `fill_rate` |
| `backend_py/src/bfx_funding_bot/modules/backtest/engine.py` | Modify | Add `config` arg, `_apply_friction`, gross/net equity tracking |
| `backend_py/tests/modules/backtest/test_engine.py` | Modify | Update 1 existing assertion + add 3 friction tests |
| `backend_py/tests/modules/backtest/test_schemas.py` | Modify | Use new field names |
| `backend_py/scripts/run_backtest.py` | Modify | Use new field names; log gross/net/fill_rate |
| `~/second-brain/wiki/projects/bfx-funding-bot.md` | Modify | One-line Lessons Learned (different repo; controller step) |

---

## Phase 1 — Task 1: BacktestConfig + compute_fill_prob

**Phase exit criterion:** Standalone `config.py` module with `BacktestConfig` frozen dataclass and `compute_fill_prob` pure function. 9 unit tests pass. mypy and ruff clean.

**Files:**
- Create: `backend_py/src/bfx_funding_bot/modules/backtest/config.py`
- Create: `backend_py/tests/modules/backtest/test_config.py`

- [ ] **Step 1: Write failing test**

Path: `backend_py/tests/modules/backtest/test_config.py`

```python
from decimal import Decimal

import pytest

from bfx_funding_bot.modules.backtest.config import BacktestConfig, compute_fill_prob


def test_config_default_is_realistic_friction() -> None:
    """Defaults should model real Bitfinex funding conditions."""
    cfg = BacktestConfig()
    assert cfg.fee_rate == Decimal("0.15")
    assert cfg.gap_minutes == 30
    assert cfg.fill_alpha == Decimal("5.0")
    assert cfg.market_rate_source == "candle_close"


def test_config_rejects_negative_fee() -> None:
    with pytest.raises(ValueError, match="fee_rate"):
        BacktestConfig(fee_rate=Decimal("-0.1"))


def test_config_rejects_fee_above_one() -> None:
    with pytest.raises(ValueError, match="fee_rate"):
        BacktestConfig(fee_rate=Decimal("1.5"))


def test_config_rejects_negative_gap_minutes() -> None:
    with pytest.raises(ValueError, match="gap_minutes"):
        BacktestConfig(gap_minutes=-1)


def test_config_rejects_negative_fill_alpha() -> None:
    with pytest.raises(ValueError, match="fill_alpha"):
        BacktestConfig(fill_alpha=Decimal("-1"))


def test_fill_prob_at_market_is_one() -> None:
    """spread_pct=0 → fill_prob=1.0."""
    assert compute_fill_prob(Decimal("0"), Decimal("5")) == Decimal("1.0")


def test_fill_prob_below_market_is_one() -> None:
    """spread_pct<0 (offer below market) → always fills."""
    assert compute_fill_prob(Decimal("-0.10"), Decimal("5")) == Decimal("1.0")


def test_fill_prob_linear_decay() -> None:
    """spread_pct=0.10, alpha=5 → fill_prob = 1 - 5*0.10 = 0.5."""
    result = compute_fill_prob(Decimal("0.10"), Decimal("5"))
    assert result == Decimal("0.5")


def test_fill_prob_clamps_at_zero() -> None:
    """spread_pct=0.30, alpha=5 → 1 - 1.5 = -0.5 → clamped to 0."""
    result = compute_fill_prob(Decimal("0.30"), Decimal("5"))
    assert result == Decimal("0")
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd backend_py && uv run pytest tests/modules/backtest/test_config.py -v
```

Expected: `ModuleNotFoundError: No module named 'bfx_funding_bot.modules.backtest.config'`.

- [ ] **Step 3: Write `config.py`**

Path: `backend_py/src/bfx_funding_bot/modules/backtest/config.py`

```python
from dataclasses import dataclass
from decimal import Decimal
from typing import Literal


@dataclass(frozen=True)
class BacktestConfig:
    """Backtest engine friction parameters.

    Defaults model realistic Bitfinex funding market conditions:
      - fee_rate=0.15 (Bitfinex platform fee, takes 15% of interest)
      - gap_minutes=30 (avg credit return → next offer fill latency)
      - fill_alpha=5.0 (fill probability slope; 10% above market = 50% fill)
      - market_rate_source="candle_close" (FRR proxy until Phase 2 backfills funding_stats)
    """

    fee_rate: Decimal = Decimal("0.15")
    gap_minutes: int = 30
    fill_alpha: Decimal = Decimal("5.0")
    market_rate_source: Literal["candle_close", "frr"] = "candle_close"

    def __post_init__(self) -> None:
        if not (Decimal("0") <= self.fee_rate <= Decimal("1")):
            raise ValueError(f"fee_rate must be in [0,1], got {self.fee_rate}")
        if self.gap_minutes < 0:
            raise ValueError(f"gap_minutes must be non-negative, got {self.gap_minutes}")
        if self.fill_alpha < 0:
            raise ValueError(f"fill_alpha must be non-negative, got {self.fill_alpha}")


def compute_fill_prob(spread_pct: Decimal, fill_alpha: Decimal) -> Decimal:
    """Linear fill probability model.

    Args:
        spread_pct: (offer_rate - market_rate) / market_rate.
                    Negative = offer below market → always fills.
                    Positive = offer above market → linear decay.
        fill_alpha: slope; alpha=5 means 10% above market = 50% fill, 20% = 0%.

    Returns:
        Probability in [0, 1].
    """
    if spread_pct <= 0:
        return Decimal("1.0")
    return max(Decimal("0"), Decimal("1") - fill_alpha * spread_pct)
```

- [ ] **Step 4: Run test to verify it passes**

```bash
cd backend_py && uv run pytest tests/modules/backtest/test_config.py -v
```

Expected: 9 passed.

- [ ] **Step 5: Run mypy + ruff**

```bash
cd backend_py && uv run mypy src/ && uv run ruff check
```

Expected: no errors.

- [ ] **Step 6: Commit**

```bash
git add backend_py/src/bfx_funding_bot/modules/backtest/config.py \
        backend_py/tests/modules/backtest/test_config.py
git commit -m "✨ Feat: backtest/config — BacktestConfig + compute_fill_prob

Frozen dataclass for engine friction parameters (fee_rate, gap_minutes,
fill_alpha, market_rate_source). Linear fill probability helper.

Phase 1 of v2 strategy iteration. Standalone — engine integration
in next commit.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Phase 1 — Task 2: Engine + Schema atomic refactor

**Phase exit criterion:** `run_backtest()` accepts optional `BacktestConfig`; applies fee, gap, fill_prob, spread per spec. `BacktestResult` schema has `gross_monthly_return_pct`, `net_monthly_return_pct` (renamed from `monthly_return_pct`), and `fill_rate`. All existing tests adjusted to new field names. 3 new friction tests pass. `scripts/run_backtest.py` compiles and runs (logging not yet enhanced).

**Files:**
- Modify: `backend_py/src/bfx_funding_bot/modules/backtest/schemas.py`
- Modify: `backend_py/tests/modules/backtest/test_schemas.py`
- Modify: `backend_py/src/bfx_funding_bot/modules/backtest/engine.py`
- Modify: `backend_py/tests/modules/backtest/test_engine.py`
- Modify: `backend_py/scripts/run_backtest.py` (minimal — field rename only)

- [ ] **Step 1: Update `BacktestResult` schema**

Path: `backend_py/src/bfx_funding_bot/modules/backtest/schemas.py`

Replace the `BacktestResult` class (keep `LendDecision` unchanged):

```python
class BacktestResult(BaseModel):
    """Output of a single backtest run.

    Tracks gross (pre-fee) and net (post-fee) returns separately so callers
    can attribute the gap between gross and net to the Bitfinex 15% fee.
    """

    model_config = ConfigDict(frozen=True)

    strategy_name: str
    symbol: str
    start_mts: int
    end_mts: int
    n_candles: int
    gross_monthly_return_pct: Decimal  # before Bitfinex 15% fee
    net_monthly_return_pct: Decimal    # after fee + fill_prob + gap (what user keeps)
    max_drawdown_pct: Decimal           # tracked on net equity (conservative)
    n_trades: int
    fill_rate: Decimal                  # avg fill_prob across trades; 1.0 = always filled
```

- [ ] **Step 2: Update `test_schemas.py` to use new field names**

Path: `backend_py/tests/modules/backtest/test_schemas.py`

Replace the file contents:

```python
from decimal import Decimal

from bfx_funding_bot.modules.backtest.schemas import BacktestResult, LendDecision


def test_backtest_result_construction() -> None:
    result = BacktestResult(
        strategy_name="always_frr",
        symbol="fUST",
        start_mts=1704067200000,
        end_mts=1706745600000,
        n_candles=720,
        gross_monthly_return_pct=Decimal("0.45"),
        net_monthly_return_pct=Decimal("0.3825"),
        max_drawdown_pct=Decimal("0.05"),
        n_trades=30,
        fill_rate=Decimal("1.0"),
    )
    assert result.strategy_name == "always_frr"
    assert result.gross_monthly_return_pct == Decimal("0.45")
    assert result.net_monthly_return_pct == Decimal("0.3825")
    assert result.max_drawdown_pct == Decimal("0.05")
    assert result.fill_rate == Decimal("1.0")


def test_lend_decision_construction() -> None:
    d = LendDecision(mts=1704067200000, rate=Decimal("0.000123"), period_days=2)
    assert d.mts == 1704067200000
    assert d.rate == Decimal("0.000123")
    assert d.period_days == 2
```

- [ ] **Step 3: Update `engine.py` to apply friction**

Path: `backend_py/src/bfx_funding_bot/modules/backtest/engine.py`

Replace the file contents:

```python
import math
from decimal import Decimal

from bfx_funding_bot.modules.backtest.config import BacktestConfig, compute_fill_prob
from bfx_funding_bot.modules.backtest.schemas import BacktestResult, LendDecision
from bfx_funding_bot.modules.backtest.strategies.base import Strategy
from bfx_funding_bot.modules.candles.schemas import FundingCandle


def _resolve_market_rate(candle: FundingCandle, source: str) -> Decimal | None:
    """Phase 1 only supports candle_close. Phase 3 will add 'frr' source."""
    if source == "candle_close":
        return candle.close
    raise ValueError(f"unsupported market_rate_source: {source!r}")


def _apply_friction(
    decision: LendDecision,
    candle: FundingCandle,
    config: BacktestConfig,
) -> tuple[Decimal, Decimal]:
    """Compute (gross_rate, fill_prob) for a strategy decision.

    gross_rate = decision.rate * fill_prob   (pre-fee per-period rate)
    Caller multiplies by period_days and (1 - fee_rate) for net.
    """
    market_rate = _resolve_market_rate(candle, config.market_rate_source)
    if market_rate is None or market_rate == 0:
        # Cannot resolve market — assume offer fills at decision rate (no spread penalty)
        return decision.rate, Decimal("1")

    spread_pct = (decision.rate - market_rate) / market_rate
    fill_prob = compute_fill_prob(spread_pct, config.fill_alpha)
    gross_rate = decision.rate * fill_prob
    return gross_rate, fill_prob


def run_backtest(
    candles: list[FundingCandle],
    strategy: Strategy,
    config: BacktestConfig | None = None,
) -> BacktestResult:
    """Run a single strategy over a candle series and return summary metrics.

    Friction model (controlled by `config`):
      - fee_rate: Bitfinex 15% fee on interest paid (default)
      - gap_minutes: idle time between credit return and next offer (default 30)
      - fill_alpha: linear fill probability slope (default 5.0)
      - market_rate_source: "candle_close" (Phase 1 FRR proxy)

    Per-decision flow:
      1. Strategy emits LendDecision (rate, period_days)
      2. Spread vs market → fill_prob (EV-based, deterministic)
      3. gross_rate = decision.rate * fill_prob   (pre-fee)
      4. net_rate = gross_rate * (1 - fee_rate)
      5. equity *= 1 + rate * period_days  (gross & net tracked separately)
      6. Cooldown extends by ceil(gap_minutes / 60) candles (1h candle assumption)

    Drawdown is computed on net equity (matches what the user actually has).
    """
    config = config or BacktestConfig()

    if not candles:
        return BacktestResult(
            strategy_name=strategy.name,
            symbol="",
            start_mts=0,
            end_mts=0,
            n_candles=0,
            gross_monthly_return_pct=Decimal("0"),
            net_monthly_return_pct=Decimal("0"),
            max_drawdown_pct=Decimal("0"),
            n_trades=0,
            fill_rate=Decimal("0"),
        )

    sorted_candles = sorted(candles, key=lambda c: c.mts)
    symbol = sorted_candles[0].symbol
    start_mts = sorted_candles[0].mts
    end_mts = sorted_candles[-1].mts

    gross_equity = Decimal("1")
    net_equity = Decimal("1")
    peak = net_equity
    max_dd = Decimal("0")
    n_trades = 0
    n_filled_acc = Decimal("0")  # sum of fill_prob across trades
    cooldown_until_idx = -1
    gap_candles = math.ceil(config.gap_minutes / 60)
    one_minus_fee = Decimal("1") - config.fee_rate

    for i, candle in enumerate(sorted_candles):
        if i <= cooldown_until_idx:
            continue
        decision = strategy.decide(candle)
        if decision is None:
            continue

        gross_rate, fill_prob = _apply_friction(decision, candle, config)

        period = Decimal(decision.period_days)
        gross_equity = gross_equity * (Decimal("1") + gross_rate * period)
        net_rate = gross_rate * one_minus_fee
        net_equity = net_equity * (Decimal("1") + net_rate * period)

        n_trades += 1
        n_filled_acc += fill_prob
        cooldown_until_idx = i + decision.period_days * 24 + gap_candles

        if net_equity > peak:
            peak = net_equity
        dd = (peak - net_equity) / peak if peak > 0 else Decimal("0")
        if dd > max_dd:
            max_dd = dd

    if end_mts > start_mts:
        # Decimal-only arithmetic to avoid float precision drift
        total_hours = Decimal(end_mts - start_mts) / Decimal(3_600_000)
    else:
        total_hours = Decimal("0")
    months_elapsed = total_hours / Decimal("720") if total_hours > 0 else Decimal("0")

    if months_elapsed > 0:
        gross_monthly = (gross_equity - Decimal("1")) / months_elapsed * Decimal("100")
        net_monthly = (net_equity - Decimal("1")) / months_elapsed * Decimal("100")
    else:
        gross_monthly = Decimal("0")
        net_monthly = Decimal("0")

    fill_rate = (n_filled_acc / Decimal(n_trades)) if n_trades > 0 else Decimal("0")

    return BacktestResult(
        strategy_name=strategy.name,
        symbol=symbol,
        start_mts=start_mts,
        end_mts=end_mts,
        n_candles=len(sorted_candles),
        gross_monthly_return_pct=gross_monthly,
        net_monthly_return_pct=net_monthly,
        max_drawdown_pct=max_dd * Decimal("100"),
        n_trades=n_trades,
        fill_rate=fill_rate,
    )
```

- [ ] **Step 4: Update `test_engine.py` (1 assertion change + 3 new tests)**

Path: `backend_py/tests/modules/backtest/test_engine.py`

Replace the file contents:

```python
from decimal import Decimal

from bfx_funding_bot.modules.backtest.config import BacktestConfig
from bfx_funding_bot.modules.backtest.engine import run_backtest
from bfx_funding_bot.modules.backtest.schemas import LendDecision
from bfx_funding_bot.modules.backtest.strategies.always_frr import AlwaysFRRStrategy
from bfx_funding_bot.modules.backtest.strategies.base import Strategy
from bfx_funding_bot.modules.candles.schemas import FundingCandle


def _candles_constant_rate(rate: str, n: int = 720) -> list[FundingCandle]:
    """720 hourly candles ≈ 30 days. Constant rate makes math hand-checkable."""
    return [
        FundingCandle(
            symbol="fUST",
            timeframe="1h",
            period_agg="p2",
            mts=1704067200000 + i * 3_600_000,  # +1h per candle
            open=Decimal(rate),
            close=Decimal(rate),
            high=Decimal(rate),
            low=Decimal(rate),
            volume=Decimal("100"),
        )
        for i in range(n)
    ]


def test_run_backtest_constant_rate_produces_expected_monthly_return() -> None:
    """At constant 0.0001 daily rate over 30 days lending continuously with
    default config (fee=0.15, gap=30min):
      - Per trade: rate*period = 0.0002 gross; *(1-0.15) = 0.00017 net
      - 15 lends compounded: net ≈ 1.00017^15 - 1 ≈ 0.002553 = 0.2553%
      - Updated from old expected 0.3% (pre-friction)
    """
    candles = _candles_constant_rate("0.0001", n=720)
    strategy = AlwaysFRRStrategy(period_days=2)

    result = run_backtest(candles, strategy)

    assert result.strategy_name == "always_frr_p2"
    assert result.symbol == "fUST"
    assert result.n_candles == 720
    # 15% fee adjustment: 0.3% → 0.255% (compounding deviation < 0.001%)
    assert abs(result.net_monthly_return_pct - Decimal("0.255")) < Decimal("0.01")
    # Gross is pre-fee, ~0.3%
    assert abs(result.gross_monthly_return_pct - Decimal("0.3")) < Decimal("0.01")
    assert result.n_trades == 15
    assert result.max_drawdown_pct == Decimal("0")
    # AlwaysFRR posts at candle close → spread=0 → fill_prob=1
    assert result.fill_rate == Decimal("1.0")


def test_run_backtest_handles_empty_candles() -> None:
    candles: list[FundingCandle] = []
    strategy = AlwaysFRRStrategy(period_days=2)
    result = run_backtest(candles, strategy)
    assert result.n_candles == 0
    assert result.n_trades == 0
    assert result.gross_monthly_return_pct == Decimal("0")
    assert result.net_monthly_return_pct == Decimal("0")
    assert result.max_drawdown_pct == Decimal("0")
    assert result.fill_rate == Decimal("0")


def test_run_backtest_skips_candles_with_no_close() -> None:
    candles = _candles_constant_rate("0.0001", n=720)
    candles_with_holes = []
    for i, c in enumerate(candles):
        if i % 100 == 0:
            candles_with_holes.append(c.model_copy(update={"close": None}))
        else:
            candles_with_holes.append(c)
    strategy = AlwaysFRRStrategy(period_days=2)
    result = run_backtest(candles_with_holes, strategy)
    assert result.n_candles == 720
    assert result.n_trades > 0


def test_run_backtest_applies_15pct_fee() -> None:
    """Default config has fee_rate=0.15. Net should equal gross × 0.85
    (small compounding deviation < 0.1%)."""
    candles = _candles_constant_rate("0.0001", n=720)
    strategy = AlwaysFRRStrategy(period_days=2)

    result = run_backtest(candles, strategy)

    # Per-trade gross ≈ 0.0002, net ≈ 0.00017 → ratio 0.85 holds at compounded level
    ratio = result.net_monthly_return_pct / result.gross_monthly_return_pct
    assert abs(ratio - Decimal("0.85")) < Decimal("0.001")


def test_run_backtest_gap_minutes_extends_cooldown() -> None:
    """Comparing gap_minutes=0 vs gap_minutes=180 (3 candles).

    With period=2 (48 candles cooldown) and 720 candles:
      - gap=0:   cooldown_until = i+48 → 15 trades
      - gap=180: cooldown_until = i+51 → 14 trades
    Higher gap → fewer trades → lower compounded return.
    """
    candles = _candles_constant_rate("0.0001", n=720)
    strategy = AlwaysFRRStrategy(period_days=2)

    no_gap = run_backtest(candles, strategy, BacktestConfig(gap_minutes=0))
    big_gap = run_backtest(candles, strategy, BacktestConfig(gap_minutes=180))

    assert big_gap.n_trades < no_gap.n_trades
    assert big_gap.net_monthly_return_pct < no_gap.net_monthly_return_pct


def test_run_backtest_spread_above_market_reduces_fill() -> None:
    """Strategy posts 10% above candle close → spread_pct=0.10.
    With default fill_alpha=5: fill_prob = 1 - 5*0.10 = 0.5.
    So gross = 0.5 × what AlwaysFRR-at-1.10× would have been.
    """
    candles = _candles_constant_rate("0.0001", n=720)

    class BidAboveMarketStrategy(Strategy):
        @property
        def name(self) -> str:
            return "bid_10pct_above_market"

        def decide(self, candle: FundingCandle) -> LendDecision | None:
            if candle.close is None:
                return None
            return LendDecision(
                mts=candle.mts,
                rate=candle.close * Decimal("1.10"),  # 10% above market
                period_days=2,
            )

    result = run_backtest(candles, BidAboveMarketStrategy())

    # fill_rate = 0.5 (every trade had spread_pct=0.10)
    assert abs(result.fill_rate - Decimal("0.5")) < Decimal("0.001")
    # gross_rate per trade = 0.0001 × 1.10 × 0.5 = 0.000055; period=2 → 0.00011
    # 15 trades compounded: gross_monthly ≈ (1.00011^15 - 1) / 1 month * 100 ≈ 0.165%
    assert abs(result.gross_monthly_return_pct - Decimal("0.165")) < Decimal("0.02")
```

- [ ] **Step 5: Update `scripts/run_backtest.py` (minimal — field rename only)**

Path: `backend_py/scripts/run_backtest.py`

Find this line (around line 84):

```python
        logger.info("  Monthly return:     %s%%", result.monthly_return_pct)
```

Replace with:

```python
        logger.info("  Monthly return:     %s%% (net)", result.net_monthly_return_pct)
```

(More detailed logging added in Task 3.)

- [ ] **Step 6: Run all tests**

```bash
cd backend_py && uv run pytest -v -m "not integration"
```

Expected:
- 9 new test_config.py tests pass
- 3 existing test_engine.py tests still pass (1 assertion updated)
- 3 new test_engine.py tests pass
- 2 test_schemas.py tests pass (using new field names)
- All other tests unchanged
- Total: 28 prior + 9 new + 3 new = 40 tests pass

- [ ] **Step 7: Run mypy + ruff**

```bash
cd backend_py && uv run mypy src/ && uv run ruff check
```

Expected: no errors.

- [ ] **Step 8: Commit**

```bash
git add backend_py/src/bfx_funding_bot/modules/backtest/schemas.py \
        backend_py/src/bfx_funding_bot/modules/backtest/engine.py \
        backend_py/tests/modules/backtest/test_schemas.py \
        backend_py/tests/modules/backtest/test_engine.py \
        backend_py/scripts/run_backtest.py
git commit -m "✨ Feat: backtest/engine — apply config-driven friction

Engine now accepts BacktestConfig:
- 15% Bitfinex fee (default)
- gap_minutes=30 → cooldown extension
- fill_alpha=5.0 → linear EV-based fill probability
- market_rate_source='candle_close' as Phase 1 FRR proxy

Schema changes:
- BacktestResult: rename monthly_return_pct → net_monthly_return_pct
- BacktestResult: add gross_monthly_return_pct, fill_rate
- max_drawdown_pct now tracked on net equity

Tests:
- test_config.py 9 new tests pass
- test_engine.py: existing constant-rate assertion updated 0.3 → 0.255 (15% fee)
- test_engine.py: 3 new friction isolation tests
- test_schemas.py: updated to new field names

CLI temporarily logs only net (gross/fill_rate enhancement next commit).

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Phase 1 — Task 3: CLI logging enhancement + verification run

**Phase exit criterion:** `scripts/run_backtest.py` logs gross / net / fill_rate / max_drawdown separately. Real run against Neon completes with exit 0; new baseline number captured.

**Files:**
- Modify: `backend_py/scripts/run_backtest.py`

- [ ] **Step 1: Update CLI logging block**

Path: `backend_py/scripts/run_backtest.py`

Find the logging block (around line 78-86):

```python
        logger.info("=" * 60)
        logger.info("✅ Checkpoint 2 backtest result")
        logger.info("  Strategy:           %s", result.strategy_name)
        logger.info("  Symbol:             %s", result.symbol)
        logger.info("  Candles processed:  %d", result.n_candles)
        logger.info("  Trades simulated:   %d", result.n_trades)
        logger.info("  Monthly return:     %s%% (net)", result.net_monthly_return_pct)
        logger.info("  Max drawdown:       %s%%", result.max_drawdown_pct)
        logger.info("=" * 60)
```

Replace with:

```python
        logger.info("=" * 60)
        logger.info("✅ Backtest result (v2 friction-aware engine)")
        logger.info("  Strategy:           %s", result.strategy_name)
        logger.info("  Symbol:             %s", result.symbol)
        logger.info("  Candles processed:  %d", result.n_candles)
        logger.info("  Trades simulated:   %d", result.n_trades)
        logger.info("  Fill rate:          %s", result.fill_rate)
        logger.info("  Gross monthly ret:  %s%% (pre-fee)", result.gross_monthly_return_pct)
        logger.info("  Net monthly ret:    %s%% (after 15%% fee + gap)", result.net_monthly_return_pct)
        logger.info("  Max drawdown:       %s%%", result.max_drawdown_pct)
        logger.info("=" * 60)
```

Also update the heading docstring (top of file) since this is no longer the Checkpoint 2 driver. Find:

```python
"""Day-4/5 Checkpoint 2 driver: load candles from Neon → run baseline strategy
→ print monthly return + drawdown.
```

Replace with:

```python
"""Backtest CLI: load candles from Neon → run strategy → print friction-aware result.

v2 Phase 1: applies BacktestConfig defaults (fee_rate=0.15, gap_minutes=30,
fill_alpha=5.0, market_rate_source='candle_close').
```

- [ ] **Step 2: Run all tests (sanity check no regression)**

```bash
cd backend_py && uv run pytest -v -m "not integration"
```

Expected: 40 tests pass (no change from Task 2).

- [ ] **Step 3: Run script against real Neon**

```bash
cd backend_py && uv run python scripts/run_backtest.py --days-back 30
```

Expected: exit 0; new log includes lines like:

```
  Fill rate:          1.0
  Gross monthly ret:  0.40XX% (pre-fee)
  Net monthly ret:    0.34XX% (after 15% fee + gap)
  Max drawdown:       0%
```

Capture the **net monthly return number** — this is the new baseline. Note it for Task 4.

If exit ≠ 0, triage: most likely DATABASE_URL or Neon connectivity issue (not engine). Check `backend_py/.env`.

- [ ] **Step 4: Run mypy + ruff**

```bash
cd backend_py && uv run mypy src/ && uv run ruff check
```

Expected: no errors.

- [ ] **Step 5: Commit**

```bash
git add backend_py/scripts/run_backtest.py
git commit -m "♻️ Refactor: run_backtest CLI — log gross/net + fill_rate

Friction-aware engine emits more dimensions; CLI now prints them
explicitly so the gap between gross and net is attributable to the
15% fee + gap cost.

Heading updated: this is no longer specifically the Checkpoint 2
driver, just the v2 baseline backtest CLI.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Phase 1 — Task 4: Wiki Lessons Learned (different repo — controller step)

**Phase exit criterion:** Wiki page `~/second-brain/wiki/projects/bfx-funding-bot.md` has one new bullet under Lessons Learned recording the new baseline number.

**This task runs in a different git repository (`~/second-brain/`).** Subagent must `cd` out of the bfx-funding-bot worktree to make this change. Alternatively, controller can do it manually after Task 3 completes.

**Files:**
- Modify: `~/second-brain/wiki/projects/bfx-funding-bot.md`

- [ ] **Step 1: Find the Lessons Learned section**

In `~/second-brain/wiki/projects/bfx-funding-bot.md`, locate `## Lessons Learned`. There are already 4 bullets there.

- [ ] **Step 2: Add a new bullet at the end of Lessons Learned**

Append (using `<NEW_NET_NUMBER>` from Task 3 Step 3):

```markdown
- **v2 friction-aware baseline**: AlwaysFRR post-friction = `<NEW_NET_NUMBER>%/月`（was 0.4028 pre-friction）。差距 = 15% Bitfinex fee + 30min gap cost；spread/fill_prob 對 AlwaysFRR 無效（出價等於市場）
```

- [ ] **Step 3: Commit in the second-brain repo**

```bash
cd ~/second-brain
git add wiki/projects/bfx-funding-bot.md
git commit -m "wiki: bfx-funding-bot — engine v2 friction-aware baseline"
```

---

## Phase 1 verification — exit gate

- [ ] **Step 1: Full unit test suite + lint + type check (in worktree)**

```bash
cd backend_py && uv run pytest -v -m "not integration" && uv run mypy src/ && uv run ruff check
```

Expected: all 40 tests pass; mypy and ruff clean.

- [ ] **Step 2: Real backtest run against Neon**

```bash
cd backend_py && uv run python scripts/run_backtest.py --days-back 30
```

Expected: exit 0; new baseline number observable.

**Phase 1 exit gate:** unit + integration tests pass + new baseline number recorded in second-brain wiki.

---

## Plan completion summary

| Task | Files touched | Commits |
|------|---|---|
| 1 — BacktestConfig + helper | 2 new | 1 |
| 2 — Engine + schema atomic | 5 modified | 1 |
| 3 — CLI logging + verification run | 1 modified | 1 |
| 4 — Wiki Lessons (different repo) | 1 modified | 1 (in second-brain) |

**Estimated time**: 4-6 hours (1 working day).

**What v2 Phase 1 delivers**: A friction-aware baseline number that all v2 future strategies (Phase 3) compare against.

**Next phase**: Phase 2 — funding_stats backfill + multi-symbol/multi-period_agg data layer (own brainstorm cycle).
