# Tier 2 Deployment Safety Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the hand-picked, inert canary MeanReversion config with WFO-winner-selected params produced by a reproducible code pipeline, guarded by a deterministic CI gate that fails when a deployed config collapses to the passive AlwaysFRR baseline.

**Architecture:** One derivation pipeline (`scripts/derive_cells.py`, `--write`/`--check` modes like `uv lock`) plus one fixture-based gate. `--write` pulls Neon once, freezes a candle fixture, derives each MeanReversion cell's params (EDA → grid → fixed-combo rolling-OOS selection), and patches `cells.yaml`/`cells.canary.yaml` params + a `_provenance` block. CI runs `--check` (re-derive from the frozen fixture == committed YAML) and a `gate`-marked pytest (deployed cells must beat AlwaysFRR), both offline. Core principle: validate-exactly-what-you-deploy.

**Tech Stack:** Python 3.13, pydantic v2, SQLAlchemy async, pytest, ruamel.yaml (new, for comment-preserving YAML patching in `--write`), gzipped JSONL fixtures. Spec: `docs/superpowers/specs/2026-05-28-tier2-deployment-safety-design.md`.

**Execution dir:** all `pytest`/`mypy`/`ruff`/scripts run from `backend_py/` (`cd backend_py && uv run ...`).

---

## File Structure

**New source modules** (`backend_py/src/bfx_funding_bot/modules/backtest/`):
- `oos_eval.py` — `evaluate_oos_windows(candles, windows, make_strategy)` → paired `(strat_outcomes, base_outcomes)`. The fixed-param rolling-OOS evaluation shared by the gate, the derivation, and the research script. No DB.
- `deploy_gate.py` — `GateResult` + `evaluate_gate(...)`. The pure pass/fail rule (a)+(b).
- `cell_derivation.py` — `DerivedCell` + `derive_cell_params(candles)` + `select_winner(...)`. EDA → grid → per-combo rolling eval → winner.
- `fixture_io.py` — `freeze_candles`, `load_candles`, `fixture_data_hash`. JSONL.gz candle snapshot I/O + content hash.
- `cell_pipeline.py` — `write_outputs(...)`, `check_against_fixture(...)`, `MR_CELLS`, path constants. The pure/offline pipeline logic (freeze + patch YAML + drift check), importable and unit-tested. (Pattern: logic in `modules/`, the `scripts/` runner is a thin CLI — mirrors `matrix.py` vs `run_phase3b_wfo_matrix.py`.)

**Modified source:**
- `modules/marketfeed/config.py` — `MeanReversionParams`: `ema_alpha` → `ema_span`; add `load_cells_only(path)`.
- `modules/marketfeed/strategy_registry.py` — `build_strategy`: read `ema_span`, drop `_ema_alpha_to_span`.
- `modules/backtest/oos_profitability.py` — add pure `paired_active_returns(strat, base)`.
- `scripts/run_oos_profitability.py` — drop hardcoded `CanaryCell` mirror; read `cells.canary.yaml`; reuse `evaluate_oos_windows`.

**New script:** `scripts/derive_cells.py` — thin CLI over `cell_pipeline`: `--write` (Neon, manual) / `--check` (CI, offline).

**New fixture dir:** `backend_py/fixtures/candles/` — `*.jsonl.gz` per `(symbol, period_agg, timeframe)` series, committed.

**Config files:** `backend_py/configs/cells.yaml`, `backend_py/configs/cells.canary.yaml` — `ema_alpha`→`ema_span`; params patched to winner; `_provenance` added to `cells.yaml`.

**New tests** (`backend_py/tests/modules/backtest/`): `test_oos_eval.py`, `test_deploy_gate.py`, `test_cell_derivation.py`, `test_fixture_io.py`, `test_derive_cells.py`. Plus edits to existing tests referencing `ema_alpha`.

**CI:** `.github/workflows/ci.yml` — exclude `gate` from the unit job; add a `backend_py_gate` job.

---

## Task 1: Schema change `ema_alpha` → `ema_span` + `load_cells_only`

**Files:**
- Modify: `src/bfx_funding_bot/modules/marketfeed/config.py:37-42` (`MeanReversionParams`), add `load_cells_only`
- Modify: `src/bfx_funding_bot/modules/marketfeed/strategy_registry.py:42-59` (`_ema_alpha_to_span`, `build_strategy`)
- Modify: `configs/cells.yaml`, `configs/cells.canary.yaml` (5 MR cells: `ema_alpha: 0.01183` → `ema_span: 168`)
- Test: `tests/modules/marketfeed/test_strategy_registry.py` (or wherever `build_strategy` is tested), `tests/modules/marketfeed/test_config.py`

- [ ] **Step 1: Find existing tests referencing `ema_alpha`**

Run: `cd backend_py && grep -rn "ema_alpha" src tests configs scripts`
Expected: hits in `config.py`, `strategy_registry.py`, both YAMLs, `run_oos_profitability.py`, and any tests. Note each — they all change in this plan (this task handles config/registry/YAML; `run_oos_profitability.py` is Task 8).

- [ ] **Step 2: Write the failing test for `ema_span` schema + build**

In `tests/modules/marketfeed/test_strategy_registry.py` add:

```python
from decimal import Decimal

import pytest
from pydantic import ValidationError

from bfx_funding_bot.modules.marketfeed.config import CellConfig
from bfx_funding_bot.modules.marketfeed.schemas import StrategyName
from bfx_funding_bot.modules.marketfeed.strategy_registry import build_strategy


def test_build_mean_reversion_reads_ema_span():
    cell = CellConfig(
        strategy=StrategyName.MEAN_REVERSION, symbol="fUST", period_agg="a30",
        params={"ema_span": 24, "threshold_sigma": 0.5, "ratio_sigma": 0.9915},
    )
    strat = build_strategy(cell)
    # MeanReversionStrategy stores _ema_span directly; no alpha round-trip.
    assert strat._ema_span == 24  # type: ignore[attr-defined]
    assert strat._threshold_sigma == Decimal("0.5")  # type: ignore[attr-defined]


def test_cell_config_rejects_legacy_ema_alpha():
    with pytest.raises(ValidationError):
        CellConfig(
            strategy=StrategyName.MEAN_REVERSION, symbol="fUST", period_agg="a30",
            params={"ema_alpha": 0.01183, "threshold_sigma": 1.0, "ratio_sigma": 0.99},
        )
```

- [ ] **Step 3: Run to verify failure**

Run: `cd backend_py && uv run pytest tests/modules/marketfeed/test_strategy_registry.py -k "ema_span or legacy_ema_alpha" -q`
Expected: FAIL — `extra="forbid"` still expects `ema_alpha`, so `ema_span` is rejected / `build_strategy` reads `p["ema_alpha"]` → KeyError.

- [ ] **Step 4: Change the schema**

In `config.py` replace `MeanReversionParams`:

```python
class MeanReversionParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    threshold_sigma: float = Field(gt=0)
    ratio_sigma: float = Field(gt=0)
    ema_span: int = Field(ge=1)
```

- [ ] **Step 5: Update `build_strategy` and delete the converter**

In `strategy_registry.py` delete `_ema_alpha_to_span` (lines 42-47) and its docstring references (the module docstring lines 6-8 about alpha→span), and change the MEAN_REVERSION branch of `build_strategy`:

```python
    if cell.strategy == StrategyName.MEAN_REVERSION:
        p = cell.params
        return MeanReversionStrategy(
            ema_span=int(p["ema_span"]),
            threshold_sigma=Decimal(str(p["threshold_sigma"])),
            ratio_sigma=Decimal(str(p["ratio_sigma"])),
        )
```

- [ ] **Step 6: Migrate both YAML files (behavior-preserving)**

In `configs/cells.yaml` and `configs/cells.canary.yaml`, for every `mean_reversion` cell replace `ema_alpha: 0.01183` with `ema_span: 168` inside the `params: {...}` map. (168 == `round(2/0.01183 - 1)`, so behavior is unchanged this task; Task 6 replaces these with the winner.) Update the `cells.yaml` header comment line `MR: ema_alpha=0.01183 (-> ema_span=168)` to `MR: ema_span=168`.

- [ ] **Step 7: Add `load_cells_only` to `config.py`**

Append to `config.py` (a lightweight, env-free cells reader for scripts/gate — `load_config` stays for the daemon):

```python
def load_cells_only(cells_yaml_path: Path) -> list[CellConfig]:
    """Parse just the `cells:` list from a YAML file, no env vars, no daemon config.

    For tooling (derive_cells, run_oos_profitability) and the deploy gate that
    need the deployed cell definitions without the full daemon environment.
    """
    if not cells_yaml_path.exists():
        raise FileNotFoundError(f"cells.yaml not found at {cells_yaml_path}")
    raw = yaml.safe_load(cells_yaml_path.read_text())
    return [CellConfig.model_validate(c) for c in raw.get("cells", [])]
```

- [ ] **Step 8: Run the full gate**

Run: `cd backend_py && uv run pytest -m "not integration" -q && uv run mypy --strict src && uv run ruff check`
Expected: PASS. Fix any other tests that referenced `ema_alpha` by switching them to `ema_span` (use `168` where they previously used `0.01183`).

- [ ] **Step 9: Commit**

```bash
cd backend_py && git add src/bfx_funding_bot/modules/marketfeed/config.py src/bfx_funding_bot/modules/marketfeed/strategy_registry.py configs/cells.yaml configs/cells.canary.yaml tests/
git commit -m "$(cat <<'EOF'
♻️ Refactor: cells store ema_span (drop ema_alpha round-trip)

Behavior-preserving (span=168). Removes a derived-then-rounded skew source
ahead of the Tier 2 param pipeline. Adds load_cells_only for tooling/gate.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 2: `deploy_gate.evaluate_gate` + `paired_active_returns`

**Files:**
- Create: `src/bfx_funding_bot/modules/backtest/deploy_gate.py`
- Modify: `src/bfx_funding_bot/modules/backtest/oos_profitability.py` (add `paired_active_returns`)
- Test: `tests/modules/backtest/test_deploy_gate.py`

- [ ] **Step 1: Write the failing test for the gate rule**

Create `tests/modules/backtest/test_deploy_gate.py`:

```python
from decimal import Decimal

from bfx_funding_bot.modules.backtest.deploy_gate import evaluate_gate


def test_inert_config_fails_distinguishability():
    # active identically 0: IR==0, no month outperforms -> fails (a)
    r = evaluate_gate(
        information_ratio=Decimal("0"), pct_outperform=Decimal("0"),
        mean_active_ci_low=Decimal("0"),
    )
    assert r.passed is False
    assert r.distinguishable is False


def test_worse_than_passive_fails_not_worse():
    # does something (IR<0) but CI low < 0 -> fails (b)
    r = evaluate_gate(
        information_ratio=Decimal("-0.4"), pct_outperform=Decimal("0.2"),
        mean_active_ci_low=Decimal("-0.03"),
    )
    assert r.passed is False
    assert r.distinguishable is True
    assert r.not_worse is False


def test_good_config_passes():
    r = evaluate_gate(
        information_ratio=Decimal("0.5"), pct_outperform=Decimal("0.84"),
        mean_active_ci_low=Decimal("0.01"),
    )
    assert r.passed is True


def test_noisy_but_ok_passes_default_fails_strict():
    # positive point estimate but CI lower bound sits at 0
    common = dict(information_ratio=Decimal("0.1"), pct_outperform=Decimal("0.55"))
    assert evaluate_gate(mean_active_ci_low=Decimal("0"), **common).passed is True
    assert evaluate_gate(mean_active_ci_low=Decimal("0"), strict=True, **common).passed is False
```

- [ ] **Step 2: Run to verify failure**

Run: `cd backend_py && uv run pytest tests/modules/backtest/test_deploy_gate.py -q`
Expected: FAIL — `deploy_gate` module does not exist.

- [ ] **Step 3: Implement `deploy_gate.py`**

Create `src/bfx_funding_bot/modules/backtest/deploy_gate.py`:

```python
"""Deterministic deploy sanity gate for yield/carry cells.

A deployed config must (a) be distinguishable from the passive AlwaysFRR
baseline (it actually acts) and (b) not be worse than passive (mean active
return's bootstrap CI lower bound >= 0). Catches the canary-mr-config-inert
failure mode where a shipped config collapses to passive. See
docs/superpowers/specs/2026-05-28-tier2-deployment-safety-design.md.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class GateResult:
    passed: bool
    distinguishable: bool  # (a): acts differently from passive
    not_worse: bool        # (b): not statistically worse than passive
    reasons: tuple[str, ...]


def evaluate_gate(
    *,
    information_ratio: Decimal,
    pct_outperform: Decimal,
    mean_active_ci_low: Decimal,
    strict: bool = False,
) -> GateResult:
    """Apply the two-part deploy gate.

    (a) distinguishable: information_ratio != 0 AND pct_outperform > 0.
        The inert config has IR == 0 and 0% months outperforming -> fails.
    (b) not_worse: bootstrap 95% CI lower bound of mean active return >= 0
        (strict: > 0). On a ~49-window noisy sample the default floor avoids
        rejecting genuine-but-noisy edges (see spec Risks).
    """
    distinguishable = information_ratio != Decimal("0") and pct_outperform > Decimal("0")
    not_worse = mean_active_ci_low > Decimal("0") if strict else mean_active_ci_low >= Decimal("0")

    reasons: list[str] = []
    if not distinguishable:
        reasons.append(
            f"indistinguishable from passive (IR={information_ratio}, "
            f"pct_outperform={pct_outperform})"
        )
    if not not_worse:
        bound = "> 0" if strict else ">= 0"
        reasons.append(f"mean active CI low {mean_active_ci_low} not {bound}")
    return GateResult(
        passed=distinguishable and not_worse,
        distinguishable=distinguishable,
        not_worse=not_worse,
        reasons=tuple(reasons),
    )
```

- [ ] **Step 4: Run to verify the gate tests pass**

Run: `cd backend_py && uv run pytest tests/modules/backtest/test_deploy_gate.py -q`
Expected: PASS (4 tests).

- [ ] **Step 5: Write the failing test for `paired_active_returns`**

In `tests/modules/backtest/test_oos_profitability.py` add (reuse the existing `_w` helper):

```python
from bfx_funding_bot.modules.backtest.oos_profitability import paired_active_returns


def test_paired_active_returns_aligned():
    strat = [_w(1, "0.6"), _w(2, "0.5")]
    base = [_w(1, "0.5"), _w(2, "0.5")]
    assert paired_active_returns(strat, base) == [Decimal("0.1"), Decimal("0.0")]


def test_paired_active_returns_misaligned_raises():
    with pytest.raises(ValueError):
        paired_active_returns([_w(1, "0.6")], [_w(2, "0.5")])
```

- [ ] **Step 6: Run to verify failure**

Run: `cd backend_py && uv run pytest tests/modules/backtest/test_oos_profitability.py -k paired_active -q`
Expected: FAIL — function not defined.

- [ ] **Step 7: Implement `paired_active_returns`**

In `oos_profitability.py`, just above `active_return_summary`, add:

```python
def paired_active_returns(
    strat: list[WindowOutcome], base: list[WindowOutcome]
) -> list[Decimal]:
    """Per-window (strat - base) net_monthly, aligned 1:1 by month_mts (same order).

    The raw input to bootstrap_ci for the deploy gate's mean-active CI.
    """
    if len(strat) != len(base):
        raise ValueError(f"paired_active_returns: length mismatch {len(strat)} != {len(base)}")
    if not strat:
        raise ValueError("paired_active_returns: no outcomes")
    out: list[Decimal] = []
    for s, b in zip(strat, base, strict=True):
        if s.month_mts != b.month_mts:
            raise ValueError(f"paired_active_returns: misaligned month {s.month_mts} != {b.month_mts}")
        out.append(s.net_monthly - b.net_monthly)
    return out
```

- [ ] **Step 8: Run + gate**

Run: `cd backend_py && uv run pytest tests/modules/backtest/test_oos_profitability.py tests/modules/backtest/test_deploy_gate.py -q && uv run mypy --strict src && uv run ruff check`
Expected: PASS.

- [ ] **Step 9: Commit**

```bash
cd backend_py && git add src/bfx_funding_bot/modules/backtest/deploy_gate.py src/bfx_funding_bot/modules/backtest/oos_profitability.py tests/modules/backtest/test_deploy_gate.py tests/modules/backtest/test_oos_profitability.py
git commit -m "$(cat <<'EOF'
✨ Feat: deploy_gate.evaluate_gate + paired_active_returns

Pure (a)+(b) deploy sanity rule + the per-window active-return series it
consumes. No data dependency yet.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 3: `oos_eval.evaluate_oos_windows` (shared rolling evaluation)

**Files:**
- Create: `src/bfx_funding_bot/modules/backtest/oos_eval.py`
- Test: `tests/modules/backtest/test_oos_eval.py`

- [ ] **Step 1: Write the failing test**

Create `tests/modules/backtest/test_oos_eval.py`:

```python
from decimal import Decimal

from bfx_funding_bot.modules.backtest.oos_eval import evaluate_oos_windows
from bfx_funding_bot.modules.backtest.strategies.always_frr import AlwaysFRRStrategy
from bfx_funding_bot.modules.backtest.wfo import WfoWindow
from bfx_funding_bot.modules.candles.schemas import FundingCandle


def _candles(start_mts: int, n: int, close: str, step_ms: int = 3_600_000):
    return [
        FundingCandle(symbol="fUST", timeframe="1h", period_agg="a30",
                      mts=start_mts + i * step_ms, close=Decimal(close))
        for i in range(n)
    ]


def test_evaluate_oos_windows_pairs_outcomes_per_window():
    # two windows, flat rate; AlwaysFRR for both arms -> identical outcomes,
    # one WindowOutcome per window, aligned by month_mts.
    candles = _candles(0, 1000, "0.0003")
    windows = [
        WfoWindow(train_start_mts=0, train_end_mts=499_999_999,
                  test_start_mts=500_000_000, test_end_mts=900_000_000),
    ]
    strat_out, base_out = evaluate_oos_windows(
        candles, windows, make_strategy=lambda: AlwaysFRRStrategy(period_days=2)
    )
    assert len(strat_out) == len(base_out) == 1
    assert strat_out[0].month_mts == base_out[0].month_mts == 500_000_000
    assert strat_out[0].net_monthly == base_out[0].net_monthly
```

- [ ] **Step 2: Run to verify failure**

Run: `cd backend_py && uv run pytest tests/modules/backtest/test_oos_eval.py -q`
Expected: FAIL — module does not exist.

- [ ] **Step 3: Implement `oos_eval.py`**

Create `src/bfx_funding_bot/modules/backtest/oos_eval.py`:

```python
"""Fixed-param rolling out-of-sample evaluation.

For each WFO window: slice candles to [train_start, test_end] (EMA warmup +
test month), run the strategy and the AlwaysFRR baseline recording only the
test month, and emit one paired WindowOutcome each. Params are FIXED (no
per-window re-fit) -- this is the evaluation the deploy gate, the derivation
sweep, and the OOS research script all share. No DB.
"""
from __future__ import annotations

from collections.abc import Callable

from bfx_funding_bot.modules.backtest.config import BacktestConfig
from bfx_funding_bot.modules.backtest.engine import run_backtest
from bfx_funding_bot.modules.backtest.oos_profitability import WindowOutcome
from bfx_funding_bot.modules.backtest.schemas import BacktestResult
from bfx_funding_bot.modules.backtest.strategies.always_frr import AlwaysFRRStrategy
from bfx_funding_bot.modules.backtest.strategies.base import Strategy
from bfx_funding_bot.modules.backtest.wfo import WfoWindow
from bfx_funding_bot.modules.candles.schemas import FundingCandle

# Deterministic fill model -- matches the OOS research script (run_oos_profitability).
LINEAR_CONFIG = BacktestConfig(fill_model="linear")


def _outcome(result: BacktestResult, month_mts: int) -> WindowOutcome:
    return WindowOutcome(
        month_mts=month_mts,
        net_monthly=result.net_monthly_return_pct,
        n_trades=result.n_trades,
        fill_rate=result.fill_rate,
    )


def evaluate_oos_windows(
    candles: list[FundingCandle],
    windows: list[WfoWindow],
    make_strategy: Callable[[], Strategy],
    *,
    config: BacktestConfig = LINEAR_CONFIG,
    baseline_period_days: int = 2,
) -> tuple[list[WindowOutcome], list[WindowOutcome]]:
    """Run a fixed-param strategy and AlwaysFRR over rolling test months.

    `make_strategy` is called once per window (strategy state is per-window:
    fresh EMA warmed only on that window's slice -> no cross-window leakage).
    Returns (strat_outcomes, base_outcomes), aligned 1:1 by window order.
    """
    strat_outcomes: list[WindowOutcome] = []
    base_outcomes: list[WindowOutcome] = []
    for w in windows:
        sliced = [c for c in candles if w.train_start_mts <= c.mts <= w.test_end_mts]
        rs = run_backtest(sliced, make_strategy(), config, w.test_start_mts, w.test_end_mts)
        rb = run_backtest(
            sliced, AlwaysFRRStrategy(period_days=baseline_period_days),
            config, w.test_start_mts, w.test_end_mts,
        )
        strat_outcomes.append(_outcome(rs, w.test_start_mts))
        base_outcomes.append(_outcome(rb, w.test_start_mts))
    return strat_outcomes, base_outcomes
```

- [ ] **Step 4: Run to verify pass**

Run: `cd backend_py && uv run pytest tests/modules/backtest/test_oos_eval.py -q && uv run mypy --strict src && uv run ruff check`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
cd backend_py && git add src/bfx_funding_bot/modules/backtest/oos_eval.py tests/modules/backtest/test_oos_eval.py
git commit -m "$(cat <<'EOF'
✨ Feat: oos_eval.evaluate_oos_windows — shared fixed-param rolling OOS

Extracts the per-window backtest loop (strategy vs AlwaysFRR) shared by the
gate, the derivation sweep, and the research script.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 4: `cell_derivation` — EDA → grid → winner selection

**Files:**
- Create: `src/bfx_funding_bot/modules/backtest/cell_derivation.py`
- Test: `tests/modules/backtest/test_cell_derivation.py`

- [ ] **Step 1: Write the failing test**

Create `tests/modules/backtest/test_cell_derivation.py`:

```python
from decimal import Decimal

import pytest

from bfx_funding_bot.modules.backtest.cell_derivation import (
    DerivedCell,
    NoDistinguishableComboError,
    select_winner,
)
from bfx_funding_bot.modules.backtest.deploy_gate import GateResult


def _combo(span, ts, sigma, mean_active, ir, pct):
    params = {"ema_span": span, "threshold_sigma": Decimal(ts), "ratio_sigma": Decimal(sigma)}
    return (params, Decimal(mean_active), Decimal(ir), Decimal(pct))


def test_select_winner_picks_max_mean_active_among_distinguishable():
    combos = [
        _combo(168, "1.0", "0.99", "0.0", "0", "0"),     # inert -> filtered
        _combo(24, "0.5", "0.05", "0.07", "0.5", "0.85"),  # best distinguishable
        _combo(24, "1.0", "0.05", "0.03", "0.3", "0.7"),
    ]
    winner = select_winner(combos)
    assert winner["ema_span"] == 24
    assert winner["threshold_sigma"] == Decimal("0.5")


def test_select_winner_stable_tie_break_by_param_tuple():
    combos = [
        _combo(168, "1.5", "0.05", "0.05", "0.4", "0.7"),
        _combo(24, "0.5", "0.05", "0.05", "0.4", "0.7"),  # equal mean_active
    ]
    # tie -> smallest (ema_span, threshold_sigma) wins, deterministically
    assert select_winner(combos)["ema_span"] == 24


def test_select_winner_all_inert_raises():
    combos = [_combo(24, "0.5", "0.05", "0.0", "0", "0")]
    with pytest.raises(NoDistinguishableComboError):
        select_winner(combos)
```

- [ ] **Step 2: Run to verify failure**

Run: `cd backend_py && uv run pytest tests/modules/backtest/test_cell_derivation.py -k select_winner -q`
Expected: FAIL — module does not exist.

- [ ] **Step 3: Implement `cell_derivation.py`**

Create `src/bfx_funding_bot/modules/backtest/cell_derivation.py`:

```python
"""Reproducible per-cell MeanReversion parameter derivation.

EDA (close/EMA sigma on the train portion) -> the existing param grid ->
fixed-combo rolling-OOS evaluation per combo -> select the combo that beats
the passive baseline most, among those that pass the deploy gate's
distinguishability filter. Replaces the hand-picked middle-of-grid config.
Tier 3 (deferred): plateau/robustness selection -- this picks the single best
point. See docs/superpowers/specs/2026-05-28-tier2-deployment-safety-design.md.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from bfx_funding_bot.modules.backtest.eda import close_over_ema_sigma
from bfx_funding_bot.modules.backtest.oos_eval import evaluate_oos_windows
from bfx_funding_bot.modules.backtest.oos_profitability import active_return_summary
from bfx_funding_bot.modules.backtest.split import compute_train_end_mts
from bfx_funding_bot.modules.backtest.strategies.mean_reversion import MeanReversionStrategy
from bfx_funding_bot.modules.backtest.wfo import compute_wfo_windows
from bfx_funding_bot.modules.candles.schemas import FundingCandle

# A scored combo: (params, mean_active, information_ratio, pct_outperform)
ScoredCombo = tuple[dict[str, Any], Decimal, Decimal, Decimal]


class NoDistinguishableComboError(Exception):
    """No grid combo acts differently from passive -- MeanReversion has no edge
    on this cell. Caller (derive_cells --write) must fail loudly, not ship inert."""


@dataclass(frozen=True)
class DerivedCell:
    symbol: str
    period_agg: str
    timeframe: str
    ema_span: int
    threshold_sigma: Decimal
    ratio_sigma: Decimal
    mean_active: Decimal       # winner's mean active return (%/mo)
    information_ratio: Decimal
    pct_outperform: Decimal
    n_windows: int


def select_winner(scored: list[ScoredCombo]) -> dict[str, Any]:
    """Pick the params with max mean_active among distinguishable combos
    (IR != 0 and pct_outperform > 0). Stable tie-break: smallest
    (ema_span, threshold_sigma). Raises if none are distinguishable.
    """
    eligible = [
        (params, ma) for (params, ma, ir, pct) in scored
        if ir != Decimal("0") and pct > Decimal("0")
    ]
    if not eligible:
        raise NoDistinguishableComboError(
            "no grid combo is distinguishable from passive AlwaysFRR"
        )
    return max(
        eligible,
        key=lambda pm: (pm[1], -int(pm[0]["ema_span"]), -pm[0]["threshold_sigma"]),
    )[0]


def derive_cell_params(candles: list[FundingCandle]) -> DerivedCell:
    """Derive the deployed MeanReversion params for one cell from its candles.

    Deterministic given `candles`. ratio_sigma per ema_span is computed from EDA
    on the train portion only (no snooping); each grid combo is then evaluated
    fixed over all rolling windows.
    """
    if not candles:
        raise ValueError("derive_cell_params: empty candles")
    symbol, period_agg, timeframe = candles[0].symbol, candles[0].period_agg, candles[0].timeframe

    train_end_mts = compute_train_end_mts(candles)
    train = [c for c in candles if c.mts <= train_end_mts]
    eda = {
        "close_over_ema_sigma_24": close_over_ema_sigma(train, ema_span=24),
        "close_over_ema_sigma_168": close_over_ema_sigma(train, ema_span=168),
    }
    grid = MeanReversionStrategy.param_grid_for_cell(
        symbol=symbol, period_agg=period_agg, eda=eda
    )
    windows = compute_wfo_windows(candles)
    if not windows:
        raise ValueError(f"derive_cell_params: no WFO windows for {symbol}_{period_agg}")

    scored: list[ScoredCombo] = []
    for params in grid:
        strat_out, base_out = evaluate_oos_windows(
            candles, windows,
            make_strategy=lambda p=params: MeanReversionStrategy(
                ema_span=int(p["ema_span"]),
                threshold_sigma=p["threshold_sigma"],
                ratio_sigma=p["ratio_sigma"],
            ),
        )
        active = active_return_summary(strat_out, base_out)
        scored.append((params, active.mean_active, active.information_ratio,
                       active.pct_months_outperform))

    winner = select_winner(scored)
    w_active = next(
        (ma, ir, pct) for (p, ma, ir, pct) in scored if p == winner
    )
    return DerivedCell(
        symbol=symbol, period_agg=period_agg, timeframe=timeframe,
        ema_span=int(winner["ema_span"]),
        threshold_sigma=winner["threshold_sigma"],
        ratio_sigma=winner["ratio_sigma"],
        mean_active=w_active[0], information_ratio=w_active[1],
        pct_outperform=w_active[2], n_windows=len(windows),
    )
```

- [ ] **Step 4: Run `select_winner` tests**

Run: `cd backend_py && uv run pytest tests/modules/backtest/test_cell_derivation.py -k select_winner -q`
Expected: PASS (3 tests).

- [ ] **Step 5: Write a derivation integration-ish test on synthetic candles**

Add to `tests/modules/backtest/test_cell_derivation.py` a deterministic end-to-end test using a synthetic series long enough to yield WFO windows. Use a regime where a tighter band (lower threshold) clearly acts (a square-wave rate that periodically dips, so a selective strategy pauses on dips):

```python
from datetime import UTC, datetime

from bfx_funding_bot.modules.backtest.cell_derivation import derive_cell_params
from bfx_funding_bot.modules.candles.schemas import FundingCandle


def _synthetic_series():
    # 8 months of hourly candles; rate dips hard every 50h then recovers.
    start = int(datetime(2022, 1, 1, tzinfo=UTC).timestamp() * 1000)
    out = []
    for i in range(24 * 30 * 8):
        base = 0.0004
        close = base * (0.2 if (i % 50) < 3 else 1.0)  # periodic deep dips
        out.append(FundingCandle(
            symbol="fUST", timeframe="1h", period_agg="a30",
            mts=start + i * 3_600_000, close=str(close),
        ))
    return out


def test_derive_cell_params_is_deterministic_and_distinguishable():
    candles = _synthetic_series()
    d1 = derive_cell_params(candles)
    d2 = derive_cell_params(candles)
    assert (d1.ema_span, d1.threshold_sigma, d1.ratio_sigma) == \
           (d2.ema_span, d2.threshold_sigma, d2.ratio_sigma)
    assert d1.information_ratio != 0  # selected combo actually acts
```

- [ ] **Step 6: Run, verify pass, gate**

Run: `cd backend_py && uv run pytest tests/modules/backtest/test_cell_derivation.py -q && uv run mypy --strict src && uv run ruff check`
Expected: PASS. (If the synthetic series produces no distinguishable combo, adjust the dip depth/frequency until a selective combo measurably beats passive — the test documents the expected regime.)

- [ ] **Step 7: Commit**

```bash
cd backend_py && git add src/bfx_funding_bot/modules/backtest/cell_derivation.py tests/modules/backtest/test_cell_derivation.py
git commit -m "$(cat <<'EOF'
✨ Feat: cell_derivation — WFO winner selection (replaces middle-of-grid)

EDA -> grid -> fixed-combo rolling OOS -> max mean-active among gate-passing
combos. Raises NoDistinguishableComboError rather than ship an inert config.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 5: `fixture_io` — frozen candle snapshot I/O + content hash

**Files:**
- Create: `src/bfx_funding_bot/modules/backtest/fixture_io.py`
- Test: `tests/modules/backtest/test_fixture_io.py`

- [ ] **Step 1: Write the failing test**

Create `tests/modules/backtest/test_fixture_io.py`:

```python
from decimal import Decimal

from bfx_funding_bot.modules.backtest.fixture_io import (
    fixture_data_hash,
    freeze_candles,
    load_candles,
)
from bfx_funding_bot.modules.candles.schemas import FundingCandle


def _series(close="0.0003"):
    return [
        FundingCandle(symbol="fUST", timeframe="1h", period_agg="a30",
                      mts=i * 3_600_000, close=Decimal(close))
        for i in range(5)
    ]


def test_freeze_load_round_trip_preserves_close_exactly(tmp_path):
    series = {("fUST", "a30", "1h"): _series("0.00031234567890123")}
    freeze_candles(series, tmp_path)
    loaded = load_candles(tmp_path / "fUST_a30_1h.jsonl.gz")
    assert len(loaded) == 5
    assert loaded[0].close == Decimal("0.00031234567890123")
    assert loaded[0].symbol == "fUST" and loaded[0].period_agg == "a30"
    assert [c.mts for c in loaded] == [i * 3_600_000 for i in range(5)]


def test_data_hash_deterministic_and_sensitive(tmp_path):
    freeze_candles({("fUST", "a30", "1h"): _series("0.0003")}, tmp_path)
    h1 = fixture_data_hash(tmp_path)
    freeze_candles({("fUST", "a30", "1h"): _series("0.0003")}, tmp_path)
    assert fixture_data_hash(tmp_path) == h1  # deterministic
    freeze_candles({("fUST", "a30", "1h"): _series("0.0009")}, tmp_path)
    assert fixture_data_hash(tmp_path) != h1  # content-sensitive
```

- [ ] **Step 2: Run to verify failure**

Run: `cd backend_py && uv run pytest tests/modules/backtest/test_fixture_io.py -q`
Expected: FAIL — module does not exist.

- [ ] **Step 3: Implement `fixture_io.py`**

Create `src/bfx_funding_bot/modules/backtest/fixture_io.py`:

```python
"""Frozen candle fixtures for the deploy gate + derivation --check.

One gzipped JSONL file per (symbol, period_agg, timeframe) series. Only the
fields the backtest reads are stored (mts, close as a string for exact Decimal
round-trip). A content hash over all files pins the dataset so derive_cells
--check can prove the committed YAML was derived from this exact data.
"""
from __future__ import annotations

import gzip
import hashlib
import json
from pathlib import Path

from bfx_funding_bot.modules.candles.schemas import FundingCandle

_SUFFIX = ".jsonl.gz"


def _series_path(directory: Path, symbol: str, period_agg: str, timeframe: str) -> Path:
    return directory / f"{symbol}_{period_agg}_{timeframe}{_SUFFIX}"


def freeze_candles(
    series: dict[tuple[str, str, str], list[FundingCandle]],
    directory: Path,
) -> None:
    """Write each (symbol, period_agg, timeframe) -> candles to a JSONL.gz file.

    Deterministic: candles sorted by mts, fixed JSON key order, mtime-free gzip.
    """
    directory.mkdir(parents=True, exist_ok=True)
    for (symbol, period_agg, timeframe), candles in series.items():
        path = _series_path(directory, symbol, period_agg, timeframe)
        rows = [
            json.dumps({"mts": c.mts, "close": str(c.close)}, sort_keys=True)
            for c in sorted(candles, key=lambda c: c.mts)
        ]
        payload = ("\n".join(rows) + "\n").encode("utf-8")
        # mtime=0 -> byte-identical archive for identical content.
        with gzip.GzipFile(filename=str(path), mode="wb", mtime=0) as fh:
            fh.write(payload)


def load_candles(path: Path) -> list[FundingCandle]:
    """Load one frozen series. (symbol, period_agg, timeframe) come from the filename."""
    stem = path.name[: -len(_SUFFIX)] if path.name.endswith(_SUFFIX) else path.stem
    symbol, period_agg, timeframe = stem.split("_")
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        out: list[FundingCandle] = []
        for line in fh:
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            out.append(FundingCandle(
                symbol=symbol, timeframe=timeframe, period_agg=period_agg,
                mts=int(rec["mts"]), close=rec["close"],
            ))
    return out


def fixture_data_hash(directory: Path) -> str:
    """SHA-256 over all *.jsonl.gz raw bytes, ordered by filename. Hex digest."""
    h = hashlib.sha256()
    for path in sorted(directory.glob(f"*{_SUFFIX}")):
        h.update(path.name.encode("utf-8"))
        h.update(path.read_bytes())
    return h.hexdigest()
```

- [ ] **Step 4: Run, verify pass, gate**

Run: `cd backend_py && uv run pytest tests/modules/backtest/test_fixture_io.py -q && uv run mypy --strict src && uv run ruff check`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
cd backend_py && git add src/bfx_funding_bot/modules/backtest/fixture_io.py tests/modules/backtest/test_fixture_io.py
git commit -m "$(cat <<'EOF'
✨ Feat: fixture_io — frozen candle snapshot (jsonl.gz) + content hash

Deterministic, dependency-free fixture I/O so the gate and derivation --check
run offline against a pinned dataset.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 6: `scripts/derive_cells.py` — `--write` (Neon) / `--check` (offline)

**Files:**
- Create: `src/bfx_funding_bot/modules/backtest/cell_pipeline.py`, `scripts/derive_cells.py`
- Modify: `pyproject.toml` (add `ruamel.yaml` dependency)
- Modify (by `--write` run): `configs/cells.yaml`, `configs/cells.canary.yaml`, `fixtures/candles/*.jsonl.gz`
- Test: `tests/modules/backtest/test_derive_cells.py`

- [ ] **Step 1: Add `ruamel.yaml` dependency**

In `pyproject.toml` `dependencies`, add `"ruamel.yaml>=0.18"`. Run: `cd backend_py && uv sync --all-groups`
Expected: resolves and installs ruamel.yaml.

- [ ] **Step 2: Write failing `--check` tests (offline, synthetic fixture+YAML)**

Create `tests/modules/backtest/test_derive_cells.py`. These drive the pure pipeline logic (`write_outputs` + `check_against_fixture`) from the `cell_pipeline` module without Neon — build a tiny fixture + a YAML whose params match the derivation, then assert pass / mutation-fail. Self-contained synthetic series (no cross-test import):

```python
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from bfx_funding_bot.modules.backtest.cell_derivation import derive_cell_params
from bfx_funding_bot.modules.backtest.cell_pipeline import (
    check_against_fixture,
    write_outputs,
)
from bfx_funding_bot.modules.backtest.fixture_io import freeze_candles
from bfx_funding_bot.modules.candles.schemas import FundingCandle


def _synthetic_series():
    # 8 months of hourly candles; rate dips hard every 50h then recovers
    # (same regime as test_cell_derivation -> a selective combo beats passive).
    start = int(datetime(2022, 1, 1, tzinfo=UTC).timestamp() * 1000)
    return [
        FundingCandle(
            symbol="fUST", timeframe="1h", period_agg="a30",
            mts=start + i * 3_600_000,
            close=str(0.0004 * (0.2 if (i % 50) < 3 else 1.0)),
        )
        for i in range(24 * 30 * 8)
    ]


def _setup(tmp_path: Path):
    fixtures = tmp_path / "fixtures"
    candles = _synthetic_series()
    series = {("mean_reversion", "fUST", "a30"): candles}  # keyed by CellKey
    derived = {("mean_reversion", "fUST", "a30"): derive_cell_params(candles)}
    cells_yaml = tmp_path / "cells.yaml"
    cells_yaml.write_text(
        "cells:\n"
        "  - strategy: mean_reversion\n"
        "    symbol: fUST\n"
        "    period_agg: a30\n"
        "    timeframe: 1h\n"
        "    params: {threshold_sigma: 1.0, ratio_sigma: 0.5, ema_span: 168}\n"
        "    reference_amount_usdt: 150.0\n"
    )
    write_outputs(series, derived, fixtures, [cells_yaml], canary_path=None)
    return fixtures, cells_yaml


def test_check_passes_on_freshly_written(tmp_path):
    fixtures, cells_yaml = _setup(tmp_path)
    assert check_against_fixture(fixtures, [cells_yaml], canary_path=None) == []


def test_check_fails_on_param_drift(tmp_path):
    fixtures, cells_yaml = _setup(tmp_path)
    text = cells_yaml.read_text().replace("threshold_sigma:", "threshold_sigma: 9.9 #")
    cells_yaml.write_text(text)
    problems = check_against_fixture(fixtures, [cells_yaml], canary_path=None)
    assert any("threshold_sigma" in p for p in problems)


def test_check_fails_on_fixture_hash_mismatch(tmp_path):
    fixtures, cells_yaml = _setup(tmp_path)
    # mutate the frozen fixture without updating _provenance.data_hash
    freeze_candles({("fUST", "a30", "1h"): [
        FundingCandle(symbol="fUST", timeframe="1h", period_agg="a30",
                      mts=i * 3_600_000, close=Decimal("0.0009")) for i in range(5)
    ]}, fixtures)
    problems = check_against_fixture(fixtures, [cells_yaml], canary_path=None)
    assert any("data_hash" in p for p in problems)
```

- [ ] **Step 3: Run to verify failure**

Run: `cd backend_py && uv run pytest tests/modules/backtest/test_derive_cells.py -q`
Expected: FAIL — `bfx_funding_bot.modules.backtest.cell_pipeline` does not exist.

- [ ] **Step 4: Implement the `cell_pipeline` module**

Create `src/bfx_funding_bot/modules/backtest/cell_pipeline.py` (pure/offline pipeline logic — importable and unit-tested; the `scripts/` runner in Step 5 is a thin CLI over it):

```python
"""Offline pipeline logic for derive_cells: freeze fixtures, patch YAML params
+ _provenance, and the drift check. No network. The Neon pull + CLI live in
scripts/derive_cells.py. See docs/superpowers/specs/2026-05-28-tier2-deployment-safety-design.md.
"""
from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from bfx_funding_bot.modules.backtest.cell_derivation import DerivedCell, derive_cell_params
from bfx_funding_bot.modules.backtest.fixture_io import (
    fixture_data_hash,
    freeze_candles,
    load_candles,
)
from bfx_funding_bot.modules.candles.schemas import FundingCandle
from bfx_funding_bot.modules.marketfeed.config import load_cells_only

CONFIGS = Path("configs")
CELLS_YAML = CONFIGS / "cells.yaml"
CANARY_YAML = CONFIGS / "cells.canary.yaml"
FIXTURES = Path("fixtures/candles")

# MeanReversion cells to derive (RatePercentile is static, not EDA-derived).
MR_CELLS = [("fUST", "a30", "1h"), ("fUST", "p2", "1h"),
            ("fUSD", "a30", "1h"), ("fUSD", "p2", "1h"), ("fUSD", "p30", "1h")]

CellKey = tuple[str, str, str]  # (strategy, symbol, period_agg)


def _mr_cells_in(path: Path) -> dict[CellKey, dict[str, Any]]:
    """Map (strategy, symbol, period_agg) -> params dict for MR cells in a YAML."""
    out: dict[CellKey, dict[str, Any]] = {}
    for c in load_cells_only(path):
        if c.strategy.value == "mean_reversion":
            out[(c.strategy.value, c.symbol, c.period_agg)] = dict(c.params)
    return out


def write_outputs(
    series: dict[CellKey, list[FundingCandle]],
    derived: dict[CellKey, DerivedCell],
    fixtures_dir: Path,
    yaml_paths: list[Path],
    *,
    canary_path: Path | None,
) -> None:
    """Freeze fixtures and patch params + _provenance into each YAML (ruamel
    round-trip preserves comments). Deterministic; only touches disk."""
    from ruamel.yaml import YAML

    # series keyed by (symbol, period_agg, timeframe) for fixture_io
    freeze_candles(
        {(s, p, "1h"): cs for (_strat, s, p), cs in series.items()}, fixtures_dir
    )
    data_hash = fixture_data_hash(fixtures_dir)

    ruamel = YAML()
    ruamel.preserve_quotes = True
    prov_cells: dict[str, dict[str, Any]] = {}
    for (strat, _sym, _pa), d in derived.items():
        prov_cells[f"{strat}/{d.symbol}/{d.period_agg}"] = {
            "ema_span": d.ema_span,
            "threshold_sigma": float(d.threshold_sigma),
            "ratio_sigma": float(d.ratio_sigma),
            "mean_active": float(d.mean_active),
            "information_ratio": float(d.information_ratio),
            "pct_outperform": float(d.pct_outperform),
            "n_windows": d.n_windows,
        }
    provenance = {
        "data_hash": data_hash,
        "derived_at": datetime.now(UTC).isoformat(),
        "fixture_window": {"start": "2022-01-01", "end": datetime.now(UTC).date().isoformat()},
        "cells": prov_cells,
    }

    for path in yaml_paths:
        doc = ruamel.load(path.read_text())
        for cell in doc.get("cells", []):
            if cell.get("strategy") != "mean_reversion":
                continue
            d = derived.get(("mean_reversion", cell["symbol"], cell["period_agg"]))
            if d is None:
                continue
            cell["params"]["ema_span"] = d.ema_span
            cell["params"]["threshold_sigma"] = float(d.threshold_sigma)
            cell["params"]["ratio_sigma"] = float(d.ratio_sigma)
        # _provenance only in the full cells.yaml, not the canary subset.
        if canary_path is None or path != canary_path:
            doc["_provenance"] = provenance
        with path.open("w") as fh:
            ruamel.dump(doc, fh)


def check_against_fixture(
    fixtures_dir: Path, yaml_paths: list[Path], *, canary_path: Path | None
) -> list[str]:
    """Offline drift check. Returns a list of human-readable problems ([] = OK)."""
    problems: list[str] = []

    # 1. Re-derive from the frozen fixtures (only MR cells whose fixture exists).
    derived: dict[CellKey, DerivedCell] = {}
    for symbol, period_agg, timeframe in MR_CELLS:
        fpath = fixtures_dir / f"{symbol}_{period_agg}_{timeframe}.jsonl.gz"
        if not fpath.exists():
            continue
        derived[("mean_reversion", symbol, period_agg)] = derive_cell_params(load_candles(fpath))

    # 2. Committed params must equal the re-derivation (the main file).
    main = next((p for p in yaml_paths if canary_path is None or p != canary_path), yaml_paths[0])
    committed = _mr_cells_in(main)
    for key, d in derived.items():
        params = committed.get(key)
        if params is None:
            continue  # cell not deployed in this file; fine
        for field, want in (("ema_span", d.ema_span),
                            ("threshold_sigma", float(d.threshold_sigma)),
                            ("ratio_sigma", float(d.ratio_sigma))):
            got = params.get(field)
            if got is None or abs(float(got) - float(want)) > 1e-9:
                problems.append(
                    f"{key[1]}_{key[2]} {field}: committed {got!r} != derived {want!r}"
                )

    # 3. _provenance.data_hash must match the on-disk fixtures.
    raw = yaml.safe_load(main.read_text())
    prov = raw.get("_provenance", {})
    on_disk = fixture_data_hash(fixtures_dir)
    if prov.get("data_hash") != on_disk:
        problems.append(
            f"_provenance.data_hash {prov.get('data_hash')!r} != on-disk fixture hash {on_disk!r}"
        )

    # 4. Canary params must match the main file for shared MR cells.
    if canary_path is not None and canary_path.exists():
        canary = _mr_cells_in(canary_path)
        for key, cparams in canary.items():
            mparams = committed.get(key)
            if mparams is None:
                problems.append(f"canary cell {key} not in {main.name}")
                continue
            for field in ("ema_span", "threshold_sigma", "ratio_sigma"):
                if abs(float(cparams.get(field, 0)) - float(mparams.get(field, 0))) > 1e-9:
                    problems.append(f"canary {key[1]}_{key[2]} {field} != {main.name}")
    return problems
```

- [ ] **Step 5: Run the `--check` unit tests + types**

Run: `cd backend_py && uv run pytest tests/modules/backtest/test_derive_cells.py -q && uv run mypy --strict src && uv run ruff check`
Expected: PASS (3 tests).

- [ ] **Step 6: Implement the thin CLI `scripts/derive_cells.py`**

Create `scripts/derive_cells.py` (`--write` pulls Neon then calls `write_outputs`; `--check` calls `check_against_fixture`):

```python
"""Reproducible MeanReversion param derivation pipeline (Tier 2) — thin CLI.

--write  (manual, needs Neon): pull candles -> cell_pipeline.write_outputs
         (freeze fixtures + patch params/_provenance into the YAML files).
--check  (CI, offline): cell_pipeline.check_against_fixture — re-derive from the
         committed fixtures and assert the committed YAML matches. Non-zero on drift.

Usage:
    cd backend_py
    uv run python scripts/derive_cells.py --write   # operator, before deploy
    uv run python scripts/derive_cells.py --check    # CI / pre-commit
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from datetime import UTC, datetime

from bfx_funding_bot.modules.backtest.cell_derivation import DerivedCell, derive_cell_params
from bfx_funding_bot.modules.backtest.cell_pipeline import (
    CANARY_YAML,
    CELLS_YAML,
    FIXTURES,
    MR_CELLS,
    CellKey,
    check_against_fixture,
    write_outputs,
)
from bfx_funding_bot.modules.candles.schemas import FundingCandle

logger = logging.getLogger("derive_cells")

START_MTS = int(datetime(2022, 1, 1, tzinfo=UTC).timestamp() * 1000)


async def _write_main() -> int:
    from bfx_funding_bot.core.db import make_engine, make_session_factory, session_scope
    from bfx_funding_bot.core.settings import Settings
    from bfx_funding_bot.modules.candles.repository import get_candles_in_range

    settings = Settings()
    engine = make_engine(settings)
    session_factory = make_session_factory(engine)
    end_mts = int(datetime.now(UTC).timestamp() * 1000)
    series: dict[CellKey, list[FundingCandle]] = {}
    derived: dict[CellKey, DerivedCell] = {}
    try:
        for symbol, period_agg, _timeframe in MR_CELLS:
            async with session_scope(session_factory) as session:
                candles = await get_candles_in_range(
                    session, symbol=symbol, timeframe="1h",
                    period_agg=period_agg, start_mts=START_MTS, end_mts=end_mts,
                )
            if not candles:
                logger.error("no candles for %s_%s; run backfill", symbol, period_agg)
                return 2
            key: CellKey = ("mean_reversion", symbol, period_agg)
            series[key] = candles
            d = derive_cell_params(candles)
            derived[key] = d
            logger.info("derived %s_%s: ema_span=%d thr=%s ratio=%s mean_active=%s IR=%s",
                        symbol, period_agg, d.ema_span, d.threshold_sigma,
                        d.ratio_sigma, d.mean_active, d.information_ratio)
    finally:
        await engine.dispose()

    write_outputs(series, derived, FIXTURES, [CELLS_YAML, CANARY_YAML], canary_path=CANARY_YAML)
    logger.info("wrote fixtures + patched %s, %s", CELLS_YAML, CANARY_YAML)
    return 0


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description=__doc__)
    g = parser.add_mutually_exclusive_group(required=True)
    g.add_argument("--write", action="store_true", help="pull Neon, freeze, derive, patch YAML")
    g.add_argument("--check", action="store_true", help="offline: committed YAML == re-derive(fixture)")
    args = parser.parse_args()

    if args.write:
        sys.exit(asyncio.run(_write_main()))
    problems = check_against_fixture(FIXTURES, [CELLS_YAML, CANARY_YAML], canary_path=CANARY_YAML)
    if problems:
        for p in problems:
            logger.error("DRIFT: %s", p)
        sys.exit(1)
    logger.info("derive_cells --check: OK")
    sys.exit(0)


if __name__ == "__main__":
    main()
```

- [ ] **Step 7: Run the CLI types + a `--check` smoke against the empty fixture dir**

Run: `cd backend_py && uv run mypy --strict src scripts/derive_cells.py && uv run ruff check`
Expected: PASS. (`--check` against real fixtures comes after the `--write` run; the module logic is already covered by Step 5's unit tests.)

- [ ] **Step 8: Commit the pipeline code (before the Neon run)**

```bash
cd backend_py && git add src/bfx_funding_bot/modules/backtest/cell_pipeline.py scripts/derive_cells.py tests/modules/backtest/test_derive_cells.py pyproject.toml uv.lock
git commit -m "$(cat <<'EOF'
✨ Feat: cell_pipeline + derive_cells.py --write/--check

Reproducible MeanReversion derivation (uv lock-style): logic in cell_pipeline
(offline, unit-tested), thin CLI in scripts. --write needs Neon (run next).

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

- [ ] **Step 9: Run `--write` against Neon (MANUAL — needs DATABASE_URL)**

Ensure `.env` has a fresh Neon credential (`ln -sf ../.env backend_py/.env` if the symlink is missing; refresh via Neon MCP `get_connection_string` if stale).
Run: `cd backend_py && uv run python scripts/derive_cells.py --write`
Expected: logs `derived fUST_a30: ema_span=... thr=... mean_active=... IR=...` for all 5 cells; writes `fixtures/candles/*.jsonl.gz` and patches both YAMLs.

- [ ] **Step 10: INSPECT the selected winner (human checkpoint)**

Run: `cd backend_py && git diff configs/cells.canary.yaml configs/cells.yaml && ls -la fixtures/candles/`
Expected: confirm the fUST winners (spec evidence: `ema_span=24, threshold_sigma=0.5`); confirm fUSD winners are sane (distinguishable, plausible). **STOP and report the derived params + mean_active/IR per cell to the operator before continuing.** If a cell raised `NoDistinguishableComboError`, that is a finding (MeanReversion has no edge there) — surface it; do not force-ship.

- [ ] **Step 11: Verify the fixture size is git-acceptable**

Run: `cd backend_py && du -sh fixtures/candles/ && du -h fixtures/candles/*`
Expected: total in the low MB. If unexpectedly large (>~20MB), switch tracking to git-lfs for `*.jsonl.gz` (per spec Risk 1) before committing.

- [ ] **Step 12: Confirm `--check` passes on the real artifacts, then commit**

Run: `cd backend_py && uv run python scripts/derive_cells.py --check`
Expected: `derive_cells --check: OK` (exit 0).

```bash
cd backend_py && git add configs/cells.yaml configs/cells.canary.yaml fixtures/candles/
git commit -m "$(cat <<'EOF'
🔧 Chore: derive canary MR params via pipeline + freeze fixture

Replaces hand-picked middle-of-grid (span=168/thr=1.0, inert) with the WFO
winner. cells.yaml carries _provenance; fixture pins the dataset.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 7: Sanity gate test (real fixture) + old-config regression + marker

**Files:**
- Create: `tests/modules/backtest/test_deploy_gate_e2e.py` (marked `gate`)
- Modify: `pyproject.toml` (add `gate` marker)
- Test runs against committed `fixtures/candles/` + `configs/cells.canary.yaml`

- [ ] **Step 1: Add the `gate` pytest marker**

In `pyproject.toml` `[tool.pytest.ini_options] markers`, add:
`"gate: deployment sanity gate; offline but slow (reads the committed candle fixture); runs as its own CI job",`

- [ ] **Step 2: Write the gate end-to-end test (will pass on the new config)**

Create `tests/modules/backtest/test_deploy_gate_e2e.py`:

```python
"""Deploy sanity gate over the committed fixture + deployed canary config.

Marked `gate`: offline but slower than unit tests (rolling backtest over ~49
windows per cell). Run as its own CI job. Asserts the deployed config beats
passive AND that the pre-fix inert config (span=168/thr=1.0) would fail.
"""
from decimal import Decimal
from pathlib import Path

import pytest

from bfx_funding_bot.modules.backtest.deploy_gate import evaluate_gate
from bfx_funding_bot.modules.backtest.fixture_io import load_candles
from bfx_funding_bot.modules.backtest.oos_eval import evaluate_oos_windows
from bfx_funding_bot.modules.backtest.oos_profitability import (
    active_return_summary,
    paired_active_returns,
)
from bfx_funding_bot.modules.backtest.strategies.mean_reversion import MeanReversionStrategy
from bfx_funding_bot.modules.backtest.wfo import compute_wfo_windows
from bfx_funding_bot.modules.marketfeed.config import load_cells_only

pytestmark = pytest.mark.gate

ROOT = Path(__file__).resolve().parents[3]  # backend_py/ (test is at backend_py/tests/modules/backtest/)
FIXTURES = ROOT / "fixtures" / "candles"
CANARY = ROOT / "configs" / "cells.canary.yaml"


def _gate_for(make_strategy, candles):
    windows = compute_wfo_windows(candles)
    strat_out, base_out = evaluate_oos_windows(candles, windows, make_strategy=make_strategy)
    active = active_return_summary(strat_out, base_out)
    actives = paired_active_returns(strat_out, base_out)
    ci_low, _ = bootstrap_ci_mean(actives)
    return evaluate_gate(
        information_ratio=active.information_ratio,
        pct_outperform=active.pct_months_outperform,
        mean_active_ci_low=ci_low,
    )


def bootstrap_ci_mean(values):
    from bfx_funding_bot.modules.backtest.oos_profitability import bootstrap_ci
    return bootstrap_ci(values, lambda vs: sum(vs, Decimal("0")) / Decimal(len(vs)), seed=20260528)


@pytest.mark.parametrize("cell", load_cells_only(CANARY))
def test_deployed_canary_cell_beats_passive(cell):
    candles = load_candles(FIXTURES / f"{cell.symbol}_{cell.period_agg}_{cell.timeframe}.jsonl.gz")
    p = cell.params
    result = _gate_for(
        lambda: MeanReversionStrategy(
            ema_span=int(p["ema_span"]),
            threshold_sigma=Decimal(str(p["threshold_sigma"])),
            ratio_sigma=Decimal(str(p["ratio_sigma"])),
        ),
        candles,
    )
    assert result.passed, f"{cell.pair_id} failed deploy gate: {result.reasons}"


def test_old_inert_config_would_fail_gate():
    # The pre-fix hand-picked params for fUST_a30: span=168, thr=1.0, ratio=0.9915.
    candles = load_candles(FIXTURES / "fUST_a30_1h.jsonl.gz")
    result = _gate_for(
        lambda: MeanReversionStrategy(
            ema_span=168, threshold_sigma=Decimal("1.0"), ratio_sigma=Decimal("0.9915")
        ),
        candles,
    )
    assert result.passed is False
    assert result.distinguishable is False  # collapses to passive
```

- [ ] **Step 3: Run the gate tests against the real fixture**

Run: `cd backend_py && uv run pytest -m gate -q`
Expected: PASS — each deployed cell passes; the old inert config fails (distinguishable is False). If a deployed cell fails, the derivation (Task 6) selected a non-distinguishable winner — return to Task 6 Step 8 (it should have raised `NoDistinguishableComboError`); reconcile before proceeding.

- [ ] **Step 4: Confirm the unit gate excludes `gate` (next task wires CI; verify locally now)**

Run: `cd backend_py && uv run pytest -m "not integration and not gate" -q`
Expected: PASS and noticeably faster (no rolling backtests).

- [ ] **Step 5: Commit**

```bash
cd backend_py && git add tests/modules/backtest/test_deploy_gate_e2e.py pyproject.toml
git commit -m "$(cat <<'EOF'
✅ Test: deploy gate e2e over fixture + old-config-fails regression

gate-marked: deployed canary cells must beat AlwaysFRR; the pre-fix inert
config (span=168/thr=1.0) is asserted to fail the gate.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 8: Refactor `run_oos_profitability.py` + re-run the research doc

**Files:**
- Modify: `scripts/run_oos_profitability.py` (drop `CanaryCell`/`CANARY_CELLS`/`alpha_to_ema_span`; read `cells.canary.yaml`; reuse `evaluate_oos_windows`)
- Modify: `docs/research/2026-05-28-canary-oos-profitability.md` (+ `.json`) — regenerated against the new config

- [ ] **Step 1: Refactor the script to read the deployed YAML + shared eval**

In `run_oos_profitability.py`:
- Delete `CanaryCell`, `CANARY_CELLS` (lines 57-78) and `alpha_to_ema_span` (93-98).
- Replace `_run_cell`'s per-window loop with `evaluate_oos_windows`, building the strategy from the `CellConfig`:

```python
from bfx_funding_bot.modules.backtest.oos_eval import evaluate_oos_windows
from bfx_funding_bot.modules.marketfeed.config import CellConfig, load_cells_only
from bfx_funding_bot.modules.marketfeed.strategy_registry import build_strategy
# ... remove the now-unused MeanReversionStrategy / run_backtest / LINEAR_CONFIG imports
# that evaluate_oos_windows now owns.

CANARY_YAML = Path("configs/cells.canary.yaml")


async def _run_cell(session: AsyncSession, cell: CellConfig, n_trials: int) -> CellReport:
    end_mts = int(datetime.now(UTC).timestamp() * 1000)
    candles = await get_candles_in_range(
        session, symbol=cell.symbol, timeframe=cell.timeframe,
        period_agg=cell.period_agg, start_mts=START_MTS, end_mts=end_mts,
    )
    if not candles:
        raise SystemExit(f"No candles for {cell.cell_id}; run scripts/backfill_candles.py")
    windows = compute_wfo_windows(candles)
    if not windows:
        raise SystemExit(f"No WFO windows for {cell.cell_id}; candle series too short?")
    strat_outcomes, base_outcomes = evaluate_oos_windows(
        candles, windows, make_strategy=lambda: build_strategy(cell)
    )
    logger.info("%s: %d windows", cell.cell_id, len(windows))
    return build_cell_report(cell, strat_outcomes, base_outcomes, n_trials)
```

- Update `build_cell_report`'s signature to take `CellConfig` (use `cell.cell_id` for `cell_label`) instead of `CanaryCell`.
- In `_amain`, replace the `for cell in CANARY_CELLS` loop with `for cell in load_cells_only(CANARY_YAML)`.

- [ ] **Step 2: Run the gate + types (no behavior test changes; this is a script)**

Run: `cd backend_py && uv run mypy --strict src scripts/run_oos_profitability.py && uv run ruff check && uv run pytest -m "not integration and not gate" -q`
Expected: PASS.

- [ ] **Step 3: Re-run the OOS research doc against the new config (MANUAL — Neon)**

Run: `cd backend_py && uv run python scripts/run_oos_profitability.py --output ../docs/research/2026-05-28-canary-oos-profitability.md`
Expected: regenerates `.md` + `.json`. The active return should now be non-zero (recovered edge), unlike the prior inert run. **Report the new median/active/IR numbers to the operator.**

- [ ] **Step 4: Commit**

```bash
cd backend_py && git add scripts/run_oos_profitability.py ../docs/research/2026-05-28-canary-oos-profitability.md ../docs/research/2026-05-28-canary-oos-profitability.json
git commit -m "$(cat <<'EOF'
♻️ Refactor: run_oos_profitability reads cells.canary.yaml (kills 2nd hand-copy)

Removes the hardcoded CanaryCell mirror; reuses evaluate_oos_windows. Re-runs
the research doc against the pipeline-derived config (active return recovered).

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 9: Wire CI — exclude `gate` from unit, add `backend_py_gate` job

**Files:**
- Modify: `.github/workflows/ci.yml`

- [ ] **Step 1: Exclude `gate` from the unit job**

In `.github/workflows/ci.yml` change the unit job's test step (line 98) from:
`      - run: uv run pytest -m "not integration" -q`
to:
`      - run: uv run pytest -m "not integration and not gate" -q`

- [ ] **Step 2: Add the gate job (offline, uses the committed fixture)**

After the `backend_py_unit` job, add:

```yaml
  backend_py_gate:
    name: Backend (Python) — deploy gate
    runs-on: ubuntu-latest
    timeout-minutes: 15
    permissions:
      contents: read
    needs: backend_py_unit
    defaults:
      run:
        working-directory: backend_py
    steps:
      - uses: actions/checkout@v4
      - uses: astral-sh/setup-uv@v3
        with:
          enable-cache: true
      - run: uv sync --all-groups
      - name: Param drift check (committed YAML == re-derive(fixture))
        run: uv run python scripts/derive_cells.py --check
      - name: Deploy sanity gate (deployed cells beat AlwaysFRR)
        run: uv run pytest -m gate -q
```

- [ ] **Step 3: Validate the workflow YAML locally**

Run: `cd backend_py && python -c "import yaml,sys; yaml.safe_load(open('../.github/workflows/ci.yml')); print('ci.yml valid')"`
Expected: `ci.yml valid`.

- [ ] **Step 4: Confirm the full local gate is green end to end**

Run: `cd backend_py && uv run pytest -m "not integration and not gate" -q && uv run pytest -m gate -q && uv run python scripts/derive_cells.py --check && uv run mypy --strict src && uv run ruff check`
Expected: all PASS / OK.

- [ ] **Step 5: Commit**

```bash
cd backend_py && git add ../.github/workflows/ci.yml
git commit -m "$(cat <<'EOF'
👷 CI: add backend_py_gate job (--check drift + deploy sanity gate)

Unit job now excludes `gate`; the new offline job runs derive_cells --check
and the fixture-based deploy gate. No Neon in CI.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 10: Finish the branch

- [ ] **Step 1: Full verification sweep**

Run: `cd backend_py && uv run pytest -m "not integration and not gate" -q && uv run pytest -m gate -q && uv run python scripts/derive_cells.py --check && uv run mypy --strict src && uv run ruff check`
Expected: all green.

- [ ] **Step 2: Decide branch integration**

This work sits on `feat/oos-profitability-validation` (already carrying the OOS validation + this Tier 2 design). Use the `superpowers:finishing-a-development-branch` skill to choose: merge `feat/oos-profitability-validation` to `main` (the OOS work + Tier 2 are complete and reviewed) vs. continue. Surface the derived-params numbers and the recovered-edge research doc to the operator as the merge evidence.

- [ ] **Step 3: Update memory**

Update `canary-mr-config-inert` memory: the inert config is fixed (pipeline-derived winner deployed, gate guards regressions). Add a note that the deploy gate + `derive_cells --check` now exist.

---

## Self-Review Notes (for the executor)

- **Determinism is load-bearing.** `bootstrap_ci` uses a fixed seed; `select_winner` has a stable tie-break; `fixture_io` writes mtime-0 gzip with sorted keys. If `--check` is flaky, suspect a non-deterministic sort or an unseeded RNG before suspecting the data.
- **`--check` cost (spec Risk 2).** Task 6/9 re-derive the full sweep on every push. If the `backend_py_gate` job exceeds ~10 min, downgrade per-push `--check` to a `_provenance`-vs-YAML comparison (skip the re-derive) and run the full re-derive on a schedule — the `evaluate_gate` e2e test still proves the deployed config beats passive each push.
- **Fixture size (spec Risk 1).** Decided at Task 6 Step 9; git-lfs is the fallback, not pre-judged.
- **No-edge finding (spec Risk 4).** If `derive_cell_params` raises `NoDistinguishableComboError` for a cell, that is a real result (MeanReversion ≈ passive there), not a bug — surface it and let the operator decide whether to run plain AlwaysFRR for that cell.
- **Deliberate narrowing vs spec.** The spec says `--check` "still covers RP cells for drift". This plan's `check_against_fixture` only verifies the MeanReversion cells it derives (plus the provenance hash and canary↔main consistency). RatePercentile is LOCF-disqualified, not deployed in the canary, and its params are static — drift-checking it adds machinery for cells that never ship. If RP is ever deployed, extend `check_against_fixture` with a static-value assertion for RP cells.
