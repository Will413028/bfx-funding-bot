# E3 Measurement Automation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 G3 從「手動一次性腳本、停在 05-31 INSUFFICIENT_DATA」變成每週自動儀表：weekly systemd timer 產 G3 報告（含新 AlwaysFRR benchmark arm + fee-adjusted 數字）、per-cell 每週 fee-adjusted realized APR 落 DB 表、webapi endpoint 餵前端三線圖（bot / always-close / AlwaysFRR）— 修 2026-07-06 profit review 的第三號 finding（量測斷線）。

**Architecture:** 全部 read-only over `event_log`/`diagnostics`（零 invariant 風險），只寫自己的新表 `attribution_weekly`。純計算層在 `modules/live_validation/`（`live_attribution.py` 加 AlwaysFRR arm 素材；新 `weekly_attribution.py` 做 UTC-Monday calendar-week per-cell 分箱 + 15% fee），I/O 層在 `scripts/`（`_g3_loaders.py` 擴充 + 新 `run_weekly_attribution.py`），呈現層 = G3 markdown/JSON 報告 + webapi `GET /api/v1/attribution/weekly` + FE `(dashboard)/attribution` 頁（recharts 三線圖）。排程 = VM systemd timer 每週跑 compose one-shot container（`bfx-pg-backup.timer` 同型 pattern；Postgres internal-only，只能在 docker network 內跑）。

**Tech Stack:** Python 3.13 + uv、pytest（asyncio auto）、mypy strict、ruff、SQLAlchemy 2.0 + Alembic、FastAPI；FE：Next.js 16 + TanStack Query v5 + recharts ^3.8（已裝）+ Vitest；ops：systemd timer + docker compose profiles。

**背景（實作者必讀，10 分鐘）：** `backend_py/docs/research/2026-07-06-profit-design-review.md` §0 + §1 E3 段。既有 G3：`scripts/run_g3_live_validation.py` + `scripts/_g3_loaders.py` + `modules/live_validation/live_attribution.py`（純函式，483 行）。上次報告格式：`backend_py/docs/research/2026-05-31-g3-live-validation.md`。

**資料事實（2026-07-06 已驗，實作前不用重查）：**
- **VM DB 的 `funding_stats` 是空的**（Neon 歷史未遷移）；`funding_candles` 只有 2026-06-12 起（每 cell 569 rows）。回填腳本已存在（`scripts/ingest_funding_stats.py` forward-fill、`modules/funding_stats/service.py` 的 `backfill_funding_stats_to_earliest/to_latest` walk-back；Bitfinex endpoint 上限 limit=250/頁）。
- **FRR 單位**：`funding_stats.frr`（~1e-6）不可直接當 rate（5 個轉換假設全 FAIL，ADR `2026-05-28-frr-not-a-market-rate-proxy`）。但 **`funding_stats.frr × 365 ≈ ticker FRR（per-day 日利率）`** — 2026-07-06 兩 symbol 實測：fUSD `1.02e-6×365=3.723e-4` vs ticker `3.7074e-4`（差 0.4%）；fUST `7.9e-7×365=2.8835e-4` vs `2.8868e-4`（差 0.1%）。×365 後（~3e-4）落在 `assert_market_rate_band` 的 `[1e-5, 0.05]` 內。此換算是**本 plan 的新採用**，有 runbook 驗證步驟（Task 8）+ band guard 雙保險。
- **cell identity**：event_log 的 ORDER_FILL 只帶 `signal_correlation_id`（無 cell）；`ORDER_SUBMIT`（有 cell）只進 stdout sink（ephemeral）。**唯一 durable 的 scid→cell 對照 = PG `diagnostics` 表 `kind='decision'` rows**（`payload["cell"]` + `payload["correlation_id"]`，`signal_engine.py:288-298` 寫入）。diagnostics 是 best-effort + prunable（30-90d）→ join 不到的 fills 必須歸 `unattributed` bucket，不得丟棄。
- **G3 live 路徑目前零 fee 處理**（全 gross）；15% 常數只在 `modules/backtest/config.py:20`（`fee_rate: Decimal = Decimal("0.15")`）。
- `weekly_window_bounds`（`live_attribution.py:103`）是「從資料 min_ts 起算的 rolling 7 天 bins」，**不是** calendar week — G3 保持原樣；attribution 表需要穩定 keys，用新的 UTC-Monday calendar-week 分箱（Task 4）。
- webapi role `bfx_webapi` 對新表**預設零權限**（public schema 無 default privileges）→ GRANT 是手動 psql runbook step（repo 慣例，migration 不含 GRANT）。
- VM：repo 在 `~/bfx-funding-bot`（user `ubuntu`），env 注入鏈 `~/bfx/bot.env` + `deploy/vm/canary.env` → `.env.runtime`；Postgres internal-only（無 published port）→ 排程必須在 VM docker network 內跑；一次性 container 先例 = compose `migrate` service。`bfx-pg-backup.{timer,service}` unit 檔只在 VM `/etc/systemd/system/`（不在 repo）。

## Global Constraints

- 所有後端指令在 `backend_py/` 下跑：`cd backend_py && uv run ...`；FE 指令在 `frontend/` 下跑 `pnpm ...`。
- **Read-only over SoT**：attribution/G3 任何 code 對 `event_log`、`diagnostics`、`position_state` 只讀；唯一寫入目標是新表 `attribution_weekly`（bot owner role）。絕不動 ledger/tracker/execution 模組。
- **不改 G3 四態 verdict 語意**：`decide_verdict` 的狀態機與 gross headline 保持原樣（與 05-30/05-31 報告連續）。fee-adjusted 數字與 AlwaysFRR benchmark 是**新增並列欄位**；「cap 加碼 gating」是 operator 政策 bar（報告醒目呈現），不是新 verdict state。
- **FRR 單位紀律（不可放寬）**：`funding_stats.frr` 原值嚴禁直接當 rate；唯一允許換算 = `× 365`（常數具名 `FRR_ANNUALIZATION`，出處註解引本 plan 背景實測），且換算後序列必須過 `assert_market_rate_band`；band fail → AlwaysFRR arm 標 unavailable（不炸主報告）。
- **cell join 失敗 = `unattributed` bucket**（period 取保守 p2=2 天，同 G3 慣例），行數與金額都要進表與報告 — 靜默丟棄 fills 是 bug。
- 每個後端 commit 前：`uv run pytest -m "not integration"` 全綠 + `uv run mypy src/` + `uv run ruff check`（import 按 isort 字母序插入，錯了 I001 擋 gate，`--fix` 可修）。FE commit 前：`pnpm lint && pnpm test`。
- Migration 一律 `cd backend_py && uv run alembic upgrade head` 套用；不可 MCP 直接 SQL。migration 檔不含 GRANT（GRANT 是 Task 8 psql runbook）。
- Commit 訊息用 repo emoji prefix。
- 新常數/約定：`FEE_RATE = Decimal("0.15")`（與 `backtest/config.py` fee_rate 同步，註解互指）；weekly bins = UTC Monday 00:00 對齊；VM 報告落地 `~/bfx/reports/`（新約定），值得留存的報告手動 promote 到 `backend_py/docs/research/`（無 auto-commit）。

---

### Task 1: G3 script 文件債 + `--capital` env default

**Files:**
- Modify: `scripts/run_g3_live_validation.py`（docstring :1-11、`--capital` default）
- Modify: `scripts/_g3_loaders.py`（docstring :1 與 :82/:87 的 "Neon" 字樣）
- Test: `tests/scripts/test_g3_capital_default.py`（新檔）

**Interfaces:**
- Produces: `run_g3_live_validation._default_capital(environ: Mapping[str, str]) -> str` — Task 8 的 weekly job 依賴此行為（不另傳 `--capital`，吃 `.env.runtime` 的 `BFX_ALLOCATION_CAP_USDT=10000`）。

- [ ] **Step 1: Write the failing test**

```python
# tests/scripts/test_g3_capital_default.py
from scripts.run_g3_live_validation import _default_capital


def test_capital_defaults_to_allocation_cap_env():
    assert _default_capital({"BFX_ALLOCATION_CAP_USDT": "10000"}) == "10000"


def test_capital_falls_back_without_env():
    # 歷史 fallback 570（僅 env 全缺時；VM .env.runtime 必有 cap）
    assert _default_capital({}) == "570"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend_py && uv run pytest tests/scripts/test_g3_capital_default.py -v`
Expected: FAIL — `ImportError: cannot import name '_default_capital'`

- [ ] **Step 3: Implement**

(a) `run_g3_live_validation.py` — `import argparse` 區補 `import os` 與 `from collections.abc import Mapping`（isort 位置），加純函式（放 `render_markdown` 之前）：

```python
def _default_capital(environ: Mapping[str, str]) -> str:
    """--capital 預設：跟著部署 cap 走（.env.runtime 的 BFX_ALLOCATION_CAP_USDT），
    避免 cap 調整後報告還用舊 570 分母（2026-07-06 review 發現 3000→10000 期間
    的 stale default）。"""
    return environ.get("BFX_ALLOCATION_CAP_USDT", "570")
```

`_amain` 內改：

```python
    parser.add_argument(
        "--capital",
        default=_default_capital(os.environ),
        help="capital budget C (default: BFX_ALLOCATION_CAP_USDT env, else 570)",
    )
```

(b) docstring 修正（`run_g3_live_validation.py:1-11`）：`reconcile state from Neon` → `reconcile state from Postgres (VM 自托 bfx-postgres；2026-06-23 前為 Neon)`；`:9-10` 的執行指令改為可直跑的 `-m` 形式：

```
Run from backend_py/:
  uv run python -m scripts.run_g3_live_validation --out docs/research/<date>-g3-live-validation.md
```

(c) `_g3_loaders.py` — `:1` docstring `Neon data loader` → `Postgres data loader`；`build_verdict_from_neon` docstring `:82` `Query Neon` → `Query Postgres`、`:87` `a Neon engine` → `an engine`。**函式名 `build_verdict_from_neon` 保留**（呼叫點多、rename 是無行為收益的 churn；docstring 註明名字是歷史遺留）。

- [ ] **Step 4: Run tests + gates**

Run: `cd backend_py && uv run pytest tests/scripts/test_g3_capital_default.py -v && uv run pytest -m "not integration" -q && uv run mypy src/ && uv run ruff check`
Expected: 全綠（scripts/ 不在 mypy src/ 範圍，但 ruff 掃全 repo — import 排序注意）

- [ ] **Step 5: Commit**

```bash
git add -u && git add backend_py/tests/scripts/test_g3_capital_default.py
git commit -m "🩹 Patch: E3 G3 docstring Neon→VM 文債 + --capital 吃 BFX_ALLOCATION_CAP_USDT"
```

---

### Task 2: AlwaysFRR arm 純函式素材（live_attribution 擴充）

**Files:**
- Modify: `src/bfx_funding_bot/modules/live_validation/live_attribution.py`（`FRR_ANNUALIZATION`、`frr_points_from_stats`、`FrrBenchmark`、`fill_duration_days` 公開化）
- Test: `tests/modules/live_validation/test_live_attribution.py`（檔尾新增）

**Interfaces:**
- Consumes: 既有 `MarketRatePoint`、`assert_market_rate_band`、`attribute_passive`、`_fill_duration_days`；`FundingStat`（`modules/funding_stats/schemas.py:39-45`，欄位 `symbol/mts/frr/avg_period/...`，`frr: Decimal | None`）
- Produces（Task 3/4/5 依賴這些名稱與簽名）:
  - `FRR_ANNUALIZATION: Decimal`（= 365）
  - `frr_points_from_stats(stats: list[FundingStat]) -> list[MarketRatePoint]`
  - `FrrBenchmark`（frozen dataclass：`available: bool, spread: Decimal, ci_lo: Decimal, ci_hi: Decimal, reason: str | None`）
  - `fill_duration_days(f: FillRecord) -> Decimal`（既有 `_fill_duration_days` 公開化：rename，原名留 alias 一行以免內部呼叫點漏改）

- [ ] **Step 1: Write the failing tests**

**import 必須加在檔頭既有 import 區**（不可放檔尾 — 該檔 ruff `E`/`I` 規則含 E402「module import not at top」，E402 **不可** `--fix`，放檔尾會硬擋 gate）：把 `FRR_ANNUALIZATION, FrrBenchmark, fill_duration_days, frr_points_from_stats` 按字母序併入既有的 `from bfx_funding_bot.modules.live_validation.live_attribution import (...)` block；`from bfx_funding_bot.modules.funding_stats.schemas import FundingStat` 加進頂部 import 區（isort 位置：`funding_stats` < `live_validation`）。

測試 body 加在 `tests/modules/live_validation/test_live_attribution.py` 檔尾（該檔慣例：module-level 常數 + helper、無 fixture、Decimal 字串建構；沿用檔內既有 `_fill` helper 與 `WEEK` 常數）：

```python
# ---- E3: AlwaysFRR arm 素材（import 已在檔頭，見上）----
def _stat(mts: int, frr: str | None) -> FundingStat:
    return FundingStat(
        symbol="fUST", mts=mts,
        frr=Decimal(frr) if frr is not None else None,
        avg_period=Decimal("95"),
    )


def test_frr_points_annualize_by_365():
    pts = frr_points_from_stats([_stat(0, "0.00000102")])
    assert pts[0].rate == Decimal("0.00000102") * FRR_ANNUALIZATION
    # ×365 後落在 assert_market_rate_band 的 [1e-5, 0.05] 內
    assert Decimal("0.00001") <= pts[0].rate <= Decimal("0.05")


def test_frr_points_skip_null_frr():
    assert frr_points_from_stats([_stat(0, None)]) == []


def test_frr_points_preserve_mts_order():
    pts = frr_points_from_stats([_stat(100, "1e-6"), _stat(200, "2e-6")])
    assert [p.mts for p in pts] == [100, 200]


def test_frr_benchmark_dataclass_shape():
    b = FrrBenchmark(
        available=False, spread=Decimal("0"), ci_lo=Decimal("0"),
        ci_hi=Decimal("0"), reason="funding_stats empty",
    )
    assert b.available is False and b.reason == "funding_stats empty"


def test_fill_duration_days_public_alias():
    # 公開版與既有語意一致：無 release = held-to-term
    assert fill_duration_days(_fill(0, "100", "0.0002", "2")) == Decimal("2")
    # release 提早 → 取實際存續
    one_day_ms = 24 * 60 * 60 * 1000
    assert fill_duration_days(
        _fill(0, "100", "0.0002", "2", release=one_day_ms)
    ) == Decimal("1")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd backend_py && uv run pytest tests/modules/live_validation/test_live_attribution.py -v -k "frr or duration_days_public"`
Expected: FAIL — `ImportError: cannot import name 'FRR_ANNUALIZATION'`

- [ ] **Step 3: Implement**

`live_attribution.py` 修改：

(a) 檔頭 import 補（isort 位置；`FundingStat` 是 pydantic model，pure module 引 schema 無 I/O 疑慮）：

```python
from bfx_funding_bot.modules.funding_stats.schemas import FundingStat
```

(b) `_fill_duration_days` 公開化 — 定義改名 `fill_duration_days`，緊接其後留 alias。**唯一內部呼叫點是 `clamp_active_window`（`live_attribution.py:204`）**（`open_principal_at` 是 inline 算 `f.fill_ts_ms + int(f.period_days * MS_PER_DAY)`，不呼叫本函式）；另 `clamp_active_window` docstring `:194` 有文字提及。`grep -n _fill_duration_days` 應剩 2 hit（def + 該 call），把 call 改新名、**保留 alias 的 LHS 不動**：

```python
def fill_duration_days(f: FillRecord) -> Decimal:
    """Held-to-term, capped by actual lifetime when a release exists.

    （E3 公開化：weekly_attribution 需要同一套 duration 語意。）"""
    if f.release_ts_ms is None:
        return f.period_days
    actual = Decimal(f.release_ts_ms - f.fill_ts_ms) / MS_PER_DAY
    if actual < 0:
        actual = Decimal("0")
    return min(f.period_days, actual)


_fill_duration_days = fill_duration_days  # 舊名 alias（防漏改；勿新增使用）
```

(c) `assert_market_rate_band` 之後加 AlwaysFRR 素材區塊：

```python
# ---- E3: AlwaysFRR benchmark arm（docs/research/2026-07-06-profit-design-review.md §1 E3 (c)）----
# funding_stats.frr 不是市場利率、也非 candle close 的單位轉換（ADR
# 2026-05-28-frr-not-a-market-rate-proxy；5 個假設全 FAIL）。但它是 ticker FRR
# 的 /365 表示：2026-07-06 兩 symbol 實測 frr×365 ≈ ticker FRR（per-day）誤差
# <0.5%（plan 2026-07-06-e3-measurement-automation.md 背景段）。AlwaysFRR arm
# 用 frr×365 當「FRR auto-renew 掛單者實得的日利率」序列；換算後仍須過
# assert_market_rate_band（雙保險：任何未來單位漂移會炸 loader 而非產出錯報告）。
FRR_ANNUALIZATION = Decimal("365")


def frr_points_from_stats(stats: list[FundingStat]) -> list[MarketRatePoint]:
    """funding_stats rows → AlwaysFRR arm 的 per-day rate 序列（×365，skip null）。"""
    return [
        MarketRatePoint(mts=s.mts, rate=s.frr * FRR_ANNUALIZATION)
        for s in stats
        if s.frr is not None
    ]


@dataclass(frozen=True)
class FrrBenchmark:
    """bot(active) vs AlwaysFRR 的比較結果 — 報告/JSON 用，NEVER 改變 verdict 狀態。

    這是 cap 加碼的政策 gating bar（「贏不了免費的 FRR auto-renew 就是零附加值」），
    由 operator 讀報告執行，不進 decide_verdict 狀態機。
    """

    available: bool
    spread: Decimal   # active − AlwaysFRR 的 paired headline spread（%，gross）
    ci_lo: Decimal
    ci_hi: Decimal
    reason: str | None  # unavailable 時的人話原因；available 時 None
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd backend_py && uv run pytest tests/modules/live_validation/ -v`
Expected: 全 PASS（既有 + 新增；alias 保證既有測試不動）

- [ ] **Step 5: Quality gates + commit**

```bash
cd backend_py && uv run pytest -m "not integration" -q && uv run mypy src/ && uv run ruff check
git add -u
git commit -m "✨ Feat: E3 AlwaysFRR arm 素材（frr×365 換算 + FrrBenchmark）+ fill_duration_days 公開化"
```

---

### Task 3: G3 報告整合 — AlwaysFRR arm + fee-adjusted 欄位

**Files:**
- Modify: `scripts/_g3_loaders.py`（載 funding_stats → frr arm → `FrrBenchmark`；`_compute_verdict` 回傳擴充）
- Modify: `scripts/run_g3_live_validation.py`（`render_markdown` + `_verdict_to_json` 加 AlwaysFRR 與 fee-adjusted 段）
- Test: `tests/scripts/test_g3_render_frr.py`（新檔，純 renderer 測試）+ 既有 seeded loader 測試跟上簽名

**Interfaces:**
- Consumes: Task 2 全部、既有 `attribute_passive`/`paired_active_returns`/`bootstrap_ci`（`_g3_loaders.py` 已 import，mr_alpha 段同款用法）、`FundingStatRow`（`modules/funding_stats/tables.py`）
- Produces: `_compute_verdict(..., frr_points: list[MarketRatePoint] = [])`→ 回傳 tuple 尾端加第 5 元素 `FrrBenchmark`；`render_markdown(..., frr: FrrBenchmark, fee_rate: Decimal)`；JSON 加 `"frr_benchmark"` 與 `"fee_adjusted"` 兩個 key — Task 8 的報告消費格式。

- [ ] **Step 1: Write the failing test（renderer 純函式）**

```python
# tests/scripts/test_g3_render_frr.py
from decimal import Decimal

from bfx_funding_bot.modules.live_validation.live_attribution import (
    ClampDiagnostic,
    FrrBenchmark,
    G3Verdict,
    VerdictState,
)
from scripts.run_g3_live_validation import _verdict_to_json, render_markdown


def _verdict() -> G3Verdict:
    return G3Verdict(
        state=VerdictState.INSUFFICIENT_DATA,
        headline_bot_vs_idle=Decimal("0.04"), n_windows=5,
        ci_lo=Decimal("0"), ci_hi=Decimal("0"), reasons=["only 5 weekly windows"],
        mr_alpha_spread=Decimal("0.005"), mr_alpha_ci_lo=Decimal("0"),
        mr_alpha_ci_hi=Decimal("0"), mr_alpha_available=True,
    )


def _diag() -> ClampDiagnostic:
    return ClampDiagnostic(
        cap=Decimal("10000"), peak_concurrent=Decimal("500"),
        raw_interest=Decimal("1"), clamped_interest=Decimal("1"),
    )


def test_render_includes_frr_benchmark_section():
    frr = FrrBenchmark(
        available=True, spread=Decimal("0.01"),
        ci_lo=Decimal("-0.005"), ci_hi=Decimal("0.02"), reason=None,
    )
    md = render_markdown(
        verdict=_verdict(), data_window="w", n_fills=4, clamp_diag=_diag(),
        frr=frr, fee_rate=Decimal("0.15"),
    )
    assert "AlwaysFRR benchmark" in md
    assert "cap-increase gating bar" in md
    assert "0.01" in md


def test_render_frr_unavailable_shows_reason():
    frr = FrrBenchmark(
        available=False, spread=Decimal("0"), ci_lo=Decimal("0"),
        ci_hi=Decimal("0"), reason="funding_stats empty — run backfill (E3 Task 8)",
    )
    md = render_markdown(
        verdict=_verdict(), data_window="w", n_fills=4, clamp_diag=_diag(),
        frr=frr, fee_rate=Decimal("0.15"),
    )
    assert "unavailable" in md.lower()
    assert "funding_stats empty" in md


def test_render_includes_fee_adjusted_headline():
    frr = FrrBenchmark(
        available=False, spread=Decimal("0"), ci_lo=Decimal("0"),
        ci_hi=Decimal("0"), reason="x",
    )
    md = render_markdown(
        verdict=_verdict(), data_window="w", n_fills=4, clamp_diag=_diag(),
        frr=frr, fee_rate=Decimal("0.15"),
    )
    # 0.04 × 0.85 = 0.034
    assert "fee-adjusted" in md
    assert "0.034" in md


def test_json_includes_frr_and_fee_keys():
    frr = FrrBenchmark(
        available=True, spread=Decimal("0.01"),
        ci_lo=Decimal("-0.005"), ci_hi=Decimal("0.02"), reason=None,
    )
    j = _verdict_to_json(_verdict(), _diag(), frr=frr, fee_rate=Decimal("0.15"))
    assert j["frr_benchmark"]["available"] is True
    assert j["frr_benchmark"]["spread"] == "0.01"
    assert j["fee_adjusted"]["fee_rate"] == "0.15"
    assert j["fee_adjusted"]["headline_bot_vs_idle_net"] == str(
        Decimal("0.04") * Decimal("0.85")
    )
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend_py && uv run pytest tests/scripts/test_g3_render_frr.py -v`
Expected: FAIL — `TypeError: render_markdown() got an unexpected keyword argument 'frr'`

- [ ] **Step 3: Implement**

(a) `_g3_loaders.py`：

- import 補：`from bfx_funding_bot.modules.funding_stats.tables import FundingStatRow`、live_attribution import 區補 `FrrBenchmark, frr_points_from_stats`、schemas import 補 `FundingStat`。
- `build_verdict_from_neon` 的 I/O 段（讀 candles 的同一個 session scope 內）加 funding_stats range 查詢（時間窗與 market-rate 查詢相同：`min_ts..max_ts` 或 fallback window）：

```python
        frr_rows = (
            await session.execute(
                select(FundingStatRow)
                .where(
                    FundingStatRow.symbol == _MARKET_SYMBOL,
                    FundingStatRow.mts >= rate_start_ms,
                    FundingStatRow.mts <= rate_end_ms,
                )
                .order_by(FundingStatRow.mts)
            )
        ).scalars().all()
        frr_stats = [
            FundingStat(
                symbol=r.symbol, mts=r.mts,
                frr=Decimal(str(r.frr)) if r.frr is not None else None,
                avg_period=Decimal(str(r.avg_period)) if r.avg_period is not None else None,
            )
            for r in frr_rows
        ]
```

（`rate_start_ms/rate_end_ms` 用該函式內 market-rate 查詢已有的同名邊界變數 — 先讀函式體對齊實際變數名。）

- `_compute_verdict` 簽名加 `frr_points: list[MarketRatePoint] | None = None`，回傳 tuple 加第 5 元素。

**(i) default 提到分支之上**：`frr_bench` 預設值必須放在 `if fills and bounds:` / `else` 分支**之前**，否則 empty-fills 路徑（`else` branch，`_g3_loaders.py:282-299`）不定義 `frr_bench`，新 5-tuple return 會 `UnboundLocalError`（`test_g3_loaders.py:137` empty-fills 測試會踩到）：

```python
    # empty-fills 路徑也要有預設 → 放在 if fills and bounds: 之前
    frr_bench = FrrBenchmark(
        available=False, spread=Decimal("0"), ci_lo=Decimal("0"),
        ci_hi=Decimal("0"),
        reason="funding_stats empty — run backfill (E3 Task 8)",
    )
```

**(ii) frr 計算放在 fills branch 內、mr_alpha 段之後**（需要 mr_alpha 段的既有區域變數 `strat_outcomes`/`bounds`/`mean_fn`/`single_strat`/`min_ts`/`max_ts` — **先讀 `_g3_loaders.py:237-281` 的 mr_alpha 段確認這些變數的實際名字**，下面用的是 verifier 核對過的真實名）：

```python
    # AlwaysFRR benchmark（政策 bar；不進 decide_verdict）。統計處理與 mr_alpha
    # 段同構：paired_active_returns 回的「已是」per-window diff list（非 tuple list），
    # bootstrap_ci 需第二個 positional stat_fn（= mr_alpha 段用的 mean_fn），
    # spread 取 full-span single-window 的 headline 差（同 mr_alpha）。
    if frr_points:
        try:
            assert_market_rate_band([p.rate for p in frr_points])
        except ValueError as exc:
            frr_bench = FrrBenchmark(
                available=False, spread=Decimal("0"), ci_lo=Decimal("0"),
                ci_hi=Decimal("0"), reason=str(exc),
            )
        else:
            frr_arm = attribute_passive(frr_points, window_bounds=bounds)
            frr_diffs = paired_active_returns(strat_outcomes, frr_arm)  # 已是 diff list
            if frr_diffs:
                lo, hi = bootstrap_ci(frr_diffs, mean_fn)
                single_frr = attribute_passive(frr_points, window_bounds=[(min_ts, max_ts)])
                spread = single_strat[0].net_monthly - single_frr[0].net_monthly
                frr_bench = FrrBenchmark(
                    available=True, spread=spread, ci_lo=lo, ci_hi=hi, reason=None,
                )
            else:
                frr_bench = FrrBenchmark(
                    available=False, spread=Decimal("0"), ci_lo=Decimal("0"),
                    ci_hi=Decimal("0"), reason="no overlapping windows",
                )
```

**注意**：`paired_active_returns(a, b)` 回 `list[Decimal]`（**已是** per-window diff list，不要再 `[x - y for x, y in ...]`）；`bootstrap_ci(values, stat_fn, *, ...)` 的 `stat_fn` 是**必填 positional**（mr_alpha 段用的是 `mean_fn`，`_g3_loaders.py:228`）。active-arm 變數叫 `strat_outcomes`（不是 `active_outcomes`）。若讀 mr_alpha 段發現變數名不同，照 mr_alpha 段的實名 — 兩個 secondary arm 的統計處理必須同構。

- `build_verdict_from_neon` 把 `frr_points=frr_points_from_stats(frr_stats)` 傳給 `_compute_verdict`，回傳 tuple 尾端帶 `FrrBenchmark`（呼叫端 Task 3(b) 同步跟上）。

(b) `run_g3_live_validation.py`：

- `render_markdown` 簽名加 `frr: FrrBenchmark, fee_rate: Decimal`（keyword-only）。TL;DR 區塊 `bot-vs-idle 95% CI` 行之後加：

```python
        f"- Headline bot-vs-idle **fee-adjusted** (×{Decimal('1') - fee_rate}): "
        f"{verdict.headline_bot_vs_idle * (Decimal('1') - fee_rate)}%"
        " — Bitfinex takes 15% of interest; gross headline kept for continuity",
```

- AlwaysFRR section：`render_markdown` 的 `lines` 是**單一 list literal**（mr_alpha 以 `*mr_alpha` splice 在中段、結尾是 Recommendation 段）。故先在 `lines` literal 之前算好 `frr_section`，再於 literal 內 `*mr_alpha,` 那行之後插一行 `*frr_section,`（**別用 `lines += [...]`** — 那會落到 Recommendation 之後，順序不對）：

```python
    frr_section = ["", "## AlwaysFRR benchmark (cap-increase gating bar)"]
    if frr.available:
        frr_section += [
            f"- bot − AlwaysFRR spread: {frr.spread}%  (95% CI [{frr.ci_lo}, {frr.ci_hi}])",
            "- POLICY: cap 再加碼前，本 spread 需非負（贏不了免費的 FRR auto-renew"
            " = 零附加值）且 verdict 為最新 PASS。",
            "- Rate source: funding_stats.frr × 365 (≈ ticker per-day FRR;"
            " band-guarded, see live_attribution.FRR_ANNUALIZATION).",
        ]
    else:
        frr_section += [
            f"- **unavailable** — {frr.reason}",
            "- POLICY: benchmark unavailable 時 cap 一律不加碼。",
        ]
    # …接著在 lines = [...] literal 內 `*mr_alpha,` 之後插入 `*frr_section,`
```

- `_verdict_to_json` 簽名加 `frr: FrrBenchmark, fee_rate: Decimal`，dict 加：

```python
        "fee_adjusted": {
            "fee_rate": str(fee_rate),
            "headline_bot_vs_idle_net": str(
                verdict.headline_bot_vs_idle * (Decimal("1") - fee_rate)
            ),
        },
        "frr_benchmark": {
            "available": frr.available,
            "spread": str(frr.spread),
            "ci_lo": str(frr.ci_lo),
            "ci_hi": str(frr.ci_hi),
            "reason": frr.reason,
        },
```

- `_amain` 解包第 5 元素並傳入兩處；`fee_rate` 用模組常數 `_FEE_RATE = Decimal("0.15")  # keep in sync with modules/backtest/config.py fee_rate`。
- 報告標題行（hardcoded 單 cell 字串）維持不動（per-cell 呈現在 Task 4-7 的 attribution 表/前端，G3 報告仍是帳戶級主 gate）。

(c) **既有測試呼叫點全部跟上第 5 元素**（tuple 從 4→5，漏改任何一處 = `ValueError: too many values to unpack`）。`grep -n "_compute_verdict(\|build_verdict_from_neon(" tests/` 找齊：
- `tests/scripts/test_g3_loaders.py` 有 **7 個 `_compute_verdict(...)` 直呼點**（`:80,94,108,126,137,153,164`，各解 4-tuple）→ 全改成解 5 元素（`verdict, _window, n_fills, _clamp, _frr = ...`）。這些是 non-integration 測試，漏改會擋 Step 4 gate。
- `tests/scripts/test_g3_loaders.py` 的 2 個 `build_verdict_from_neon(...)` 呼點（`:215,239`）同樣跟上第 5 元素；並加斷言：seeded DB 無 funding_stats 時 `_frr.available is False`。
- `tests/scripts/test_g3_loaders_integration.py:21` 有第 3 個 `build_verdict_from_neon` 呼點（解 3-tuple，**已 stale**、`@pytest.mark.integration` 排除在 gate 外）→ 順手改成 `verdict, data_window, n_fills, *_ = ...`（incidental cleanup，避免留一個更 stale 的 3-vs-5 unpack）。

- [ ] **Step 4: Run tests + gates**

Run: `cd backend_py && uv run pytest tests/scripts/ tests/modules/live_validation/ -v && uv run pytest -m "not integration" -q && uv run mypy src/ && uv run ruff check`
Expected: 全綠

- [ ] **Step 5: Commit**

```bash
git add -u && git add backend_py/tests/scripts/test_g3_render_frr.py
git commit -m "✨ Feat: E3 G3 報告加 AlwaysFRR benchmark arm（政策 gating bar）+ fee-adjusted headline"
```

---

### Task 4: `weekly_attribution` 純模組 + `attribution_weekly` 表 + migration

**Files:**
- Create: `src/bfx_funding_bot/modules/live_validation/weekly_attribution.py`
- Create: `src/bfx_funding_bot/modules/live_validation/tables.py`
- Modify: `alembic/env.py`（side-effect import 一行）
- Create: `alembic/versions/<rev>_add_attribution_weekly.py`
- Test: `tests/modules/live_validation/test_weekly_attribution.py`

**Interfaces:**
- Consumes: Task 2 `fill_duration_days`、既有 `FillRecord`/`MarketRatePoint`
- Produces（Task 5/6 依賴）:
  - `WEEK_MS: int`、`calendar_week_start(mts: int) -> int`（UTC Monday 00:00 對齊）
  - `FEE_RATE: Decimal`（0.15）
  - `WeeklyCellRow`（frozen dataclass：`cell: str, week_start_ms: int, week_end_ms: int, n_fills: int, gross_interest_usdt: Decimal, net_interest_usdt: Decimal, capital_days: Decimal, realized_apr_net_pct: Decimal | None, baseline_close_apr_net_pct: Decimal | None, baseline_frr_apr_net_pct: Decimal | None`）
  - `compute_weekly_rows(*, fills_by_cell: dict[str, list[FillRecord]], close_points: list[MarketRatePoint], frr_points: list[MarketRatePoint]) -> list[WeeklyCellRow]`
  - ORM `AttributionWeeklyRow`（`__tablename__ = "attribution_weekly"`）

- [ ] **Step 1: Write the failing tests**

```python
# tests/modules/live_validation/test_weekly_attribution.py
from decimal import Decimal

from bfx_funding_bot.modules.live_validation.live_attribution import (
    FillRecord,
    MarketRatePoint,
)
from bfx_funding_bot.modules.live_validation.weekly_attribution import (
    FEE_RATE,
    WEEK_MS,
    WeeklyCellRow,
    calendar_week_start,
    compute_weekly_rows,
)

# 2026-06-29 是週一。UTC 2026-06-29T00:00:00 = 1782691200000 ms
_MON = 1_782_691_200_000
_DAY = 24 * 60 * 60 * 1000


def _fill(ts: int, size: str, rate: str, period: str = "2") -> FillRecord:
    return FillRecord(
        venue_offer_id=str(ts), fill_ts_ms=ts, size_usdt=Decimal(size),
        rate=Decimal(rate), period_days=Decimal(period), release_ts_ms=None,
    )


def test_calendar_week_start_aligns_to_utc_monday():
    assert calendar_week_start(_MON) == _MON               # 週一 00:00 本身
    assert calendar_week_start(_MON + 3 * _DAY + 5) == _MON  # 週四某刻 → 本週一
    assert calendar_week_start(_MON - 1) == _MON - WEEK_MS   # 週日深夜 → 上週一


def test_compute_weekly_rows_fee_and_apr():
    # 單 cell 單 fill：500 USDT × 0.0002/day × 2 天 = gross 0.2
    fills = {"fUST_p2": [_fill(_MON + _DAY, "500", "0.0002")]}
    rows = compute_weekly_rows(
        fills_by_cell=fills, close_points=[], frr_points=[],
    )
    assert len(rows) == 1
    r = rows[0]
    assert r.cell == "fUST_p2"
    assert r.week_start_ms == _MON
    assert r.week_end_ms == _MON + WEEK_MS
    assert r.n_fills == 1
    assert r.gross_interest_usdt == Decimal("0.2")
    assert r.net_interest_usdt == Decimal("0.2") * (Decimal("1") - FEE_RATE)
    # capital_days = 500 × 2 = 1000；APR_net = 0.17/1000 × 365 × 100 = 6.205%
    assert r.capital_days == Decimal("1000")
    assert r.realized_apr_net_pct == (
        Decimal("0.17") / Decimal("1000") * Decimal("365") * Decimal("100")
    )
    # 無 baseline 資料 → None（不是 0 — 缺資料與零收益必須可區分）
    assert r.baseline_close_apr_net_pct is None
    assert r.baseline_frr_apr_net_pct is None


def test_compute_weekly_rows_bins_by_fill_week():
    fills = {"fUST_p2": [
        _fill(_MON + _DAY, "500", "0.0002"),
        _fill(_MON + WEEK_MS + _DAY, "300", "0.0003"),
    ]}
    rows = compute_weekly_rows(fills_by_cell=fills, close_points=[], frr_points=[])
    assert [(r.week_start_ms, r.n_fills) for r in rows] == [
        (_MON, 1), (_MON + WEEK_MS, 1),
    ]


def test_compute_weekly_rows_baselines_use_week_mean_rate():
    # close 均值 0.0002/day → APR_net = 0.0002×365×100×0.85 = 6.205%
    fills = {"fUST_p2": [_fill(_MON + _DAY, "500", "0.0002")]}
    close = [
        MarketRatePoint(mts=_MON + i * _DAY, rate=Decimal("0.0002"))
        for i in range(3)
    ]
    frr = [MarketRatePoint(mts=_MON + _DAY, rate=Decimal("0.0003"))]
    rows = compute_weekly_rows(fills_by_cell=fills, close_points=close, frr_points=frr)
    r = rows[0]
    expected_close = Decimal("0.0002") * Decimal("365") * Decimal("100") * Decimal("0.85")
    expected_frr = Decimal("0.0003") * Decimal("365") * Decimal("100") * Decimal("0.85")
    assert r.baseline_close_apr_net_pct == expected_close
    assert r.baseline_frr_apr_net_pct == expected_frr


def test_compute_weekly_rows_baseline_weeks_without_fills_still_emitted():
    # 有市場資料但該週無 fill 的 cell 也要出 row（三線圖的 baseline 線不能斷）
    close = [MarketRatePoint(mts=_MON + _DAY, rate=Decimal("0.0002"))]
    rows = compute_weekly_rows(
        fills_by_cell={"fUST_p2": []}, close_points=close, frr_points=[],
    )
    assert len(rows) == 1
    r = rows[0]
    assert r.n_fills == 0
    assert r.gross_interest_usdt == Decimal("0")
    assert r.realized_apr_net_pct is None  # capital_days=0 → APR 未定義
    assert r.baseline_close_apr_net_pct is not None


def test_unattributed_bucket_is_a_normal_cell_key():
    rows = compute_weekly_rows(
        fills_by_cell={"unattributed": [_fill(_MON, "200", "0.0002")]},
        close_points=[], frr_points=[],
    )
    assert rows[0].cell == "unattributed"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd backend_py && uv run pytest tests/modules/live_validation/test_weekly_attribution.py -v`
Expected: FAIL — `ModuleNotFoundError: ... weekly_attribution`

- [ ] **Step 3: Implement 純模組**

```python
# src/bfx_funding_bot/modules/live_validation/weekly_attribution.py
"""Per-cell weekly fee-adjusted realized APR（E3 (b) — 2026-07-06 review §1）。

Pure, I/O-free。與 G3 的差異：
- 分箱 = UTC Monday 00:00 對齊的 calendar weeks（穩定的 upsert key），
  非 weekly_window_bounds 的「資料 min_ts 起算 rolling 7 天 bins」（G3 保持原樣）。
- 有 fee：net = gross × (1 − FEE_RATE)。G3 主 headline 維持 gross（報告連續性），
  本模組是 operator 儀表，直接給扣費後數字。
- per-cell：cell key 由 loader（scripts/run_weekly_attribution.py）經 diagnostics
  DECISION join 提供；join 不到的 fills 歸 "unattributed"（loader 保證，非本模組）。

語意：
- realized_apr_net_pct = net_interest / capital_days × 365 × 100
  — 實際部署資本的年化報酬（utilization 無關；capital_days = Σ size×duration）。
- baseline_*_apr_net_pct = mean(rate in week) × 365 × 100 × (1−fee)
  — 滿倉掛市場價/FRR 的理想化年化（full utilization 假設，與 G3 passive arm 同構）。
- 缺資料 = None，不是 0（零收益與缺資料必須可區分）。
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from bfx_funding_bot.modules.live_validation.live_attribution import (
    FillRecord,
    MarketRatePoint,
    fill_duration_days,
)

WEEK_MS = 7 * 24 * 60 * 60 * 1000
# 1970-01-01 是週四；第一個 UTC 週一 = 1970-01-05 = 345_600_000 ms
_EPOCH_MONDAY_MS = 4 * 24 * 60 * 60 * 1000
# Bitfinex 抽 15% 利息。keep in sync with modules/backtest/config.py fee_rate。
FEE_RATE = Decimal("0.15")
_ONE_MINUS_FEE = Decimal("1") - FEE_RATE
_DAYS_PER_YEAR = Decimal("365")


def calendar_week_start(mts: int) -> int:
    """該 timestamp 所屬 UTC calendar week 的週一 00:00（ms）。"""
    return mts - (mts - _EPOCH_MONDAY_MS) % WEEK_MS


@dataclass(frozen=True)
class WeeklyCellRow:
    cell: str
    week_start_ms: int
    week_end_ms: int
    n_fills: int
    gross_interest_usdt: Decimal
    net_interest_usdt: Decimal
    capital_days: Decimal
    realized_apr_net_pct: Decimal | None
    baseline_close_apr_net_pct: Decimal | None
    baseline_frr_apr_net_pct: Decimal | None


def _mean_rate_by_week(points: list[MarketRatePoint]) -> dict[int, Decimal]:
    by_week: dict[int, list[Decimal]] = {}
    for p in points:
        by_week.setdefault(calendar_week_start(p.mts), []).append(p.rate)
    return {
        wk: sum(rates, Decimal("0")) / Decimal(len(rates))
        for wk, rates in by_week.items()
    }


def _baseline_apr_net(mean_rate: Decimal | None) -> Decimal | None:
    if mean_rate is None:
        return None
    return mean_rate * _DAYS_PER_YEAR * Decimal("100") * _ONE_MINUS_FEE


def compute_weekly_rows(
    *,
    fills_by_cell: dict[str, list[FillRecord]],
    close_points: list[MarketRatePoint],
    frr_points: list[MarketRatePoint],
) -> list[WeeklyCellRow]:
    """cell × calendar-week 的 fee-adjusted 實得 + 兩條 baseline。

    row 集合 = (每個 cell) × (該 cell 有 fill 的週 ∪ 有 baseline 資料的週) —
    baseline 週沒 fill 也出 row（前端 baseline 線不斷），fill 週沒 baseline
    也出 row（baseline 欄 None）。fill 歸屬其 fill_ts 所在週（G3 同慣例）。
    """
    close_by_week = _mean_rate_by_week(close_points)
    frr_by_week = _mean_rate_by_week(frr_points)
    baseline_weeks = set(close_by_week) | set(frr_by_week)

    rows: list[WeeklyCellRow] = []
    for cell, fills in fills_by_cell.items():
        fills_by_week: dict[int, list[FillRecord]] = {}
        for f in fills:
            fills_by_week.setdefault(calendar_week_start(f.fill_ts_ms), []).append(f)
        for wk in sorted(set(fills_by_week) | baseline_weeks):
            wk_fills = fills_by_week.get(wk, [])
            gross = sum(
                (f.size_usdt * f.rate * fill_duration_days(f) for f in wk_fills),
                Decimal("0"),
            )
            capital_days = sum(
                (f.size_usdt * fill_duration_days(f) for f in wk_fills),
                Decimal("0"),
            )
            net = gross * _ONE_MINUS_FEE
            apr = (
                net / capital_days * _DAYS_PER_YEAR * Decimal("100")
                if capital_days > 0 else None
            )
            rows.append(WeeklyCellRow(
                cell=cell,
                week_start_ms=wk,
                week_end_ms=wk + WEEK_MS,
                n_fills=len(wk_fills),
                gross_interest_usdt=gross,
                net_interest_usdt=net,
                capital_days=capital_days,
                realized_apr_net_pct=apr,
                baseline_close_apr_net_pct=_baseline_apr_net(close_by_week.get(wk)),
                baseline_frr_apr_net_pct=_baseline_apr_net(frr_by_week.get(wk)),
            ))
    return rows
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd backend_py && uv run pytest tests/modules/live_validation/test_weekly_attribution.py -v`
Expected: 6 PASS

- [ ] **Step 5: ORM 表 + migration**

(a) `src/bfx_funding_bot/modules/live_validation/tables.py`（照 `execution/diagnostics/tables.py` 的雙 dialect house style；Numeric 存 Decimal）：

```python
from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Integer, Numeric, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from bfx_funding_bot.core.db import Base

_NOW = func.current_timestamp()


class AttributionWeeklyRow(Base):
    """E3 (b) — per-cell weekly fee-adjusted realized APR（operator 儀表）。

    由 scripts/run_weekly_attribution.py 每週全量重算 + upsert（event-sourced：
    event_log 是 SoT，本表是可隨時重建的 read model）。webapi 只 SELECT
    （GRANT 是部署 runbook step，不在 migration）。
    """

    __tablename__ = "attribution_weekly"

    deployment_environment: Mapped[str] = mapped_column(Text, primary_key=True)
    account_id: Mapped[str] = mapped_column(Text, primary_key=True)
    cell: Mapped[str] = mapped_column(Text, primary_key=True)
    week_start_ms: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer(), "sqlite"), primary_key=True,
    )
    week_end_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    n_fills: Mapped[int] = mapped_column(Integer, nullable=False)
    gross_interest_usdt: Mapped[Decimal] = mapped_column(Numeric, nullable=False)
    net_interest_usdt: Mapped[Decimal] = mapped_column(Numeric, nullable=False)
    capital_days: Mapped[Decimal] = mapped_column(Numeric, nullable=False)
    realized_apr_net_pct: Mapped[Decimal | None] = mapped_column(Numeric, nullable=True)
    baseline_close_apr_net_pct: Mapped[Decimal | None] = mapped_column(Numeric, nullable=True)
    baseline_frr_apr_net_pct: Mapped[Decimal | None] = mapped_column(Numeric, nullable=True)
    computed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=_NOW,
    )
```

（檔頭補 `from decimal import Decimal`。）

(b) `alembic/env.py` 的 side-effect import 區（`:17-24`）按字母序加：

```python
import bfx_funding_bot.modules.live_validation.tables
```

加完**跑 `uv run ruff check --fix`**（不是純 `ruff check`）：ruff 會 (a) 在既有 lending 行的 `# noqa: F401` 報 RUF100（因新增 import 後排序變動觸發重掃）、(b) 把新 import 從 side-effect 註解區搬到頂部 import 群 — 兩者都是預期且無害（side-effect import 照樣 register `AttributionWeeklyRow`）。別手動刪 noqa，讓 `--fix` 處理。

(c) 產 migration：`cd backend_py && uv run alembic revision --autogenerate -m "add attribution_weekly"`。若本地 DB 不可用（autogenerate 需連線），改 `uv run alembic revision -m "add attribution_weekly"` 手寫 upgrade（照 hand-crafted 先例 `a2b3c4d5e6f7_add_user_configs.py` 風格），`op.create_table` 欄位 = 上表 ORM 一一對應（PK = 四欄複合），downgrade = `op.drop_table("attribution_weekly")`。migration docstring 註明：「GRANT SELECT to bfx_webapi is a separate psql runbook step (E3 Task 8)」。

Run: `cd backend_py && uv run alembic upgrade head && uv run alembic check`（本地 DB 可用時）
Expected: upgrade 成功且 check 無 drift

- [ ] **Step 6: Quality gates + commit**

```bash
cd backend_py && uv run pytest -m "not integration" -q && uv run mypy src/ && uv run ruff check
git add -u && git add backend_py/src/bfx_funding_bot/modules/live_validation/weekly_attribution.py backend_py/src/bfx_funding_bot/modules/live_validation/tables.py backend_py/alembic/versions/ backend_py/tests/modules/live_validation/test_weekly_attribution.py
git commit -m "✨ Feat: E3 weekly_attribution 純模組（calendar-week per-cell fee-adjusted APR）+ attribution_weekly 表"
```

---

### Task 5: `run_weekly_attribution.py` — loader + persist script

**Files:**
- Create: `scripts/run_weekly_attribution.py`
- Test: `tests/scripts/test_weekly_attribution_loader.py`（seeded sqlite）

**Interfaces:**
- Consumes: Task 4 全部、`EventLogRow`（`modules/execution/event_store/tables.py:24-52`）、`DiagnosticsRow`（`modules/execution/diagnostics/tables.py`）、`FundingStatRow`、candles `get_candles_in_range`（`modules/candles/repository.py:87-96`）、Task 2 `frr_points_from_stats`、`cell_period_days`
- Produces: CLI `uv run python -m scripts.run_weekly_attribution`（env：`DATABASE_URL`/`BFX_ACCOUNT_ID`/`BFX_DEPLOYMENT_ENV`，同 G3）；核心函式 `load_and_compute(session_factory, *, account_id, deployment_environment) -> list[WeeklyCellRow]` 與 `persist_rows(session_factory, rows, *, account_id, deployment_environment) -> int` — Task 8 的 weekly chain 依賴 CLI 形式。

- [ ] **Step 1: Write the failing test（seeded sqlite，照 webapi 測試的 `sqlite_engine` fixture 慣例）**

```python
# tests/scripts/test_weekly_attribution_loader.py
from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

import bfx_funding_bot.modules.execution.diagnostics.tables  # noqa: F401
import bfx_funding_bot.modules.execution.event_store.tables  # noqa: F401
import bfx_funding_bot.modules.funding_stats.tables  # noqa: F401
import bfx_funding_bot.modules.live_validation.tables  # noqa: F401
from bfx_funding_bot.core.db import Base
from bfx_funding_bot.modules.execution.diagnostics.tables import DiagnosticsRow
from bfx_funding_bot.modules.execution.event_store.tables import EventLogRow
from bfx_funding_bot.modules.live_validation.tables import AttributionWeeklyRow
from scripts.run_weekly_attribution import load_and_compute, persist_rows

_MON = 1_782_691_200_000  # 2026-06-29 UTC Monday
_ENV = "prod"
_ACCT = "default"


@pytest_asyncio.fixture
async def sf(sqlite_engine):
    async with sqlite_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return async_sessionmaker(sqlite_engine, expire_on_commit=False)


def _fill_event(scid: str, voi: str, ts: int) -> EventLogRow:
    return EventLogRow(
        account_id=_ACCT, deployment_environment=_ENV, event_type="ORDER_FILL",
        venue_offer_id=voi,
        payload={
            "symbol": "fUST", "signal_correlation_id": scid,
            "size_usdt": "500", "fill_rate": 0.0002, "venue_offer_id": voi,
        },
        occurred_at_ms=ts,
    )


def _decision_row(scid: str, cell: str) -> DiagnosticsRow:
    return DiagnosticsRow(
        account_id=_ACCT, deployment_environment=_ENV, kind="decision",
        payload={"cell": cell, "correlation_id": scid, "event_type": "DECISION"},
        occurred_at=datetime.now(UTC),
    )


async def test_fills_joined_to_cell_via_decision_diagnostics(sf):
    scid = str(uuid4())
    async with sf() as s:
        s.add(_fill_event(scid, "42", _MON + 1000))
        s.add(_decision_row(scid, "fUST_p2"))
        await s.commit()
    rows = await load_and_compute(sf, account_id=_ACCT, deployment_environment=_ENV)
    cells = {r.cell for r in rows}
    assert "fUST_p2" in cells
    p2 = next(r for r in rows if r.cell == "fUST_p2" and r.n_fills == 1)
    assert p2.gross_interest_usdt == Decimal("500") * Decimal("0.0002") * Decimal("2")


async def test_unjoinable_fill_lands_in_unattributed(sf):
    async with sf() as s:
        s.add(_fill_event(str(uuid4()), "43", _MON + 1000))  # 無對應 DECISION row
        await s.commit()
    rows = await load_and_compute(sf, account_id=_ACCT, deployment_environment=_ENV)
    assert any(r.cell == "unattributed" and r.n_fills == 1 for r in rows)


async def test_p30_cell_uses_conservative_period_not_crash(sf):
    # cell_period_days 不認 p30 → _period_for_cell fail-soft 回 p2=2d，不炸整個 job
    scid = str(uuid4())
    async with sf() as s:
        s.add(_fill_event(scid, "46", _MON + 1000))
        s.add(_decision_row(scid, "fUST_p30"))
        await s.commit()
    rows = await load_and_compute(sf, account_id=_ACCT, deployment_environment=_ENV)
    p30 = next(r for r in rows if r.cell == "fUST_p30" and r.n_fills == 1)
    # 保守 p2=2d：500 × 0.0002 × 2 = 0.2
    assert p30.gross_interest_usdt == Decimal("500") * Decimal("0.0002") * Decimal("2")


async def test_persist_rows_upserts_idempotently(sf):
    scid = str(uuid4())
    async with sf() as s:
        s.add(_fill_event(scid, "44", _MON + 1000))
        s.add(_decision_row(scid, "fUST_p2"))
        await s.commit()
    rows = await load_and_compute(sf, account_id=_ACCT, deployment_environment=_ENV)
    n1 = await persist_rows(sf, rows, account_id=_ACCT, deployment_environment=_ENV)
    n2 = await persist_rows(sf, rows, account_id=_ACCT, deployment_environment=_ENV)
    assert n1 == n2 == len(rows)
    async with sf() as s:
        db_rows = (await s.execute(select(AttributionWeeklyRow))).scalars().all()
    assert len(db_rows) == len(rows)  # 重跑不重複


async def test_release_shortens_duration(sf):
    scid = str(uuid4())
    one_day = 24 * 60 * 60 * 1000
    async with sf() as s:
        s.add(_fill_event(scid, "45", _MON + 1000))
        s.add(_decision_row(scid, "fUST_p2"))
        s.add(EventLogRow(
            account_id=_ACCT, deployment_environment=_ENV,
            event_type="RESERVATION_RELEASED", venue_offer_id="45",
            payload={"venue_offer_id": "45"}, occurred_at_ms=_MON + 1000 + one_day,
        ))
        await s.commit()
    rows = await load_and_compute(sf, account_id=_ACCT, deployment_environment=_ENV)
    p2 = next(r for r in rows if r.cell == "fUST_p2")
    # duration 被 release 截到 1 天
    assert p2.gross_interest_usdt == Decimal("500") * Decimal("0.0002") * Decimal("1")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd backend_py && uv run pytest tests/scripts/test_weekly_attribution_loader.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'scripts.run_weekly_attribution'`

- [ ] **Step 3: Implement**

```python
# scripts/run_weekly_attribution.py
"""E3 (b) — per-cell weekly fee-adjusted realized APR → attribution_weekly 表。

Read-only over event_log/diagnostics/funding_candles/funding_stats；唯一寫入
= attribution_weekly（全量重算 + upsert，event_log 是 SoT、本表是 read model）。
cell identity：ORDER_FILL.signal_correlation_id → diagnostics kind='decision'
payload.correlation_id → payload.cell。diagnostics 是 best-effort/prunable —
join 不到的 fills 歸 "unattributed"（保守 p2 period），絕不丟棄。

Run from backend_py/ (env: DATABASE_URL / BFX_ACCOUNT_ID / BFX_DEPLOYMENT_ENV):
  uv run python -m scripts.run_weekly_attribution
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bfx_funding_bot.core.db import make_engine, make_session_factory
from bfx_funding_bot.core.settings import Settings
from bfx_funding_bot.modules.candles.repository import get_candles_in_range
from bfx_funding_bot.modules.execution.diagnostics.tables import DiagnosticsRow
from bfx_funding_bot.modules.execution.event_store.tables import EventLogRow
from bfx_funding_bot.modules.funding_stats.schemas import FundingStat
from bfx_funding_bot.modules.funding_stats.tables import FundingStatRow
from bfx_funding_bot.modules.live_validation.live_attribution import (
    FillRecord,
    MarketRatePoint,
    cell_period_days,
    frr_points_from_stats,
)
from bfx_funding_bot.modules.live_validation.tables import AttributionWeeklyRow
from bfx_funding_bot.modules.live_validation.weekly_attribution import (
    WeeklyCellRow,
    compute_weekly_rows,
)

log = logging.getLogger(__name__)

_FILL_TYPE = "ORDER_FILL"
_RELEASE_TYPE = "RESERVATION_RELEASED"
_DECISION_KIND = "decision"
_MARKET_SYMBOL = "fUST"
_MARKET_TIMEFRAME = "1h"
_MARKET_PERIOD_AGG = "p2"
_UNATTRIBUTED = "unattributed"


def _period_for_cell(cell: str, frr_avg_period: Decimal) -> Decimal:
    """cell id 形如 '<symbol>_<period_agg>'（fUST_p2 / fUST_a30 / fUST_p30 …）。

    cell_period_days 只認 p2/a30，對 p30（shadow realm cells.yaml 有）、
    未來 p7/p14 會 raise ValueError。這裡 fail-soft → 保守 p2（G3 同慣例）+ log，
    絕不讓一筆 p30 fill 炸掉整個 weekly sweep（那會重演「量測斷線」的問題）。
    """
    if cell == _UNATTRIBUTED:
        return Decimal("2")
    period_agg = cell.rsplit("_", 1)[-1]
    try:
        return cell_period_days(period_agg, frr_avg_period)
    except ValueError:
        log.warning(
            "unknown period_agg %r for cell %r; using conservative p2=2d",
            period_agg, cell,
        )
        return Decimal("2")


async def load_and_compute(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    account_id: str,
    deployment_environment: str,
) -> list[WeeklyCellRow]:
    async with session_factory() as session:
        fill_rows = (
            await session.execute(
                select(EventLogRow)
                .where(
                    EventLogRow.event_type == _FILL_TYPE,
                    EventLogRow.account_id == account_id,
                    EventLogRow.deployment_environment == deployment_environment,
                )
                .order_by(EventLogRow.occurred_at_ms)
            )
        ).scalars().all()
        release_rows = (
            await session.execute(
                select(EventLogRow).where(
                    EventLogRow.event_type == _RELEASE_TYPE,
                    EventLogRow.account_id == account_id,
                    EventLogRow.deployment_environment == deployment_environment,
                )
            )
        ).scalars().all()
        decision_rows = (
            await session.execute(
                select(DiagnosticsRow).where(
                    DiagnosticsRow.kind == _DECISION_KIND,
                    DiagnosticsRow.account_id == account_id,
                    DiagnosticsRow.deployment_environment == deployment_environment,
                )
            )
        ).scalars().all()

        if not fill_rows:
            return []
        min_ts = fill_rows[0].occurred_at_ms
        max_ts = fill_rows[-1].occurred_at_ms + 1

        candles = await get_candles_in_range(
            session, symbol=_MARKET_SYMBOL, timeframe=_MARKET_TIMEFRAME,
            period_agg=_MARKET_PERIOD_AGG, start_mts=min_ts, end_mts=max_ts,
        )
        frr_rows = (
            await session.execute(
                select(FundingStatRow)
                .where(
                    FundingStatRow.symbol == _MARKET_SYMBOL,
                    FundingStatRow.mts >= min_ts,
                    FundingStatRow.mts <= max_ts,
                )
                .order_by(FundingStatRow.mts)
            )
        ).scalars().all()

    scid_to_cell = {
        str(d.payload.get("correlation_id")): str(d.payload.get("cell"))
        for d in decision_rows
        if d.payload.get("correlation_id") and d.payload.get("cell")
    }
    release_map = {
        str(r.payload.get("venue_offer_id") or r.venue_offer_id or ""): r.occurred_at_ms
        for r in release_rows
    }
    frr_stats = [
        FundingStat(
            symbol=r.symbol, mts=r.mts,
            frr=Decimal(str(r.frr)) if r.frr is not None else None,
            avg_period=Decimal(str(r.avg_period)) if r.avg_period is not None else None,
        )
        for r in frr_rows
    ]
    latest_avg_period = next(
        (s.avg_period for s in reversed(frr_stats) if s.avg_period is not None),
        Decimal("30"),
    )

    fills_by_cell: dict[str, list[FillRecord]] = {}
    for row in fill_rows:
        payload = row.payload
        scid = str(payload.get("signal_correlation_id") or "")
        cell = scid_to_cell.get(scid, _UNATTRIBUTED)
        voi = str(payload.get("venue_offer_id") or row.venue_offer_id or "")
        # Decimal(str(float)) 防科學記號（live executor 踩坑 ae2c59d 同慣例）
        fills_by_cell.setdefault(cell, []).append(FillRecord(
            venue_offer_id=voi,
            fill_ts_ms=row.occurred_at_ms,
            size_usdt=Decimal(str(payload.get("size_usdt", "0"))),
            rate=Decimal(str(payload.get("fill_rate", "0"))),
            period_days=_period_for_cell(cell, latest_avg_period),
            release_ts_ms=release_map.get(voi),
        ))

    close_points = [
        MarketRatePoint(mts=c.mts, rate=c.close)
        for c in candles if c.close is not None
    ]
    return compute_weekly_rows(
        fills_by_cell=fills_by_cell,
        close_points=close_points,
        frr_points=frr_points_from_stats(frr_stats),
    )


async def persist_rows(
    session_factory: async_sessionmaker[AsyncSession],
    rows: list[WeeklyCellRow],
    *,
    account_id: str,
    deployment_environment: str,
) -> int:
    """Upsert by PK（merge — sqlite/PG 通用；全量重算所以 merge 語意即覆蓋）。"""
    async with session_factory() as session:
        for r in rows:
            await session.merge(AttributionWeeklyRow(
                deployment_environment=deployment_environment,
                account_id=account_id,
                cell=r.cell,
                week_start_ms=r.week_start_ms,
                week_end_ms=r.week_end_ms,
                n_fills=r.n_fills,
                gross_interest_usdt=r.gross_interest_usdt,
                net_interest_usdt=r.net_interest_usdt,
                capital_days=r.capital_days,
                realized_apr_net_pct=r.realized_apr_net_pct,
                baseline_close_apr_net_pct=r.baseline_close_apr_net_pct,
                baseline_frr_apr_net_pct=r.baseline_frr_apr_net_pct,
            ))
        await session.commit()
    return len(rows)


async def _amain() -> int:
    settings = Settings()
    engine = make_engine(settings)
    sf = make_session_factory(engine)
    account_id = os.environ.get("BFX_ACCOUNT_ID", "default")
    env = os.environ.get("BFX_DEPLOYMENT_ENV", "prod")
    try:
        rows = await load_and_compute(
            sf, account_id=account_id, deployment_environment=env,
        )
        n = await persist_rows(
            sf, rows, account_id=account_id, deployment_environment=env,
        )
    finally:
        await engine.dispose()
    unattributed = sum(1 for r in rows if r.cell == _UNATTRIBUTED)
    print(f"attribution_weekly upserted={n} unattributed_rows={unattributed}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(_amain()))
```

**實作備註**：`make_engine`/`make_session_factory`/`get_candles_in_range` 的實際簽名以 `core/db.py`、`candles/repository.py` 為準（`_g3_loaders.py:89-96` 是既有用法範本 — 先讀它，若那邊是別的建構方式照抄那邊）。

- [ ] **Step 4: Run tests + gates**

Run: `cd backend_py && uv run pytest tests/scripts/test_weekly_attribution_loader.py -v && uv run pytest -m "not integration" -q && uv run mypy src/ && uv run ruff check`
Expected: 全綠

- [ ] **Step 5: Commit**

```bash
git add backend_py/scripts/run_weekly_attribution.py backend_py/tests/scripts/test_weekly_attribution_loader.py
git commit -m "✨ Feat: E3 run_weekly_attribution loader/persist（DECISION join per-cell + unattributed bucket）"
```

---

### Task 6: webapi `GET /api/v1/attribution/weekly`

**Files:**
- Create: `src/bfx_funding_bot/modules/api/attribution.py`
- Modify: `src/bfx_funding_bot/modules/api/schemas.py`（加 DTO）
- Modify: `src/bfx_funding_bot/main.py`（include router）
- Test: `tests/test_attribution_router.py`

**Interfaces:**
- Consumes: Task 4 `AttributionWeeklyRow`、既有 `require_user`（`core/auth.py`）、`get_session`（`modules/api/deps.py`）
- Produces: `build_attribution_router() -> APIRouter`；`GET /api/v1/attribution/weekly?cell=<optional>` → `{"data": [WeeklyAttributionResponse...]}`（camelCase）— Task 7 FE 依賴此 schema。

- [ ] **Step 1: Write the failing tests（照 `tests/test_config_router.py` pattern：sqlite + dependency_overrides + 裸 app auth-gate）**

```python
# tests/test_attribution_router.py
from decimal import Decimal

import pytest_asyncio
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker

import bfx_funding_bot.modules.live_validation.tables  # noqa: F401  # registers ORM
from bfx_funding_bot.core.auth import Principal, require_user
from bfx_funding_bot.core.db import Base
from bfx_funding_bot.modules.api.attribution import build_attribution_router
from bfx_funding_bot.modules.api.deps import get_session
from bfx_funding_bot.modules.live_validation.tables import AttributionWeeklyRow


def _row(cell: str = "fUST_p2", week: int = 1_782_691_200_000) -> AttributionWeeklyRow:
    return AttributionWeeklyRow(
        deployment_environment="prod", account_id="default", cell=cell,
        week_start_ms=week, week_end_ms=week + 604_800_000, n_fills=2,
        gross_interest_usdt=Decimal("0.2"), net_interest_usdt=Decimal("0.17"),
        capital_days=Decimal("1000"), realized_apr_net_pct=Decimal("6.205"),
        baseline_close_apr_net_pct=Decimal("6.205"),
        baseline_frr_apr_net_pct=None,
    )


@pytest_asyncio.fixture
async def app_client(sqlite_engine):
    async with sqlite_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(sqlite_engine, expire_on_commit=False)
    async with factory() as s:
        s.add(_row())
        s.add(_row(cell="fUST_a30"))
        await s.commit()

    app = FastAPI()
    app.include_router(build_attribution_router())

    async def _fake_user():
        return Principal(user_id="user_abc", email="will@example.com", role="operator")

    async def _override_session():
        async with factory() as s:
            try:
                yield s
                await s.commit()
            except Exception:
                await s.rollback()
                raise

    app.dependency_overrides[require_user] = _fake_user
    app.dependency_overrides[get_session] = _override_session
    return TestClient(app)


def test_weekly_returns_rows_camel_case(app_client):
    resp = app_client.get("/api/v1/attribution/weekly")
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert len(data) == 2
    row = next(d for d in data if d["cell"] == "fUST_p2")
    assert row["weekStartMs"] == 1_782_691_200_000
    assert row["realizedAprNetPct"] == "6.205"
    assert row["baselineFrrAprNetPct"] is None


def test_weekly_cell_filter(app_client):
    resp = app_client.get("/api/v1/attribution/weekly", params={"cell": "fUST_a30"})
    assert [d["cell"] for d in resp.json()["data"]] == ["fUST_a30"]


def test_weekly_requires_auth(sqlite_engine):
    app = FastAPI()
    app.include_router(build_attribution_router())
    client = TestClient(app)
    assert client.get("/api/v1/attribution/weekly").status_code in (401, 403)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd backend_py && uv run pytest tests/test_attribution_router.py -v`
Expected: FAIL — `ModuleNotFoundError: ... modules.api.attribution`

- [ ] **Step 3: Implement**

(a) `schemas.py` 檔尾加（Decimal 序列化成字串 — FE 用 `Number()` 轉，精度不丟）：

```python
class WeeklyAttributionResponse(BaseModel):
    cell: str
    week_start_ms: int = Field(serialization_alias="weekStartMs")
    week_end_ms: int = Field(serialization_alias="weekEndMs")
    n_fills: int = Field(serialization_alias="nFills")
    gross_interest_usdt: str = Field(serialization_alias="grossInterestUsdt")
    net_interest_usdt: str = Field(serialization_alias="netInterestUsdt")
    capital_days: str = Field(serialization_alias="capitalDays")
    realized_apr_net_pct: str | None = Field(serialization_alias="realizedAprNetPct")
    baseline_close_apr_net_pct: str | None = Field(
        serialization_alias="baselineCloseAprNetPct"
    )
    baseline_frr_apr_net_pct: str | None = Field(
        serialization_alias="baselineFrrAprNetPct"
    )
```

(b) `modules/api/attribution.py`（照 `config.py:16-37` 的 factory + envelope 慣例）：

```python
"""E3 (b) — per-cell weekly attribution 讀端點（operator 儀表；read-only）。"""
from __future__ import annotations

import os

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bfx_funding_bot.core.auth import Principal, require_user
from bfx_funding_bot.modules.api.deps import get_session
from bfx_funding_bot.modules.api.schemas import WeeklyAttributionResponse
from bfx_funding_bot.modules.live_validation.tables import AttributionWeeklyRow


def _to_response(row: AttributionWeeklyRow) -> dict[str, object]:
    return WeeklyAttributionResponse(
        cell=row.cell,
        week_start_ms=row.week_start_ms,
        week_end_ms=row.week_end_ms,
        n_fills=row.n_fills,
        gross_interest_usdt=str(row.gross_interest_usdt),
        net_interest_usdt=str(row.net_interest_usdt),
        capital_days=str(row.capital_days),
        realized_apr_net_pct=(
            str(row.realized_apr_net_pct)
            if row.realized_apr_net_pct is not None else None
        ),
        baseline_close_apr_net_pct=(
            str(row.baseline_close_apr_net_pct)
            if row.baseline_close_apr_net_pct is not None else None
        ),
        baseline_frr_apr_net_pct=(
            str(row.baseline_frr_apr_net_pct)
            if row.baseline_frr_apr_net_pct is not None else None
        ),
    ).model_dump(by_alias=True)


def build_attribution_router() -> APIRouter:
    router = APIRouter(prefix="/api/v1", tags=["attribution"])
    # 儀表只看單一部署 realm — 多 env/account 的 row 不可混進同一 cell 序列
    # （否則 FE 每週有重複點）。與 loader 寫入時的 env 對齊（同 default）。
    account_id = os.environ.get("BFX_ACCOUNT_ID", "default")
    deployment_environment = os.environ.get("BFX_DEPLOYMENT_ENV", "prod")

    @router.get("/attribution/weekly")
    async def weekly(
        cell: str | None = None,
        user: Principal = Depends(require_user),  # noqa: B008
        session: AsyncSession = Depends(get_session),  # noqa: B008
    ) -> dict[str, object]:
        stmt = (
            select(AttributionWeeklyRow)
            .where(
                AttributionWeeklyRow.account_id == account_id,
                AttributionWeeklyRow.deployment_environment == deployment_environment,
            )
            .order_by(AttributionWeeklyRow.cell, AttributionWeeklyRow.week_start_ms)
        )
        if cell is not None:
            stmt = stmt.where(AttributionWeeklyRow.cell == cell)
        rows = (await session.execute(stmt)).scalars().all()
        return {"data": [_to_response(r) for r in rows]}

    return router
```

（測試 seed 的 row 用 `deployment_environment="prod"`/`account_id="default"`，與 endpoint default 一致 → 測試不需改。）

(c) `main.py`：import 補 `from bfx_funding_bot.modules.api.attribution import build_attribution_router`（isort 位置 — `api_keys` 之前，`attribution` < `api_keys`？字母序 `api_keys` < `attribution` < `config` → 放 api_keys 之後），`app.include_router(build_attribution_router())` 加在既有三行旁。

- [ ] **Step 4: Run tests + gates**

Run: `cd backend_py && uv run pytest tests/test_attribution_router.py -v && uv run pytest -m "not integration" -q && uv run mypy src/ && uv run ruff check`
Expected: 全綠

- [ ] **Step 5: Commit**

```bash
git add -u && git add backend_py/src/bfx_funding_bot/modules/api/attribution.py backend_py/tests/test_attribution_router.py
git commit -m "✨ Feat: E3 webapi GET /api/v1/attribution/weekly（per-cell 三線資料）"
```

---

### Task 7: FE `(dashboard)/attribution` 三線圖頁

**Files:**（全在 `frontend/`）
- Modify: `src/types/index.ts`、`src/lib/query-keys.ts`
- Create: `src/features/attribution/hooks/use-weekly-attribution.ts`
- Create: `src/features/attribution/components/attribution-chart.tsx`
- Create: `src/app/[locale]/(dashboard)/attribution/page.tsx`
- Modify: `src/components/layout/sidebar-nav.tsx`（navItems 加一項）+ `messages/en.json`、`messages/zh-TW.json`
- Test: `src/features/attribution/components/__tests__/attribution-chart.test.tsx`

**Interfaces:**
- Consumes: Task 6 endpoint（經 `/api/proxy`；`(dashboard)` route group 自動帶 auth）；`apiClient`（自動解包 `{data: T}`）；recharts ^3.8（已裝，零新依賴）
- Produces: `/attribution` 頁 — 每 cell 一張 LineChart：bot realized APR（net）/ always-close / AlwaysFRR 三條線

- [ ] **Step 1: 型別 + query key + hook**

`src/types/index.ts` 檔尾加：

```typescript
export interface WeeklyAttributionPoint {
  cell: string;
  weekStartMs: number;
  weekEndMs: number;
  nFills: number;
  grossInterestUsdt: string;
  netInterestUsdt: string;
  capitalDays: string;
  realizedAprNetPct: string | null;
  baselineCloseAprNetPct: string | null;
  baselineFrrAprNetPct: string | null;
}
```

`src/lib/query-keys.ts` 檔尾加（照 `earningsKeys` pattern）：

```typescript
export const attributionKeys = {
  all: ["attribution"] as const,
  weekly: () => [...attributionKeys.all, "weekly"] as const,
};
```

`src/features/attribution/hooks/use-weekly-attribution.ts`（照 `use-earnings-history.ts` 全文 pattern）：

```typescript
import { useQuery } from "@tanstack/react-query";
import { apiClient } from "@/lib/api-client";
import { attributionKeys } from "@/lib/query-keys";
import type { WeeklyAttributionPoint } from "@/types";

export function useWeeklyAttribution() {
  return useQuery({
    queryKey: attributionKeys.weekly(),
    queryFn: () =>
      apiClient.get<WeeklyAttributionPoint[]>("/attribution/weekly"),
    staleTime: 60 * 60_000, // 資料每週更新，1h stale 足夠
  });
}
```

- [ ] **Step 2: Chart component（照 `earnings-chart.tsx` 的容器/axis/tooltip 樣式，AreaChart → LineChart 三線 + Legend）**

```tsx
// src/features/attribution/components/attribution-chart.tsx
"use client";

import { useTranslations } from "next-intl";
import {
  CartesianGrid,
  Legend,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import type { WeeklyAttributionPoint } from "@/types";

function toSeries(points: WeeklyAttributionPoint[]) {
  return points.map((p) => ({
    week: new Date(p.weekStartMs).toISOString().slice(0, 10),
    bot: p.realizedAprNetPct === null ? null : Number(p.realizedAprNetPct),
    close: p.baselineCloseAprNetPct === null ? null : Number(p.baselineCloseAprNetPct),
    frr: p.baselineFrrAprNetPct === null ? null : Number(p.baselineFrrAprNetPct),
  }));
}

export function AttributionChart({
  cell,
  points,
}: {
  cell: string;
  points: WeeklyAttributionPoint[];
}) {
  const t = useTranslations("attribution");
  const data = toSeries(points);
  return (
    <div className="rounded-xl border border-white/5 bg-white/[0.02] p-5 shadow-[inset_0_1px_0_0_rgba(255,255,255,0.1)]">
      <h3 className="text-sm font-medium text-zinc-200">{cell}</h3>
      <p className="text-xs text-zinc-500">{t("subtitle")}</p>
      <div className="mt-4 h-[240px]">
        <ResponsiveContainer width="100%" height="100%">
          <LineChart data={data}>
            <CartesianGrid stroke="#27272a" strokeDasharray="3 3" />
            <XAxis
              dataKey="week"
              tick={{ fill: "#a1a1aa", fontSize: 11 }}
              tickFormatter={(v: string) => v.slice(5)}
              axisLine={{ stroke: "#3f3f46" }}
              tickLine={false}
            />
            <YAxis
              tick={{ fill: "#a1a1aa", fontSize: 11 }}
              axisLine={{ stroke: "#3f3f46" }}
              tickLine={false}
              unit="%"
            />
            <Tooltip
              contentStyle={{
                backgroundColor: "#18181b",
                border: "1px solid #3f3f46",
                borderRadius: 8,
                fontSize: 12,
              }}
            />
            <Legend wrapperStyle={{ fontSize: 12 }} />
            <Line type="monotone" dataKey="bot" name={t("botLine")} stroke="#10b981" strokeWidth={1.5} dot={false} connectNulls />
            <Line type="monotone" dataKey="close" name={t("closeLine")} stroke="#818cf8" strokeWidth={1.5} dot={false} connectNulls />
            <Line type="monotone" dataKey="frr" name={t("frrLine")} stroke="#f59e0b" strokeWidth={1.5} dot={false} connectNulls />
          </LineChart>
        </ResponsiveContainer>
      </div>
    </div>
  );
}
```

- [ ] **Step 3: Page + nav + i18n**

`src/app/[locale]/(dashboard)/attribution/page.tsx`（照 overview 的 loading/QueryError 骨架，先讀 `overview/page.tsx` 對齊 skeleton/QueryError import 路徑）：

```tsx
"use client";

import { useTranslations } from "next-intl";
import { QueryError } from "@/components/shared/query-error";
import { AttributionChart } from "@/features/attribution/components/attribution-chart";
import { useWeeklyAttribution } from "@/features/attribution/hooks/use-weekly-attribution";

export default function AttributionPage() {
  const t = useTranslations("attribution");
  const { data, isLoading, isError, refetch } = useWeeklyAttribution();

  if (isLoading) return <div className="p-6 text-sm text-zinc-500">{t("loading")}</div>;
  if (isError || !data)
    return <QueryError message={t("loadFailed")} onRetry={() => refetch()} />;

  const cells = [...new Set(data.map((p) => p.cell))].sort();
  return (
    <div className="space-y-4 p-6">
      <h1 className="text-lg font-semibold text-zinc-100">{t("title")}</h1>
      {cells.length === 0 && (
        <p className="text-sm text-zinc-500">{t("empty")}</p>
      )}
      <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
        {cells.map((cell) => (
          <AttributionChart
            key={cell}
            cell={cell}
            points={data.filter((p) => p.cell === cell)}
          />
        ))}
      </div>
    </div>
  );
}
```

nav：先讀 `src/components/layout/sidebar-nav.tsx:20-26` 的 `navItems` 現狀，照既有項目格式加 `{ href: "/attribution", labelKey: "attribution", icon: <既有慣例選一個 lucide icon，如 LineChart> }`（欄位名以現檔為準）。

i18n：`messages/en.json` 與 `messages/zh-TW.json` — `"nav"` 段加 `"attribution"` key；新增 `"attribution"` namespace：

```json
"attribution": {
  "title": "Weekly Attribution",
  "subtitle": "Fee-adjusted APR — bot vs always-close vs AlwaysFRR",
  "botLine": "Bot (realized, net)",
  "closeLine": "Always-close",
  "frrLine": "AlwaysFRR",
  "loading": "Loading…",
  "loadFailed": "Failed to load attribution data",
  "empty": "No attribution rows yet — weekly job has not run."
}
```

（zh-TW 對應翻譯：標題「每週歸因」、線名「Bot 實得（扣費）」「Always-close」「AlwaysFRR」等。）

- [ ] **Step 4: Component test（照 repo idiom `src/features/dashboard/components/__tests__/stats-grid.test.tsx` — 用 `@/lib/test-utils` 的 `render`（已 wrap `NextIntlClientProvider` + messages/en.json）+ 斷言 `container.textContent`）**

> **不可用 `@testing-library/jest-dom`**（`.toBeInTheDocument()`）：該套件雖在 devDependencies 但**從未 wire**（vitest.config 無 setupFiles、無 `import "@testing-library/jest-dom"`）→ `pnpm lint`（`tsc --noEmit`）與 `pnpm test`（runtime）都會炸。**先讀 stats-grid.test.tsx 確認 `render` 的 import 路徑與是否自帶 provider**，照抄其斷言風格。

```tsx
// src/features/attribution/components/__tests__/attribution-chart.test.tsx
import { describe, expect, it } from "vitest";
import { render } from "@/lib/test-utils";  // 路徑/是否含 provider 以 stats-grid.test.tsx 為準
import { AttributionChart } from "../attribution-chart";

const POINTS = [
  {
    cell: "fUST_p2", weekStartMs: 1782691200000, weekEndMs: 1783296000000,
    nFills: 2, grossInterestUsdt: "0.2", netInterestUsdt: "0.17",
    capitalDays: "1000", realizedAprNetPct: "6.205",
    baselineCloseAprNetPct: "6.205", baselineFrrAprNetPct: null,
  },
];

describe("AttributionChart", () => {
  it("renders cell title", () => {
    const { container } = render(
      <AttributionChart cell="fUST_p2" points={POINTS} />,
    );
    expect(container.textContent).toContain("fUST_p2");
  });
});
```

- [ ] **Step 5: Gates + commit**

Run: `cd frontend && pnpm lint && pnpm test`
Expected: 全綠

```bash
git add frontend/src frontend/messages
git commit -m "💄 Feat: E3 FE attribution 頁（per-cell 三線圖：bot/always-close/AlwaysFRR，recharts）"
```

---

### Task 8: Ops — compose weekly service + systemd timer + runbook + 文件（人工 gate，VM 步驟不可由 subagent 執行）

**Files:**
- Modify: `docker-compose.bot.yml`（加 `weekly-report` one-shot service，`profiles: ["ops"]`）
- Create: `deploy/vm/systemd/bfx-weekly-report.service` + `deploy/vm/systemd/bfx-weekly-report.timer`
- Modify: `ARCHITECTURE.md` §8（量測自動化段 + cap 加碼政策）
- Modify: repo root `CLAUDE.md` 部署架構段（timer 一行）

- [ ] **Step 1: compose service（repo 內，照 `migrate` service pattern；`profiles: ["ops"]` 讓 `up -d` 不啟動）**

`docker-compose.bot.yml` 的 `migrate` service 之後加：

```yaml
  # E3 weekly measurement chain（systemd timer 觸發，不隨 up -d 啟動）：
  # ① funding_stats forward-fill（AlwaysFRR arm 資料）② attribution 落表 ③ G3 報告。
  # 步驟 ① 失敗不擋 ②③（set +e per-step；G3/attribution 對缺資料 fail-soft）。
  weekly-report:
    image: bfx-bot:local
    profiles: ["ops"]
    working_dir: /app
    env_file: .env.runtime
    restart: "no"
    volumes:
      - ${HOME}/bfx/reports:/reports
    command:
      - sh
      - -c
      - |
        python -m scripts.ingest_funding_stats --symbols fUST,fUSD || echo "WARN ingest_funding_stats failed (frr arm may be stale)"
        python -m scripts.run_weekly_attribution
        python -m scripts.run_g3_live_validation --out "/reports/$$(date +%F)-g3-live-validation.md"
    depends_on:
      postgres:
        condition: service_healthy
```

**`$$(date +%F)` 是雙錢號**：compose 對 `command` 字串先做自己的變數插值，單 `$(...)` 在部分 compose 版本會被吃掉→檔名塌成 `-g3-live-validation.md` 每週覆蓋（同檔 `pg_isready -U $$POSTGRES_USER` 就是這個 escape 慣例）。`docker compose config` 應把它 render 回單 `$(date +%F)`（= 容器 shell runtime 才替換）。

**`ingest_funding_stats` CLI**：實測 CLI 是 `--symbols fUST,fUSD`（**逗號分隔的單一 flag**，非 positional；default 已是 `fUST,fUSD`，其實可不帶）。上面顯式帶著以求清楚 — 勿寫成 `fUST fUSD` positional（argparse 會拒）。

- [ ] **Step 2: systemd units（repo 內版本控制，install 是 VM 手動步驟）**

`deploy/vm/systemd/bfx-weekly-report.service`：

```ini
[Unit]
Description=bfx weekly measurement chain (funding_stats ingest + attribution + G3 report)
Requires=docker.service
After=docker.service

[Service]
Type=oneshot
User=ubuntu
WorkingDirectory=/home/ubuntu/bfx-funding-bot
ExecStart=/usr/bin/docker compose -f docker-compose.bot.yml --profile ops run --rm weekly-report
```

`deploy/vm/systemd/bfx-weekly-report.timer`：

```ini
[Unit]
Description=Weekly bfx measurement chain (Mon 04:17 UTC, after 03:17 pg backup)

[Timer]
OnCalendar=Mon *-*-* 04:17:00 UTC
Persistent=true

[Install]
WantedBy=timers.target
```

- [ ] **Step 3: ARCHITECTURE.md 文件**

§8（Phases & Deployment）加一段：

```markdown
### 量測自動化（E3，2026-07-06）

- **Weekly chain**：VM systemd timer `bfx-weekly-report.timer`（Mon 04:17 UTC，unit 檔在 `deploy/vm/systemd/`）→ compose one-shot `weekly-report`（`--profile ops`）：`ingest_funding_stats`（AlwaysFRR arm 資料）→ `run_weekly_attribution`（per-cell fee-adjusted APR → `attribution_weekly` 表）→ `run_g3_live_validation`（報告 → VM `~/bfx/reports/<date>-g3-live-validation.{md,json}`）。值得留存的報告手動 promote 進 `backend_py/docs/research/` 並 commit。
- **儀表**：webapi `GET /api/v1/attribution/weekly`（bfx_webapi 需 GRANT SELECT ON attribution_weekly）→ FE `/attribution` 頁三線圖（bot net APR / always-close / AlwaysFRR）。
- **政策（cap 加碼 gate）**：`BFX_ALLOCATION_CAP_USDT` 再加碼前必須：最新 weekly G3 verdict = PASS **且** AlwaysFRR benchmark spread 非負（`frr_benchmark` unavailable 時一律不加碼）。本次 3000→10000（`aa4842c`）是在 INSUFFICIENT_DATA 上拉的 — 此政策防重演。
- **FRR 單位**：AlwaysFRR arm 的 rate = `funding_stats.frr × 365`（≈ ticker per-day FRR，2026-07-06 實測誤差 <0.5%；`live_attribution.FRR_ANNUALIZATION`），換算後必過 `assert_market_rate_band`。`funding_stats.frr` 原值仍非市場利率（ADR 2026-05-28 不變）。
```

repo root `CLAUDE.md` 部署架構表下的備註段補一行（在 pg_dump timer 那句附近）：「每週量測 chain 由 `bfx-weekly-report.timer`（Mon 04:17 UTC）跑 compose `weekly-report` one-shot → `~/bfx/reports/`。」

- [ ] **Step 4: Repo 側 gates + commit**

Run: `cd backend_py && uv run pytest -m "not integration" -q && uv run ruff check && docker compose -f ../docker-compose.bot.yml config -q`
Expected: 全綠（compose config 驗 YAML；`${HOME}` warning 可忽略）

```bash
git add -u && git add deploy/vm/systemd/
git commit -m "🚀 Deploy: E3 weekly-report compose service + systemd units + 📝 ARCHITECTURE 量測自動化/cap 政策"
```

- [ ] **Step 5: VM rollout（人工，SSH `ubuntu@oci-a1`）**

1. `cd ~/bfx-funding-bot && ./scripts/deploy-vm.sh canary`（拉新 code + migrate service 自動跑 `alembic upgrade head` 建 `attribution_weekly`）。
2. **GRANT**（psql via `docker exec -it bfx-postgres psql -U bfx -d bfx`）：
   ```sql
   GRANT SELECT ON public.attribution_weekly TO bfx_webapi;
   SELECT has_table_privilege('bfx_webapi', 'public.attribution_weekly', 'SELECT');  -- expect: t
   SELECT has_table_privilege('bfx_webapi', 'public.attribution_weekly', 'INSERT');  -- expect: f
   ```
3. **一次性 funding_stats 歷史回填**（覆蓋 canary 期間 2026-05-27+）。注意 weekly chain 的 `ingest_funding_stats` 是 **forward-fill（只補最新→現在）**，不會回填歷史；歷史回填要走 walk-back backfill（`scripts/backfill_phase2.py`，呼叫 `backfill_funding_stats_to_earliest`；CLI 以 `sed -n '1,30p' backend_py/scripts/backfill_phase2.py` 為準）。bot image 無 ENTRYPOINT（`CMD ["sh","-c","alembic upgrade head && exec bfx-shadow"]`），故 `docker compose run --rm bot <cmd>` 的 `<cmd>` 直接 override CMD：
   ```bash
   docker compose -f docker-compose.bot.yml run --rm bot python -m scripts.backfill_phase2   # 或 ingest --symbols 依腳本
   ```
   然後驗：
   ```sql
   SELECT count(*), to_timestamp(min(mts)/1000), to_timestamp(max(mts)/1000)
   FROM funding_stats WHERE symbol='fUST';  -- 期望 min <= 2026-05-27
   ```
   （回填非阻塞項：即使只有 forward-fill 的近期資料，AlwaysFRR arm 也會在有覆蓋的 window 出值，只是不涵蓋早期 canary。）
4. **frr×365 換算抽查**（本 plan 資料假設的 runbook 驗證）：
   ```bash
   curl -s https://api-pub.bitfinex.com/v2/ticker/fUST | jq '.[0]'   # ticker per-day FRR
   # psql: SELECT frr*365 FROM funding_stats WHERE symbol='fUST' ORDER BY mts DESC LIMIT 1;
   # 兩者相對差 <10% 即通過；偏差大 → 停用 AlwaysFRR arm（報告會自動標 unavailable if band fails）並回報
   ```
5. `mkdir -p ~/bfx/reports`；手動首跑 `docker compose -f docker-compose.bot.yml --profile ops run --rm weekly-report` → 確認 `~/bfx/reports/<today>-g3-live-validation.md` 產出、`attribution_weekly` 有 rows（`SELECT cell, count(*) FROM attribution_weekly GROUP BY 1;`）、`unattributed` 比例（diagnostics 保留期內應接近 0）。
6. 安裝 timer：
   ```bash
   sudo cp ~/bfx-funding-bot/deploy/vm/systemd/bfx-weekly-report.{service,timer} /etc/systemd/system/
   sudo systemctl daemon-reload && sudo systemctl enable --now bfx-weekly-report.timer
   systemctl list-timers | grep bfx   # 期望看到 weekly-report 與 pg-backup 兩個
   ```
7. FE 驗證：登入 → `/attribution` 頁三線圖有資料（AlwaysFRR 線在 funding_stats 覆蓋期才有值）。
8. **成功判準（兩週）**：連續兩個週一自動產出報告；`attribution_weekly` rows 每週遞增；dashboard 三線可讀；G3 報告 `frr_benchmark.available=true`。

---

## Self-Review 紀錄（writing-plans 時已跑）

- **Spec 覆蓋**（review §1 E3 逐項）：(a) weekly systemd timer 照 `bfx-pg-backup` pattern = Task 8（unit 檔進 repo `deploy/vm/systemd/` — VM 上的 backup unit 不在 repo，故新寫而非照抄，pattern 同型）；(b) 持續 attribution job 重用 `_g3_loaders`/`live_validation` 邏輯、per-cell 每週 fee-adjusted realized APR、落自己的表、webapi endpoint 餵前端、read-only over event_log = Task 4+5+6；(c) AlwaysFRR arm 進 G3 報告 = Task 2+3；frr 單位問題落地為 `×365` + band 雙保險 = Task 2 + Task 8 runbook 驗證；G3 手動→自動 + `--capital` stale + Neon docstring 債 = Task 1+8；「dashboard 三條線」= Task 7；政策「cap 加碼前必須最新 G3 PASS」強化為「PASS + AlwaysFRR spread 非負」寫進 ARCHITECTURE = Task 8。
- **與 review 的刻意差異**：(1) **AlwaysFRR arm 的 WFO matrix 側延後**——三重理由：VM/本地都無足夠歷史（Neon 未遷移，`funding_stats`/`funding_candles` 歷史需回填）、matrix 插入點與 P1 G13 step ③ 動同一個 `run_cell_wfo` 簽名（該做時一起做，避免兩次 churn）、gating bar 本來就是 live G3 arm（review 原文的政策句）。素材（`frr_points_from_stats`、backfill 能力）本 plan 已備妥。(2) 「dated report 進 docs/research/」改為 VM `~/bfx/reports/` + 手動 promote——repo 無 auto-commit 先例，VM 不應持有 push 權。(3) per-cell 機制 = diagnostics DECISION join（review 未指明）——diagnostics prunable/best-effort，故 `unattributed` bucket 是一級公民（表、報告、測試都有）。(4) `frr×365` 是本 plan 新採用的換算（review 只警告單位問題）——雙保險：`assert_market_rate_band` 程式閘 + Task 8 runbook 對 ticker 抽查；ADR「FRR 非市場利率」不變（AlwaysFRR arm 量的是 FRR auto-renew 掛單者實得，不是市場利率 proxy）。
- **Placeholder 掃描**：所有 code step 附完整 code；剩餘「以現檔為準」的指令（mr_alpha 段變數實名、`@/lib/test-utils` render helper、sidebar navItems 欄位名、`backfill_phase2` CLI）都指明確切檔案 + 要抽取什麼 + 行為目標——對既有 code 的對齊指令，非 TBD。
- **型別一致**：`FrrBenchmark`/`frr_points_from_stats`/`FRR_ANNUALIZATION` 貫穿 Task 2/3/5；`WeeklyCellRow`/`compute_weekly_rows`/`calendar_week_start` 貫穿 Task 4/5；`AttributionWeeklyRow` 欄位 = migration = DTO camelCase 對映 = FE `WeeklyAttributionPoint`（逐欄比對過）；`fill_duration_days` 公開化後 Task 4 引用同名。
- **Invariant 檢查**：全部新 code 對 event_log/diagnostics 只讀；唯一寫入 = `attribution_weekly`（bot owner role，webapi 只 GRANT SELECT）；`decide_verdict` 狀態機零改動；weekly chain 跑在 one-shot container（不進 bot daemon、不碰 single-writer）。
- **多 agent adversarial verify（2026-07-06，5 verifiers × Opus 對 codebase 實測）已跑並修正**：2 blocker + 6 major + 若干 minor 全數修入。Blocker：(1) `_compute_verdict` 4→5-tuple 漏改 test_g3_loaders.py 的 7 個 unpack 點（Task 3(c) 補齊）；(2) `_period_for_cell` 對 p30/p7/p14 cell 會 `cell_period_days` raise ValueError 炸整個 job（Task 5 改 try/except fail-soft p2 + 加 p30 回歸測試）。Major：frr_bench code 不可跑（`paired_active_returns` 已是 diff list、`bootstrap_ci` 缺 stat_fn、`strat_outcomes` 實名、default 需 hoist 出 fills branch 防 UnboundLocalError）、Task 2 測試 import 放檔尾觸 E402 不可 autofix（移頂部）、FE test 用未 wire 的 jest-dom（改 `@/lib/test-utils` + `container.textContent`）、attribution page 缺 QueryError import、compose `$(date +%F)` 需 `$$` escape。Minor：webapi SELECT 加 env/account scope、`_fill_duration_days` 只有 1 個 call site、alembic env.py 用 `ruff check --fix`、移除 unused `import json`、Task 4「7 PASS」→「6 PASS」、ingest CLI `--symbols fUST,fUSD`、backfill vs forward-fill 區分。第一輪 verify 因 Fable 5 額度耗盡全 error，改用 Opus 4.8 重跑取得上述結果。
