# Phase 3a FRR Unit Investigation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 解出 Bitfinex `funding_stats.frr` 數值到 per-day decimal rate 的單位轉換，並把 `BacktestConfig.market_rate_source="frr"` 通電。

**Architecture:** 三階段 gated flow：Stage 1 reference (3 sources × 30-min timeout) → Stage 2 empirical (5-hypothesis OLS regression script) → Gate 1 PASS check → Stage 3 implementation (conversion module + new typed `FundingCandleWithStats` + repo variant + engine wiring + diagnostic Phase 2 check)。Gate 1 FAIL 路徑只 ship research note，code 不 commit。

**Tech Stack:** Python 3.13 / SQLAlchemy 2.0 async / pydantic / numpy + pandas (Stage 2 dev dep) / pytest + pytest-asyncio / aiosqlite (test DB) / asyncpg (Neon).

**Spec:** `docs/superpowers/specs/2026-05-10-phase3a-frr-unit-investigation-design.md`

---

## Prerequisites

- gh user 必須是 **`Will413028`**（個人 startup repo），執行 `gh auth switch -u Will413028` 確認。從 root pull / push 前都要 check。
- `backend_py/.env` 是 repo root `.env` 的 symlink；Neon `DATABASE_URL` 來自其中。worktree 重建後要 `ln -sf ../.env backend_py/.env`。
- 所有 pytest / mypy / ruff / alembic 命令必須 `cd backend_py` 才會走 uv 管的 Python 3.13。
- Phase 2 已 ship（commit `8b289c8`），DB 內有 547,278 rows（fUSD funding_stats 83,904 + fUST funding_stats 64,708 + 6 candles series）。

## Branching

直接在 `main` 工作（per project conventions：daily commit、無長 PR）。每個 commit 獨立可 revert，commit emoji prefix 依 CLAUDE.md（`✨ Feat:` / `📝 Docs:` / `🔧 Chore:` / `♻️ Refactor:` / `🐛 Fix:`）。

## File Structure

**新增：**
- `docs/research/2026-05-10-frr-unit-investigation.md` — 研究 note（Stage 1 + Stage 2 結果）
- `backend_py/scripts/investigate_frr_unit.py` — Stage 2 一次性 OLS regression script
- `backend_py/data/research/.gitkeep` — 標記輸出目錄存在
- `backend_py/data/research/frr_hypothesis_results.json` — 跑 script 後產生（**不 commit 進 git**，加 `.gitignore`）
- `backend_py/src/bfx_funding_bot/modules/funding_stats/conversion.py` — `frr_to_daily_rate` + 常數（Stage 3 only）
- `backend_py/tests/modules/funding_stats/test_conversion.py` — 5 tests（Stage 3 only）
- `backend_py/tests/modules/candles/test_repository_with_funding_stats.py` — 4 tests（Stage 3 only）

**修改：**
- `backend_py/pyproject.toml` — 加 `numpy` / `pandas` 進 `[dependency-groups].dev`
- `backend_py/src/bfx_funding_bot/modules/candles/schemas.py` — 加 `FundingCandleWithStats` subclass（Stage 3 only）
- `backend_py/src/bfx_funding_bot/modules/candles/repository.py` — 加 `get_candles_with_funding_stats` 方法（Stage 3 only）
- `backend_py/src/bfx_funding_bot/modules/backtest/engine.py` — `_resolve_market_rate` 加 `"frr"` 分支 + 型別 gate（Stage 3 only）
- `backend_py/tests/modules/backtest/test_engine.py` — 加 2 tests（Stage 3 only）
- `backend_py/scripts/run_backtest.py` — 加 `--market-rate-source` CLI flag（Stage 3 only）
- `backend_py/src/bfx_funding_bot/modules/backfill/checks.py` — `check_frr_unit` 改名 `check_frr_unit_stability`（diagnostic-only）
- `backend_py/scripts/backfill_phase2.py` — 對應改 import + label 文字
- `backend_py/.gitignore` — 加 `backend_py/data/research/*.json`

**Implementation note**：spec 寫 "LATERAL JOIN" 但 sqlite 測試 DB 不支援 LATERAL；改用 Python-side bisect merge（兩 query：先 candles，再 funding_stats，bisect 對齊），同時相容 sqlite + postgres，效能也夠（O(n + m log m)，547K rows 仍秒級完成）。

---

## Stage 1: Reference Investigation

### Task 1.1: Scaffold research note + data dir

**Files:**
- Create: `docs/research/2026-05-10-frr-unit-investigation.md`
- Create: `backend_py/data/research/.gitkeep`
- Modify: `backend_py/.gitignore`（加 1 行）

- [ ] **Step 1: Create research note skeleton**

```bash
mkdir -p /Users/will/second-brain/projects/startup/bfx-funding-bot/docs/research
mkdir -p /Users/will/second-brain/projects/startup/bfx-funding-bot/backend_py/data/research
touch /Users/will/second-brain/projects/startup/bfx-funding-bot/backend_py/data/research/.gitkeep
```

- [ ] **Step 2: Write the research note skeleton**

Write `docs/research/2026-05-10-frr-unit-investigation.md`:

```markdown
# FRR Unit Investigation

**Date**: 2026-05-10
**Owner**: Will
**Spec**: `docs/superpowers/specs/2026-05-10-phase3a-frr-unit-investigation-design.md`
**Sample row anchor** (per `funding_stats/schemas.py` docstring):

```
mts=1778411100000, frr=1.12e-06, avg_period=94.98, candle.close (1h p2)=0.00014465
```

## 1. Problem statement

Phase 2 抽樣 `frr × 86400 / candle.close = 668.98`，推翻 5/9「FRR=秒利率」推測。
Phase 3a 走 reference-first → empirical-confirmation 流程解單位。

## 2. Stage 1 reference findings

### 2.1 bitfinex-api-py SDK

[FILL: Stage 1 task 1.2]

### 2.2 ccxt python source

[FILL: Stage 1 task 1.3]

### 2.3 Bitfinex Help Center

[FILL: Stage 1 task 1.4]

## 3. Stage 2 empirical results

[FILL: after Stage 2 runs]

## 4. Conversion factor with provenance

[FILL: only if Gate 1 PASS]

## 5. Caveats

[FILL: at note finalization]

## 6. Next: Phase 3b

[FILL: at note finalization]
```

- [ ] **Step 3: Add gitignore entry for JSON output**

Append to `backend_py/.gitignore` (read first to know current state):

```
# Phase 3a research script output (re-derivable from DB; not source-of-truth)
data/research/*.json
```

- [ ] **Step 4: Commit scaffold**

```bash
cd /Users/will/second-brain/projects/startup/bfx-funding-bot
git add docs/research/2026-05-10-frr-unit-investigation.md backend_py/data/research/.gitkeep backend_py/.gitignore
git commit -m "$(cat <<'EOF'
📝 Docs: scaffold Phase 3a research note + data/research dir

Skeleton for FRR unit investigation note (Stage 1+2 fill in following commits).
data/research/ kept via .gitkeep; JSON output ignored (re-derivable from DB).

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 1.2: Stage 1 source 1 — bitfinex-api-py SDK

**Files:**
- Modify: `docs/research/2026-05-10-frr-unit-investigation.md`（fill section 2.1）

- [ ] **Step 1: Pull bitfinex-api-py source**

```bash
cd /tmp
mkdir -p bitfinex-api-py-stage1 && cd bitfinex-api-py-stage1
pip download --no-deps --no-binary=:all: bitfinex-api-py 2>&1 | tail -3
ls -1 *.tar.gz | head -1   # capture filename for next step
tar -xzf bitfinex-api-py-*.tar.gz
```

If `pip download` fails or returns no source dist, fallback:

```bash
gh repo clone bitfinexcom/bitfinex-api-py /tmp/bitfinex-api-py-stage1/clone
```

**Hard timeout: 30 min on this step. If both fail, write `[NOT FOUND: pip + gh both failed]` into note section 2.1 and move to Task 1.3.**

- [ ] **Step 2: Find FundingStatistic class**

```bash
cd /tmp/bitfinex-api-py-stage1
grep -rn "FundingStatistic\|FRR\|frr\b" --include="*.py" . | head -30
```

Capture the file path + line numbers of the FundingStatistic serializer class. Read 30 lines above + below to capture docstring + field comments.

- [ ] **Step 3: Append findings to research note**

Edit `docs/research/2026-05-10-frr-unit-investigation.md` section 2.1. Replace `[FILL: Stage 1 task 1.2]` with：

```markdown
**Source**: bitfinex-api-py vX.Y.Z (downloaded YYYY-MM-DD)
**File**: <path>:<lines>

**Class signature** (verbatim):
\`\`\`python
[paste the FundingStatistic class + serializer]
\`\`\`

**Unit hint** (verbatim docstring or comment about FRR units):
[paste verbatim, or "no unit comment found"]

**Derived hypothesis weight**:
- H1 (per-day): [supports / refutes / silent]
- H2 (per-second): [supports / refutes / silent]
- H3 (per-period × avg_period): [supports / refutes / silent]
- H4 (per-period-second): [supports / refutes / silent]
- H5 (annualized / 365): [supports / refutes / silent]
```

If the SDK is silent on units, write "no unit comment found" — that's a valid finding, not a failure.

- [ ] **Step 4: No commit yet** — wait until Task 1.5 to consolidate Stage 1 commit.

---

### Task 1.3: Stage 1 source 2 — ccxt python source

**Files:**
- Modify: `docs/research/2026-05-10-frr-unit-investigation.md`（fill section 2.2）

- [ ] **Step 1: Fetch ccxt bitfinex.py from master**

```bash
gh api repos/ccxt/ccxt/contents/python/ccxt/bitfinex.py --jq '.content' | base64 -d > /tmp/ccxt_bitfinex.py
wc -l /tmp/ccxt_bitfinex.py    # sanity check it's >1000 lines
```

If `gh api` fails (rate limit / network), fallback `curl`:

```bash
curl -sSL "https://raw.githubusercontent.com/ccxt/ccxt/master/python/ccxt/bitfinex.py" -o /tmp/ccxt_bitfinex.py
wc -l /tmp/ccxt_bitfinex.py
```

**Hard timeout: 30 min.** If both fail, write `[NOT FOUND: gh api + curl failed]` and move on.

- [ ] **Step 2: Grep for FRR / LAST_APR / funding parsing**

```bash
grep -nE "frr|FRR|LAST_APR|FRR_AMOUNT|fetch_funding_rate|parseTicker" /tmp/ccxt_bitfinex.py | head -40
```

Capture the line numbers, then read the surrounding 30 lines for each match.

- [ ] **Step 3: Append findings to research note**

Edit `docs/research/2026-05-10-frr-unit-investigation.md` section 2.2. Replace `[FILL: Stage 1 task 1.3]` with：

```markdown
**Source**: ccxt master @ commit <SHA from gh api>
**File**: python/ccxt/bitfinex.py
**Relevant lines**: <list>

**FRR parser** (verbatim if exists):
\`\`\`python
[paste the FRR parsing snippet + comments]
\`\`\`

**Unit hint**:
[paste verbatim quote, or "no FRR-specific parsing in ccxt; only LAST_APR which is annualized %"]

**Derived hypothesis weight**:
- H1 (per-day): [supports / refutes / silent]
- H2 (per-second): [supports / refutes / silent]
- H3 (per-period × avg_period): [supports / refutes / silent]
- H4 (per-period-second): [supports / refutes / silent]
- H5 (annualized / 365): [supports / refutes / silent]
```

- [ ] **Step 4: No commit yet** — wait until Task 1.5.

---

### Task 1.4: Stage 1 source 3 — Bitfinex Help Center

**Files:**
- Modify: `docs/research/2026-05-10-frr-unit-investigation.md`（fill section 2.3）

- [ ] **Step 1: Curl Help Center with browser User-Agent**

WebFetch 在前期 brainstorm 已撞 403。用 `curl -A` 繞：

```bash
curl -sSL -A "Mozilla/5.0 (Macintosh; Intel Mac OS X 14.0) AppleWebKit/605.1.15" \
  "https://support.bitfinex.com/hc/en-us/articles/213919009-What-is-the-Bitfinex-Funding-Flash-Return-Rate" \
  -o /tmp/frr_article.html

curl -sSL -A "Mozilla/5.0 (Macintosh; Intel Mac OS X 14.0) AppleWebKit/605.1.15" \
  "https://support.bitfinex.com/hc/en-us/articles/115003284729-What-is-the-Bitfinex-Funding-FRR-Delta" \
  -o /tmp/frr_delta_article.html

ls -lh /tmp/frr_article.html /tmp/frr_delta_article.html
```

If file size < 1KB or contains "403" in head, fallback：

```bash
gh api 'https://support.bitfinex.com/hc/en-us/articles/213919009.json' 2>&1 | head -50
```

**Hard timeout: 30 min.** If both fail, write `[NOT FOUND: curl + gh api failed]` and move on.

- [ ] **Step 2: Extract article body text**

```bash
# Strip HTML to text-ish (rough but enough for definitions)
python3 -c "
from html.parser import HTMLParser
import sys
class T(HTMLParser):
    def __init__(self): super().__init__(); self.t=[]
    def handle_data(self, d): self.t.append(d)
p=T(); p.feed(open('/tmp/frr_article.html').read()); print(''.join(p.t))
" > /tmp/frr_article.txt

head -200 /tmp/frr_article.txt
```

- [ ] **Step 3: Append findings to research note**

Edit `docs/research/2026-05-10-frr-unit-investigation.md` section 2.3. Paste the FRR article's定義段（"FRR is the average of..."）+ FRR Delta 的定義段。Same hypothesis-weight rubric as Tasks 1.2-1.3.

If both articles 403'd, write the explicit finding：

```markdown
**Source**: Bitfinex Help Center articles 213919009 + 115003284729

**Status**: 403 Forbidden via WebFetch / curl (with browser UA) / gh api proxy.

**Best-available secondary source** (from earlier WebSearch results):
"FRR is based on the average of all active fixed-rate fundings weighted by
their amount, this rate updates once per hour"
(quoted in Bitfinex Help Center summary).

**Derived hypothesis weight**: silent on numerical units; only confirms FRR
is amount-weighted average across active fundings (relevant for Stage 2
methodology — we should NOT use unweighted rolling-mean(close) cross-check).
```

- [ ] **Step 4: No commit yet** — wait until Task 1.5.

---

### Task 1.5: Stage 1 commit

- [ ] **Step 1: Decide if Stage 2 enters confirm-only mode**

Read research note sections 2.1-2.3。Check：

- 是否任一 source 明確說 FRR formula（含時間單位 + 是否 require avg_period）？

If YES (rare, but possible) → Stage 2 在 Task 2.4 跑 script 時可選擇 `--confirm-only HYPOTHESIS_ID`。
If NO (most likely) → Stage 2 跑全 5 hypotheses。

寫一行 `**Stage 2 mode**: full / confirm-only` 進 research note section 2 開頭。

- [ ] **Step 2: Commit Stage 1 research findings**

```bash
cd /Users/will/second-brain/projects/startup/bfx-funding-bot
git add docs/research/2026-05-10-frr-unit-investigation.md
git commit -m "$(cat <<'EOF'
📝 Docs: Phase 3a Stage 1 reference findings — bitfinex-api-py / ccxt / Help Center

3 sources scanned (each with 30-min hard timeout). Stage 2 mode decision
recorded inline.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Stage 2: Empirical Hypothesis Testing

### Task 2.1: Add numpy / pandas as dev deps

**Files:**
- Modify: `backend_py/pyproject.toml`
- Modify: `backend_py/uv.lock`（auto-regenerated）

- [ ] **Step 1: Add deps via uv**

```bash
cd /Users/will/second-brain/projects/startup/bfx-funding-bot/backend_py
uv add --dev numpy pandas
```

Expected: `uv.lock` updates, `pyproject.toml` `[dependency-groups].dev` 多兩行。

- [ ] **Step 2: Verify install + import**

```bash
cd /Users/will/second-brain/projects/startup/bfx-funding-bot/backend_py
uv run python -c "import numpy, pandas; print(numpy.__version__, pandas.__version__)"
```

Expected: 兩個版本號印出（e.g. `2.2.x 2.2.x`）。

- [ ] **Step 3: Run existing tests to confirm no regression**

```bash
cd /Users/will/second-brain/projects/startup/bfx-funding-bot/backend_py
uv run pytest -m "not integration" -q
```

Expected: 全綠（既有 28+ tests）。

- [ ] **Step 4: Commit**

```bash
cd /Users/will/second-brain/projects/startup/bfx-funding-bot
git add backend_py/pyproject.toml backend_py/uv.lock
git commit -m "$(cat <<'EOF'
🔧 Chore: add numpy/pandas dev deps for FRR investigation script

Phase 3a Stage 2 needs OLS regression + per-year/per-symbol invariance
metrics; numpy + pandas only used by scripts/investigate_frr_unit.py
(one-off research), not production code.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 2.2: Write investigate_frr_unit.py

**Files:**
- Create: `backend_py/scripts/investigate_frr_unit.py`

> **Note**：spec 規定此 script 不寫 unit test（一次性 research）。正確性靠 deterministic JSON output schema + sample-row golden cross-check。Task 2.3 跑完會把結果嵌進 research note，人眼 review 數字合理性。

- [ ] **Step 1: Write script (~250 LOC)**

Write `backend_py/scripts/investigate_frr_unit.py`:

```python
"""scripts/investigate_frr_unit.py — Phase 3a Stage 2.

Pulls (mts, symbol, frr, avg_period, candle.close) sample from Neon by
JOINing funding_stats point-in-time (latest fs.mts <= candle.mts) with
funding_candles (1h p2). Fits 5 named hypotheses via OLS regression
(close = a × predictor + b), computes R² + per-year/per-symbol slope
invariance + median relative error.

Writes data/research/frr_hypothesis_results.json. Exit 0 if at least one
hypothesis passes Gate 1 (R²>0.99 + slope_cv<0.05 + slope_diff<0.05 +
median_rel_err<0.05); exit 1 if all fail.

See docs/research/2026-05-10-frr-unit-investigation.md for narrative.
"""
from __future__ import annotations

import asyncio
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Callable

import numpy as np
import pandas as pd
from sqlalchemy import text

from bfx_funding_bot.core.db import (
    make_engine,
    make_session_factory,
    session_scope,
)
from bfx_funding_bot.core.settings import Settings


HYPOTHESES: dict[str, dict] = {
    "H1": {
        "label": "frr is per-day rate",
        "predictor": lambda df: df["frr"],
        "requires_avg_period": False,
    },
    "H2": {
        "label": "frr is per-second rate",
        "predictor": lambda df: df["frr"] * 86400.0,
        "requires_avg_period": False,
    },
    "H3": {
        "label": "frr × avg_period (per-period rate)",
        "predictor": lambda df: df["frr"] * df["avg_period"],
        "requires_avg_period": True,
    },
    "H4": {
        "label": "frr × avg_period × 86400 (per-period-second hybrid)",
        "predictor": lambda df: df["frr"] * df["avg_period"] * 86400.0,
        "requires_avg_period": True,
    },
    "H5": {
        "label": "frr / 365 (annualized rate)",
        "predictor": lambda df: df["frr"] / 365.0,
        "requires_avg_period": False,
    },
}

GATE_R2_MIN = 0.99
GATE_SLOPE_CV_MAX = 0.05
GATE_SLOPE_DIFF_MAX = 0.05
GATE_MEDIAN_REL_ERR_MAX = 0.05

LOAD_SQL = """
SELECT
    c.mts AS mts,
    c.symbol AS symbol,
    (
        SELECT fs.frr FROM funding_stats fs
        WHERE fs.symbol = c.symbol AND fs.mts <= c.mts
        ORDER BY fs.mts DESC LIMIT 1
    ) AS frr,
    (
        SELECT fs.avg_period FROM funding_stats fs
        WHERE fs.symbol = c.symbol AND fs.mts <= c.mts
        ORDER BY fs.mts DESC LIMIT 1
    ) AS avg_period,
    c.close AS close
FROM funding_candles c
WHERE c.symbol IN ('fUSD', 'fUST')
  AND c.timeframe = '1h'
  AND c.period_agg = 'p2'
  AND c.close IS NOT NULL
ORDER BY c.mts ASC
"""


async def load_sample() -> pd.DataFrame:
    settings = Settings()
    engine = make_engine(settings)
    factory = make_session_factory(engine)
    try:
        async with session_scope(factory) as session:
            result = await session.execute(text(LOAD_SQL))
            rows = result.fetchall()
    finally:
        await engine.dispose()
    df = pd.DataFrame(rows, columns=["mts", "symbol", "frr", "avg_period", "close"])
    df = df.dropna(subset=["frr", "avg_period", "close"])
    df["frr"] = df["frr"].astype(float)
    df["avg_period"] = df["avg_period"].astype(float)
    df["close"] = df["close"].astype(float)
    df = df[(df["close"] > 0) & (df["frr"] > 0) & (df["avg_period"] > 0)]
    df["year"] = pd.to_datetime(df["mts"], unit="ms", utc=True).dt.year
    return df.reset_index(drop=True)


def fit_ols(predictor: np.ndarray, target: np.ndarray) -> tuple[float, float, float]:
    """Returns (slope, intercept, r2). NaN for any if input too small."""
    if len(predictor) < 2 or np.var(predictor) == 0:
        return float("nan"), float("nan"), float("nan")
    A = np.vstack([predictor, np.ones(len(predictor))]).T
    coef, *_ = np.linalg.lstsq(A, target, rcond=None)
    slope, intercept = float(coef[0]), float(coef[1])
    pred = slope * predictor + intercept
    ss_res = float(np.sum((target - pred) ** 2))
    ss_tot = float(np.sum((target - target.mean()) ** 2))
    r2 = 1.0 - ss_res / ss_tot if ss_tot > 0 else 0.0
    return slope, intercept, r2


def evaluate_hypothesis(
    hid: str,
    hdef: dict,
    df: pd.DataFrame,
) -> dict:
    predictor_fn: Callable = hdef["predictor"]
    pred_full = predictor_fn(df).to_numpy(dtype=float)
    target_full = df["close"].to_numpy(dtype=float)
    valid = (
        ~np.isnan(pred_full)
        & ~np.isnan(target_full)
        & ~np.isinf(pred_full)
        & ~np.isinf(target_full)
    )
    pred = pred_full[valid]
    target = target_full[valid]
    df_v = df.loc[valid].reset_index(drop=True)

    slope, intercept, r2 = fit_ols(pred, target)
    if np.isnan(slope):
        median_rel_err = float("nan")
    else:
        reconstructed = slope * pred + intercept
        denom = np.where(target != 0, np.abs(target), 1.0)
        median_rel_err = float(np.median(np.abs(target - reconstructed) / denom))

    # Per-year slope invariance
    per_year_slopes: dict[int, float] = {}
    for year, group in df_v.groupby("year"):
        if len(group) < 50:
            continue
        gp = predictor_fn(group).to_numpy(dtype=float)
        gt = group["close"].to_numpy(dtype=float)
        s, _, _ = fit_ols(gp, gt)
        if not np.isnan(s):
            per_year_slopes[int(year)] = s
    if per_year_slopes:
        arr = np.array(list(per_year_slopes.values()))
        mean_s = float(arr.mean())
        slope_cv = float(arr.std() / abs(mean_s)) if mean_s != 0 else float("nan")
    else:
        slope_cv = float("nan")

    # Per-symbol slope invariance
    per_symbol_slopes: dict[str, float] = {}
    for sym, group in df_v.groupby("symbol"):
        if len(group) < 50:
            continue
        gp = predictor_fn(group).to_numpy(dtype=float)
        gt = group["close"].to_numpy(dtype=float)
        s, _, _ = fit_ols(gp, gt)
        if not np.isnan(s):
            per_symbol_slopes[str(sym)] = s
    if len(per_symbol_slopes) >= 2:
        sym_arr = np.array(list(per_symbol_slopes.values()))
        mean_s = float(sym_arr.mean())
        slope_diff = float(
            (sym_arr.max() - sym_arr.min()) / abs(mean_s)
        ) if mean_s != 0 else float("nan")
    else:
        slope_diff = float("nan")

    pass_gate = bool(
        not np.isnan(r2)
        and r2 > GATE_R2_MIN
        and not np.isnan(slope_cv)
        and slope_cv < GATE_SLOPE_CV_MAX
        and not np.isnan(slope_diff)
        and slope_diff < GATE_SLOPE_DIFF_MAX
        and not np.isnan(median_rel_err)
        and median_rel_err < GATE_MEDIAN_REL_ERR_MAX
    )

    return {
        "id": hid,
        "predictor_label": hdef["label"],
        "requires_avg_period": hdef["requires_avg_period"],
        "r2": r2,
        "slope": slope,
        "intercept": intercept,
        "median_rel_err": median_rel_err,
        "per_year_slope_cv": slope_cv,
        "per_symbol_slope_diff": slope_diff,
        "per_year_slopes": {str(k): v for k, v in sorted(per_year_slopes.items())},
        "per_symbol_slopes": dict(sorted(per_symbol_slopes.items())),
        "pass_gate": pass_gate,
    }


def select_winner(results: list[dict]) -> dict | None:
    """Highest R² wins; tie-break by simpler predictor (H1 < H2 < H3 < H4 < H5)."""
    passing = [r for r in results if r["pass_gate"]]
    if not passing:
        return None
    passing.sort(key=lambda r: (-r["r2"], r["id"]))
    return passing[0]


async def amain() -> int:
    df = await load_sample()
    print(f"Loaded {len(df)} samples after dropna/positivity filter")
    print(f"Symbols: {df['symbol'].value_counts().to_dict()}")
    print(f"Year range: {int(df['year'].min())}–{int(df['year'].max())}")

    results = [evaluate_hypothesis(hid, hdef, df) for hid, hdef in HYPOTHESES.items()]
    winner = select_winner(results)

    output = {
        "generated_at_utc": datetime.now(UTC).isoformat(),
        "sample_size": int(len(df)),
        "symbols_used": sorted(df["symbol"].unique().tolist()),
        "year_range": [int(df["year"].min()), int(df["year"].max())],
        "gate_thresholds": {
            "r2_min": GATE_R2_MIN,
            "slope_cv_max": GATE_SLOPE_CV_MAX,
            "slope_diff_max": GATE_SLOPE_DIFF_MAX,
            "median_rel_err_max": GATE_MEDIAN_REL_ERR_MAX,
        },
        "hypotheses": results,
        "winning_hypothesis": winner["id"] if winner else None,
        "winning_factor": winner["slope"] if winner else None,
        "winning_intercept": winner["intercept"] if winner else None,
        "winning_requires_avg_period": (
            winner["requires_avg_period"] if winner else False
        ),
    }

    out_path = Path(__file__).parent.parent / "data" / "research" / "frr_hypothesis_results.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(output, indent=2, default=str))
    print(f"Wrote {out_path}")

    print()
    print("=== Hypothesis Results ===")
    for r in results:
        mark = "✓" if r["pass_gate"] else "✗"
        print(
            f"[{mark}] {r['id']} ({r['predictor_label']})"
            f" — R²={r['r2']:.4f}, slope_cv={r['per_year_slope_cv']:.4f},"
            f" slope_diff={r['per_symbol_slope_diff']:.4f},"
            f" median_rel_err={r['median_rel_err']:.4f}"
        )

    if winner:
        print()
        print(f"🎯 Winning hypothesis: {winner['id']} — {winner['predictor_label']}")
        print(f"   slope (conversion factor) = {winner['slope']:.10e}")
        print(f"   intercept                  = {winner['intercept']:.10e}")
        print(f"   requires avg_period        = {winner['requires_avg_period']}")
        return 0

    print()
    print("❌ Gate 1 FAIL — no hypothesis met all 4 thresholds.")
    print("   See data/research/frr_hypothesis_results.json for diagnostics.")
    return 1


def main() -> None:
    sys.exit(asyncio.run(amain()))


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Verify imports + lint**

```bash
cd /Users/will/second-brain/projects/startup/bfx-funding-bot/backend_py
uv run ruff check scripts/investigate_frr_unit.py
uv run mypy scripts/investigate_frr_unit.py
```

Expected: 都過。如有 mypy `Untyped function` warnings on lambda predictors，可以 `# type: ignore[arg-type]` 或改成 named functions。

- [ ] **Step 3: Smoke run with --help (no DB hit)**

```bash
cd /Users/will/second-brain/projects/startup/bfx-funding-bot/backend_py
uv run python -c "from scripts.investigate_frr_unit import HYPOTHESES, GATE_R2_MIN; print(list(HYPOTHESES.keys()), GATE_R2_MIN)"
```

Expected output: `['H1', 'H2', 'H3', 'H4', 'H5'] 0.99`

- [ ] **Step 4: Commit script**

```bash
cd /Users/will/second-brain/projects/startup/bfx-funding-bot
git add backend_py/scripts/investigate_frr_unit.py
git commit -m "$(cat <<'EOF'
✨ Feat: scripts/investigate_frr_unit.py — 5-hypothesis OLS regression

Phase 3a Stage 2 empirical investigation. Loads (frr, avg_period, close)
sample point-in-time joined from Neon, fits H1-H5 with OLS, computes R² +
per-year/per-symbol slope invariance + median rel err. Writes
data/research/frr_hypothesis_results.json. Exit 0 = Gate 1 PASS, 1 = FAIL.

No unit test (one-off research); correctness via JSON schema + Stage 3
sample-row golden test in funding_stats/test_conversion.py.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 2.3: Run Stage 2 + finalize research note

**Files:**
- Modify: `docs/research/2026-05-10-frr-unit-investigation.md`（fill section 3 + 4 + 5 + 6）
- Run-time output: `backend_py/data/research/frr_hypothesis_results.json`（gitignored）

- [ ] **Step 1: Run script against Neon**

```bash
cd /Users/will/second-brain/projects/startup/bfx-funding-bot/backend_py
uv run python scripts/investigate_frr_unit.py
echo "Exit code: $?"
```

Expected: exit 0 (Gate 1 PASS) or exit 1 (FAIL). Either way, `data/research/frr_hypothesis_results.json` 應產出。

- [ ] **Step 2: Inspect JSON**

```bash
cd /Users/will/second-brain/projects/startup/bfx-funding-bot/backend_py
cat data/research/frr_hypothesis_results.json | python3 -m json.tool | head -80
```

Capture：
- `winning_hypothesis`: H1 / H2 / H3 / H4 / H5 / null
- `winning_factor`（slope）
- `winning_requires_avg_period`
- 5 個 hypotheses 的 `r2 / slope_cv / slope_diff / median_rel_err`

- [ ] **Step 3: Fill research note section 3**

Edit `docs/research/2026-05-10-frr-unit-investigation.md` section 3：

```markdown
## 3. Stage 2 empirical results

**Sample**: <N> rows, fUSD + fUST, year range <START>-<END>.
**Generated**: <ISO timestamp from JSON>.

### 3.1 Hypothesis comparison

| ID | Predictor | R² | Per-year slope CV | Per-symbol slope diff | Median rel err | Pass |
|---|---|---|---|---|---|---|
| H1 | frr | <r2> | <cv> | <diff> | <err> | ✓/✗ |
| H2 | frr × 86400 | ... | ... | ... | ... | ✓/✗ |
| H3 | frr × avg_period | ... | ... | ... | ... | ✓/✗ |
| H4 | frr × avg_period × 86400 | ... | ... | ... | ... | ✓/✗ |
| H5 | frr / 365 | ... | ... | ... | ... | ✓/✗ |

### 3.2 Winning hypothesis

**Winner**: <H_X> — <predictor label>
**Conversion factor (slope)**: <slope_value>
**Intercept**: <intercept_value> （理想情況 ≈ 0；非 0 表示有 systematic offset）
**Requires `avg_period`**: <bool>

Tie-break rule: highest R²，相同 R² 取 simpler predictor（H1 > H2 > H3 > H4 > H5）。

### 3.3 Per-year stability

\`\`\`
<paste per_year_slopes from JSON, sorted by year>
\`\`\`

### 3.4 Per-symbol stability

\`\`\`
<paste per_symbol_slopes from JSON>
\`\`\`
```

If Gate 1 FAIL，把 3.2 改成：

```markdown
### 3.2 Verdict: Gate 1 FAIL

No hypothesis met all 4 thresholds (R²>0.99, slope_cv<5%, slope_diff<5%,
median_rel_err<5%).

**Best candidate**: <H_X> with R²=<value> (still failed on
<which_threshold>).

**Implication**: FRR is not a simple unit conversion of candle.close. Phase
3b proceeds with 4🟢 strategies only; 2🟡 (FRR-trend, SpikeDetect)推遲到
Phase 3c with extended hypothesis set (e.g. multivariate with funding_amount
ratio).
```

- [ ] **Step 4: Fill research note section 4 (only if Gate 1 PASS)**

```markdown
## 4. Conversion factor with provenance

\`\`\`python
# bfx_funding_bot.modules.funding_stats.conversion
FRR_TO_DAILY_RATE_FACTOR = Decimal("<slope_value>")
FRR_REQUIRES_AVG_PERIOD = <bool>
\`\`\`

**Derivation**:
- Source: `data/research/frr_hypothesis_results.json` (generated <ISO time>)
- Sample size: <N> rows
- Symbols: fUSD + fUST
- Year range: <START>-<END>

**Cross-check (golden sample)**: 用 `funding_stats/schemas.py` docstring 的
sample row（mts=1778411100000, frr=1.12e-06, avg_period=94.98, close=0.00014465）
代入：
- Predicted close = <slope> × <predictor(1.12e-06, 94.98)> + <intercept>
- Actual close = 0.00014465
- Relative error = <err>%

**Validity bounds (mts-bound)**:
- Verified valid for funding_stats rows with mts ∈ [<min>, <max>]
- 若 Bitfinex 改 FRR 計算（例如改成 amount-weighted 或加 hidden volume floor），
  此 factor 失效。Phase 2 backfill 的 `check_frr_unit_stability` 會 log R² drift。
```

If Gate 1 FAIL，section 4 寫：

```markdown
## 4. Conversion factor with provenance

**Status**: not derived (Gate 1 FAIL). conversion code 不 commit；
`market_rate_source="frr"` 引擎仍視為未通電。
```

- [ ] **Step 5: Fill research note sections 5 + 6**

```markdown
## 5. Caveats

- Stage 1 reference 三 source 結果（PASS/silent/FAIL）：[已寫於 2.1-2.3]
- Stage 2 sample 限 fUSD + fUST × 1h × p2；其他 timeframe / period_agg 未驗證
- 若有第三個 stablecoin（fEURT 等）上線，invariance 需重驗
- Conversion factor 的精度由 Stage 2 OLS 決定；Decimal 化時用 str(float) 保留 ~15 位有效數字

## 6. Next: Phase 3b

[Gate 1 PASS]
- Phase 3b 可全 6 候選策略（4🟢 + 2🟡）並行
- backtest engine `market_rate_source="frr"` 通電於 Phase 3a Stage 3
- 預期 backtest 結果中 fill_rate / spread distribution 與 Phase 1 candle_close
  proxy 結果應接近（不應劇變）

[Gate 1 FAIL]
- Phase 3b 只跑 4🟢（24 runs，candle_close proxy）
- 2🟡 推遲 Phase 3c：擴展 hypothesis 集（multivariate w/ funding_amount_used /
  funding_amount，考慮 lower-50% lifetime weighting）
```

- [ ] **Step 6: Commit Stage 2 results**

```bash
cd /Users/will/second-brain/projects/startup/bfx-funding-bot
git add docs/research/2026-05-10-frr-unit-investigation.md
git commit -m "$(cat <<'EOF'
📝 Docs: Phase 3a Stage 2 — empirical hypothesis verdict

5-hypothesis OLS regression run against Neon (<N> samples). Winning
hypothesis: <H_X> (R²=<value>, slope=<value>). [or "Gate 1 FAIL" if all
hypotheses missed thresholds]

JSON output kept locally (gitignored, re-derivable from DB).

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Gate 1 Decision Point

Read the script's exit code + `winning_hypothesis` field：

- **Exit 0 / `winning_hypothesis` non-null** → 走 Stage 3 PASS 路徑（Tasks 3.1-3.5）
- **Exit 1 / `winning_hypothesis` null** → 走 Stage 3' FAIL 路徑（Task 3'.1）

兩條路徑互斥。**不要兩條都跑**。

---

## Stage 3 (Gate 1 PASS): Implementation

### Task 3.1: TDD funding_stats/conversion.py

**Files:**
- Create: `backend_py/src/bfx_funding_bot/modules/funding_stats/conversion.py`
- Create: `backend_py/tests/modules/funding_stats/test_conversion.py`

> **Substitute throughout:** Replace `<SLOPE>` with the slope from JSON, `<REQUIRES_AVG_PERIOD>` with True/False per `winning_requires_avg_period`. Use `Decimal(str(float_value))` to preserve precision.

- [ ] **Step 1: Write all 5 tests**

Write `backend_py/tests/modules/funding_stats/test_conversion.py`:

```python
"""Tests for FRR-to-daily-rate conversion. Constants verified by Stage 2
empirical regression (see docs/research/2026-05-10-frr-unit-investigation.md).

Sample row anchor (per funding_stats/schemas.py docstring, observed
2026-05-10): mts=1778411100000, frr=1.12e-06, avg_period=94.98,
candle.close (1h p2) = 0.00014465.
"""
from decimal import Decimal

import pytest

from bfx_funding_bot.modules.funding_stats.conversion import (
    FRR_REQUIRES_AVG_PERIOD,
    FRR_TO_DAILY_RATE_FACTOR,
    frr_to_daily_rate,
)


def test_frr_to_daily_rate_uses_correct_factor() -> None:
    """Linear: output = factor × frr (when avg_period not required)."""
    if FRR_REQUIRES_AVG_PERIOD:
        pytest.skip("H3/H4 winner — covered by test_uses_avg_period")
    out = frr_to_daily_rate(Decimal("1.0e-06"))
    expected = Decimal("1.0e-06") * FRR_TO_DAILY_RATE_FACTOR
    assert out == expected


def test_frr_to_daily_rate_handles_none_avg_period_when_not_required() -> None:
    """If FRR_REQUIRES_AVG_PERIOD is False, avg_period arg is ignored."""
    if FRR_REQUIRES_AVG_PERIOD:
        pytest.skip("H3/H4 winner — None avg_period must raise")
    out_with = frr_to_daily_rate(Decimal("2.0e-06"), avg_period=Decimal("50.0"))
    out_without = frr_to_daily_rate(Decimal("2.0e-06"), avg_period=None)
    assert out_with == out_without


def test_frr_to_daily_rate_requires_avg_period_when_flagged() -> None:
    """If FRR_REQUIRES_AVG_PERIOD is True, None avg_period raises."""
    if not FRR_REQUIRES_AVG_PERIOD:
        pytest.skip("H1/H2/H5 winner — None avg_period is ignored")
    with pytest.raises(ValueError, match="avg_period"):
        frr_to_daily_rate(Decimal("1.0e-06"), avg_period=None)


def test_frr_to_daily_rate_zero_returns_zero() -> None:
    """frr=0 → output=0 regardless of avg_period or formula."""
    out = frr_to_daily_rate(Decimal("0"), avg_period=Decimal("95.0"))
    assert out == Decimal("0")


def test_frr_to_daily_rate_uses_sample_row_from_schema_docstring() -> None:
    """Golden cross-check using the sample row recorded in
    funding_stats/schemas.py module docstring (verified 2026-05-10):

        mts=1778411100000, frr=1.12e-06, avg_period=94.98
        candle.close (1h p2) = 0.00014465

    Reconstructed daily_rate must match candle.close within 5% relative
    error (Stage 2 Gate threshold).
    """
    sample_frr = Decimal("1.12e-06")
    sample_avg_period = Decimal("94.98")
    sample_close = Decimal("0.00014465")

    predicted = frr_to_daily_rate(sample_frr, avg_period=sample_avg_period)
    rel_err = abs(predicted - sample_close) / sample_close

    # 5% threshold matches Gate 1 median_rel_err_max
    assert rel_err < Decimal("0.05"), (
        f"Sample row reconstruction off by {float(rel_err):.4%}: "
        f"predicted={predicted}, actual={sample_close}"
    )
```

- [ ] **Step 2: Run tests, verify they fail (import error)**

```bash
cd /Users/will/second-brain/projects/startup/bfx-funding-bot/backend_py
uv run pytest tests/modules/funding_stats/test_conversion.py -v
```

Expected: 5 ERRORs — `ModuleNotFoundError: No module named 'bfx_funding_bot.modules.funding_stats.conversion'`.

- [ ] **Step 3: Implement conversion module**

Write `backend_py/src/bfx_funding_bot/modules/funding_stats/conversion.py`:

```python
"""FRR → per-day decimal rate conversion.

Derived empirically by Phase 3a Stage 2; see
`docs/research/2026-05-10-frr-unit-investigation.md` for hypothesis
comparison, cross-source confirmation status, and provenance.

**Validity bound**: Verified valid for funding_stats rows observed
through 2026-05-10 (sample mts ≈ 1778411100000). If Bitfinex changes
FRR semantics, `scripts/backfill_phase2.py::check_frr_unit_stability`
will surface R² drift in regular backfill logs — see appendix in
`docs/superpowers/specs/2026-05-10-phase2-result.md`.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Final

# Stage 2 OLS slope — winning hypothesis selected by R² + invariance gate.
FRR_TO_DAILY_RATE_FACTOR: Final[Decimal] = Decimal("<SLOPE>")

# True if winning hypothesis is H3 or H4 (predictor uses avg_period).
FRR_REQUIRES_AVG_PERIOD: Final[bool] = <REQUIRES_AVG_PERIOD>


def frr_to_daily_rate(
    frr: Decimal,
    avg_period: Decimal | None = None,
) -> Decimal:
    """Convert raw FRR (from /v2/funding/stats) to per-day decimal rate.

    Args:
        frr: Raw FRR value from funding_stats endpoint.
        avg_period: Average funding period (days) — required iff
            FRR_REQUIRES_AVG_PERIOD is True.

    Returns:
        Per-day decimal rate equivalent (same scale as candle.close for
        1h p2 funding candles).

    Raises:
        ValueError: If FRR_REQUIRES_AVG_PERIOD is True and avg_period is
            None.
    """
    if FRR_REQUIRES_AVG_PERIOD:
        if avg_period is None:
            raise ValueError(
                "frr_to_daily_rate requires avg_period for the winning "
                "hypothesis (H3 or H4); got None"
            )
        return frr * FRR_TO_DAILY_RATE_FACTOR * avg_period
    return frr * FRR_TO_DAILY_RATE_FACTOR
```

**Important**: substitute `<SLOPE>` with the actual slope value from `data/research/frr_hypothesis_results.json` (use `str(float_value)` to convert without precision loss). Substitute `<REQUIRES_AVG_PERIOD>` with `True` or `False` per JSON. Reference the JSON's `winning_intercept` — if intercept is non-trivial（e.g., > 1e-6），考慮把 conversion 改為 `frr * FACTOR + INTERCEPT`，並在 docstring + commit message 註明。

- [ ] **Step 4: Run tests, verify they pass**

```bash
cd /Users/will/second-brain/projects/startup/bfx-funding-bot/backend_py
uv run pytest tests/modules/funding_stats/test_conversion.py -v
```

Expected: 5 PASSED. Three of the tests skip based on `FRR_REQUIRES_AVG_PERIOD`; that is expected — the relevant 3-of-5 plus the always-applicable zero + golden-sample tests run.

If `test_frr_to_daily_rate_uses_sample_row_from_schema_docstring` fails (rel_err >= 5%), the conversion factor extracted from JSON does not pass the golden test. Inspect the JSON's `winning_factor` precision and confirm it matches what was committed; if so, the Stage 2 winner barely passed and the spec's Gate 1 was met but with edge-case tolerance — proceed but flag in research note section 5 caveats.

- [ ] **Step 5: Run typecheck + lint**

```bash
cd /Users/will/second-brain/projects/startup/bfx-funding-bot/backend_py
uv run mypy src/bfx_funding_bot/modules/funding_stats/conversion.py
uv run ruff check src/bfx_funding_bot/modules/funding_stats/conversion.py tests/modules/funding_stats/test_conversion.py
```

Expected: 都過。

- [ ] **Step 6: Commit**

```bash
cd /Users/will/second-brain/projects/startup/bfx-funding-bot
git add backend_py/src/bfx_funding_bot/modules/funding_stats/conversion.py backend_py/tests/modules/funding_stats/test_conversion.py
git commit -m "$(cat <<'EOF'
✨ Feat: funding_stats/conversion — frr_to_daily_rate

Phase 3a Stage 3 commit 3/6. Conversion factor derived empirically by
Stage 2 (winning hypothesis: <H_X>; slope=<value>; requires_avg_period=<bool>).

5 unit tests including golden sample-row cross-check using the docstring
anchor in funding_stats/schemas.py (mts=1778411100000).

See docs/research/2026-05-10-frr-unit-investigation.md for derivation.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 3.2: TDD candles repository — get_candles_with_funding_stats variant + new type

**Files:**
- Modify: `backend_py/src/bfx_funding_bot/modules/candles/schemas.py`（add subclass）
- Modify: `backend_py/src/bfx_funding_bot/modules/candles/repository.py`（add new method）
- Create: `backend_py/tests/modules/candles/test_repository_with_funding_stats.py`

- [ ] **Step 1: Write 4 failing tests**

Write `backend_py/tests/modules/candles/test_repository_with_funding_stats.py`:

```python
"""Tests for get_candles_with_funding_stats — point-in-time JOIN of
funding_candles with funding_stats (latest fs.mts <= candle.mts).
"""
from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from bfx_funding_bot.core.db import Base
from bfx_funding_bot.modules.candles.repository import (
    get_candles_with_funding_stats,
    upsert_candles,
)
from bfx_funding_bot.modules.candles.schemas import (
    FundingCandle,
    FundingCandleWithStats,
)
from bfx_funding_bot.modules.funding_stats.repository import upsert_funding_stats
from bfx_funding_bot.modules.funding_stats.schemas import FundingStat


@pytest.fixture
async def setup_schema(sqlite_engine: AsyncEngine):
    async with sqlite_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


def _candle(mts: int, close: str = "0.0001") -> FundingCandle:
    return FundingCandle(
        symbol="fUSD",
        timeframe="1h",
        period_agg="p2",
        mts=mts,
        open=Decimal(close),
        close=Decimal(close),
        high=Decimal(close),
        low=Decimal(close),
        volume=Decimal("100"),
    )


def _stat(mts: int, frr: str = "1.12e-6", avg_period: str = "94.98") -> FundingStat:
    return FundingStat(
        symbol="fUSD",
        mts=mts,
        frr=Decimal(frr),
        avg_period=Decimal(avg_period),
        funding_amount=Decimal("5000000000"),
        funding_amount_used=Decimal("4900000000"),
        funding_below_threshold=Decimal("100000000"),
    )


@pytest.mark.asyncio
async def test_joins_latest_funding_stat_at_or_before_candle_mts(
    sqlite_session: AsyncSession,
    setup_schema: None,
) -> None:
    """3 stats at t=100/200/300; candle at t=250 → frr/avg_period from t=200."""
    stats = [
        _stat(100, frr="1.0e-6", avg_period="50.0"),
        _stat(200, frr="2.0e-6", avg_period="60.0"),
        _stat(300, frr="3.0e-6", avg_period="70.0"),
    ]
    candles = [_candle(250)]
    await upsert_funding_stats(sqlite_session, stats)
    await upsert_candles(sqlite_session, candles)
    await sqlite_session.commit()

    result = await get_candles_with_funding_stats(
        sqlite_session,
        symbol="fUSD",
        timeframe="1h",
        period_agg="p2",
        start_mts=0,
        end_mts=400,
    )
    assert len(result) == 1
    assert result[0].frr == Decimal("2.0e-6")
    assert result[0].avg_period == Decimal("60.0")


@pytest.mark.asyncio
async def test_returns_none_frr_when_no_funding_stat_exists_yet(
    sqlite_session: AsyncSession,
    setup_schema: None,
) -> None:
    """Candle at t=50 with first funding_stat at t=100 → frr/avg_period None."""
    stats = [_stat(100)]
    candles = [_candle(50)]
    await upsert_funding_stats(sqlite_session, stats)
    await upsert_candles(sqlite_session, candles)
    await sqlite_session.commit()

    result = await get_candles_with_funding_stats(
        sqlite_session,
        symbol="fUSD", timeframe="1h", period_agg="p2",
        start_mts=0, end_mts=200,
    )
    assert len(result) == 1
    assert result[0].frr is None
    assert result[0].avg_period is None


@pytest.mark.asyncio
async def test_returns_funding_candle_with_stats_subclass(
    sqlite_session: AsyncSession,
    setup_schema: None,
) -> None:
    """Type contract — every result is FundingCandleWithStats (not base)."""
    await upsert_funding_stats(sqlite_session, [_stat(100)])
    await upsert_candles(sqlite_session, [_candle(150)])
    await sqlite_session.commit()

    result = await get_candles_with_funding_stats(
        sqlite_session,
        symbol="fUSD", timeframe="1h", period_agg="p2",
        start_mts=0, end_mts=200,
    )
    assert all(isinstance(r, FundingCandleWithStats) for r in result)


@pytest.mark.asyncio
async def test_empty_candle_range_returns_empty_list(
    sqlite_session: AsyncSession,
    setup_schema: None,
) -> None:
    await upsert_funding_stats(sqlite_session, [_stat(100)])
    await sqlite_session.commit()

    result = await get_candles_with_funding_stats(
        sqlite_session,
        symbol="fUSD", timeframe="1h", period_agg="p2",
        start_mts=500, end_mts=600,
    )
    assert result == []
```

- [ ] **Step 2: Run tests, verify failures**

```bash
cd /Users/will/second-brain/projects/startup/bfx-funding-bot/backend_py
uv run pytest tests/modules/candles/test_repository_with_funding_stats.py -v
```

Expected: 4 ERRORs — import errors for `FundingCandleWithStats` and `get_candles_with_funding_stats`.

- [ ] **Step 3: Add FundingCandleWithStats subclass**

Edit `backend_py/src/bfx_funding_bot/modules/candles/schemas.py`. After `FundingCandle` class, append：

```python
class FundingCandleWithStats(FundingCandle):
    """Candle joined with point-in-time funding_stats (latest fs.mts <= mts).

    Use this type ONLY when frr / avg_period are needed. Backtest engine
    enforces this via `isinstance` check on `market_rate_source='frr'`.

    `frr` / `avg_period` may still be None if no funding_stat row exists
    on or before this candle's mts (e.g., very early historical candles).
    """
    frr: Decimal | None = None
    avg_period: Decimal | None = None

    @field_validator("frr", "avg_period", mode="before")
    @classmethod
    def _coerce_stats_decimal(cls, v: Any) -> Decimal | None:
        return _to_decimal(v)
```

- [ ] **Step 4: Add get_candles_with_funding_stats to repository**

Edit `backend_py/src/bfx_funding_bot/modules/candles/repository.py`：

1. Add import at top:
```python
from bisect import bisect_right

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bfx_funding_bot.modules.candles.schemas import (
    FundingCandle,
    FundingCandleWithStats,
)
from bfx_funding_bot.modules.candles.tables import FundingCandleRow
from bfx_funding_bot.modules.funding_stats.tables import FundingStatRow
```

(merge with existing imports — don't duplicate `select` etc.)

2. Update `get_candles_in_range` docstring 加一行「See also」：
```python
async def get_candles_in_range(
    session: AsyncSession,
    *,
    symbol: str,
    timeframe: str,
    period_agg: str,
    start_mts: int,
    end_mts: int,
) -> list[FundingCandle]:
    """Fetch candles in [start_mts, end_mts] (inclusive) ordered by mts ASC.

    See also: `get_candles_with_funding_stats` for FRR-joined queries
    (point-in-time, returns FundingCandleWithStats).
    """
```

3. Append new method after `get_min_mts`:

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
    """Like get_candles_in_range but each candle is enriched with the
    latest `funding_stats.frr` / `funding_stats.avg_period` row whose
    `mts <= candle.mts` (point-in-time, no lookahead).

    Implementation: two queries + Python-side bisect merge (portable
    across sqlite test DB and Postgres production; LATERAL JOIN would
    be cleaner on Postgres but breaks sqlite). For 547K-row scale
    seen in Phase 2, performance is sub-second.

    `frr` / `avg_period` are None when no funding_stat exists on or
    before the candle's mts (early historical candles).
    """
    candles = await get_candles_in_range(
        session,
        symbol=symbol,
        timeframe=timeframe,
        period_agg=period_agg,
        start_mts=start_mts,
        end_mts=end_mts,
    )
    if not candles:
        return []

    # Pull all funding_stats rows up to the latest candle.mts in one query.
    fs_stmt = (
        select(FundingStatRow)
        .where(
            FundingStatRow.symbol == symbol,
            FundingStatRow.mts <= candles[-1].mts,
        )
        .order_by(FundingStatRow.mts.asc())
    )
    fs_result = await session.execute(fs_stmt)
    fs_rows = list(fs_result.scalars().all())
    fs_mts: list[int] = [r.mts for r in fs_rows]

    enriched: list[FundingCandleWithStats] = []
    for c in candles:
        # bisect_right(fs_mts, c.mts) - 1 = index of latest fs.mts <= c.mts;
        # -1 means no fs row at or before this candle.
        idx = bisect_right(fs_mts, c.mts) - 1
        if idx < 0:
            frr_val: Decimal | None = None
            avg_period_val: Decimal | None = None
        else:
            fs = fs_rows[idx]
            frr_val = Decimal(str(fs.frr)) if fs.frr is not None else None
            avg_period_val = (
                Decimal(str(fs.avg_period)) if fs.avg_period is not None else None
            )
        enriched.append(
            FundingCandleWithStats(
                symbol=c.symbol,
                timeframe=c.timeframe,
                period_agg=c.period_agg,
                mts=c.mts,
                open=c.open,
                close=c.close,
                high=c.high,
                low=c.low,
                volume=c.volume,
                frr=frr_val,
                avg_period=avg_period_val,
            )
        )
    return enriched
```

- [ ] **Step 5: Run tests, verify all 4 pass**

```bash
cd /Users/will/second-brain/projects/startup/bfx-funding-bot/backend_py
uv run pytest tests/modules/candles/test_repository_with_funding_stats.py -v
```

Expected: 4 PASSED.

- [ ] **Step 6: Run full pytest + mypy + ruff**

```bash
cd /Users/will/second-brain/projects/startup/bfx-funding-bot/backend_py
uv run pytest -m "not integration" -q
uv run mypy src/
uv run ruff check
```

Expected: 全綠。

- [ ] **Step 7: EXPLAIN ANALYZE on Neon (production sanity)**

```bash
cd /Users/will/second-brain/projects/startup/bfx-funding-bot/backend_py
uv run python -c "
import asyncio
from sqlalchemy import text
from bfx_funding_bot.core.db import make_engine, make_session_factory, session_scope
from bfx_funding_bot.core.settings import Settings

async def main():
    settings = Settings()
    engine = make_engine(settings)
    factory = make_session_factory(engine)
    try:
        async with session_scope(factory) as session:
            r = await session.execute(text('''
                EXPLAIN ANALYZE
                SELECT mts FROM funding_stats
                WHERE symbol = 'fUSD' AND mts <= 1778411100000
                ORDER BY mts DESC LIMIT 1
            '''))
            for row in r: print(row[0])
    finally:
        await engine.dispose()

asyncio.run(main())
"
```

Capture output. Expected: index scan on `idx_funding_stats_mts`，total time < 10ms。記下實際 plan 進 commit message。

- [ ] **Step 8: Commit**

```bash
cd /Users/will/second-brain/projects/startup/bfx-funding-bot
git add backend_py/src/bfx_funding_bot/modules/candles/schemas.py backend_py/src/bfx_funding_bot/modules/candles/repository.py backend_py/tests/modules/candles/test_repository_with_funding_stats.py
git commit -m "$(cat <<'EOF'
✨ Feat: candles repository — get_candles_with_funding_stats variant + new type

Phase 3a Stage 3 commit 4/6. New FundingCandleWithStats subclass + repo
method that JOINs candles point-in-time with the latest funding_stats row
whose mts <= candle.mts. Implementation uses two queries + Python bisect
merge (portable across sqlite test DB and Postgres; LATERAL JOIN would
break sqlite).

EXPLAIN ANALYZE on Neon: <plan summary, e.g., "Index Scan using
idx_funding_stats_mts, 0.5ms">.

4 new tests cover happy path, no-stat-yet edge case, type contract, and
empty range. Existing get_candles_in_range docstring updated with
"See also" pointer.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 3.3: TDD engine market_rate_source="frr" + run_backtest CLI flag

**Files:**
- Modify: `backend_py/src/bfx_funding_bot/modules/backtest/engine.py`
- Modify: `backend_py/tests/modules/backtest/test_engine.py`
- Modify: `backend_py/scripts/run_backtest.py`

- [ ] **Step 1: Write 2 failing tests**

Append to `backend_py/tests/modules/backtest/test_engine.py`:

```python
# ---------- Phase 3a frr branch ----------

from bfx_funding_bot.modules.backtest.config import BacktestConfig
from bfx_funding_bot.modules.candles.schemas import FundingCandleWithStats
from bfx_funding_bot.modules.funding_stats.conversion import (
    FRR_REQUIRES_AVG_PERIOD,
    FRR_TO_DAILY_RATE_FACTOR,
)


def _candle_with_stats(
    mts: int,
    close: str = "0.0001",
    frr: str | None = "1.0e-6",
    avg_period: str | None = "95.0",
) -> FundingCandleWithStats:
    return FundingCandleWithStats(
        symbol="fUSD",
        timeframe="1h",
        period_agg="p2",
        mts=mts,
        open=Decimal(close),
        close=Decimal(close),
        high=Decimal(close),
        low=Decimal(close),
        volume=Decimal("100"),
        frr=Decimal(frr) if frr is not None else None,
        avg_period=Decimal(avg_period) if avg_period is not None else None,
    )


def test_run_backtest_with_frr_source_uses_converted_rate() -> None:
    """frr branch happy path — strategy that bids exactly at the converted
    daily rate yields fill_prob=1; result is well-defined non-zero return."""
    candles = [_candle_with_stats(mts=i * 3_600_000) for i in range(48)]
    config = BacktestConfig(market_rate_source="frr")

    class FrrFollowingStrategy(Strategy):
        name = "frr_following"
        def decide(self, candle):
            if isinstance(candle, FundingCandleWithStats) and candle.frr is not None:
                from bfx_funding_bot.modules.funding_stats.conversion import (
                    frr_to_daily_rate,
                )
                kwargs = {}
                if FRR_REQUIRES_AVG_PERIOD:
                    kwargs["avg_period"] = candle.avg_period
                rate = frr_to_daily_rate(candle.frr, **kwargs)
                return LendDecision(mts=candle.mts, rate=rate, period_days=2)
            return None

    result = run_backtest(candles, FrrFollowingStrategy(), config=config)
    assert result.n_trades > 0
    assert result.fill_rate == Decimal("1")  # bidding at market → full fill
    assert result.net_monthly_return_pct > Decimal("0")


def test_run_backtest_with_frr_source_raises_on_plain_candle() -> None:
    """Engine type-gates frr source: plain FundingCandle raises ValueError."""
    plain_candles = [_candle_with_stats(mts=i * 3_600_000) for i in range(2)]
    # Convert to plain FundingCandle (drop frr/avg_period)
    plain = [
        FundingCandle(
            symbol=c.symbol, timeframe=c.timeframe, period_agg=c.period_agg,
            mts=c.mts, open=c.open, close=c.close, high=c.high, low=c.low,
            volume=c.volume,
        )
        for c in plain_candles
    ]
    config = BacktestConfig(market_rate_source="frr")
    strategy = AlwaysFRRStrategy(period_days=2)

    with pytest.raises(ValueError, match="FundingCandleWithStats"):
        run_backtest(plain, strategy, config=config)
```

(Imports `Strategy`, `LendDecision`, `FundingCandle`, `AlwaysFRRStrategy`, `pytest`, `Decimal` may already be in test_engine.py — adjust to dedupe; do not introduce duplicates.)

- [ ] **Step 2: Run tests, verify they fail**

```bash
cd /Users/will/second-brain/projects/startup/bfx-funding-bot/backend_py
uv run pytest tests/modules/backtest/test_engine.py -v -k "frr_source"
```

Expected: 2 FAILed — engine 還沒有 frr branch；第一個應 raise (`unsupported market_rate_source: 'frr'`)，第二個一樣。

- [ ] **Step 3: Implement frr branch in engine**

Edit `backend_py/src/bfx_funding_bot/modules/backtest/engine.py`. Replace `_resolve_market_rate`：

```python
from bfx_funding_bot.modules.candles.schemas import (
    FundingCandle,
    FundingCandleWithStats,
)
from bfx_funding_bot.modules.funding_stats.conversion import (
    FRR_REQUIRES_AVG_PERIOD,
    frr_to_daily_rate,
)


def _resolve_market_rate(
    candle: FundingCandle,
    source: str,
) -> Decimal | None:
    """Phase 1: candle_close. Phase 3a: frr (requires FundingCandleWithStats)."""
    if source == "candle_close":
        return candle.close
    if source == "frr":
        if not isinstance(candle, FundingCandleWithStats):
            raise ValueError(
                "market_rate_source='frr' requires FundingCandleWithStats; "
                "use candles.repository.get_candles_with_funding_stats(...) "
                "instead of get_candles_in_range(...)"
            )
        if candle.frr is None:
            return None  # caller falls through to instant-fill (no spread penalty)
        kwargs: dict[str, Decimal] = {}
        if FRR_REQUIRES_AVG_PERIOD:
            if candle.avg_period is None:
                return None  # missing avg_period → cannot resolve, instant-fill
            kwargs["avg_period"] = candle.avg_period
        return frr_to_daily_rate(candle.frr, **kwargs)
    raise ValueError(f"unsupported market_rate_source: {source!r}")
```

(Adjust imports — `Decimal` is already imported; `FundingCandle` already imported.)

- [ ] **Step 4: Run tests, verify they pass**

```bash
cd /Users/will/second-brain/projects/startup/bfx-funding-bot/backend_py
uv run pytest tests/modules/backtest/test_engine.py -v
```

Expected: 既有 + 2 new tests 全綠。

- [ ] **Step 5: Add --market-rate-source CLI flag to run_backtest.py**

Edit `backend_py/scripts/run_backtest.py`:

1. In `_parse_args`, add：

```python
p.add_argument(
    "--market-rate-source",
    choices=["candle_close", "frr"],
    default="candle_close",
    help="Source for market rate (default: candle_close, Phase 1 proxy)",
)
```

2. In `_amain`, replace the candle-loading block：

```python
    try:
        async with session_scope(session_factory) as session:
            if args.market_rate_source == "frr":
                from bfx_funding_bot.modules.candles.repository import (
                    get_candles_with_funding_stats,
                )
                candles = await get_candles_with_funding_stats(
                    session,
                    symbol=args.symbol,
                    timeframe=args.timeframe,
                    period_agg=args.period_agg,
                    start_mts=start_ms,
                    end_mts=end_ms,
                )
            else:
                candles = await get_candles_in_range(
                    session,
                    symbol=args.symbol,
                    timeframe=args.timeframe,
                    period_agg=args.period_agg,
                    start_mts=start_ms,
                    end_mts=end_ms,
                )
        logger.info("loaded %d candles from Neon (source=%s)",
                    len(candles), args.market_rate_source)
```

3. Pass config to `run_backtest`:

```python
        from bfx_funding_bot.modules.backtest.config import BacktestConfig
        config = BacktestConfig(market_rate_source=args.market_rate_source)
        strategy = AlwaysFRRStrategy(period_days=args.period_days)
        result = run_backtest(candles, strategy, config=config)
```

(Note: `BacktestConfig` import可能需要放到 module top depending on style; match existing pattern in test_engine.py.)

- [ ] **Step 6: Smoke run — candle_close (existing default) + frr against Neon**

```bash
cd /Users/will/second-brain/projects/startup/bfx-funding-bot/backend_py

# Existing default — should be unchanged
uv run python scripts/run_backtest.py --symbol fUSD --days-back 30

# Phase 3a new path
uv run python scripts/run_backtest.py --symbol fUSD --days-back 30 --market-rate-source frr
uv run python scripts/run_backtest.py --symbol fUST --days-back 30 --market-rate-source frr
```

Expected: 三次都 exit 0；第一次數字應跟之前 baseline 一致；後兩次印出 fill_rate / net_monthly_return_pct（不檢驗水位、只 sanity 不爆）。

記下兩個新數字（fUSD/fUST frr-source net_monthly_return_pct）進 commit message。

- [ ] **Step 7: Run full quality gate**

```bash
cd /Users/will/second-brain/projects/startup/bfx-funding-bot/backend_py
uv run pytest -m "not integration" -q
uv run mypy src/ scripts/run_backtest.py
uv run ruff check
```

Expected: 全綠。

- [ ] **Step 8: Commit**

```bash
cd /Users/will/second-brain/projects/startup/bfx-funding-bot
git add backend_py/src/bfx_funding_bot/modules/backtest/engine.py backend_py/tests/modules/backtest/test_engine.py backend_py/scripts/run_backtest.py
git commit -m "$(cat <<'EOF'
♻️ Refactor: backtest engine market_rate_source="frr" 通電 + CLI flag

Phase 3a Stage 3 commit 5/6. Engine _resolve_market_rate adds "frr" branch
that type-gates FundingCandleWithStats and applies frr_to_daily_rate. None
frr/avg_period falls through to instant-fill (no spread penalty).

run_backtest.py picks repo variant based on --market-rate-source. Default
candle_close unchanged — existing callers zero impact.

Sanity numbers (30-day window, AlwaysFRR p=2):
  fUSD frr-source: net_monthly_return_pct=<X>
  fUST frr-source: net_monthly_return_pct=<Y>

2 new engine tests cover happy path + type-gate.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 3.4: Refactor check_frr_unit → check_frr_unit_stability (diagnostic)

**Files:**
- Modify: `backend_py/src/bfx_funding_bot/modules/backfill/checks.py`（rewrite check_frr_unit）
- Modify: `backend_py/scripts/backfill_phase2.py`（rename + label）
- Modify: `docs/superpowers/specs/2026-05-10-phase2-result.md`（appendix）

- [ ] **Step 1: Rewrite check in checks.py**

Edit `backend_py/src/bfx_funding_bot/modules/backfill/checks.py`. Replace the `check_frr_unit` function（lines ~214-271）with:

```python
# ---------- 3. FRR unit stability (Phase 3a diagnostic) ----------

async def check_frr_unit_stability(
    session: AsyncSession,
    symbol: str = "fUSD",
    sample_size: int = 1000,
) -> CheckResult:
    """Diagnostic: sample N (frr, candle.close) pairs and check the Phase
    3a-derived conversion is still healthy via R² of the linear
    relationship close ~ a × predictor.

    Per Phase 3a spec, this check is **diagnostic-only** (always returns
    passed=True). State changes show up in the `message` text, not in
    the exit code — Bitfinex changing FRR semantics shouldn't block
    Phase 2 backfill from shipping.

    See `bfx_funding_bot.modules.funding_stats.conversion` for the
    derived constants and `docs/research/2026-05-10-frr-unit-investigation.md`
    for derivation methodology.
    """
    from bfx_funding_bot.modules.funding_stats.conversion import (
        FRR_REQUIRES_AVG_PERIOD,
        FRR_TO_DAILY_RATE_FACTOR,
    )

    # Pull joined sample (latest fs.mts <= candle.mts).
    sql = text("""
        SELECT c.mts, fs.frr, fs.avg_period, c.close
        FROM funding_candles c
        CROSS JOIN LATERAL (
            SELECT frr, avg_period FROM funding_stats fs2
            WHERE fs2.symbol = c.symbol AND fs2.mts <= c.mts
            ORDER BY fs2.mts DESC LIMIT 1
        ) fs
        WHERE c.symbol = :symbol
          AND c.timeframe = '1h' AND c.period_agg = 'p2'
          AND c.close IS NOT NULL AND c.close > 0
          AND fs.frr IS NOT NULL AND fs.frr > 0
        ORDER BY c.mts DESC
        LIMIT :limit
    """)
    # NOTE: This query uses LATERAL JOIN which Postgres supports but sqlite
    # does not. This check only runs against Neon (production); unit tests
    # for the check itself are intentionally absent (per Phase 3a spec —
    # diagnostic, not core logic).
    try:
        result = await session.execute(
            sql, {"symbol": symbol, "limit": sample_size},
        )
        rows = result.fetchall()
    except Exception as e:
        return CheckResult(
            passed=True,
            message=f"check_frr_unit_stability skipped: query failed ({e!r})",
        )

    if len(rows) < 50:
        return CheckResult(
            passed=True,
            message=(
                f"check_frr_unit_stability skipped: only {len(rows)} samples "
                f"(< 50 required for stable r²)"
            ),
        )

    # Compute reconstruction r² using committed FRR factor.
    factor = float(FRR_TO_DAILY_RATE_FACTOR)
    n_total = 0
    ss_res = 0.0
    targets: list[float] = []
    for _mts, frr_val, avg_period, close in rows:
        if frr_val is None or close is None or close <= 0:
            continue
        if FRR_REQUIRES_AVG_PERIOD and (avg_period is None or avg_period <= 0):
            continue
        predicted = float(frr_val) * factor
        if FRR_REQUIRES_AVG_PERIOD:
            predicted *= float(avg_period)
        actual = float(close)
        ss_res += (actual - predicted) ** 2
        targets.append(actual)
        n_total += 1

    if n_total < 50:
        return CheckResult(
            passed=True,
            message=(
                f"check_frr_unit_stability skipped: only {n_total} valid "
                f"samples after filtering"
            ),
        )

    import statistics
    mean_target = statistics.mean(targets)
    ss_tot = sum((t - mean_target) ** 2 for t in targets)
    r2 = 1.0 - ss_res / ss_tot if ss_tot > 0 else 0.0

    return CheckResult(
        passed=True,
        message=(
            f"check_frr_unit_stability: r²={r2:.4f} on {n_total} samples "
            f"(factor={factor:.6e}, requires_avg_period={FRR_REQUIRES_AVG_PERIOD}). "
            f"{'OK' if r2 > 0.95 else '⚠️ DRIFT — re-run Phase 3a Stage 2'}"
        ),
    )
```

Also remove (or keep but unused) the `FRR_SECONDS_PER_DAY`, `FRR_RATIO_LOW`, `FRR_RATIO_HIGH` constants if they were defined for the old check — `grep -n "FRR_SECONDS_PER_DAY\|FRR_RATIO_LOW\|FRR_RATIO_HIGH" backend_py/src/bfx_funding_bot/modules/backfill/checks.py` and remove unused.

- [ ] **Step 2: Update backfill_phase2.py to use new check name**

Edit `backend_py/scripts/backfill_phase2.py` lines 41-44 (the imports):

```python
from bfx_funding_bot.modules.backfill.checks import (
    check_continuity,
    check_frr_unit_stability,
    check_round_trip,
    check_row_counts,
)
```

Lines ~172 (call site):

```python
            fr = await check_frr_unit_stability(session, symbol="fUSD")
```

Lines ~177 (label):

```python
            for label, res in [
                ("row_count > 0", rc),
                ("round-trip exact-match", rt),
                ("FRR unit stability (diagnostic)", fr),
                ("continuity", ct),
            ]:
```

- [ ] **Step 3: Run pytest + mypy + ruff**

```bash
cd /Users/will/second-brain/projects/startup/bfx-funding-bot/backend_py
uv run pytest -m "not integration" -q
uv run mypy src/ scripts/backfill_phase2.py
uv run ruff check
```

Expected: 全綠。注意現有 `backfill/test_checks.py` 如果有 `test_check_frr_unit` 測試 — read 一下，把命名改對應。如果該測試 assert old behavior（ratio in [0.1, 10]），刪掉那測試 → spec 已寫此 check 改 diagnostic。

```bash
grep -n "check_frr_unit" backend_py/tests/modules/backfill/test_checks.py 2>/dev/null
```

如果有：刪掉舊 test，spec 不要求新 test（diagnostic-only check 不在測試覆蓋目標）。

- [ ] **Step 4: Run backfill_phase2 dry-run**

```bash
cd /Users/will/second-brain/projects/startup/bfx-funding-bot/backend_py
uv run python scripts/backfill_phase2.py --dry-run
```

Expected: exit 0、印 `✅ Dry-run PASS`。

- [ ] **Step 5: Optional — full Phase 2 re-run for end-to-end verification**

只有在你想確認 production 路徑 OK 才跑（會花 1-2 分鐘 resume backfill）：

```bash
cd /Users/will/second-brain/projects/startup/bfx-funding-bot/backend_py
uv run python scripts/backfill_phase2.py 2>&1 | tee /tmp/phase2_rerun.log
```

Expected: 4 PASS checks 全 ✓、`FRR unit stability (diagnostic)` 印出 r² 數字。

- [ ] **Step 6: Update phase2-result.md appendix**

Append to `docs/superpowers/specs/2026-05-10-phase2-result.md`:

```markdown
## Appendix — Phase 3a evolution of `check_frr_unit`

Original check（`scripts/backfill_phase2.py` 跑 frr × 86400 / candle.close
ratio in [0.1, 10] sanity range）已被 Phase 3a 取代。新 check
`check_frr_unit_stability` 跑 R² 對 Phase 3a-derived FRR conversion
（`modules/funding_stats/conversion.py`）做 reconstruction，作為
**diagnostic-only**（永遠 passed=True）監測 Bitfinex API 是否有 silent
unit drift。

Phase 3a 結果：[Gate 1 PASS — 寫入 winning hypothesis / Gate 1 FAIL —
寫 raw r² 版本]
```

- [ ] **Step 7: Commit**

```bash
cd /Users/will/second-brain/projects/startup/bfx-funding-bot
git add backend_py/src/bfx_funding_bot/modules/backfill/checks.py backend_py/scripts/backfill_phase2.py backend_py/tests/modules/backfill/test_checks.py docs/superpowers/specs/2026-05-10-phase2-result.md
git commit -m "$(cat <<'EOF'
🔧 Chore: backfill check_frr_unit → check_frr_unit_stability (diagnostic)

Phase 3a Stage 3 commit 6/6. Old single-sample [0.1, 10] ratio check
(refuted by Phase 2 result with ratio=668.98) replaced with R²-based
reconstruction sanity using Phase 3a conversion factor.

Behaviour changes from old check:
- Always passed=True (diagnostic, doesn't block backfill exit code)
- Logs r² + factor + drift warning when r² drops below 0.95
- Uses LATERAL JOIN (Postgres-only); sqlite tests skipped per spec

Phase 2 result spec gets an appendix documenting the evolution.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 3.5: Wiki Lessons Learned (PASS variant)

**Files:**
- Modify: `~/second-brain/wiki/projects/bfx-funding-bot.md`（不同 git repo）

> **Note**: This commits to the second-brain parent repo, NOT bfx-funding-bot. Per CLAUDE.md, second-brain commits go in once a day (`daily: YYYY-MM-DD <summary>`). This step **prepares** the line; commit is part of end-of-day daily commit, not a per-task commit.

- [ ] **Step 1: Add Lessons Learned line**

Read `~/second-brain/wiki/projects/bfx-funding-bot.md` — find the Lessons Learned section. Append a row。 Use the actual values from `data/research/frr_hypothesis_results.json`：

```markdown
- **2026-05-10 Phase 3a — FRR unit resolved**: H<X> (slope=<value>,
  requires_avg_period=<bool>; per-year CV=<value>, fUSD/fUST diff=<value>).
  Conversion factor in `modules/funding_stats/conversion.py`. Phase 3b
  cleared to use full 6-strategy matrix.
```

- [ ] **Step 2: Don't commit yet** — folded into end-of-day second-brain daily commit per CLAUDE.md.

---

## Stage 3' (Gate 1 FAIL): Alternative path

> **Use only if Stage 2 exited 1 (no winning hypothesis). Do NOT do this AND Stage 3.**

### Task 3'.1: backfill check_frr_unit → diagnostic raw r² (no conversion factor)

**Files:**
- Modify: `backend_py/src/bfx_funding_bot/modules/backfill/checks.py`
- Modify: `backend_py/scripts/backfill_phase2.py`
- Modify: `docs/superpowers/specs/2026-05-10-phase2-result.md`

- [ ] **Step 1: Rewrite check (raw r² only, no conversion module dependency)**

Edit `backend_py/src/bfx_funding_bot/modules/backfill/checks.py`. Replace `check_frr_unit` with：

```python
# ---------- 3. FRR raw correlation diagnostic (Phase 3a Gate 1 FAIL fallback) ----------

async def check_frr_unit_stability(
    session: AsyncSession,
    symbol: str = "fUSD",
    sample_size: int = 1000,
) -> CheckResult:
    """Diagnostic-only raw correlation: r² of close ~ a × frr (no
    avg_period weighting) on N most-recent samples.

    Phase 3a Gate 1 FAIL: no winning hypothesis was committed. This
    check is a placeholder until Phase 3c re-investigates with extended
    hypothesis set. Always returns passed=True.
    """
    sql = text("""
        SELECT c.mts, fs.frr, c.close
        FROM funding_candles c
        CROSS JOIN LATERAL (
            SELECT frr FROM funding_stats fs2
            WHERE fs2.symbol = c.symbol AND fs2.mts <= c.mts
            ORDER BY fs2.mts DESC LIMIT 1
        ) fs
        WHERE c.symbol = :symbol
          AND c.timeframe = '1h' AND c.period_agg = 'p2'
          AND c.close IS NOT NULL AND c.close > 0
          AND fs.frr IS NOT NULL AND fs.frr > 0
        ORDER BY c.mts DESC
        LIMIT :limit
    """)
    try:
        result = await session.execute(sql, {"symbol": symbol, "limit": sample_size})
        rows = result.fetchall()
    except Exception as e:
        return CheckResult(passed=True, message=f"FRR raw r² skipped: {e!r}")

    pairs = [(float(frr), float(close)) for _mts, frr, close in rows
             if frr is not None and close is not None and float(close) > 0]
    if len(pairs) < 50:
        return CheckResult(
            passed=True,
            message=f"FRR raw r² skipped: only {len(pairs)} valid samples",
        )

    import statistics
    xs = [p[0] for p in pairs]
    ys = [p[1] for p in pairs]
    mean_x, mean_y = statistics.mean(xs), statistics.mean(ys)
    s_xy = sum((x - mean_x) * (y - mean_y) for x, y in pairs)
    s_xx = sum((x - mean_x) ** 2 for x in xs)
    s_yy = sum((y - mean_y) ** 2 for y in ys)
    r2 = (s_xy ** 2) / (s_xx * s_yy) if s_xx > 0 and s_yy > 0 else 0.0

    return CheckResult(
        passed=True,
        message=(
            f"FRR raw r² (Gate 1 FAIL fallback): r²={r2:.4f} on {len(pairs)} "
            f"samples. Phase 3a no winning hypothesis; conversion not committed."
        ),
    )
```

- [ ] **Step 2: Same backfill_phase2.py update as Task 3.4 Step 2**（import name + label string）.

- [ ] **Step 3: Update phase2-result.md appendix（FAIL variant）**

```markdown
## Appendix — Phase 3a evolution of `check_frr_unit`

Phase 3a 結果：**Gate 1 FAIL**。所有 5 個 hypothesis (H1-H5) 都未通過
R²>0.99 + slope_cv<5% + slope_diff<5% + median_rel_err<5% 的 quad-gate。

`check_frr_unit` 改名 `check_frr_unit_stability`，改成 raw r²(close ~ frr)
correlation 診斷（diagnostic-only，永遠 passed=True）。完整失敗診斷見
`data/research/frr_hypothesis_results.json` 與
`docs/research/2026-05-10-frr-unit-investigation.md` section 3.2。

下一輪：Phase 3c 擴展 hypothesis 集（multivariate w/ funding_amount_used /
funding_amount, lower-50% lifetime weighting）。
```

- [ ] **Step 4: Quality gate**

```bash
cd /Users/will/second-brain/projects/startup/bfx-funding-bot/backend_py
uv run pytest -m "not integration" -q
uv run mypy src/
uv run ruff check
```

Expected: 全綠。

- [ ] **Step 5: Commit**

```bash
cd /Users/will/second-brain/projects/startup/bfx-funding-bot
git add backend_py/src/bfx_funding_bot/modules/backfill/checks.py backend_py/scripts/backfill_phase2.py backend_py/tests/modules/backfill/test_checks.py docs/superpowers/specs/2026-05-10-phase2-result.md
git commit -m "$(cat <<'EOF'
🔧 Chore: Phase 3a Gate 1 FAIL — check_frr_unit → raw r² diagnostic

All 5 hypotheses (H1-H5) missed Gate 1 thresholds. conversion module not
committed; market_rate_source="frr" remains un-wired. Phase 3b will run
4🟢 strategies only with candle_close proxy; 2🟡 (FRR-trend, SpikeDetect)
deferred to Phase 3c with extended hypothesis set.

check_frr_unit replaced with raw r²(close ~ frr) diagnostic (always passes;
logs r² for ongoing visibility).

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

- [ ] **Step 6: Wiki Lessons Learned line (FAIL variant)**

Edit `~/second-brain/wiki/projects/bfx-funding-bot.md`：

```markdown
- **2026-05-10 Phase 3a — FRR unit unresolved**: 5 hypothesis (H1-H5) 全
  missed Gate 1。Best candidate: H<X> R²=<value>。Conversion code not
  shipped; `market_rate_source="frr"` 仍未通電。Phase 3b 限 4🟢 (24 runs)，
  2🟡 推遲 Phase 3c。
```

Don't commit (folds into end-of-day daily commit per CLAUDE.md).

---

## Final Verification

完成 Stage 3 (PASS 路徑) 或 Stage 3' (FAIL 路徑) 任一後跑：

- [ ] **Step 1: Full pytest**

```bash
cd /Users/will/second-brain/projects/startup/bfx-funding-bot/backend_py
uv run pytest -m "not integration" -v
```

Expected: 全綠。Stage 3 PASS 路徑下總 tests = 既有 + 5 (conversion) + 4 (repo with stats) + 2 (engine frr branch) = 既有 + 11。FAIL 路徑下 = 既有（不變）。

- [ ] **Step 2: mypy + ruff**

```bash
cd /Users/will/second-brain/projects/startup/bfx-funding-bot/backend_py
uv run mypy src/ scripts/
uv run ruff check
```

Expected: 都過。

- [ ] **Step 3: alembic check (no schema drift)**

```bash
cd /Users/will/second-brain/projects/startup/bfx-funding-bot/backend_py
uv run alembic check
```

Expected: `No new upgrade operations detected.` Phase 3a 沒改 ORM table，schema 不應 drift。

- [ ] **Step 4: Stage 3 PASS smoke — backtest with frr**

```bash
cd /Users/will/second-brain/projects/startup/bfx-funding-bot/backend_py
uv run python scripts/run_backtest.py --symbol fUSD --days-back 90 --market-rate-source frr
uv run python scripts/run_backtest.py --symbol fUST --days-back 90 --market-rate-source frr
```

Expected: 兩個 exit 0。`fill_rate` 應 > 0、`net_monthly_return_pct` 應為 finite 數字。

(Skip this step if Gate 1 FAIL — `--market-rate-source frr` 會 raise type-gate error，符合預期。)

- [ ] **Step 5: Stage 3 PASS smoke — backfill_phase2 with new diagnostic**

```bash
cd /Users/will/second-brain/projects/startup/bfx-funding-bot/backend_py
uv run python scripts/backfill_phase2.py
```

Expected: 4 PASS checks 全 ✓ (or ✓ for diagnostic 改的)；`FRR unit stability (diagnostic)` 印出 r² 數字。Total runtime ~30-60 秒（resume mode，Phase 2 已 backfilled）。

- [ ] **Step 6: Confirm git history**

```bash
cd /Users/will/second-brain/projects/startup/bfx-funding-bot
git log --oneline -10
```

Expected commits（Stage 3 PASS）:
```
<sha> 🔧 Chore: backfill check_frr_unit → check_frr_unit_stability (diagnostic)
<sha> ♻️ Refactor: backtest engine market_rate_source="frr" 通電 + CLI flag
<sha> ✨ Feat: candles repository — get_candles_with_funding_stats variant + new type
<sha> ✨ Feat: funding_stats/conversion — frr_to_daily_rate
<sha> 📝 Docs: Phase 3a Stage 2 — empirical hypothesis verdict
<sha> ✨ Feat: scripts/investigate_frr_unit.py — 5-hypothesis OLS regression
<sha> 🔧 Chore: add numpy/pandas dev deps for FRR investigation script
<sha> 📝 Docs: Phase 3a Stage 1 reference findings ...
<sha> 📝 Docs: scaffold Phase 3a research note ...
2bb9938 📝 Docs: v2 Phase 3a FRR unit investigation design
```

(Gate 1 FAIL: replaces last 4 commits with single Stage 3' commit.)

- [ ] **Step 7: Append second-brain wiki line + end-of-day daily commit**

In `~/second-brain` repo (separate git history):

```bash
cd ~/second-brain
git add wiki/projects/bfx-funding-bot.md
git commit -m "daily: 2026-05-10 Phase 3a FRR unit <resolved/unresolved>"
git push
```

Per CLAUDE.md: one commit per day, `daily: YYYY-MM-DD <summary>` format, push to private GitHub repo.

---

## Self-Review Notes

(For the writer of this plan — pre-commit checklist on the plan itself.)

1. **Spec coverage cross-check**:
   - ✓ Stage 1 (Tasks 1.1-1.5) covers spec Architecture §Stage 1
   - ✓ Stage 2 (Tasks 2.1-2.3) covers spec §Stage 2 + 5 hypothesis registry
   - ✓ Gate 1 decision point matches spec §Gate 1 PASS conditions (4 thresholds)
   - ✓ Stage 3 PASS (Tasks 3.1-3.5) covers spec §Components + §Commit Plan commits 3-6
   - ✓ Stage 3' FAIL (Task 3'.1) covers spec §Commit Plan FAIL fallback
   - ✓ Final Verification covers spec §Verification PASS conditions

2. **Type consistency**:
   - `FundingCandleWithStats` introduced Task 3.2 step 3, used in Task 3.3 step 1 — matches.
   - `frr_to_daily_rate(frr, avg_period=None)` defined Task 3.1 step 3, called Task 3.3 step 3 + Task 3.4 step 1 — matches.
   - `FRR_REQUIRES_AVG_PERIOD` boolean — used consistently as gate check.

3. **No placeholder smell**:
   - `<SLOPE>` / `<REQUIRES_AVG_PERIOD>` / `<H_X>` / `<value>` 等是 **runner 從 Stage 2 JSON 抄入的具體值** 標記，不是 TBD/TODO 漏寫；步驟明確指示從哪裡取值。
   - Conversion module 的 `Decimal("<SLOPE>")` 在 Task 3.1 step 3 註明「用 `str(float_value)` 轉 Decimal 保留精度」。

4. **Gate behaviour**:
   - 全程 Gate 1 PASS / FAIL 兩條互斥路徑明確分隔；Final Verification step 4 + step 7 都對 Gate 1 FAIL 給 "skip this step" 指引。
