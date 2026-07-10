# Strategy-Review Batch (E2 flip enablers + measurement + stranding + ladder observe) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Land the five verified items from the 2026-07-10 strategy review: persist E1/E2 flags in committed deploy config (fixing the `.env.runtime` regeneration landmine), fix single-active-cell capital stranding, add a utilization-adjusted AlwaysFRR baseline, add config-regime + fill-latency telemetry so the E2 flip is measurable within days, and an observe-only spike-rung ladder.

**Architecture:** All changes follow existing house patterns: pure policy functions beside `sizing.py`/`book_clamp.py`, read-model tables rebuilt from `event_log` (SoT untouched), observe-only logging before any enforce path, alembic for every schema change. No new dependencies.

**Tech Stack:** Python 3.13, SQLAlchemy 2.0 async, Alembic, pytest. All commands run from `backend_py/` (`cd backend_py && uv run ...`).

## Global Constraints

- Every change ships with unit tests; `cd backend_py && uv run pytest -m "not integration"` must pass before every commit.
- `cd backend_py && uv run mypy src/ && uv run ruff check` must pass before every commit.
- Migrations only via `uv run alembic revision --autogenerate` + `uv run alembic upgrade head` — never raw SQL.
- `event_log` is append-only SoT; new tables are read models or telemetry only.
- Commit messages use the repo emoji prefixes (`✨ Feat:` / `🐛 Fix:` / `🩹 Patch:` / `📝 Docs:` / `✅ Test:`).
- Decimal literals via `Decimal(str(...))` when converting floats (repo convention, ae2c59d).
- Observe-only features must produce zero behavioral difference to submits.

---

### Task 1: Persist E1/E2 flags in `deploy/vm/canary.env` (deploy-regeneration landmine fix)

**Files:**
- Modify: `deploy/vm/canary.env`
- Modify: `backend_py/ARCHITECTURE.md:257-263` (params table rows for reprice/clamp)

**Interfaces:**
- Consumes: nothing (config-only).
- Produces: committed source of truth for `BFX_REPRICE_*` / `BFX_CLAMP_*`; `deploy-vm.sh` regenerates `.env.runtime` as `cat ~/bfx/bot.env deploy/vm/canary.env` — phase file is concatenated last, so these values win over any stray copies in `bot.env`.

**Context:** On 2026-07-07 `BFX_REPRICE_ENABLED=true` was written directly into the VM's `.env.runtime`. `scripts/deploy-vm.sh` regenerates that file from `bot.env` + `deploy/vm/canary.env` on every deploy, so the next deploy would silently revert E1 to observe-only. This task makes the committed phase file the source of truth and simultaneously arms E2 (user-approved 2026-07-10, superseding the ~07-14 date gate; the VM runbook in Task 7 still checks the observe logs before restart).

- [ ] **Step 1: Append the E1/E2 block to `deploy/vm/canary.env`**

Append after the existing `BFX_HEALTHZ_PORT=8080` line:

```bash
# E1 stale-offer reprice sweep — live since 2026-07-07 (was hand-edited into
# .env.runtime; committed here so deploy-vm.sh regeneration keeps it).
BFX_REPRICE_ENABLED=true
BFX_REPRICE_TOLERANCE_PCT=0.10
BFX_REPRICE_MIN_AGE_S=1800
BFX_REPRICE_MAX_CANCELS_PER_TICK=3
# E2 book-aware clamp — armed 2026-07-10 (strategy review; user waived the
# ~07-14 date gate, log gate still applies — see VM runbook in the plan).
BFX_CLAMP_ENABLED=true
BFX_CLAMP_MAX_DOWN_PCT=0.15
BFX_CLAMP_TAKER_MAX_PERIOD_D=7
```

- [ ] **Step 2: Update `backend_py/ARCHITECTURE.md` §4 params table**

In the params table (around line 263), replace the Book clamp row:

```markdown
| Book clamp（E2） | observe（enabled=false）；down floor 15%；taker ≤7d | `BFX_CLAMP_ENABLED`、`BFX_CLAMP_MAX_DOWN_PCT`、`BFX_CLAMP_TAKER_MAX_PERIOD_D` |
```

with:

```markdown
| Reprice sweep（E1） | enabled（canary 2026-07-07 起）；tolerance 10%；min age 30min；≤3 cancels/tick | `BFX_REPRICE_ENABLED`、`BFX_REPRICE_TOLERANCE_PCT`、`BFX_REPRICE_MIN_AGE_S`、`BFX_REPRICE_MAX_CANCELS_PER_TICK` |
| Book clamp（E2） | enabled（canary 2026-07-10 起）；down floor 15%；taker ≤7d；flags source of truth = `deploy/vm/canary.env` | `BFX_CLAMP_ENABLED`、`BFX_CLAMP_MAX_DOWN_PCT`、`BFX_CLAMP_TAKER_MAX_PERIOD_D` |
```

- [ ] **Step 3: Sanity-check env parsing agrees with the file**

Run: `cd backend_py && uv run python -c "
from bfx_funding_bot.modules.execution.deployment.reprice import policy_from_env
from bfx_funding_bot.modules.execution.deployment.book_clamp import clamp_policy_from_env
env = {}
for line in open('../deploy/vm/canary.env'):
    line = line.strip()
    if line and not line.startswith('#') and '=' in line:
        k, v = line.split('=', 1); env[k] = v
print(policy_from_env(env))
print(clamp_policy_from_env(env))
"`
Expected: `RepricePolicy(enabled=True, tolerance_pct=0.1, min_age_ms=1800000, max_cancels_per_tick=3)` and `ClampPolicy(enabled=True, max_down_pct=0.15, taker_max_period_days=7)`.

- [ ] **Step 4: Commit**

```bash
git add deploy/vm/canary.env backend_py/ARCHITECTURE.md
git commit -m "🩹 Patch: E1/E2 flags 入 deploy/vm/canary.env（修 .env.runtime 重生成回退地雷）+ E2 canary arm

Lesson: deploy-vm.sh 每次 cat bot.env + canary.env 重生成 .env.runtime — 手改 .env.runtime 的 flag 下次 deploy 必回退。committed phase env 才是 source of truth。"
```

---

### Task 2: Single-active-cell stranding fix (`allocate_gap` cap relaxation + tracker clamp alignment)

**Files:**
- Modify: `backend_py/src/bfx_funding_bot/modules/execution/deployment/sizing.py:47`
- Modify: `backend_py/src/bfx_funding_bot/modules/execution/deployment/reconciler.py:202-210`
- Test: `backend_py/tests/modules/execution/deployment/test_sizing.py`
- Test: `backend_py/tests/modules/execution/deployment/test_reconciler.py`

**Interfaces:**
- Consumes: existing `allocate_gap(*, target, current_exposure, deployed, active_cells, concentration_pct, min_fill, available_headroom)` signature — unchanged.
- Produces: relaxed per-cell cap semantics: `cap_per_cell = max(concentration_pct * target, target / len(active_cells))`. `DeploymentReconciler.deploy` passes the SAME relaxed cap to `tracker.reconcile_to_total` (moving the `active` computation above it).

**Context:** With 2 configured fUST cells but only 1 holding an active POST quote, `cap_per_cell = 0.70 × 10000 = 7000` strands up to 3000 USDT at 0%. Both fUST cells run the identical MR strategy, so the 70% cap buys no diversification in the degenerate case. `max(concentration_pct*target, target/n_active)` is behavior-identical for ≥2 active cells (`10000/2 = 5000 < 7000`) and fully deploys for 1 active cell. The tracker clamp site must use the same value or `reconcile_to_total` spuriously clamp-warns every tick once a lone cell's intent legitimately exceeds 70%.

- [ ] **Step 1: Write the failing tests**

Append to `backend_py/tests/modules/execution/deployment/test_sizing.py`:

```python
class TestSingleActiveCellRelaxation:
    def test_single_active_cell_absorbs_full_gap(self):
        # 1 active cell of 2 configured: 70% cap would strand 3000 — relaxed
        # cap (target / n_active = 10000) lets the lone cell take everything.
        fills = allocate_gap(
            target=Decimal("10000"),
            current_exposure=Decimal("0"),
            deployed={},
            active_cells=["fUST_p2"],
            concentration_pct=Decimal("0.70"),
            min_fill=Decimal("153"),
        )
        assert fills == {"fUST_p2": Decimal("10000")}

    def test_two_active_cells_unchanged(self):
        # max(0.70*10000, 10000/2) = 7000 — byte-identical to pre-change split.
        fills = allocate_gap(
            target=Decimal("10000"),
            current_exposure=Decimal("0"),
            deployed={},
            active_cells=["fUST_a30", "fUST_p2"],
            concentration_pct=Decimal("0.70"),
            min_fill=Decimal("153"),
        )
        assert fills == {"fUST_a30": Decimal("7000"), "fUST_p2": Decimal("3000")}

    def test_single_active_cell_respects_headroom(self):
        # Relaxation raises the CAP, never the gap: balance headroom still binds.
        fills = allocate_gap(
            target=Decimal("10000"),
            current_exposure=Decimal("0"),
            deployed={},
            active_cells=["fUST_p2"],
            concentration_pct=Decimal("0.70"),
            min_fill=Decimal("153"),
            available_headroom=Decimal("4000"),
        )
        assert fills == {"fUST_p2": Decimal("4000")}
```

(match the existing import style at the top of the file — `allocate_gap` and `Decimal` are already imported there.)

- [ ] **Step 2: Run tests to verify the first fails**

Run: `cd backend_py && uv run pytest tests/modules/execution/deployment/test_sizing.py -k SingleActiveCell -v`
Expected: `test_single_active_cell_absorbs_full_gap` FAILS (fills == {"fUST_p2": Decimal("7000")}); the other two PASS (they pin current behavior).

- [ ] **Step 3: Implement the relaxation in `sizing.py`**

Replace line 47:

```python
    cap_per_cell = concentration_pct * target
```

with:

```python
    # Degenerate-case relaxation: with a single active cell the concentration
    # cap buys no diversification (all cells run the same strategy per symbol)
    # and strands (1 − concentration_pct) × target at 0%. max() is
    # behavior-identical for ≥2 active cells.
    cap_per_cell = max(concentration_pct * target, target / len(active_cells))
```

(`active_cells` is guaranteed non-empty here — the `if gap < min_fill or not active_cells` guard on line 44 returns first.)

- [ ] **Step 4: Run sizing tests**

Run: `cd backend_py && uv run pytest tests/modules/execution/deployment/test_sizing.py -v`
Expected: ALL PASS (including pre-existing tests — they use ≥2 active cells or hit the cap with equal math).
If a pre-existing test fails: it is pinning the old stranding behavior for ONE active cell — update that test's expectation to the relaxed cap and note it in the commit body.

- [ ] **Step 5: Write the failing reconciler alignment test**

Append to `backend_py/tests/modules/execution/deployment/test_reconciler.py` (reuse the file's existing builder/fixture helpers for a reconciler with 2 cells — follow the pattern of the nearest existing test that asserts on `tracker` state):

```python
async def test_tracker_clamp_uses_relaxed_cap_for_single_active_cell(
    reconciler_env_factory,
):
    """With 1 active POST quote of 2 configured cells, the tracker rescale clamp
    must use the SAME relaxed cap as allocate_gap — otherwise reconcile_to_total
    clamp-warns every tick once the lone cell's intent legitimately exceeds
    concentration_pct * cap."""
    env = reconciler_env_factory(
        cap=Decimal("10000"),
        active_quotes={"fUST_p2": 0.0002},   # only one of the two cells POSTs
        reserved=Decimal("9000"),            # lone cell's venue-true intent > 7000
        deployed={"fUST_p2": Decimal("9000")},
    )
    await env.reconciler.deploy()
    # NOT clamped down to 7000: relaxed cap = max(7000, 10000/1) = 10000.
    assert env.tracker.deployed("fUST_p2") == Decimal("9000")
```

**Note to implementer:** `test_reconciler.py` may not have a factory with these exact knobs — adapt to the file's existing helper (there are existing tests constructing `DeploymentReconciler` with a stub ledger/store/tracker; copy the nearest one and set: 2 cells configured, store returns an active quote for only one, `tracker.record_deploy("fUST_p2", Decimal("9000"))` beforehand, ledger `reserved_exposure` returning `Decimal("9000")`). The assertion is the contract; the setup mechanics should follow the file's local idiom.

- [ ] **Step 6: Run it to verify it fails**

Run: `cd backend_py && uv run pytest tests/modules/execution/deployment/test_reconciler.py -k relaxed_cap -v`
Expected: FAIL — `deployed("fUST_p2") == Decimal("7000")` (clamped by the un-relaxed cap).

- [ ] **Step 7: Align the reconciler tracker clamp**

In `backend_py/src/bfx_funding_bot/modules/execution/deployment/reconciler.py`, move the `active` computation above the tracker rescale and reuse the relaxed formula. Replace lines 202-210:

```python
            cap_per_cell = self._concentration_pct * cap
            self._tracker.reconcile_to_total(
                self._ledger.reserved_exposure(symbol),
                cells=[c.cell_id for c in symbol_cells],
                cap_per_cell=cap_per_cell,
            )

            active = [c.cell_id for c in symbol_cells
                      if self._store.get_active(c.cell_id, now_ms=now) is not None]
```

with:

```python
            active = [c.cell_id for c in symbol_cells
                      if self._store.get_active(c.cell_id, now_ms=now) is not None]
            # Keep the tracker's defense-in-depth clamp aligned with
            # allocate_gap's relaxed per-cell cap (single-active-cell case),
            # or reconcile_to_total spuriously clamp-warns every tick while a
            # lone cell legitimately holds more than concentration_pct * cap.
            # No active cells → allocation below is a no-op; keep the strict cap.
            cap_per_cell = (
                max(self._concentration_pct * cap, cap / len(active))
                if active else self._concentration_pct * cap
            )
            self._tracker.reconcile_to_total(
                self._ledger.reserved_exposure(symbol),
                cells=[c.cell_id for c in symbol_cells],
                cap_per_cell=cap_per_cell,
            )
```

- [ ] **Step 8: Run the full deployment test module + quality gates**

Run: `cd backend_py && uv run pytest tests/modules/execution/deployment/ -v && uv run mypy src/ && uv run ruff check`
Expected: ALL PASS.

- [ ] **Step 9: Commit**

```bash
git add backend_py/src/bfx_funding_bot/modules/execution/deployment/sizing.py \
        backend_py/src/bfx_funding_bot/modules/execution/deployment/reconciler.py \
        backend_py/tests/modules/execution/deployment/test_sizing.py \
        backend_py/tests/modules/execution/deployment/test_reconciler.py
git commit -m "🐛 Fix: 單一 active cell 時 concentration cap 擱淺 30% 資金（relaxed cap = max(pct×target, target/n_active)）

sizing.allocate_gap 與 reconciler tracker clamp 同步同公式，避免 lone-cell 合法超過 70% 時每 tick 誤 clamp-warn。≥2 active cells 行為 byte-identical。"
```

---

### Task 3: Utilization-adjusted AlwaysFRR baseline (`baseline_frr_util_apr_net_pct`)

**Files:**
- Modify: `backend_py/src/bfx_funding_bot/modules/live_validation/weekly_attribution.py`
- Modify: `backend_py/src/bfx_funding_bot/modules/live_validation/tables.py`
- Modify: `backend_py/scripts/run_weekly_attribution.py`
- Modify: `backend_py/src/bfx_funding_bot/modules/api/schemas.py` (WeeklyAttributionResponse)
- Modify: `backend_py/src/bfx_funding_bot/modules/api/attribution.py` (`_to_response`)
- Create: `backend_py/alembic/versions/<autogen>_add_frr_util_baseline.py` (autogenerated)
- Test: `backend_py/tests/modules/live_validation/test_weekly_attribution.py`
- Test: `backend_py/tests/scripts/test_weekly_attribution_loader.py`
- Test: `backend_py/tests/test_attribution_router.py`

**Interfaces:**
- Consumes: `FundingStatRow.funding_amount` / `funding_amount_used` (already ingested weekly by `scripts/ingest_funding_stats.py`; both nullable floats).
- Produces: `WeeklyCellRow.baseline_frr_util_apr_net_pct: Decimal | None`; `compute_weekly_rows(..., utilization_points: list[MarketRatePoint])` (new keyword, required); DB column `attribution_weekly.baseline_frr_util_apr_net_pct NUMERIC NULL`; API field `baselineFrrUtilAprNetPct` (follow the alias convention of the sibling fields in `WeeklyAttributionResponse`).

**Context:** The idealized AlwaysFRR arm assumes full utilization; real FRR offers only fill when the market rate crosses FRR. Market-level utilization `funding_amount_used / funding_amount` is directly observable per funding_stats snapshot. Keep the idealized column (upper bound, continuity); add the utilization-adjusted line — the cap-gate spread should read the adjusted one. `MarketRatePoint(mts, rate)` is reused for utilization ratios (a `Decimal` in [0,1]).

- [ ] **Step 1: Write the failing compute tests**

Append to `backend_py/tests/modules/live_validation/test_weekly_attribution.py` (reuse the file's existing fixtures/helpers for building fills and points — mirror the style of the existing baseline tests):

```python
class TestUtilizationAdjustedFrrBaseline:
    def test_util_baseline_scales_idealized_by_mean_utilization(self):
        wk = calendar_week_start(1_700_000_000_000)
        frr = [MarketRatePoint(mts=wk + 1000, rate=Decimal("0.0002"))]
        util = [
            MarketRatePoint(mts=wk + 1000, rate=Decimal("0.8")),
            MarketRatePoint(mts=wk + 2000, rate=Decimal("0.6")),
        ]
        rows = compute_weekly_rows(
            fills_by_cell={"fUST_p2": []},
            close_points=[],
            frr_points=frr,
            utilization_points=util,
        )
        row = next(r for r in rows if r.week_start_ms == wk)
        # idealized = 0.0002*365*100*0.85 = 6.205; mean util = 0.7
        assert row.baseline_frr_apr_net_pct == Decimal("6.20500")
        assert row.baseline_frr_util_apr_net_pct == Decimal("6.20500") * Decimal("0.7")

    def test_util_baseline_none_when_no_utilization_data(self):
        wk = calendar_week_start(1_700_000_000_000)
        rows = compute_weekly_rows(
            fills_by_cell={"fUST_p2": []},
            close_points=[],
            frr_points=[MarketRatePoint(mts=wk + 1000, rate=Decimal("0.0002"))],
            utilization_points=[],
        )
        row = next(r for r in rows if r.week_start_ms == wk)
        assert row.baseline_frr_apr_net_pct is not None
        assert row.baseline_frr_util_apr_net_pct is None
```

(Adjust the exact `Decimal` expectations if the file's existing tests assert with `pytest.approx`-style comparisons — follow local idiom; the contract is `util_adjusted == idealized × mean(utilization)` and `None` without data.)

- [ ] **Step 2: Run to verify failure**

Run: `cd backend_py && uv run pytest tests/modules/live_validation/test_weekly_attribution.py -k Utilization -v`
Expected: FAIL with `TypeError: compute_weekly_rows() got an unexpected keyword argument 'utilization_points'`.

- [ ] **Step 3: Implement in `weekly_attribution.py`**

1. Extend the docstring 語意 block (after the `baseline_*` line, around line 15):

```python
- baseline_frr_util_apr_net_pct = baseline_frr_apr_net_pct × mean(市場 utilization)
  — funding_amount_used / funding_amount（市場級、每 stats snapshot），把
  「FRR 掛單只在 market ≥ FRR 時成交」的 idle 折進 benchmark。idealized 欄
  保留當 upper bound；cap-gate spread 應讀本欄。
```

2. Add the field to `WeeklyCellRow` (after `baseline_frr_apr_net_pct`):

```python
    baseline_frr_util_apr_net_pct: Decimal | None
```

3. Change `compute_weekly_rows` signature and body:

```python
def compute_weekly_rows(
    *,
    fills_by_cell: dict[str, list[FillRecord]],
    close_points: list[MarketRatePoint],
    frr_points: list[MarketRatePoint],
    utilization_points: list[MarketRatePoint],
) -> list[WeeklyCellRow]:
```

after `frr_by_week = _mean_rate_by_week(frr_points)` add:

```python
    util_by_week = _mean_rate_by_week(utilization_points)
```

and in the row construction, after `baseline_frr_apr_net_pct=...`:

```python
                baseline_frr_util_apr_net_pct=(
                    _baseline_apr_net(frr_by_week.get(wk)) * util_by_week[wk]
                    if frr_by_week.get(wk) is not None and wk in util_by_week
                    else None
                ),
```

- [ ] **Step 4: Run compute tests**

Run: `cd backend_py && uv run pytest tests/modules/live_validation/test_weekly_attribution.py -v`
Expected: new tests PASS; pre-existing `compute_weekly_rows` callers in this test file FAIL with the missing-kwarg TypeError — fix them by passing `utilization_points=[]` (behavioral no-op). All PASS after.

- [ ] **Step 5: Write the failing loader test**

Append to `backend_py/tests/scripts/test_weekly_attribution_loader.py` (reuse its sqlite session fixtures and row-seeding helpers — mirror the existing test that seeds `FundingStatRow`):

```python
async def test_loader_builds_utilization_points_from_funding_stats(seeded_session_factory):
    """funding_amount(_used) present → utilization point used/total; missing or
    zero total → no point for that snapshot (None column ≠ 0 utilization)."""
    # seed three FundingStatRow snapshots inside the fill week:
    #   (amount=1000, used=800) → 0.8
    #   (amount=None, used=500) → skipped
    #   (amount=0,    used=0)   → skipped (guard div-by-zero)
    # expected weekly mean = 0.8
    rows = await load_and_compute(
        seeded_session_factory, account_id="primary", deployment_environment="prod",
    )
    row = next(r for r in rows if r.baseline_frr_apr_net_pct is not None)
    assert row.baseline_frr_util_apr_net_pct == row.baseline_frr_apr_net_pct * Decimal("0.8")
```

**Note to implementer:** follow the file's existing seeding helper exactly (it already seeds `FundingStatRow` with `frr`/`avg_period` for the FRR baseline test); extend the seeded rows with `funding_amount` / `funding_amount_used` values per the comment above.

- [ ] **Step 6: Run to verify failure, then implement the loader**

Run: `cd backend_py && uv run pytest tests/scripts/test_weekly_attribution_loader.py -k utilization -v`
Expected: FAIL (loader doesn't pass `utilization_points`; TypeError from compute).

In `backend_py/scripts/run_weekly_attribution.py`, after the `frr_stats = [...]` block, add:

```python
    utilization_points = [
        MarketRatePoint(
            mts=r.mts,
            rate=(
                Decimal(str(r.funding_amount_used)) / Decimal(str(r.funding_amount))
            ),
        )
        for r in frr_rows
        if r.funding_amount is not None
        and r.funding_amount_used is not None
        and r.funding_amount > 0
    ]
```

and thread it into the return:

```python
    return compute_weekly_rows(
        fills_by_cell=fills_by_cell,
        close_points=close_points,
        frr_points=frr_points_from_stats(frr_stats),
        utilization_points=utilization_points,
    )
```

In `persist_rows`, add the column mapping after `baseline_frr_apr_net_pct=r.baseline_frr_apr_net_pct,`:

```python
                baseline_frr_util_apr_net_pct=r.baseline_frr_util_apr_net_pct,
```

- [ ] **Step 7: Add the table column + migration**

In `backend_py/src/bfx_funding_bot/modules/live_validation/tables.py`, after `baseline_frr_apr_net_pct`:

```python
    baseline_frr_util_apr_net_pct: Mapped[Decimal | None] = mapped_column(Numeric, nullable=True)
```

Run: `cd backend_py && uv run alembic revision --autogenerate -m "add baseline_frr_util_apr_net_pct to attribution_weekly"`
Inspect the generated file: it must contain exactly one `op.add_column("attribution_weekly", sa.Column("baseline_frr_util_apr_net_pct", sa.Numeric(), nullable=True))` (+ matching downgrade). Remove any unrelated autogen noise.
Run: `cd backend_py && uv run alembic upgrade head && uv run alembic check`
Expected: upgrade clean; `alembic check` reports no drift.

- [ ] **Step 8: API surface**

In `backend_py/src/bfx_funding_bot/modules/api/schemas.py`, in `WeeklyAttributionResponse`, add after `baseline_frr_apr_net_pct` (copy the exact Field/alias pattern of that sibling — the file uses camelCase aliases):

```python
    baseline_frr_util_apr_net_pct: str | None = Field(
        default=None, serialization_alias="baselineFrrUtilAprNetPct",
    )
```

In `backend_py/src/bfx_funding_bot/modules/api/attribution.py` `_to_response`, after the `baseline_frr_apr_net_pct` entry:

```python
        baseline_frr_util_apr_net_pct=(
            _dec_str(row.baseline_frr_util_apr_net_pct)
            if row.baseline_frr_util_apr_net_pct is not None else None
        ),
```

In `backend_py/tests/test_attribution_router.py`, extend the existing happy-path test's seeded row + expected JSON with the new field (follow the file's existing seeding: add `baseline_frr_util_apr_net_pct=Decimal("4.34")` → expect `"baselineFrrUtilAprNetPct": "4.34"`).

**Note:** the frontend chart adding a 4th line is follow-up FE work (`frontend/`), out of scope here — unknown JSON fields are ignored by the existing chart.

- [ ] **Step 9: Full gates + commit**

Run: `cd backend_py && uv run pytest -m "not integration" && uv run mypy src/ && uv run ruff check`
Expected: ALL PASS.

```bash
git add backend_py/src/bfx_funding_bot/modules/live_validation/weekly_attribution.py \
        backend_py/src/bfx_funding_bot/modules/live_validation/tables.py \
        backend_py/scripts/run_weekly_attribution.py \
        backend_py/src/bfx_funding_bot/modules/api/schemas.py \
        backend_py/src/bfx_funding_bot/modules/api/attribution.py \
        backend_py/alembic/versions/ \
        backend_py/tests/modules/live_validation/test_weekly_attribution.py \
        backend_py/tests/scripts/test_weekly_attribution_loader.py \
        backend_py/tests/test_attribution_router.py
git commit -m "✨ Feat: AlwaysFRR baseline 加 utilization-adjusted 欄（funding_amount_used/funding_amount）

idealized 欄保留當 upper bound；cap-gate spread 改讀 util-adjusted。市場級 utilization 直接可觀測，不用 traded≥FRR proxy（會混淆成交頻率與 capital-time utilization）。"
```

---

### Task 4: `config_regime` table + daemon boot write

**Files:**
- Modify: `backend_py/src/bfx_funding_bot/modules/live_validation/tables.py` (add `ConfigRegimeRow`)
- Create: `backend_py/src/bfx_funding_bot/modules/live_validation/regime.py`
- Modify: `backend_py/src/bfx_funding_bot/modules/marketfeed/daemon.py` (boot write, ~line 1061-1075 where reprice/clamp policies are built)
- Create: `backend_py/alembic/versions/<autogen>_add_config_regime.py` (autogenerated)
- Test: `backend_py/tests/modules/live_validation/test_regime.py`

**Interfaces:**
- Consumes: `RepricePolicy.enabled` / `ClampPolicy.enabled` (already built in `build_daemon`), `session_factory`, `account_id`, `env_str`.
- Produces: table `config_regime(deployment_environment, account_id, recorded_at_ms, clamp_enabled, reprice_enabled, git_sha)` (PK = first three); `async def record_config_regime(session_factory, *, account_id, deployment_environment, clamp_enabled, reprice_enabled, git_sha, now_ms) -> None`. Task 5's report script reads this table for regime boundaries.

**Context:** Flag flips coincide with bot restarts (config is boot-immutable). One row per boot gives exact regime boundaries, so before/after comparisons around the E2 flip are valid without waiting ~2 months of weekly windows.

- [ ] **Step 1: Write the failing test**

Create `backend_py/tests/modules/live_validation/test_regime.py` (reuse the sqlite `session_factory` fixture pattern from `tests/modules/live_validation/`'s existing tests — same `Base.metadata.create_all` conftest machinery):

```python
"""config_regime — one row per daemon boot; regime boundary = restart."""
import pytest
from sqlalchemy import select

from bfx_funding_bot.modules.live_validation.regime import record_config_regime
from bfx_funding_bot.modules.live_validation.tables import ConfigRegimeRow


@pytest.mark.asyncio
async def test_record_config_regime_inserts_row(session_factory):
    await record_config_regime(
        session_factory,
        account_id="primary",
        deployment_environment="prod",
        clamp_enabled=True,
        reprice_enabled=True,
        git_sha="abc1234",
        now_ms=1_752_100_000_000,
    )
    async with session_factory() as s:
        row = (await s.execute(select(ConfigRegimeRow))).scalar_one()
    assert (row.clamp_enabled, row.reprice_enabled) == (True, True)
    assert row.recorded_at_ms == 1_752_100_000_000
    assert row.git_sha == "abc1234"


@pytest.mark.asyncio
async def test_record_config_regime_is_best_effort(session_factory):
    """Boot must not die on telemetry: same-PK double insert logs, doesn't raise."""
    for _ in range(2):
        await record_config_regime(
            session_factory,
            account_id="primary",
            deployment_environment="prod",
            clamp_enabled=False,
            reprice_enabled=False,
            git_sha=None,
            now_ms=1_752_100_000_000,
        )
    async with session_factory() as s:
        rows = (await s.execute(select(ConfigRegimeRow))).scalars().all()
    assert len(rows) == 1
```

- [ ] **Step 2: Run to verify failure**

Run: `cd backend_py && uv run pytest tests/modules/live_validation/test_regime.py -v`
Expected: FAIL with `ImportError` (no `regime` module / `ConfigRegimeRow`).

- [ ] **Step 3: Implement table + helper**

Append to `backend_py/src/bfx_funding_bot/modules/live_validation/tables.py`:

```python
class ConfigRegimeRow(Base):
    """One row per daemon boot — execution-policy regime boundaries.

    Flag flips require a restart (config is boot-immutable), so boots ARE the
    regime boundaries. Task: attribute execution-quality metrics (fill latency,
    realized APR) to the flag state that produced them, without waiting for
    weekly windows to accumulate. Telemetry, not SoT — prunable.
    """

    __tablename__ = "config_regime"

    deployment_environment: Mapped[str] = mapped_column(Text, primary_key=True)
    account_id: Mapped[str] = mapped_column(Text, primary_key=True)
    recorded_at_ms: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer(), "sqlite"), primary_key=True,
    )
    clamp_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False)
    reprice_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False)
    git_sha: Mapped[str | None] = mapped_column(Text, nullable=True)
```

(add `Boolean` to the existing `from sqlalchemy import ...` line.)

Create `backend_py/src/bfx_funding_bot/modules/live_validation/regime.py`:

```python
"""config_regime writer — one best-effort row per daemon boot."""
from __future__ import annotations

import logging

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bfx_funding_bot.modules.live_validation.tables import ConfigRegimeRow

log = logging.getLogger(__name__)


async def record_config_regime(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    account_id: str,
    deployment_environment: str,
    clamp_enabled: bool,
    reprice_enabled: bool,
    git_sha: str | None,
    now_ms: int,
) -> None:
    """Insert this boot's execution-policy regime row. Best-effort: telemetry
    must never block or kill the boot — any failure logs and returns."""
    try:
        async with session_factory() as session:
            session.add(ConfigRegimeRow(
                deployment_environment=deployment_environment,
                account_id=account_id,
                recorded_at_ms=now_ms,
                clamp_enabled=clamp_enabled,
                reprice_enabled=reprice_enabled,
                git_sha=git_sha,
            ))
            await session.commit()
        log.info(
            "config_regime_recorded clamp=%s reprice=%s sha=%s",
            clamp_enabled, reprice_enabled, git_sha,
        )
    except Exception:
        log.warning("config_regime_record_failed", exc_info=True)
```

- [ ] **Step 4: Run tests**

Run: `cd backend_py && uv run pytest tests/modules/live_validation/test_regime.py -v`
Expected: PASS.

- [ ] **Step 5: Migration**

Run: `cd backend_py && uv run alembic revision --autogenerate -m "add config_regime table"`
Inspect: exactly one `op.create_table("config_regime", ...)` with the six columns + composite PK. Remove unrelated noise.
Run: `cd backend_py && uv run alembic upgrade head && uv run alembic check`
Expected: clean.

- [ ] **Step 6: Daemon boot wiring**

In `backend_py/src/bfx_funding_bot/modules/marketfeed/daemon.py`, the reconciler construction (~line 1061-1073) currently inlines the policies:

```python
            reprice=policy_from_env(os.environ),
            ...
            clamp=clamp_policy_from_env(os.environ),
```

Hoist them to named locals immediately before that constructor call:

```python
        reprice_policy = policy_from_env(os.environ)
        clamp_policy = clamp_policy_from_env(os.environ)
```

use them in the constructor (`reprice=reprice_policy, ... clamp=clamp_policy`), and right after the reconciler is constructed add:

```python
        # Execution-policy regime telemetry: one row per boot (flags are
        # boot-immutable, so boots are the regime boundaries). Best-effort —
        # record_config_regime never raises.
        await record_config_regime(
            session_factory,
            account_id=account_id,
            deployment_environment=env_str,
            clamp_enabled=clamp_policy.enabled,
            reprice_enabled=reprice_policy.enabled,
            git_sha=os.environ.get("GIT_SHA"),
            now_ms=now_ms_utc(),
        )
```

Add the import beside the other live_validation imports (or create the import line):

```python
from bfx_funding_bot.modules.live_validation.regime import record_config_regime
```

**Guard:** the reconciler block may be inside `if not is_simulated:`-style conditionals — place the call in the SAME scope as the reconciler construction (live paths only; paper/shadow also fine if the scope covers them — flag telemetry is realm-scoped and harmless).

- [ ] **Step 7: Full gates + commit**

Run: `cd backend_py && uv run pytest -m "not integration" && uv run mypy src/ && uv run ruff check`
Expected: ALL PASS.

```bash
git add backend_py/src/bfx_funding_bot/modules/live_validation/tables.py \
        backend_py/src/bfx_funding_bot/modules/live_validation/regime.py \
        backend_py/src/bfx_funding_bot/modules/marketfeed/daemon.py \
        backend_py/alembic/versions/ \
        backend_py/tests/modules/live_validation/test_regime.py
git commit -m "✨ Feat: config_regime 表 + daemon boot 寫入（E2 flip 前後歸因的 regime 邊界）"
```

---

### Task 5: Execution-quality report (submit→fill latency per regime)

**Files:**
- Create: `backend_py/src/bfx_funding_bot/modules/live_validation/execution_quality.py`
- Create: `backend_py/scripts/report_execution_quality.py`
- Test: `backend_py/tests/modules/live_validation/test_execution_quality.py`

**Interfaces:**
- Consumes: `event_log` rows (`RESERVATION_CLAIMED`, `ORDER_FILL` — matched on `cid`), `config_regime` rows (Task 4).
- Produces: pure functions `pair_claims_to_fills(claims, fills) -> list[ClaimOutcome]`, `bucket_by_regime(outcomes, regime_starts) -> dict[int, list[ClaimOutcome]]`, `summarize(bucketed, regimes, ttl_ms) -> list[RegimeSummary]`; operator script printing one table row per regime.

**Context:** At 10k cap a 1%/yr delta is ~1.9 USDT/week — invisible in weekly APR for months. Submit→first-fill latency and fill-within-TTL share respond to a pricing change within days. Read-only over event_log; zero invariant risk.

- [ ] **Step 1: Write the failing tests**

Create `backend_py/tests/modules/live_validation/test_execution_quality.py`:

```python
"""Pure pairing/bucketing/summary logic — no I/O."""
from decimal import Decimal

from bfx_funding_bot.modules.live_validation.execution_quality import (
    ClaimEvent,
    ClaimOutcome,
    FillEvent,
    RegimeSummary,
    bucket_by_regime,
    pair_claims_to_fills,
    summarize,
)

TTL_MS = 3_900_000  # 65 min, same default as BFX_QUOTE_TTL_MS


def test_pairs_first_fill_per_cid_and_leaves_unfilled_none():
    claims = [
        ClaimEvent(cid=1, claimed_at_ms=1_000),
        ClaimEvent(cid=2, claimed_at_ms=2_000),
    ]
    fills = [
        FillEvent(cid=1, filled_at_ms=61_000),
        FillEvent(cid=1, filled_at_ms=99_000),  # second partial fill — ignored
    ]
    outcomes = pair_claims_to_fills(claims, fills)
    by_cid = {o.cid: o for o in outcomes}
    assert by_cid[1].latency_ms == 60_000
    assert by_cid[2].latency_ms is None


def test_bucket_by_regime_assigns_claims_to_latest_boot_before_them():
    outcomes = [
        ClaimOutcome(cid=1, claimed_at_ms=5_000, latency_ms=100),
        ClaimOutcome(cid=2, claimed_at_ms=15_000, latency_ms=200),
        ClaimOutcome(cid=3, claimed_at_ms=500, latency_ms=None),  # before any regime
    ]
    buckets = bucket_by_regime(outcomes, regime_starts=[1_000, 10_000])
    assert [o.cid for o in buckets[1_000]] == [1]
    assert [o.cid for o in buckets[10_000]] == [2]
    assert all(o.cid != 3 for b in buckets.values() for o in b)


def test_summarize_reports_fill_rate_and_percentiles():
    outcomes = [
        ClaimOutcome(cid=i, claimed_at_ms=1_000, latency_ms=lat)
        for i, lat in enumerate([10_000, 20_000, 30_000, None])
    ]
    [s] = summarize({1_000: outcomes}, ttl_ms=TTL_MS)
    assert s.regime_start_ms == 1_000
    assert s.n_claims == 4
    assert s.n_filled == 3
    assert s.fill_rate == Decimal("0.75")
    assert s.p50_latency_ms == 20_000
    assert s.p90_latency_ms == 30_000
    assert s.n_unfilled_past_ttl == 1
```

- [ ] **Step 2: Run to verify failure**

Run: `cd backend_py && uv run pytest tests/modules/live_validation/test_execution_quality.py -v`
Expected: FAIL with ImportError.

- [ ] **Step 3: Implement `execution_quality.py`**

```python
"""Execution-quality metrics — submit→first-fill latency per config regime.

Pure, I/O-free (loader = scripts/report_execution_quality.py). Weekly APR at
10k cap moves ~1.9 USDT/week per 1%/yr — invisible for months; latency and
fill-within-TTL respond to a pricing change (E2 clamp) within days.
"""
from __future__ import annotations

import bisect
from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True, slots=True)
class ClaimEvent:
    cid: int
    claimed_at_ms: int


@dataclass(frozen=True, slots=True)
class FillEvent:
    cid: int
    filled_at_ms: int


@dataclass(frozen=True, slots=True)
class ClaimOutcome:
    cid: int
    claimed_at_ms: int
    latency_ms: int | None  # None = never filled (as of the data snapshot)


@dataclass(frozen=True, slots=True)
class RegimeSummary:
    regime_start_ms: int
    n_claims: int
    n_filled: int
    fill_rate: Decimal
    p50_latency_ms: int | None
    p90_latency_ms: int | None
    n_unfilled_past_ttl: int


def pair_claims_to_fills(
    claims: list[ClaimEvent], fills: list[FillEvent],
) -> list[ClaimOutcome]:
    """First fill per cid wins (partial-fill streams share the cid)."""
    first_fill: dict[int, int] = {}
    for f in fills:
        cur = first_fill.get(f.cid)
        if cur is None or f.filled_at_ms < cur:
            first_fill[f.cid] = f.filled_at_ms
    return [
        ClaimOutcome(
            cid=c.cid,
            claimed_at_ms=c.claimed_at_ms,
            latency_ms=(
                first_fill[c.cid] - c.claimed_at_ms
                if c.cid in first_fill else None
            ),
        )
        for c in claims
    ]


def bucket_by_regime(
    outcomes: list[ClaimOutcome], regime_starts: list[int],
) -> dict[int, list[ClaimOutcome]]:
    """Assign each claim to the latest regime boot at-or-before it. Claims
    predating the first recorded regime are dropped (unknown flag state)."""
    starts = sorted(regime_starts)
    buckets: dict[int, list[ClaimOutcome]] = {s: [] for s in starts}
    for o in outcomes:
        i = bisect.bisect_right(starts, o.claimed_at_ms) - 1
        if i >= 0:
            buckets[starts[i]].append(o)
    return buckets


def _percentile(sorted_vals: list[int], pct: float) -> int:
    # ponytail: nearest-rank percentile — fine for operator tables,
    # swap for interpolation if this ever feeds a gate.
    idx = max(0, min(len(sorted_vals) - 1, round(pct * (len(sorted_vals) - 1))))
    return sorted_vals[idx]


def summarize(
    bucketed: dict[int, list[ClaimOutcome]], *, ttl_ms: int,
) -> list[RegimeSummary]:
    out: list[RegimeSummary] = []
    for start in sorted(bucketed):
        outcomes = bucketed[start]
        lats = sorted(o.latency_ms for o in outcomes if o.latency_ms is not None)
        out.append(RegimeSummary(
            regime_start_ms=start,
            n_claims=len(outcomes),
            n_filled=len(lats),
            fill_rate=(
                Decimal(len(lats)) / Decimal(len(outcomes))
                if outcomes else Decimal("0")
            ),
            p50_latency_ms=_percentile(lats, 0.5) if lats else None,
            p90_latency_ms=_percentile(lats, 0.9) if lats else None,
            n_unfilled_past_ttl=sum(
                1 for o in outcomes
                if o.latency_ms is None or o.latency_ms > ttl_ms
            ),
        ))
    return out
```

**Note:** `test_summarize_reports_fill_rate_and_percentiles` expects `n_unfilled_past_ttl == 1` — the three filled latencies are all < TTL and one claim never filled; verify the implementation counts never-filled as past-TTL (it does: `latency_ms is None` counts).

- [ ] **Step 4: Run tests**

Run: `cd backend_py && uv run pytest tests/modules/live_validation/test_execution_quality.py -v`
Expected: PASS.

- [ ] **Step 5: Create the loader script**

Create `backend_py/scripts/report_execution_quality.py`:

```python
"""Operator report — submit→first-fill latency + fill rate per config regime.

Read-only over event_log + config_regime. Run from backend_py/
(env: DATABASE_URL / BFX_ACCOUNT_ID / BFX_DEPLOYMENT_ENV):
  uv run python -m scripts.report_execution_quality
"""
from __future__ import annotations

import asyncio
import os
import sys
from datetime import datetime, timezone

from sqlalchemy import select

from bfx_funding_bot.core.db import make_engine, make_session_factory
from bfx_funding_bot.core.settings import Settings
from bfx_funding_bot.modules.execution.event_store.tables import EventLogRow
from bfx_funding_bot.modules.live_validation.execution_quality import (
    ClaimEvent,
    FillEvent,
    bucket_by_regime,
    pair_claims_to_fills,
    summarize,
)
from bfx_funding_bot.modules.live_validation.tables import ConfigRegimeRow

_TTL_MS = int(os.environ.get("BFX_QUOTE_TTL_MS", "3900000"))


def _fmt_ts(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")


def _fmt_min(ms: int | None) -> str:
    return f"{ms / 60_000:.1f}m" if ms is not None else "-"


async def _amain() -> int:
    settings = Settings()
    engine = make_engine(settings)
    sf = make_session_factory(engine)
    account_id = os.environ.get("BFX_ACCOUNT_ID", "default")
    env = os.environ.get("BFX_DEPLOYMENT_ENV", "prod")
    try:
        async with sf() as session:
            def _events(event_type: str):
                return (
                    select(EventLogRow)
                    .where(
                        EventLogRow.event_type == event_type,
                        EventLogRow.account_id == account_id,
                        EventLogRow.deployment_environment == env,
                        EventLogRow.cid.is_not(None),
                    )
                    .order_by(EventLogRow.occurred_at_ms)
                )
            claim_rows = (await session.execute(_events("RESERVATION_CLAIMED"))).scalars().all()
            fill_rows = (await session.execute(_events("ORDER_FILL"))).scalars().all()
            regime_rows = (
                await session.execute(
                    select(ConfigRegimeRow).where(
                        ConfigRegimeRow.account_id == account_id,
                        ConfigRegimeRow.deployment_environment == env,
                    ).order_by(ConfigRegimeRow.recorded_at_ms)
                )
            ).scalars().all()
    finally:
        await engine.dispose()

    outcomes = pair_claims_to_fills(
        [ClaimEvent(cid=r.cid, claimed_at_ms=r.occurred_at_ms) for r in claim_rows],
        [FillEvent(cid=r.cid, filled_at_ms=r.occurred_at_ms) for r in fill_rows],
    )
    if not regime_rows:
        print("no config_regime rows yet — deploy Task 4 first; showing single bucket")
        regime_starts = [0]
        flags_by_start = {0: ("?", "?")}
    else:
        regime_starts = [r.recorded_at_ms for r in regime_rows]
        flags_by_start = {
            r.recorded_at_ms: (str(r.clamp_enabled), str(r.reprice_enabled))
            for r in regime_rows
        }
    summaries = summarize(
        bucket_by_regime(outcomes, regime_starts=regime_starts), ttl_ms=_TTL_MS,
    )
    print(f"execution quality — account={account_id} env={env} ttl={_TTL_MS}ms")
    print("regime_start (UTC)  clamp reprice  claims filled fill%   p50    p90  unfilled>TTL")
    for s in summaries:
        clamp, reprice = flags_by_start.get(s.regime_start_ms, ("?", "?"))
        print(
            f"{_fmt_ts(s.regime_start_ms):19} {clamp:5} {reprice:7} "
            f"{s.n_claims:6} {s.n_filled:6} {float(s.fill_rate) * 100:5.1f} "
            f"{_fmt_min(s.p50_latency_ms):>6} {_fmt_min(s.p90_latency_ms):>6} "
            f"{s.n_unfilled_past_ttl:12}"
        )
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(_amain()))
```

**Check before finalizing:** confirm `EventLogRow.cid`'s column name/type in `modules/execution/event_store/tables.py` (int vs str) and adapt `ClaimEvent.cid`'s construction (`int(r.cid)` if Text). If the fill events carry cid only in payload (not the column), fall back to `int(row.payload.get("cid"))` — mirror however `run_weekly_attribution.py`/`_g3_loaders.py` read fills.

- [ ] **Step 6: Smoke the script against sqlite (no DB needed)**

Run: `cd backend_py && uv run python -c "import scripts.report_execution_quality"`
Expected: imports clean (mypy/ruff cover the rest).

- [ ] **Step 7: Full gates + commit**

Run: `cd backend_py && uv run pytest -m "not integration" && uv run mypy src/ && uv run ruff check`
Expected: ALL PASS.

```bash
git add backend_py/src/bfx_funding_bot/modules/live_validation/execution_quality.py \
        backend_py/scripts/report_execution_quality.py \
        backend_py/tests/modules/live_validation/test_execution_quality.py
git commit -m "✨ Feat: execution-quality report（submit→first-fill latency + fill rate per config regime）

event_log read-only；E2 flip 的效果幾天內可見，不用等 ≥8 weekly windows。"
```

---

### Task 6: Observe-only spike-rung ladder (`ladder_would_post` logging)

**Files:**
- Create: `backend_py/src/bfx_funding_bot/modules/execution/deployment/ladder.py`
- Modify: `backend_py/src/bfx_funding_bot/modules/execution/deployment/reconciler.py` (constructor + submit loop logging)
- Modify: `backend_py/src/bfx_funding_bot/modules/marketfeed/daemon.py` (wire `ladder_policy_from_env`)
- Test: `backend_py/tests/modules/execution/deployment/test_ladder.py`
- Test: `backend_py/tests/modules/execution/deployment/test_reconciler.py`

**Interfaces:**
- Consumes: `ticker.ask` (E2 ticker fetch already in the tick), fill `amount` from `allocate_gap`.
- Produces: `LadderPolicy(spike_fraction, rung_multipliers, min_rung_usdt)`; `ladder_policy_from_env(environ) -> LadderPolicy | None` (None = feature off, the default); `spike_rungs(*, amount, ask, policy) -> list[tuple[float, float]]` returning `[(rung_amount_usdt, rung_rate)]`. Reconciler logs `ladder_would_post` — **no submit behavior change of any kind.**

**Context:** The one capability a static ladder adds over E1+E2 is spike capture — offers resting above ask. Verified rec: two-stage, observe first. This task is observe ONLY: compute + log what rungs WOULD be posted. There is deliberately no enforce path (`# ponytail:` comment marks the upgrade path); enforce needs reprice-sweep exemption + clamp bypass tags and its own plan after observe data justifies it.

- [ ] **Step 1: Write the failing tests**

Create `backend_py/tests/modules/execution/deployment/test_ladder.py`:

```python
"""spike_rungs pure policy — observe-only E3-gated experiment (2026-07-10 review)."""
from bfx_funding_bot.modules.execution.deployment.ladder import (
    LadderPolicy,
    ladder_policy_from_env,
    spike_rungs,
)

POLICY = LadderPolicy(
    spike_fraction=0.15, rung_multipliers=(1.5, 3.0), min_rung_usdt=153.0,
)


def test_two_rungs_when_budget_covers_both():
    # 7000 * 0.15 = 1050 → 525/rung ≥ 153 → two rungs at ask×1.5 / ask×3.0
    rungs = spike_rungs(amount=7000.0, ask=0.0002, policy=POLICY)
    assert rungs == [(525.0, 0.0003), (525.0, 0.0006)]


def test_drops_to_one_rung_when_budget_too_small_for_two():
    # 1200 * 0.15 = 180 → 90/rung < 153 → single rung of the full 180? No:
    # 180 ≥ 153 → one rung at the LOWEST multiplier (highest fill odds).
    rungs = spike_rungs(amount=1200.0, ask=0.0002, policy=POLICY)
    assert rungs == [(180.0, 0.0003)]


def test_no_rungs_when_budget_below_venue_floor():
    # 1000 * 0.15 = 150 < 153 → nothing to post
    assert spike_rungs(amount=1000.0, ask=0.0002, policy=POLICY) == []


def test_no_rungs_on_degenerate_ask():
    assert spike_rungs(amount=7000.0, ask=0.0, policy=POLICY) == []


def test_policy_from_env_defaults_off():
    assert ladder_policy_from_env({}) is None


def test_policy_from_env_observe_on():
    p = ladder_policy_from_env({"BFX_LADDER_OBSERVE": "true"})
    assert p == POLICY
```

- [ ] **Step 2: Run to verify failure**

Run: `cd backend_py && uv run pytest tests/modules/execution/deployment/test_ladder.py -v`
Expected: FAIL with ImportError.

- [ ] **Step 3: Implement `ladder.py`**

```python
"""Spike-rung ladder policy (observe-only — 2026-07-10 strategy review).

純 policy：把每筆 cell fill 的一小片（spike_fraction）換算成掛在 ask 之上的
1-N 檔 spike rungs。競品（MikaLendingBot/eAndrius/instabot42）用靜態 ladder
替代動態改價；E1+E2 已是動態版，ladder 唯一多出的能力 = spike capture。

目前 OBSERVE-ONLY：reconciler 只 log `ladder_would_post`，submit 行為零改變。
# ponytail: enforce path 刻意不存在 — 需要 reprice-sweep 豁免 tag + clamp
# bypass（E2 UNDERCUT 會把 rung 夷平回 ask−1tick）+ E3 per-rung attribution，
# observe 數據證明 uplift 前不建（見 plan Task 6 context）。
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class LadderPolicy:
    spike_fraction: float        # 0.15 = 每筆 fill 切 15% 給 spike rungs
    rung_multipliers: tuple[float, ...]  # (1.5, 3.0) — rung rate = ask × k
    min_rung_usdt: float         # 153 — venue floor（與 sizing.effective_min 同源）


def ladder_policy_from_env(environ: Mapping[str, str]) -> LadderPolicy | None:
    """None = feature off（預設）。BFX_LADDER_OBSERVE=true 才建 policy。"""
    if environ.get("BFX_LADDER_OBSERVE", "false").lower() not in ("1", "true", "yes"):
        return None
    return LadderPolicy(
        spike_fraction=float(environ.get("BFX_LADDER_SPIKE_FRACTION", "0.15")),
        rung_multipliers=tuple(
            float(x) for x in environ.get("BFX_LADDER_MULTIPLIERS", "1.5,3.0").split(",")
        ),
        min_rung_usdt=float(environ.get("BFX_LADDER_MIN_RUNG_USDT", "153")),
    )


def spike_rungs(
    *, amount: float, ask: float, policy: LadderPolicy,
) -> list[tuple[float, float]]:
    """回傳 [(rung_amount_usdt, rung_rate)]。

    budget = amount × spike_fraction，均分到最多 len(rung_multipliers) 檔；
    每檔必須 ≥ min_rung_usdt（venue floor），不夠就減檔——保留最低 multiplier
    優先（最高成交機率的 rung 最後放棄）。ask 退化（≤0）→ 空。
    """
    if ask <= 0.0:
        return []
    budget = amount * policy.spike_fraction
    for n in range(len(policy.rung_multipliers), 0, -1):
        per_rung = budget / n
        if per_rung >= policy.min_rung_usdt:
            return [
                (per_rung, ask * k) for k in policy.rung_multipliers[:n]
            ]
    return []
```

- [ ] **Step 4: Run ladder tests**

Run: `cd backend_py && uv run pytest tests/modules/execution/deployment/test_ladder.py -v`
Expected: PASS.

- [ ] **Step 5: Write the failing reconciler observe-log test**

Append to `backend_py/tests/modules/execution/deployment/test_reconciler.py` (reuse the same env/factory idiom as Task 2's test; the reconciler needs a ticker_source + clamp policy so `ticker` is fetched, plus `ladder=LadderPolicy(...)`):

```python
async def test_ladder_observe_logs_rungs_without_touching_submits(
    reconciler_env_factory, caplog,
):
    env = reconciler_env_factory(
        cap=Decimal("10000"),
        active_quotes={"fUST_p2": 0.0002},
        ticker=FundingTicker(frr=0.0002, bid=0.0001, bid_period=2,
                             bid_size=0.0, ask=0.00025, ask_period=2,
                             ask_size=0.0, last_price=0.0002),
        ladder=LadderPolicy(spike_fraction=0.15, rung_multipliers=(1.5, 3.0),
                            min_rung_usdt=153.0),
    )
    with caplog.at_level(logging.INFO):
        await env.reconciler.deploy()
    assert any("ladder_would_post" in r.message for r in caplog.records)
    # observe-only invariant: exactly the same submits as without a ladder —
    # one offer per active cell at the (clamped) quote rate.
    assert len(env.executor.submitted) == 1
```

**Note to implementer:** `FundingTicker`'s actual field set lives in `external/bitfinex/rest.py:29` — construct it however `test_book_clamp.py`/existing reconciler clamp tests do; the contract is the log line + unchanged submit count.

- [ ] **Step 6: Run to verify failure, then wire the reconciler**

Run: `cd backend_py && uv run pytest tests/modules/execution/deployment/test_reconciler.py -k ladder -v`
Expected: FAIL (constructor has no `ladder` param).

In `reconciler.py`:

1. Import: `from bfx_funding_bot.modules.execution.deployment.ladder import LadderPolicy, spike_rungs`
2. Constructor: add keyword param `ladder: LadderPolicy | None = None` after `clamp`, store `self._ladder = ladder`.
3. In the submit loop, right AFTER the clamp log block (after `offer_rate` is final, before `decision = DecisionPayload(...)`), add:

```python
                # Observe-only spike-rung ladder（2026-07-10 review）：只 log
                # 會掛什麼 rungs，submit 行為零改變。enforce 見 ladder.py 頂註。
                if self._ladder is not None and ticker is not None and ticker.ask > TICK:
                    rungs = spike_rungs(
                        amount=float(amount), ask=ticker.ask, policy=self._ladder,
                    )
                    if rungs:
                        log.info(
                            "ladder_would_post cell=%s base_rate=%s rungs=%s ask=%s",
                            cell_id, offer_rate,
                            [(round(a, 2), r) for a, r in rungs], ticker.ask,
                        )
```

4. In `daemon.py`, add to the reconciler constructor call: `ladder=ladder_policy_from_env(os.environ),` with the import `from bfx_funding_bot.modules.execution.deployment.ladder import ladder_policy_from_env`.

- [ ] **Step 7: Full gates + commit**

Run: `cd backend_py && uv run pytest -m "not integration" && uv run mypy src/ && uv run ruff check`
Expected: ALL PASS.

```bash
git add backend_py/src/bfx_funding_bot/modules/execution/deployment/ladder.py \
        backend_py/src/bfx_funding_bot/modules/execution/deployment/reconciler.py \
        backend_py/src/bfx_funding_bot/modules/marketfeed/daemon.py \
        backend_py/tests/modules/execution/deployment/test_ladder.py \
        backend_py/tests/modules/execution/deployment/test_reconciler.py
git commit -m "✨ Feat: spike-rung ladder observe-only（ladder_would_post log；submit 零改變）

E1+E2 是動態改價，ladder 唯一增量 = spike capture（掛 ask 之上）。enforce path 刻意不建：需 sweep 豁免 + clamp bypass，等 observe 數據證明 uplift。BFX_LADDER_OBSERVE 預設 off。"
```

---

### Task 7: VM rollout (manual gate — operator runs on the VM)

**Files:** none (runbook; VM-side).

**Interfaces:**
- Consumes: Tasks 1-6 merged to `main` and pushed.
- Produces: bot running with E2 enforced + migrations applied + regime row recorded.

**Steps (run on the VM over SSH, in order):**

- [ ] **Step 1: Pre-flip log gate (abort criteria included)**

```bash
# E1 stability since 07-07: cancels should exist only when offers rest stale-high;
# zero churn (no cancel→repost→cancel loops on the same rate level).
docker logs bfx-bot --since 72h 2>&1 | grep -c "reprice_cancelled"
docker logs bfx-bot --since 72h 2>&1 | grep "reprice_cancel_error" | tail
# E2 observe distribution: FLOOR/FALLBACK dominance = clamp would mostly do
# nothing or protect-only; TAKER/UNDERCUT/RAISE presence = clamp has real work.
docker logs bfx-bot --since 72h 2>&1 | grep -o "branch=[a-z]*" | sort | uniq -c
```

**Abort and report back instead of proceeding if:** `reprice_cancel_error` recurs, or the branch histogram is empty (no ticker fetches — clamp would be a no-op; investigate `clamp_ticker_fetch_failed` first).

- [ ] **Step 2: Deploy (regenerates .env.runtime from the committed canary.env)**

```bash
cd <repo-root-on-vm>
BFX_CANARY_CONFIRM=yes ./scripts/deploy-vm.sh canary
```

- [ ] **Step 3: Apply migrations (one-shot container; disable autoheal — 2026-07-07 lesson: inherited healthcheck kills long one-shots)**

```bash
docker compose -f docker-compose.bot.yml run --rm --no-deps \
  --label autoheal=false bot uv run alembic upgrade head
docker compose -f docker-compose.bot.yml restart bot
```

(restart so the daemon boot re-runs against the migrated schema and writes the first `config_regime` row with clamp=true.)

- [ ] **Step 4: Post-flip verification**

```bash
docker logs bfx-bot --since 10m 2>&1 | grep -E "config_regime_recorded|clamp_applied|boot"
# expect: config_regime_recorded clamp=True reprice=True sha=<sha>
#         clamp_applied ... branch=... lines on subsequent ticks (not would_adjust)
docker exec bfx-bot python -c "import os; print(os.environ.get('BFX_CLAMP_ENABLED'), os.environ.get('BFX_REPRICE_ENABLED'))"
# expect: true true
```

- [ ] **Step 5: Day-2/3 check**

```bash
docker compose -f docker-compose.bot.yml run --rm --no-deps --label autoheal=false \
  bot uv run python -m scripts.report_execution_quality
```

Compare the pre-flip vs post-flip regime rows: fill_rate up / p50 latency down = clamp helping; fill_rate down = FLOOR/undercut mispricing — flip `BFX_CLAMP_ENABLED=false` in `deploy/vm/canary.env`, redeploy, report.

---

## Self-Review

- **Spec coverage:** item 1 → Task 1+7; item 2 → Task 2; item 3 → Task 3; item 4 → Tasks 4+5; item 5 → Task 6. AP canary (2026-08-04) intentionally NOT expedited (its own double gate stands). FRRDELTAVAR intentionally absent (refuted pending ≥8 windows).
- **Placeholder scan:** two "Note to implementer" blocks (Task 2 Step 5, Task 6 Step 5) delegate to existing test-file idioms by design — the contracts (assertions) are fully specified; Task 5 Step 5 flags the `cid` column-type check explicitly. No TBD/TODO remain.
- **Type consistency:** `LadderPolicy`/`spike_rungs` signatures match between Tasks 6's tests and implementation; `record_config_regime` kwargs match daemon call; `compute_weekly_rows(utilization_points=...)` consistent across compute/loader/tests; `ClaimOutcome.latency_ms: int | None` consistent with summarize.
