# G1 C1 Continuity Redesign + Self-Smoke Trigger Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace G1 C1 bucket-based check with per-cell max-gap + recency, and add daemon self-smoke trigger so G1 runs automatically after paper duration completes.

**Architecture:** Refactor `scripts/g1_smoke_check.py` business logic into new `bfx_funding_bot.smoke` module so daemon can import it; replace C1 algorithm with TDD; add a `_maybe_run_self_smoke` helper invoked from `daemon._run()` after finally block. Daemon container exit code = G1 smoke exit code in paper+duration mode.

**Tech Stack:** Python 3.13, asyncio, pytest, httpx, Axiom APL.

**Spec:** `docs/superpowers/specs/2026-05-21-g1-c1-continuity-redesign-design.md`

---

## File Structure

**New**:

```
backend_py/src/bfx_funding_bot/smoke/__init__.py        # empty package marker
backend_py/src/bfx_funding_bot/smoke/g1.py              # moved from scripts/g1_smoke_check.py + new C1
backend_py/src/bfx_funding_bot/smoke/eda_ranges.py      # moved from scripts/g1_smoke_eda_ranges.py
backend_py/tests/smoke/__init__.py                      # empty test package marker
backend_py/tests/smoke/test_g1_c1.py                    # new C1 tests (7)
backend_py/tests/marketfeed/test_daemon_self_smoke.py   # self-smoke helper tests (5)
```

**Modified**:

```
backend_py/scripts/g1_smoke_check.py                    # → 15-line CLI wrapper
backend_py/scripts/g1_smoke_eda_ranges.py               # → re-export from module
backend_py/tests/scripts/test_g1_smoke_check.py         # update imports
backend_py/src/bfx_funding_bot/modules/marketfeed/daemon.py  # add self-smoke wiring after _run() finally
docs/superpowers/specs/2026-05-18-phase4.1-paper-shadow-infra-design.md  # mark C1 row + 2 examples superseded
docs/deploy/koyeb-paper.md                              # add Self-Smoke Trigger section + demote manual section
```

**Not touched**: `docs/superpowers/plans/2026-05-18-phase4.1-paper-shadow-infra.md` (historical plan, frozen).

---

## Task 1: Extract g1_smoke_check.py to smoke module（pure refactor, no behavior change）

**Files:**
- Create: `backend_py/src/bfx_funding_bot/smoke/__init__.py`
- Create: `backend_py/src/bfx_funding_bot/smoke/eda_ranges.py`
- Create: `backend_py/src/bfx_funding_bot/smoke/g1.py`
- Modify: `backend_py/scripts/g1_smoke_check.py`（縮成 wrapper）
- Modify: `backend_py/scripts/g1_smoke_eda_ranges.py`（縮成 re-export）
- Modify: `backend_py/tests/scripts/test_g1_smoke_check.py`（更新 import）

- [ ] **Step 1: Create empty smoke package marker**

```python
# backend_py/src/bfx_funding_bot/smoke/__init__.py
```

（檔案內容空，作為 package marker）

- [ ] **Step 2: Move eda_ranges to module**

`backend_py/src/bfx_funding_bot/smoke/eda_ranges.py`：

```python
"""Phase 3b EDA-derived signal_score [min, max] per (strategy, cell).

Source: docs/research/2026-05-18-phase3b-wfo-results.md
Updated: 2026-05-18 (initial bounds -- widen if false-positives during first
        24hr live; narrow if too lax).

`signal_score` is normalized per strategy (see divergence_reporter._normalize_signal_score):
- rate_percentile: in {-1, +1} (discrete; range is informational)
- mean_reversion: in [-1, +1] (continuous)

TODO(phase-4.3): Once G2 calibration refines _normalize_signal_score, tighten
these bounds to per-cell EDA percentiles instead of full strategy range.
"""

EDA_RANGES: dict[tuple[str, str], dict[str, float]] = {
    ("rate_percentile", "fUSD_p2"):  {"signal_score_min": -1.0, "signal_score_max": 1.0},
    ("rate_percentile", "fUSD_p30"): {"signal_score_min": -1.0, "signal_score_max": 1.0},
    ("rate_percentile", "fUSD_a30"): {"signal_score_min": -1.0, "signal_score_max": 1.0},
    ("rate_percentile", "fUST_p2"):  {"signal_score_min": -1.0, "signal_score_max": 1.0},
    ("rate_percentile", "fUST_p30"): {"signal_score_min": -1.0, "signal_score_max": 1.0},
    ("rate_percentile", "fUST_a30"): {"signal_score_min": -1.0, "signal_score_max": 1.0},
    ("mean_reversion", "fUSD_p2"):  {"signal_score_min": -1.0, "signal_score_max": 1.0},
    ("mean_reversion", "fUSD_p30"): {"signal_score_min": -1.0, "signal_score_max": 1.0},
    ("mean_reversion", "fUSD_a30"): {"signal_score_min": -1.0, "signal_score_max": 1.0},
    ("mean_reversion", "fUST_p2"):  {"signal_score_min": -1.0, "signal_score_max": 1.0},
    ("mean_reversion", "fUST_a30"): {"signal_score_min": -1.0, "signal_score_max": 1.0},
}
```

- [ ] **Step 3: Move g1_smoke_check.py to module（unchanged behavior）**

把目前 `backend_py/scripts/g1_smoke_check.py` 整份 copy 到 `backend_py/src/bfx_funding_bot/smoke/g1.py`，**只改 2 處 import**：

原檔 line 29-30：

```python
sys.path.insert(0, str(Path(__file__).parent))
from g1_smoke_eda_ranges import EDA_RANGES  # type: ignore[import-not-found]
```

改成：

```python
from bfx_funding_bot.smoke.eda_ranges import EDA_RANGES
```

並移除 line 16 的 `import sys`、line 23 的 `from pathlib import Path` 若不再使用（`Path` 在 `main_async` 仍要用，保留；`sys` 也仍要用於 `sys.path.insert` 已移除但 SystemExit 用，保留）。

最後 main entry block（line 691-692）保留：

```python
if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Shrink scripts/g1_smoke_check.py to thin wrapper**

`backend_py/scripts/g1_smoke_check.py` 整檔內容替換為：

```python
#!/usr/bin/env python
"""G1 deployment-gate smoke check (CLI wrapper).

Business logic in bfx_funding_bot.smoke.g1. This file kept for backwards-compatible
invocation via `uv run python scripts/g1_smoke_check.py`.
"""
from bfx_funding_bot.smoke.g1 import main

if __name__ == "__main__":
    main()
```

- [ ] **Step 5: Shrink scripts/g1_smoke_eda_ranges.py to re-export**

`backend_py/scripts/g1_smoke_eda_ranges.py` 整檔內容替換為：

```python
"""Re-export EDA_RANGES from canonical module location.

Canonical: bfx_funding_bot.smoke.eda_ranges
"""
from bfx_funding_bot.smoke.eda_ranges import EDA_RANGES

__all__ = ["EDA_RANGES"]
```

- [ ] **Step 6: Update tests/scripts/test_g1_smoke_check.py imports**

把 `backend_py/tests/scripts/test_g1_smoke_check.py` 開頭 line 1-18：

```python
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock

sys.path.insert(0, str(Path(__file__).parents[2] / "scripts"))

from g1_smoke_check import (  # type: ignore[import-not-found]
    AxiomQueryClient,
    build_apl_query_c2,
    build_apl_query_c3,
    build_apl_query_c6,
    run_c1_continuity,
    run_c2_emit_completeness,
    run_c5_zero_error_health,
    run_c6_zero_divergence,
)
```

替換為：

```python
from __future__ import annotations

from unittest.mock import AsyncMock

from bfx_funding_bot.smoke.g1 import (
    AxiomQueryClient,
    build_apl_query_c2,
    build_apl_query_c3,
    build_apl_query_c6,
    run_c1_continuity,
    run_c2_emit_completeness,
    run_c5_zero_error_health,
    run_c6_zero_divergence,
)
```

- [ ] **Step 7: Run all existing tests to verify regression-free**

```bash
cd backend_py && uv run pytest tests/scripts/test_g1_smoke_check.py -v
```

預期：所有原有 tests PASS（c1 兩個舊測試此時仍 pass，會在 Task 3 才砍）。

- [ ] **Step 8: Run full test suite**

```bash
cd backend_py && uv run pytest -m "not integration" -q
```

預期：原有測試全 PASS（refactor 沒改行為）。

- [ ] **Step 9: Commit**

```bash
cd backend_py
git add src/bfx_funding_bot/smoke/__init__.py
git add src/bfx_funding_bot/smoke/eda_ranges.py
git add src/bfx_funding_bot/smoke/g1.py
git add scripts/g1_smoke_check.py
git add scripts/g1_smoke_eda_ranges.py
git add tests/scripts/test_g1_smoke_check.py
git commit -m "$(cat <<'EOF'
♻️ Refactor: extract G1 smoke check to bfx_funding_bot.smoke module

scripts/g1_smoke_check.py business logic moved to src/bfx_funding_bot/smoke/g1.py
so daemon can import it for self-smoke trigger (next step). Scripts kept as
thin CLI wrappers for backwards-compatible invocation.

No behavior change. All existing tests pass after import path update.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 2: Add `run_smoke_async` + `now_fn` injection seam

**Files:**
- Modify: `backend_py/src/bfx_funding_bot/smoke/g1.py`

**Why:** Daemon self-smoke needs a programmatic entry that takes `cells` (already loaded in daemon config) and an injectable `now_fn` for unit testing C1 against frozen time without `freezegun`. Existing `main_async(args)` keeps CLI entry; new `run_smoke_async` takes typed params.

- [ ] **Step 1: Refactor main_async to delegate to run_smoke_async**

In `backend_py/src/bfx_funding_bot/smoke/g1.py`, locate `main_async(args)` (currently at line ~238). Replace it with this structure:

```python
async def run_smoke_async(
    *,
    phase: str,
    hours: int,
    cells: list[dict[str, Any]],
    only: set[str] | None = None,
    now_fn: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> int:
    """Run G1 smoke checks. Returns exit code (0=pass, 1=fail, 2=auth, 3=config).

    Args:
        phase: BFX phase (paper / shadow / canary).
        hours: lookback window in hours.
        cells: cell config dicts (each with strategy, symbol, period_agg, timeframe).
        only: subset of check names to run (None = all).
        now_fn: injectable clock for C1 recency check (UTC aware).
    """
    axiom_api_key = os.environ.get("AXIOM_API_KEY", "")
    axiom_dataset = os.environ.get("AXIOM_DATASET", "")
    if not axiom_api_key or not axiom_dataset:
        print("ERROR: AXIOM_API_KEY and AXIOM_DATASET required", file=sys.stderr)
        return 3

    client = AxiomQueryClient(api_key=axiom_api_key, dataset=axiom_dataset)
    try:
        results: list[CheckResult] = []
        if only is None or "C1" in only:
            results.append(await run_c1_continuity(
                client=client, phase=phase, hours=hours, cells=cells, now_fn=now_fn,
            ))
        if only is None or "C2" in only:
            results.append(await run_c2_emit_completeness(
                client=client, phase=phase, hours=hours, cells=cells,
            ))
        if only is None or "C3" in only:
            results.append(await run_c3_range_conformance(
                client=client, phase=phase, hours=hours, cells=cells,
            ))
        if only is None or "C5" in only:
            results.append(await run_c5_zero_error_health(
                client=client, phase=phase, hours=hours,
            ))
        if only is None or "C6" in only:
            results.append(await run_c6_zero_divergence(
                client=client, phase=phase, hours=hours,
            ))
        results.append(CheckResult(
            "C4: dashboard liveness", True, skipped=True,
            detail="(4.1 V1 -- dashboard widget belongs to 4.4)",
        ))

        # Print + decide exit code
        all_passed = True
        for r in results:
            tag = "SKIP" if r.skipped else ("PASS" if r.passed else "FAIL")
            print(f"[ {tag} ] {r.name:40s} {r.detail}")
            if not r.skipped and not r.passed:
                all_passed = False
        return 0 if all_passed else 1
    except SystemExit as e:
        return int(e.code) if e.code is not None else 2
    finally:
        await client.aclose()


async def main_async(args: argparse.Namespace) -> int:
    """CLI entrypoint: load yaml, parse env, call run_smoke_async."""
    phase = os.environ.get("BFX_PHASE", "paper")
    cells_yaml = Path(
        args.cells_yaml or Path(__file__).parents[3] / "configs" / "cells.yaml",
    )
    cells_data = yaml.safe_load(cells_yaml.read_text())
    cells = cells_data["cells"]
    only = set((args.only or "").split(",")) if args.only else None
    return await run_smoke_async(phase=phase, hours=args.hours, cells=cells, only=only)
```

注意：
- `Path(__file__).parents[3]` 因為 module 路徑從 `scripts/` 變 `src/bfx_funding_bot/smoke/g1.py`，往上 3 層才到 `backend_py/`
- `run_c1_continuity` 新增 `cells` + `now_fn` 參數（Task 3 才實作；此 step 先準備呼叫端，**Task 2 結束時 C1 既有實作仍接舊 signature，會 broken**，所以 Step 4 必須跟著動 C1 signature 防壞）

實際上 cleaner：Task 2 先準備呼叫端但 c1 接 `**kwargs` 容錯，或者 Task 2 + Task 3 合併。為避免 broken middle state — **本 step 改為 c1 接舊 sig + 新 sig 兩相容**。

修改如下：暫時讓 `run_c1_continuity` 在 Task 2 接受新參數但忽略（kwargs swallow），等 Task 3 才真正用：

```python
async def run_c1_continuity(
    *, client, phase: str, hours: int,
    cells: list[dict[str, Any]] | None = None,  # noqa: ARG001 — used in Task 3
    now_fn: Callable[[], datetime] | None = None,  # noqa: ARG001 — used in Task 3
) -> CheckResult:
    # ... existing bucket logic unchanged ...
```

- [ ] **Step 2: Add imports needed**

In `backend_py/src/bfx_funding_bot/smoke/g1.py` top:

```python
from collections.abc import Callable
from datetime import UTC, datetime
```

並 `from typing import Any` 若還沒 import。

- [ ] **Step 3: Run existing tests to verify no regression**

```bash
cd backend_py && uv run pytest tests/scripts/test_g1_smoke_check.py -v
```

預期：原有 c1/c2/c3/c5/c6 tests 全 PASS（c1 接 kwargs 容錯舊測試仍走舊 bucket path）。

- [ ] **Step 4: Commit**

```bash
cd backend_py
git add src/bfx_funding_bot/smoke/g1.py
git commit -m "$(cat <<'EOF'
♻️ Refactor: G1 smoke add run_smoke_async + now_fn injection seam

Prepare programmatic entry point for daemon self-smoke trigger. C1 signature
extended with cells + now_fn params (swallowed for now; consumed in next commit).
CLI main_async stays as thin yaml-loading wrapper around run_smoke_async.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 3: Replace C1 algorithm（per-cell max-gap + recency, TDD）

**Files:**
- Create: `backend_py/tests/smoke/__init__.py`
- Create: `backend_py/tests/smoke/test_g1_c1.py`
- Modify: `backend_py/src/bfx_funding_bot/smoke/g1.py`（replace C1 algorithm + APL query）
- Modify: `backend_py/tests/scripts/test_g1_smoke_check.py`（delete 2 old C1 tests）

- [ ] **Step 1: Create test package marker**

```python
# backend_py/tests/smoke/__init__.py
```

（檔案內容空）

- [ ] **Step 2: Write first failing test — pass case**

`backend_py/tests/smoke/test_g1_c1.py`:

```python
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest

from bfx_funding_bot.smoke.g1 import run_c1_continuity


def _cell(symbol="fUSD", period_agg="p2", strategy="mean_reversion", timeframe="1h"):
    return {
        "strategy": strategy,
        "symbol": symbol,
        "period_agg": period_agg,
        "timeframe": timeframe,
    }


def _iso(ts: datetime) -> str:
    return ts.isoformat().replace("+00:00", "Z")


@pytest.mark.asyncio
async def test_c1_pass_regular_emissions():
    """11 cells each emit 1 signal in the hour, all within recency bound."""
    frozen_now = datetime(2026, 5, 21, 14, 0, 0, tzinfo=UTC)
    # 11 cells, each emitted exactly 1 signal 30min before frozen_now
    cells = [_cell(symbol=s, period_agg=p) for s, p in [
        ("fUSD", "p2"), ("fUSD", "p30"), ("fUSD", "a30"),
        ("fUST", "p2"), ("fUST", "p30"), ("fUST", "a30"),
        ("fUSD", "p2"), ("fUSD", "p30"), ("fUSD", "a30"),
        ("fUST", "p2"), ("fUST", "a30"),
    ]]
    # mark first 6 as rate_percentile, rest as mean_reversion
    for i, c in enumerate(cells):
        c["strategy"] = "rate_percentile" if i < 6 else "mean_reversion"

    events = [
        {
            "_time": _iso(frozen_now - timedelta(minutes=30)),
            "strategy": c["strategy"],
            "cell": f"{c['symbol']}_{c['period_agg']}",
        }
        for c in cells
    ]

    fake_client = AsyncMock()
    fake_client.dataset = "evt"
    fake_client.query_apl = AsyncMock(return_value=events)

    result = await run_c1_continuity(
        client=fake_client, phase="paper", hours=1, cells=cells,
        now_fn=lambda: frozen_now,
    )
    assert result.passed is True, result.detail
```

- [ ] **Step 3: Run test, confirm fail**

```bash
cd backend_py && uv run pytest tests/smoke/test_g1_c1.py::test_c1_pass_regular_emissions -v
```

預期：FAIL（C1 仍是舊 bucket 邏輯，新 sig 參數被 swallow，行為跟 cells 無關）。

- [ ] **Step 4: Replace build_apl_query_c1 + run_c1_continuity in module**

In `backend_py/src/bfx_funding_bot/smoke/g1.py`:

替換 `build_apl_query_c1` (line ~84-90)：

```python
def build_apl_query_c1(phase: str, dataset: str, hours: int) -> str:
    return f"""
['{dataset}']
| where ['event_type'] == 'signal' and phase == '{phase}' and _time > ago({hours}h)
| project _time, strategy, cell
| order by strategy asc, cell asc, _time asc
""".strip()
```

替換 `run_c1_continuity` (line ~137-150)：

```python
TF_TO_SECONDS = {"15m": 900, "30m": 1800, "1h": 3600}
TOLERANCE = 1.5


def _parse_iso_utc(s: str) -> datetime:
    """Parse Axiom ISO8601 (handles 'Z' suffix)."""
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


async def run_c1_continuity(
    *,
    client: AxiomQueryClient,
    phase: str,
    hours: int,
    cells: list[dict[str, Any]],
    now_fn: Callable[[], datetime],
) -> CheckResult:
    from collections import defaultdict

    apl = build_apl_query_c1(phase, client.dataset, hours)
    events = await client.query_apl(apl)

    by_cell: dict[tuple[str, str], list[datetime]] = defaultdict(list)
    for e in events:
        ts_raw = e.get("_time")
        if not ts_raw:
            continue
        by_cell[(e["strategy"], e["cell"])].append(_parse_iso_utc(ts_raw))

    window_end = now_fn()
    failures: list[str] = []
    for c in cells:
        key = (c["strategy"], f"{c['symbol']}_{c['period_agg']}")
        bound = TF_TO_SECONDS[c.get("timeframe", "1h")] * TOLERANCE
        ts = sorted(by_cell.get(key, []))

        if not ts:
            failures.append(f"{key[0]}:{key[1]} 0 signals")
            continue

        # C1a max-gap (skip if only 1 signal)
        if len(ts) >= 2:
            gaps = [(ts[i + 1] - ts[i]).total_seconds() for i in range(len(ts) - 1)]
            max_gap = max(gaps)
            if max_gap > bound:
                failures.append(
                    f"{key[0]}:{key[1]} max_gap={max_gap:.0f}s > {bound:.0f}s",
                )

        # C1b recency
        age = (window_end - ts[-1]).total_seconds()
        if age > bound:
            failures.append(
                f"{key[0]}:{key[1]} recency={age:.0f}s > {bound:.0f}s",
            )

    if not failures:
        return CheckResult(
            "C1: continuity", True,
            detail=f"{len(cells)} cells all within 1.5x cadence",
        )
    return CheckResult("C1: continuity", False, detail="; ".join(failures))
```

- [ ] **Step 5: Run test, confirm pass**

```bash
cd backend_py && uv run pytest tests/smoke/test_g1_c1.py::test_c1_pass_regular_emissions -v
```

預期：PASS。

- [ ] **Step 6: Add remaining 6 tests**

Append to `backend_py/tests/smoke/test_g1_c1.py`:

```python
@pytest.mark.asyncio
async def test_c1_fail_max_gap_exceeded():
    """1h cell with 91min gap between 2 signals exceeds 1.5x=90min bound."""
    frozen_now = datetime(2026, 5, 21, 14, 0, 0, tzinfo=UTC)
    cells = [_cell()]  # 1h
    events = [
        {
            "_time": _iso(frozen_now - timedelta(minutes=93)),
            "strategy": "mean_reversion", "cell": "fUSD_p2",
        },
        {
            "_time": _iso(frozen_now - timedelta(minutes=2)),
            "strategy": "mean_reversion", "cell": "fUSD_p2",
        },
    ]

    fake_client = AsyncMock()
    fake_client.dataset = "evt"
    fake_client.query_apl = AsyncMock(return_value=events)

    result = await run_c1_continuity(
        client=fake_client, phase="paper", hours=2, cells=cells,
        now_fn=lambda: frozen_now,
    )
    assert result.passed is False
    assert "mean_reversion:fUSD_p2" in result.detail
    assert "max_gap" in result.detail


@pytest.mark.asyncio
async def test_c1_fail_recency_exceeded():
    """Last signal too old vs frozen_now."""
    frozen_now = datetime(2026, 5, 21, 14, 0, 0, tzinfo=UTC)
    cells = [_cell()]  # 1h, bound = 90min
    events = [
        {
            "_time": _iso(frozen_now - timedelta(minutes=91)),
            "strategy": "mean_reversion", "cell": "fUSD_p2",
        },
    ]

    fake_client = AsyncMock()
    fake_client.dataset = "evt"
    fake_client.query_apl = AsyncMock(return_value=events)

    result = await run_c1_continuity(
        client=fake_client, phase="paper", hours=2, cells=cells,
        now_fn=lambda: frozen_now,
    )
    assert result.passed is False
    assert "recency" in result.detail


@pytest.mark.asyncio
async def test_c1_fail_zero_signals_for_cell():
    """Cell config exists but no event emitted in window."""
    frozen_now = datetime(2026, 5, 21, 14, 0, 0, tzinfo=UTC)
    cells = [_cell()]
    fake_client = AsyncMock()
    fake_client.dataset = "evt"
    fake_client.query_apl = AsyncMock(return_value=[])

    result = await run_c1_continuity(
        client=fake_client, phase="paper", hours=1, cells=cells,
        now_fn=lambda: frozen_now,
    )
    assert result.passed is False
    assert "0 signals" in result.detail


@pytest.mark.asyncio
async def test_c1_pass_single_signal_within_recency():
    """1 signal in window, age 30min < bound 90min, skip max-gap check."""
    frozen_now = datetime(2026, 5, 21, 14, 0, 0, tzinfo=UTC)
    cells = [_cell()]
    events = [
        {
            "_time": _iso(frozen_now - timedelta(minutes=30)),
            "strategy": "mean_reversion", "cell": "fUSD_p2",
        },
    ]

    fake_client = AsyncMock()
    fake_client.dataset = "evt"
    fake_client.query_apl = AsyncMock(return_value=events)

    result = await run_c1_continuity(
        client=fake_client, phase="paper", hours=1, cells=cells,
        now_fn=lambda: frozen_now,
    )
    assert result.passed is True, result.detail


@pytest.mark.asyncio
async def test_c1_pass_locf_cell_same_tolerance():
    """LOCF cell (with staleness_budget_hours) gets same 1.5x treatment."""
    frozen_now = datetime(2026, 5, 21, 14, 0, 0, tzinfo=UTC)
    cells = [{
        "strategy": "rate_percentile",
        "symbol": "fUSD",
        "period_agg": "p30",
        "timeframe": "1h",
        "staleness_budget_hours": 12,  # LOCF override — should NOT affect C1
    }]
    events = [
        {
            "_time": _iso(frozen_now - timedelta(minutes=30)),
            "strategy": "rate_percentile", "cell": "fUSD_p30",
        },
    ]

    fake_client = AsyncMock()
    fake_client.dataset = "evt"
    fake_client.query_apl = AsyncMock(return_value=events)

    result = await run_c1_continuity(
        client=fake_client, phase="paper", hours=1, cells=cells,
        now_fn=lambda: frozen_now,
    )
    assert result.passed is True, result.detail


@pytest.mark.asyncio
async def test_c1_boundary_at_exact_tolerance():
    """gap = 90min exactly should pass (<=); 90min 1s should fail."""
    frozen_now = datetime(2026, 5, 21, 14, 0, 0, tzinfo=UTC)
    cells = [_cell()]  # bound = 90min = 5400s

    # Exactly 90min — pass
    events_pass = [
        {
            "_time": _iso(frozen_now - timedelta(minutes=90)),
            "strategy": "mean_reversion", "cell": "fUSD_p2",
        },
        {
            "_time": _iso(frozen_now),
            "strategy": "mean_reversion", "cell": "fUSD_p2",
        },
    ]
    fake_client = AsyncMock()
    fake_client.dataset = "evt"
    fake_client.query_apl = AsyncMock(return_value=events_pass)
    result = await run_c1_continuity(
        client=fake_client, phase="paper", hours=2, cells=cells,
        now_fn=lambda: frozen_now,
    )
    assert result.passed is True, result.detail

    # 90min 1s — fail
    events_fail = [
        {
            "_time": _iso(frozen_now - timedelta(minutes=90, seconds=1)),
            "strategy": "mean_reversion", "cell": "fUSD_p2",
        },
        {
            "_time": _iso(frozen_now),
            "strategy": "mean_reversion", "cell": "fUSD_p2",
        },
    ]
    fake_client.query_apl = AsyncMock(return_value=events_fail)
    result = await run_c1_continuity(
        client=fake_client, phase="paper", hours=2, cells=cells,
        now_fn=lambda: frozen_now,
    )
    assert result.passed is False
```

- [ ] **Step 7: Run all 7 C1 tests**

```bash
cd backend_py && uv run pytest tests/smoke/test_g1_c1.py -v
```

預期：7 個 tests 全 PASS。

- [ ] **Step 8: Delete old C1 bucket tests**

In `backend_py/tests/scripts/test_g1_smoke_check.py`，刪除 2 個 function：

```python
async def test_c1_passes_when_all_buckets_non_empty():
    ...

async def test_c1_fails_with_empty_bucket():
    ...
```

並從 imports 移除 `run_c1_continuity`（如果不再被其他測試使用）。

- [ ] **Step 9: Run full smoke + scripts tests**

```bash
cd backend_py && uv run pytest tests/scripts/test_g1_smoke_check.py tests/smoke/ -v
```

預期：所有保留的 c2/c3/c5/c6/tabular tests + 7 個新 C1 tests 全 PASS。舊 C1 兩個 tests 不再存在。

- [ ] **Step 10: Run full suite**

```bash
cd backend_py && uv run pytest -m "not integration" -q
```

預期：整 suite 綠。

- [ ] **Step 11: Commit**

```bash
cd backend_py
git add src/bfx_funding_bot/smoke/g1.py
git add tests/smoke/__init__.py
git add tests/smoke/test_g1_c1.py
git add tests/scripts/test_g1_smoke_check.py
git commit -m "$(cat <<'EOF'
✨ Feat: G1 C1 continuity redesign (per-cell max-gap + recency)

Old definition (every 5min bucket non-empty) was mathematically incompatible
with cells.yaml all-1h cadence — 11 signals/hr can't cover 12 buckets by
pigeonhole. New definition per spec 2026-05-21-g1-c1-continuity-redesign:
max(inter-arrival gap) <= 1.5x expected_period AND recency <= 1.5x expected_period,
per cell. expected_period derived from cells[].timeframe.

LOCF cells (with staleness_budget_hours) treated same — LOCF emit-on-stale
keeps cadence unchanged.

Tests: 7 new C1 tests (pass/fail × pass-with-single-signal/recency/zero-signals/
locf/boundary). 2 old bucket tests deleted. C2-C6 tests unchanged.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 4: Self-smoke trigger helper（unit-tested, daemon-independent）

**Files:**
- Create: `backend_py/src/bfx_funding_bot/modules/marketfeed/self_smoke.py`
- Create: `backend_py/tests/marketfeed/test_daemon_self_smoke.py`

**Why:** Helper extracted as pure function so daemon `_run()` just calls it; helper takes injectable `smoke_runner` + `sleep_fn` callbacks for full unit-test isolation.

- [ ] **Step 1: Write failing test — gated case runs runner**

`backend_py/tests/marketfeed/test_daemon_self_smoke.py`:

```python
from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest

from bfx_funding_bot.modules.marketfeed.self_smoke import maybe_run_self_smoke


@pytest.mark.asyncio
async def test_self_smoke_runs_when_gated():
    """phase=paper + duration set → smoke_runner called, returns its exit code."""
    smoke_runner = AsyncMock(return_value=0)
    sleep_fn = AsyncMock(return_value=None)
    cells: list[dict[str, Any]] = [{"strategy": "x", "symbol": "y", "period_agg": "z", "timeframe": "1h"}]

    result = await maybe_run_self_smoke(
        phase="paper",
        duration=1,
        cells=cells,
        smoke_runner=smoke_runner,
        sleep_fn=sleep_fn,
    )

    assert result == 0
    smoke_runner.assert_awaited_once_with(phase="paper", hours=1, cells=cells)
```

- [ ] **Step 2: Run test, confirm fail**

```bash
cd backend_py && uv run pytest tests/marketfeed/test_daemon_self_smoke.py::test_self_smoke_runs_when_gated -v
```

預期：FAIL — module not found.

- [ ] **Step 3: Implement helper**

`backend_py/src/bfx_funding_bot/modules/marketfeed/self_smoke.py`:

```python
"""G1 self-smoke trigger helper.

Spec: docs/superpowers/specs/2026-05-21-g1-c1-continuity-redesign-design.md
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

SLEEP_BEFORE_SMOKE_SECONDS = 30


async def maybe_run_self_smoke(
    *,
    phase: str,
    duration: int | None,
    cells: list[dict[str, Any]],
    smoke_runner: Callable[..., Awaitable[int]],
    sleep_fn: Callable[[float], Awaitable[None]],
) -> int:
    """Run G1 smoke checks if gating conditions hold; otherwise return 0.

    Gated by: phase == "paper" AND duration is not None.

    Exception case is NOT a gate — daemon._run() propagates exceptions via
    `except* Exception: raise` so this helper is unreachable on daemon failure.
    SIGTERM (CancelledError) falls through to here intentionally — smoke runs
    against whatever was emitted before stop.

    Returns the smoke exit code (0 if skipped because not gated).
    """
    if phase != "paper" or duration is None:
        return 0

    await sleep_fn(SLEEP_BEFORE_SMOKE_SECONDS)
    return await smoke_runner(phase=phase, hours=duration, cells=cells)
```

- [ ] **Step 4: Re-run first test, confirm pass**

```bash
cd backend_py && uv run pytest tests/marketfeed/test_daemon_self_smoke.py::test_self_smoke_runs_when_gated -v
```

預期：PASS.

- [ ] **Step 5: Add remaining 4 tests**

Append to `backend_py/tests/marketfeed/test_daemon_self_smoke.py`:

```python
@pytest.mark.asyncio
async def test_self_smoke_skipped_shadow():
    """phase=shadow → smoke_runner NOT called, returns 0."""
    smoke_runner = AsyncMock(return_value=999)
    sleep_fn = AsyncMock(return_value=None)

    result = await maybe_run_self_smoke(
        phase="shadow",
        duration=1,
        cells=[],
        smoke_runner=smoke_runner,
        sleep_fn=sleep_fn,
    )

    assert result == 0
    smoke_runner.assert_not_awaited()
    sleep_fn.assert_not_awaited()


@pytest.mark.asyncio
async def test_self_smoke_skipped_no_duration():
    """duration=None → smoke_runner NOT called even if phase=paper."""
    smoke_runner = AsyncMock(return_value=999)
    sleep_fn = AsyncMock(return_value=None)

    result = await maybe_run_self_smoke(
        phase="paper",
        duration=None,
        cells=[],
        smoke_runner=smoke_runner,
        sleep_fn=sleep_fn,
    )

    assert result == 0
    smoke_runner.assert_not_awaited()
    sleep_fn.assert_not_awaited()


@pytest.mark.asyncio
async def test_self_smoke_propagates_exit_code():
    """smoke_runner returns 1 → helper returns 1."""
    smoke_runner = AsyncMock(return_value=1)
    sleep_fn = AsyncMock(return_value=None)

    result = await maybe_run_self_smoke(
        phase="paper",
        duration=1,
        cells=[],
        smoke_runner=smoke_runner,
        sleep_fn=sleep_fn,
    )

    assert result == 1


@pytest.mark.asyncio
async def test_self_smoke_sleeps_before_runner():
    """sleep_fn called with 30 BEFORE smoke_runner is awaited."""
    call_order: list[str] = []
    smoke_runner = AsyncMock(side_effect=lambda **kw: call_order.append("smoke") or 0)

    async def fake_sleep(seconds: float) -> None:
        call_order.append(f"sleep:{seconds}")

    result = await maybe_run_self_smoke(
        phase="paper",
        duration=1,
        cells=[],
        smoke_runner=smoke_runner,
        sleep_fn=fake_sleep,
    )

    assert result == 0
    assert call_order == ["sleep:30", "smoke"]
```

- [ ] **Step 6: Verify ALL 5 tests pass**

```bash
cd backend_py && uv run pytest tests/marketfeed/test_daemon_self_smoke.py -v
```

預期：5 passing.

- [ ] **Step 7: Commit**

```bash
cd backend_py
git add src/bfx_funding_bot/modules/marketfeed/self_smoke.py
git add tests/marketfeed/test_daemon_self_smoke.py
git commit -m "$(cat <<'EOF'
✨ Feat: G1 self-smoke trigger helper at daemon exit

Pure helper maybe_run_self_smoke gated by phase=paper AND duration set.
Sleeps 30s (Axiom ingestion catch-up) then invokes injected smoke_runner.

Exception path NOT gated — daemon._run() except* Exception: raise propagates,
helper unreachable on failure. CancelledError (SIGTERM) falls through —
smoke runs against whatever was emitted before stop, intentional.

5 unit tests cover: gated runs, shadow skipped, no-duration skipped,
exit-code propagation, sleep-before-runner ordering.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 5: Wire helper into daemon._run()

**Files:**
- Modify: `backend_py/src/bfx_funding_bot/modules/marketfeed/daemon.py:650-693`

**Why:** After `_run()` finally block, call `maybe_run_self_smoke` and propagate exit code via `sys.exit(...)`. Wiring has no automated test (per spec — skip daemon e2e); rely on helper test + code review.

- [ ] **Step 1: Add imports at top of daemon.py**

In `backend_py/src/bfx_funding_bot/modules/marketfeed/daemon.py`，找到既有 imports 區段（`asyncio` 跟 `sys` 已經有，不重複加），加 2 行：

```python
from bfx_funding_bot.modules.marketfeed.self_smoke import maybe_run_self_smoke
from bfx_funding_bot.smoke.g1 import run_smoke_async
```

擺在既有 `from bfx_funding_bot.*` imports 區段附近，依字母順序排。

- [ ] **Step 2: Modify _run() to call maybe_run_self_smoke after finally**

In `backend_py/src/bfx_funding_bot/modules/marketfeed/daemon.py:650-693`，替換整個 `_run()` 函數為：

```python
async def _run() -> None:
    daemon = await build_daemon()

    stop = daemon._stop_event  # share with signal handler
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)

    log.info(
        "daemon_started phase=%s cells=%d",
        daemon.config.phase, len(daemon.config.cells),
    )

    # Optional duration cap (used by paper smoke mode); shadow has no duration.
    duration = daemon.config.run_duration_hours
    if duration is not None:
        async def _duration_timer() -> None:
            try:
                await asyncio.wait_for(stop.wait(), timeout=duration * 3600)
            except TimeoutError:
                log.info("daemon_run_duration_reached hours=%d", duration)
                stop.set()
        _timer_task = asyncio.create_task(_duration_timer())  # noqa: RUF006

    try:
        await daemon.run()
        log.info("daemon_run_clean_exit")
    except* asyncio.CancelledError:
        log.info("daemon_cancelled_via_signal")
    except* Exception as eg:
        log.error(
            "daemon_taskgroup_fatal exceptions=%s",
            [type(e).__name__ for e in eg.exceptions],
        )
        raise
    finally:
        # Cleanup after TaskGroup completes (flush axiom, close http client)
        log.info("daemon_shutdown_complete")
        await daemon.bitfinex_http.aclose()

    # Self-smoke trigger (Phase 4.1.x — see specs/2026-05-21-g1-c1-continuity-redesign-design.md)
    # Gated by phase=paper + duration set; exception path is structurally unreachable here.
    log.info("g1_smoke_starting phase=%s duration=%s", daemon.config.phase, duration)
    smoke_exit_code = await maybe_run_self_smoke(
        phase=daemon.config.phase,
        duration=duration,
        cells=daemon.config.cells,
        smoke_runner=run_smoke_async,
        sleep_fn=asyncio.sleep,
    )
    log.info("g1_smoke_done exit_code=%d", smoke_exit_code)
    if smoke_exit_code != 0:
        sys.exit(smoke_exit_code)
```

注意：`maybe_run_self_smoke` 在 shadow / no-duration 時直接返 0 不 sleep — `g1_smoke_starting` 跟 `g1_smoke_done` log 仍會印（沒副作用），如不想印可加 `if daemon.config.phase == "paper" and duration is not None:` guard 在 log 外。**先保留全印**，方便 troubleshoot；後續嫌 noisy 再 guard。

- [ ] **Step 3: Verify unit tests still pass**

```bash
cd backend_py && uv run pytest tests/marketfeed/ -v
```

預期：所有 marketfeed tests PASS。

- [ ] **Step 4: Verify whole suite still pass**

```bash
cd backend_py && uv run pytest -m "not integration" -q
```

預期：綠。

- [ ] **Step 5: Static smoke check — manual read**

讀 `backend_py/src/bfx_funding_bot/modules/marketfeed/daemon.py:_run()` 確認：

- [ ] `maybe_run_self_smoke` 呼叫在 finally 之後而非之中
- [ ] `smoke_exit_code` 非 0 才 `sys.exit`
- [ ] `daemon.config.phase` 跟 `daemon.config.cells` 確實存在（grep `config.cells` / `config.phase` 確認）
- [ ] `asyncio.sleep` 跟 `run_smoke_async` 都是可 `await` 的 coroutine function

- [ ] **Step 6: Commit**

```bash
cd backend_py
git add src/bfx_funding_bot/modules/marketfeed/daemon.py
git commit -m "$(cat <<'EOF'
✨ Feat: wire G1 self-smoke trigger into daemon._run()

After daemon _run() finally block, call maybe_run_self_smoke with the runtime
config. In paper+duration mode, sleeps 30s then runs full G1 (C1-C6); container
exit code = smoke exit code so Koyeb deploy reflects G1 gate status.

Manual verification: daemon._run() now ends with self-smoke section; smoke
helper unit-tested separately. No daemon e2e test per spec (G1 itself is the e2e).

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 6: Mark old spec C1 row as superseded

**Files:**
- Modify: `docs/superpowers/specs/2026-05-18-phase4.1-paper-shadow-infra-design.md:717,752,808`

- [ ] **Step 1: Patch line 717 (C1 row in spec table)**

讀 `docs/superpowers/specs/2026-05-18-phase4.1-paper-shadow-infra-design.md` 找到 line 717，原內容：

```
| ≥ 1hr 連續 logging | **C1: continuity** | 每 5min 桶都至少 1 個 signal event；任兩相鄰 signal 間隔 < 5min |
```

替換為：

```
| ≥ 1hr 連續 logging | **C1: continuity** | ~~每 5min 桶都至少 1 個 signal event；任兩相鄰 signal 間隔 < 5min~~ **Superseded 2026-05-21** — 見 [`2026-05-21-g1-c1-continuity-redesign-design.md`](2026-05-21-g1-c1-continuity-redesign-design.md)。新定義：per-cell max-gap + recency ≤ 1.5× expected_period |
```

- [ ] **Step 2: Patch line 752 (APL query example)**

讀 line 752 上下文確認是 C1 APL example block。在 code block 上方一行加 note：

```markdown
> ⚠️ **此 APL 已 superseded 2026-05-21** — 見 [`2026-05-21-g1-c1-continuity-redesign-design.md`](2026-05-21-g1-c1-continuity-redesign-design.md) §Algorithm Detail 新 query。
```

舊 APL block 保留不刪（歷史紀錄）。

- [ ] **Step 3: Patch line 808 (example output)**

讀 line 808 上下文確認是 `[ C1 ] Continuity ... PASS  12/12 5min buckets non-empty` 的範例輸出。在輸出 block 上方加：

```markdown
> ⚠️ **此範例輸出格式 superseded 2026-05-21** — 新格式：`{n} cells all within 1.5x cadence`，見新 spec。
```

- [ ] **Step 4: Commit**

```bash
cd /Users/will/second-brain/projects/startup/bfx-funding-bot
git add docs/superpowers/specs/2026-05-18-phase4.1-paper-shadow-infra-design.md
git commit -m "$(cat <<'EOF'
📝 Docs: mark Phase 4.1 G1 C1 spec section as superseded

C1 row (line 717) + APL example (line 752) + output example (line 808) all
point to 2026-05-21-g1-c1-continuity-redesign-design.md. Original text kept
via strikethrough for historical reference.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 7: Update koyeb-paper.md with Self-Smoke Trigger section

**Files:**
- Modify: `docs/deploy/koyeb-paper.md`

- [ ] **Step 1: Insert Self-Smoke Trigger section before existing "## G1 Smoke 驗證"**

讀 `docs/deploy/koyeb-paper.md` line 106 找到 `## G1 Smoke 驗證（Paper 1hr 跑完後）` 標題。在這標題**前**插入新段落：

```markdown
## G1 Self-Smoke Trigger（自動）

從 [Phase 4.1.x C1 redesign](../superpowers/specs/2026-05-21-g1-c1-continuity-redesign-design.md) 起，
daemon 在 `BFX_PHASE=paper` + `BFX_RUN_DURATION_HOURS` 設定下，
跑完 duration 後**自動執行 G1 smoke**：

1. daemon `_run()` finally block 完成（Axiom flush + http client close）
2. 等 30 秒讓 Axiom ingestion 完成 catch-up
3. 跑完整 C1-C6 check
4. Container exit code = G1 exit code

Container exit code 行為：

| Exit | 意義 | Koyeb 行為 |
|---|---|---|
| 0 | Daemon + G1 全 pass | Deploy 標 healthy，container 結束 |
| 1 | G1 fail（某 check fail） | Deploy 標 unhealthy，預設 restart loop（**feature** — 強制查 log） |
| 2 | Axiom auth/network fail | 同 1，cause 在 config 而非 daemon |
| 3 | Config error（env vars 漏設 / EDA range mismatch） | 同 1，cause 在 config |

⚠️ 若 G1 fail 進 restart loop，**先查 runtime log 找 fail 原因再放著 restart**，否則 burn quota 跑無效 cycle。

```

- [ ] **Step 2: Rename existing "## G1 Smoke 驗證" to "## G1 Smoke 手動驗證（debug 用）"**

把原 line 106 標題：

```markdown
## G1 Smoke 驗證（Paper 1hr 跑完後）
```

改成：

```markdown
## G1 Smoke 手動驗證（debug 用）
```

並在標題下、原 `等 paper container 因 BFX_RUN_DURATION_HOURS=1 自動退...` 之前加一段：

```markdown
> 通常**不需要手動跑** — 上方 Self-Smoke Trigger 已自動執行。本段用於：debug 失敗原因、重跑特定 check（`--only C1`）、或對歷史 window 跑 retroactive 檢查。

```

- [ ] **Step 3: Verify markdown renders cleanly**

```bash
cd /Users/will/second-brain/projects/startup/bfx-funding-bot
head -160 docs/deploy/koyeb-paper.md
```

肉眼確認：
- [ ] Self-Smoke Trigger section 在 G1 Smoke 手動驗證 之前
- [ ] 連結 `../superpowers/specs/2026-05-21-g1-c1-continuity-redesign-design.md` 路徑相對於 `docs/deploy/` 正確
- [ ] Table 對齊

- [ ] **Step 4: Commit**

```bash
cd /Users/will/second-brain/projects/startup/bfx-funding-bot
git add docs/deploy/koyeb-paper.md
git commit -m "$(cat <<'EOF'
📝 Docs: koyeb-paper.md Self-Smoke Trigger section

Document daemon auto-runs G1 smoke after paper duration. Container exit code
= G1 exit code; Koyeb restart loop on fail is intentional. Existing manual
smoke section demoted to "debug 用" with note that auto-trigger usually covers
the gate.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Final Verification

- [ ] **Step 1: Full test suite green**

```bash
cd backend_py && uv run pytest -m "not integration" -q
```

預期：whole suite PASS（含 7 新 C1 + 5 新 self-smoke tests，2 舊 C1 bucket tests 已刪）。

- [ ] **Step 2: mypy + ruff clean**

```bash
cd backend_py && uv run mypy src/ && uv run ruff check
```

預期：no errors.

- [ ] **Step 3: Manual smoke of CLI wrapper backwards-compat**

```bash
cd backend_py && uv run python scripts/g1_smoke_check.py --help
```

預期：argparse help 印出，沒 import error。

- [ ] **Step 4: Confirm 7 commits on main**

```bash
git log --oneline -8
```

預期看到：
```
... 📝 Docs: koyeb-paper.md Self-Smoke Trigger section
... 📝 Docs: mark Phase 4.1 G1 C1 spec section as superseded
... ✨ Feat: wire G1 self-smoke trigger into daemon._run()
... ✨ Feat: G1 self-smoke trigger helper at daemon exit
... ✨ Feat: G1 C1 continuity redesign (per-cell max-gap + recency)
... ♻️ Refactor: G1 smoke add run_smoke_async + now_fn injection seam
... ♻️ Refactor: extract G1 smoke check to bfx_funding_bot.smoke module
fbdd16c 📝 Docs: G1 C1 continuity redesign + self-smoke trigger spec
```

- [ ] **Step 5: Update wiki Pending list**

在 `~/second-brain/wiki/projects/bfx-funding-bot.md` Pending 區塊找到「G1 spec C1 continuity 重設計」項目，標 ✅ 完成並加日期 2026-05-21 + commit SHA。

實際內容 grep 確認後 hand-edit；若 wiki 未明列此項目可加一行 Lessons Learned：

```markdown
- **2026-05-21**: G1 C1 continuity meta-test bug（cells.yaml all-1h vs spec 5min bucket 鴿籠原理矛盾）。
  Redesigned per-cell max-gap + recency + daemon self-smoke trigger。
  Spec: docs/superpowers/specs/2026-05-21-g1-c1-continuity-redesign-design.md
```
