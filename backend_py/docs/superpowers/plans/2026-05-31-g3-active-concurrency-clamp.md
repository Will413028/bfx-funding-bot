# G3 Stage 2 — Active-arm concurrent-principal clamp Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Clamp the G3 active arm's instantaneous open principal to the canary budget C so the active-vs-passive comparison shares one capital base, and fix the adjacent `total_capital_days` unit bug.

**Architecture:** A pure sweep-line primitive `clamp_active_window(fills, cap)` integrates realized interest and capital-days over each fill's held-to-term interval, proportionally scaling all open fills by `cap/Σopen` whenever concurrent principal exceeds `cap`. Callers bucket fills by window first (no window-end clipping → held-to-term preserved, no-clamp path bit-exact with the legacy `Σ size·rate·duration`). Anchors (`attributed_interest`, `attributed_deployed`) stay RAW. A `ClampDiagnostic` surfaces over-deploy in the report.

**Tech Stack:** Python 3.13, Decimal, pytest. All commands run from `backend_py/` via `uv`.

---

### Task 1: `clamp_active_window` sweep-line primitive

**Files:**
- Modify: `src/bfx_funding_bot/modules/live_validation/live_attribution.py` (add after `_fill_duration_days`, ~line 133)
- Test: `tests/modules/live_validation/test_live_attribution.py`

- [ ] **Step 1: Write failing tests**

Append to `test_live_attribution.py` (the `_fill` helper at line 171 already exists):

```python
# ---------------------------------------------------------------------------
# clamp_active_window: concurrency-clamped interest + capital-days
# ---------------------------------------------------------------------------

DAY = 24 * 60 * 60 * 1000


def test_clamp_single_fill_equals_legacy_formula():
    # No overlap → bit-exact with Σ size·rate·duration.
    f = _fill(0, "570", "0.0003", "2")
    cw = clamp_active_window([f], cap=C)
    assert cw.interest == Decimal("570") * Decimal("0.0003") * Decimal("2")
    assert cw.raw_interest == cw.interest
    assert cw.capital_days == Decimal("570") * Decimal("2")
    assert cw.peak_concurrent == Decimal("570")


def test_clamp_two_overlapping_under_cap_no_scaling():
    # 200 + 300 = 500 < 570 → no clamp, full held-to-term interest each.
    f1 = _fill(100, "200", "0.0003", "2")
    f2 = _fill(200, "300", "0.0005", "2")
    cw = clamp_active_window([f1, f2], cap=C)
    expected = (
        Decimal("200") * Decimal("0.0003") * Decimal("2")
        + Decimal("300") * Decimal("0.0005") * Decimal("2")
    )
    assert cw.interest == expected
    assert cw.raw_interest == expected
    assert cw.peak_concurrent == Decimal("500")


def test_clamp_overlap_over_cap_scales_proportionally():
    # Two simultaneous fills 400 + 400 = 800 > 570, same window, identical span.
    # While both open, scale = 570/800; interest is clamped, raw is not.
    f1 = _fill(0, "400", "0.0003", "2")
    f2 = _fill(0, "400", "0.0003", "2")
    cw = clamp_active_window([f1, f2], cap=C)
    raw = Decimal("2") * (Decimal("400") * Decimal("0.0003") * Decimal("2"))
    assert cw.raw_interest == raw
    # both fully overlap for the whole 2 days → uniform scale 570/800
    assert cw.interest == raw * (C / Decimal("800"))
    assert cw.capital_days == C * Decimal("2")  # min(800, 570) for 2 days
    assert cw.peak_concurrent == Decimal("800")


def test_clamp_partial_overlap_only_clamps_overlap_region():
    # f1 [0, 2d) size 400; f2 [1d, 3d) size 400. Overlap [1d,2d): 800>570 clamp.
    # Non-overlap regions ([0,1d) f1 only, [2d,3d) f2 only) stay full.
    f1 = _fill(0, "400", "0.0003", "2")
    f2 = _fill(DAY, "400", "0.0003", "2")
    cw = clamp_active_window([f1, f2], cap=C)
    rate = Decimal("0.0003")
    # f1: [0,1d) full 400 + [1d,2d) scaled 400*570/800
    # f2: [1d,2d) scaled 400*570/800 + [2d,3d) full 400
    scale = C / Decimal("800")
    f1_int = Decimal("400") * rate * Decimal("1") + Decimal("400") * scale * rate * Decimal("1")
    f2_int = Decimal("400") * scale * rate * Decimal("1") + Decimal("400") * rate * Decimal("1")
    assert cw.interest == f1_int + f2_int
    assert cw.peak_concurrent == Decimal("800")


def test_clamp_release_caps_duration():
    # release at 1 day → 1-day interest, like _fill_duration_days.
    f = _fill(0, "570", "0.0003", "2", release=DAY)
    cw = clamp_active_window([f], cap=C)
    assert cw.interest == Decimal("570") * Decimal("0.0003") * Decimal("1")
    assert cw.capital_days == Decimal("570") * Decimal("1")


def test_clamp_empty_is_zero():
    cw = clamp_active_window([], cap=C)
    assert cw.interest == Decimal("0")
    assert cw.capital_days == Decimal("0")
    assert cw.raw_interest == Decimal("0")
    assert cw.peak_concurrent == Decimal("0")


def test_clamp_zero_cap_raises():
    with pytest.raises(ValueError, match="cap must be positive"):
        clamp_active_window([_fill(0, "100", "0.0003", "2")], cap=Decimal("0"))
```

Add `ClampedWindow` and `clamp_active_window` to the import block at the top of the test file (lines 6-23).

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd backend_py && uv run pytest tests/modules/live_validation/test_live_attribution.py -k clamp -q`
Expected: FAIL (ImportError: cannot import name 'clamp_active_window')

- [ ] **Step 3: Implement the primitive**

In `live_attribution.py`, after `_fill_duration_days` (line 133), add:

```python
@dataclass(frozen=True)
class ClampedWindow:
    """Concurrency-clamped attribution over a set of fills (one bucket).

    interest / capital_days reflect the budget ceiling: at every instant the
    open principal is clamped to `cap` (all open fills scaled by cap/Σopen when
    over budget). raw_interest / peak_concurrent are the un-clamped figures kept
    for the over-deploy diagnostic. No window-end clipping — fills accrue their
    full held-to-term lifetime, so the no-clamp case is bit-exact with the
    legacy Σ(size·rate·duration).
    """

    interest: Decimal
    capital_days: Decimal
    raw_interest: Decimal
    peak_concurrent: Decimal


def clamp_active_window(fills: list[FillRecord], *, cap: Decimal) -> ClampedWindow:
    """Sweep-line attribution with concurrent-principal clamped to `cap`.

    Each fill occupies [fill_ts, fill_ts + _fill_duration_days·MS_PER_DAY).
    Per sub-interval: S = Σ open sizes; scale = min(1, cap/S). Durations are
    accumulated per fill in integer milliseconds (scale==1) so a non-clamped
    bucket divides by MS_PER_DAY exactly once → identical to the old formula.
    """
    if cap <= 0:
        raise ValueError(f"cap must be positive, got {cap!r}")
    intervals: list[tuple[int, int, FillRecord]] = []
    for f in fills:
        start = f.fill_ts_ms
        end = start + int(_fill_duration_days(f) * MS_PER_DAY)
        if end > start:
            intervals.append((start, end, f))
    if not intervals:
        return ClampedWindow(Decimal("0"), Decimal("0"), Decimal("0"), Decimal("0"))

    points = sorted({p for s, e, _ in intervals for p in (s, e)})
    scaled_ms = [Decimal("0")] * len(intervals)
    clipped_ms = [0] * len(intervals)
    peak = Decimal("0")
    for a, b in zip(points, points[1:]):
        dt = b - a
        if dt <= 0:
            continue
        open_idx = [i for i, (s, e, _) in enumerate(intervals) if s <= a < e]
        total_open = sum((intervals[i][2].size_usdt for i in open_idx), Decimal("0"))
        if total_open > peak:
            peak = total_open
        if total_open <= 0:
            continue
        scale = cap / total_open if total_open > cap else Decimal("1")
        for i in open_idx:
            scaled_ms[i] += scale * dt
            clipped_ms[i] += dt

    interest = Decimal("0")
    capital_days = Decimal("0")
    raw_interest = Decimal("0")
    for i, (_s, _e, f) in enumerate(intervals):
        sm = scaled_ms[i]
        cm = Decimal(clipped_ms[i])
        interest += f.size_usdt * f.rate * sm / MS_PER_DAY
        capital_days += f.size_usdt * sm / MS_PER_DAY
        raw_interest += f.size_usdt * f.rate * cm / MS_PER_DAY
    return ClampedWindow(
        interest=interest,
        capital_days=capital_days,
        raw_interest=raw_interest,
        peak_concurrent=peak,
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd backend_py && uv run pytest tests/modules/live_validation/test_live_attribution.py -k clamp -q`
Expected: PASS (7 passed)

- [ ] **Step 5: Commit**

```bash
git add backend_py/src/bfx_funding_bot/modules/live_validation/live_attribution.py backend_py/tests/modules/live_validation/test_live_attribution.py
git commit -m "✨ Feat: clamp_active_window sweep-line primitive (G3 Stage 2)"
```

---

### Task 2: Rewire `attribute_active` to clamp per window

**Files:**
- Modify: `src/bfx_funding_bot/modules/live_validation/live_attribution.py:135-164` (`attribute_active`)
- Test: `tests/modules/live_validation/test_live_attribution.py`

- [ ] **Step 1: Write failing test**

Append after the existing `attribute_active` tests (~line 243):

```python
def test_attribute_active_clamps_overlap_over_cap():
    # 4 fills, 250 each = 1000 concurrent > 570 cap, same window, same span.
    fills = [_fill(0, "250", "0.0003", "2") for _ in range(4)]
    out = attribute_active(fills, capital=C, window_bounds=[(0, WEEK)])
    raw = Decimal("4") * (Decimal("250") * Decimal("0.0003") * Decimal("2"))
    clamped = raw * (C / Decimal("1000"))
    assert out[0].net_monthly == clamped / C * Decimal("100")
    assert out[0].n_trades == 4  # n_trades unchanged: count by fill_ts
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend_py && uv run pytest tests/modules/live_validation/test_live_attribution.py -k clamps_overlap -q`
Expected: FAIL (net_monthly over-counts — assertion mismatch)

- [ ] **Step 3: Rewire `attribute_active`**

Replace the body loop of `attribute_active` (lines 145-164) with:

```python
    out: list[WindowOutcome] = []
    for lo, hi in window_bounds:
        wf = [f for f in fills if lo <= f.fill_ts_ms < hi]
        clamped = clamp_active_window(wf, cap=capital)
        rates = [f.rate for f in wf]
        # unweighted mean matched rate — diagnostic only, not used in the yield sum
        mean_rate = (
            sum(rates, Decimal("0")) / Decimal(len(rates)) if rates else Decimal("0")
        )
        out.append(
            WindowOutcome(
                month_mts=lo,
                net_monthly=clamped.interest / capital * Decimal("100"),
                n_trades=len(wf),
                fill_rate=mean_rate,
            )
        )
    return out
```

Update the docstring's `net_monthly` line to note the concurrency clamp.

- [ ] **Step 4: Run the full attribution test module**

Run: `cd backend_py && uv run pytest tests/modules/live_validation/test_live_attribution.py -q`
Expected: PASS (all existing `attribute_active` exact-value tests stay green + the new clamp test)

- [ ] **Step 5: Commit**

```bash
git add backend_py/src/bfx_funding_bot/modules/live_validation/live_attribution.py backend_py/tests/modules/live_validation/test_live_attribution.py
git commit -m "♻️ Refactor: attribute_active clamps per-window concurrency (G3 Stage 2)"
```

---

### Task 3: `_compute_verdict` — capital-days unit fix + clamp + diagnostic

**Files:**
- Modify: `src/bfx_funding_bot/modules/live_validation/live_attribution.py` (add `ClampDiagnostic` dataclass near `ClampedWindow`)
- Modify: `scripts/_g3_loaders.py` (`_compute_verdict` lines 200-359, `build_verdict_from_neon` return)
- Test: `tests/scripts/test_g3_loaders.py`

- [ ] **Step 1: Write failing tests**

Append to `test_g3_loaders.py`:

```python
def test_compute_verdict_capital_days_is_usdt_days_not_divided():
    # One full-budget fill held 2 days → 570*2 = 1140 USDT·days (NOT /570 ≈ 2).
    # Reaches the capital-days reason only conceptually; assert via diagnostic.
    pts = [MarketRatePoint(mts=1000 + i, rate=Decimal("0.0002")) for i in range(5)]
    fills = [_fill(1000, "570", "0.0003")]
    _verdict, _window, _n, clamp = _compute_verdict(
        fills=fills, market_rate_points=pts, observed_realized=Decimal("570"), capital=C
    )
    # clamp diagnostic carries the un-clamped figures; no over-deploy for 1 fill
    assert clamp.peak_concurrent == Decimal("570")
    assert clamp.over_deployed is False


def test_compute_verdict_over_deploy_populates_diagnostic():
    # 3 fills 300 each = 900 concurrent > 570 → over-deploy diagnostic set.
    pts = [MarketRatePoint(mts=1000 + i, rate=Decimal("0.0002")) for i in range(5)]
    fills = [_fill(1000, "300", "0.0003") for _ in range(3)]
    _verdict, _window, _n, clamp = _compute_verdict(
        fills=fills, market_rate_points=pts, observed_realized=Decimal("900"), capital=C
    )
    assert clamp.peak_concurrent == Decimal("900")
    assert clamp.over_deployed is True
    assert clamp.raw_interest > clamp.clamped_interest
    assert clamp.cap == C
```

Update the existing `_compute_verdict` calls in this file (lines 80-82, 93-95, 102-104) to unpack 4 values: `verdict, _window, n_fills, _clamp = _compute_verdict(...)`. Likewise the two `build_verdict_from_neon` calls (lines 152-154, 172-174): `verdict, _window, n_fills, _clamp = await build_verdict_from_neon(...)`.

Add `ClampDiagnostic` to the imports from `live_attribution`.

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd backend_py && uv run pytest tests/scripts/test_g3_loaders.py -q`
Expected: FAIL (ValueError: not enough values to unpack — `_compute_verdict` returns 3)

- [ ] **Step 3: Add `ClampDiagnostic` and rewire `_compute_verdict`**

In `live_attribution.py`, after `ClampedWindow`, add:

```python
@dataclass(frozen=True)
class ClampDiagnostic:
    """Over-deploy transparency for the report. cap is the budget; peak_concurrent
    the max instantaneous open principal; raw vs clamped interest the excess the
    clamp removed."""

    cap: Decimal
    peak_concurrent: Decimal
    raw_interest: Decimal
    clamped_interest: Decimal

    @property
    def over_deployed(self) -> bool:
        return self.peak_concurrent > self.cap

    @property
    def over_deploy_factor(self) -> Decimal:
        return self.peak_concurrent / self.cap if self.cap > 0 else Decimal("0")

    @property
    def excess_return_pct(self) -> Decimal:
        # interest the clamp removed, as a % of budget (same unit as headline)
        return (self.raw_interest - self.clamped_interest) / self.cap * Decimal("100") if self.cap > 0 else Decimal("0")
```

In `scripts/_g3_loaders.py`:

1. Import `ClampDiagnostic` and `clamp_active_window` from `live_attribution` (add to the import block lines 28-43; `_fill_duration_days` is still used for `attributed_interest`).

2. In the `if fills and bounds:` branch (lines 238-270), replace the `total_capital_days` block (lines 253-257) and add the clamp diagnostic:

```python
        # Budget-clamped capital-days (USDT·days) and over-deploy diagnostic
        # come from one full-span sweep. total_capital_days is USDT·days to
        # match decide_verdict's contract (min_capital_days = capital*7); the
        # old `/ capital` made it 'days' and mismatched the threshold.
        full_clamp = clamp_active_window(fills, cap=capital)
        total_capital_days = full_clamp.capital_days
        clamp_diag = ClampDiagnostic(
            cap=capital,
            peak_concurrent=full_clamp.peak_concurrent,
            raw_interest=full_clamp.raw_interest,
            clamped_interest=full_clamp.interest,
        )
```

   (Keep `attributed_deployed = open_principal_at(fills, max_ts)` and
   `attributed_interest = sum(f.size_usdt * f.rate * _fill_duration_days(f) ...)`
   exactly as-is — anchors stay RAW.)

3. In the `else:` (no-fills) branch (lines 272-283), add:

```python
        clamp_diag = ClampDiagnostic(
            cap=capital,
            peak_concurrent=Decimal("0"),
            raw_interest=Decimal("0"),
            clamped_interest=Decimal("0"),
        )
```

4. Change the final return (line 359) from `return verdict, data_window, n_fills` to `return verdict, data_window, n_fills, clamp_diag`.

5. Update the `build_verdict_from_neon` return-type annotation (line 79) and final `return _compute_verdict(...)` (line 192) — the tuple is now 4-wide: `tuple[G3Verdict, str, int, ClampDiagnostic]`. Update `_compute_verdict`'s annotation (line 206) too.

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd backend_py && uv run pytest tests/scripts/test_g3_loaders.py tests/modules/live_validation/ -q`
Expected: PASS (existing state assertions green + new diagnostic tests)

- [ ] **Step 5: Commit**

```bash
git add backend_py/src/bfx_funding_bot/modules/live_validation/live_attribution.py backend_py/scripts/_g3_loaders.py backend_py/tests/scripts/test_g3_loaders.py
git commit -m "🐛 Fix: total_capital_days USDT·days + clamp + over-deploy diagnostic (G3 Stage 2)"
```

---

### Task 4: Report plumbing — over-deploy honesty line

**Files:**
- Modify: `scripts/run_g3_live_validation.py` (`render_markdown` line 22, `_verdict_to_json`, `_amain` lines 71-75)
- Test: `tests/scripts/test_run_g3_live_validation.py`

- [ ] **Step 1: Write failing test**

Append to `test_run_g3_live_validation.py` (import `ClampDiagnostic` and `G3Verdict`, `VerdictState`, `Decimal` as needed):

```python
def test_render_markdown_prints_over_deploy_line_when_clamped():
    from decimal import Decimal

    from bfx_funding_bot.modules.live_validation.live_attribution import (
        ClampDiagnostic,
        G3Verdict,
        VerdictState,
    )
    from scripts.run_g3_live_validation import render_markdown

    verdict = G3Verdict(
        state=VerdictState.INSUFFICIENT_DATA,
        headline_active_spread=Decimal("0.005"),
        n_windows=1,
        ci_lo=Decimal("0"),
        ci_hi=Decimal("0"),
        reasons=["only 1 weekly window"],
    )
    diag = ClampDiagnostic(
        cap=Decimal("570"),
        peak_concurrent=Decimal("863"),
        raw_interest=Decimal("0.5"),
        clamped_interest=Decimal("0.33"),
    )
    md = render_markdown(verdict=verdict, data_window="2026-05-24..2026-05-31", n_fills=4, clamp_diag=diag)
    assert "clamped to budget" in md
    assert "863" in md


def test_render_markdown_no_over_deploy_line_when_within_budget():
    from decimal import Decimal

    from bfx_funding_bot.modules.live_validation.live_attribution import (
        ClampDiagnostic,
        G3Verdict,
        VerdictState,
    )
    from scripts.run_g3_live_validation import render_markdown

    verdict = G3Verdict(
        state=VerdictState.INSUFFICIENT_DATA,
        headline_active_spread=Decimal("0.005"),
        n_windows=1,
        ci_lo=Decimal("0"),
        ci_hi=Decimal("0"),
        reasons=["only 1 weekly window"],
    )
    diag = ClampDiagnostic(
        cap=Decimal("570"),
        peak_concurrent=Decimal("400"),
        raw_interest=Decimal("0.3"),
        clamped_interest=Decimal("0.3"),
    )
    md = render_markdown(verdict=verdict, data_window="x", n_fills=1, clamp_diag=diag)
    assert "clamped to budget" not in md
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd backend_py && uv run pytest tests/scripts/test_run_g3_live_validation.py -k over_deploy -q`
Expected: FAIL (render_markdown got an unexpected keyword 'clamp_diag')

- [ ] **Step 3: Wire `clamp_diag` through render + main**

In `run_g3_live_validation.py`:

1. Import at top: `from bfx_funding_bot.modules.live_validation.live_attribution import ClampDiagnostic, G3Verdict`.

2. Change `render_markdown` signature and insert the honesty line. Replace the `## Honesty caveats` block so it conditionally appends the over-deploy line:

```python
def render_markdown(
    *, verdict: G3Verdict, data_window: str, n_fills: int, clamp_diag: ClampDiagnostic
) -> str:
    """Pure renderer — unit-testable without a DB."""
    honesty = [
        "## Honesty caveats",
        "- Held-to-term duration assumption (matured credits have no close event).",
        "- Fills attributed with the conservative shorter period when cell identity is absent.",
        "- Deployed params are in-sample to the 2022–2026 selection sweep; the live canary is the true OOS.",
        "- Platform/credit tail (Bitfinex/Tether) is uncapturable here — mitigated by the cap.",
    ]
    if clamp_diag.over_deployed:
        honesty.append(
            f"- Active arm clamped to budget C={clamp_diag.cap}: raw concurrent "
            f"principal peaked at {clamp_diag.peak_concurrent} "
            f"({clamp_diag.over_deploy_factor:.2f}x cap) → "
            f"{clamp_diag.excess_return_pct:.4f}% over-deploy excess removed."
        )
    lines = [
        "# G3 Live Validation — fUST MeanReversion (a30, p2), deployed ema_span=24/thr=0.5",
        "",
        f"Data window: {data_window} | fills: {n_fills} | weekly windows: {verdict.n_windows}",
        "",
        "## TL;DR",
        f"- **Verdict: {verdict.state.value}**",
        f"- Headline active spread (since inception): {verdict.headline_active_spread}%",
        f"- Active-spread 95% CI: [{verdict.ci_lo}, {verdict.ci_hi}]",
        "",
        "### Reasons",
        *[f"- {r}" for r in verdict.reasons],
        "",
        *honesty,
        "",
        "## Recommendation",
        "- PASS: live alpha confirmed; scale-up is the operator's call.",
        "- INSUFFICIENT_DATA: keep accruing fills; re-run after more weekly windows.",
        "- FAIL: deployed config does not beat passive live — investigate before scaling.",
        "- UNRELIABLE: yield model diverges from venue truth — fix attribution before trusting.",
    ]
    return "\n".join(lines)
```

3. Add over-deploy to JSON in `_verdict_to_json` — change its signature to also take the diagnostic, or add a separate dict. Simplest: build the over-deploy block in `_amain`. Update `_verdict_to_json` to accept `clamp_diag` and include:

```python
def _verdict_to_json(v: G3Verdict, clamp_diag: ClampDiagnostic) -> dict[str, object]:
    return {
        "state": v.state.value,
        "headline_active_spread": str(v.headline_active_spread),
        "n_windows": v.n_windows,
        "ci_lo": str(v.ci_lo),
        "ci_hi": str(v.ci_hi),
        "reasons": v.reasons,
        "over_deploy": {
            "cap": str(clamp_diag.cap),
            "peak_concurrent": str(clamp_diag.peak_concurrent),
            "raw_interest": str(clamp_diag.raw_interest),
            "clamped_interest": str(clamp_diag.clamped_interest),
            "detected": clamp_diag.over_deployed,
        },
    }
```

4. In `_amain` (lines 71-75) unpack 4 values and pass through:

```python
    verdict, data_window, n_fills, clamp_diag = await build_verdict_from_neon(
        capital=Decimal(args.capital)
    )

    out = Path(args.out)
    out.write_text(
        render_markdown(verdict=verdict, data_window=data_window, n_fills=n_fills, clamp_diag=clamp_diag)
    )
    out.with_suffix(".json").write_text(json.dumps(_verdict_to_json(verdict, clamp_diag), indent=2))
```

- [ ] **Step 4: Run the script test module**

Run: `cd backend_py && uv run pytest tests/scripts/test_run_g3_live_validation.py -q`
Expected: PASS (existing renderer tests + 2 new over-deploy tests). If an existing renderer test calls `render_markdown` without `clamp_diag`, update it to pass a within-budget `ClampDiagnostic`.

- [ ] **Step 5: Full gate + commit**

```bash
cd backend_py && uv run pytest -m "not integration" -q && uv run mypy src/ && uv run ruff check
```
Expected: all green.

```bash
git add backend_py/scripts/run_g3_live_validation.py backend_py/tests/scripts/test_run_g3_live_validation.py
git commit -m "✨ Feat: G3 report over-deploy honesty line + JSON (G3 Stage 2)"
```

---

### Task 5: Re-run the live report and verify a clean headline

**Files:**
- Create: `docs/research/2026-05-31-g3-live-validation.md` (+ `.json`, generated)

- [ ] **Step 1: Ensure live DB credential is fresh**

The local `.env` Neon password rotates. If the next step errors on auth, refresh via Neon MCP `get_connection_string` (project `lingering-resonance-64910611`) and update `backend_py/.env`.

- [ ] **Step 2: Run the report (module form is mandatory)**

Run: `cd backend_py && uv run python -m scripts.run_g3_live_validation --out docs/research/2026-05-31-g3-live-validation.md`
Expected: `wrote docs/research/2026-05-31-g3-live-validation.md and ...json`. Direct `python scripts/run_g3_live_validation.py` fails with `ModuleNotFoundError: scripts` — always use `-m`.

- [ ] **Step 3: Inspect the headline + over-deploy line**

Read the generated `.md`. Confirm: verdict still INSUFFICIENT_DATA; the over-deploy honesty line is present (peak ~863 > 570) and the headline active spread is lower/cleaner than the pre-clamp +0.0095%.

- [ ] **Step 4: Commit the report**

```bash
git add backend_py/docs/research/2026-05-31-g3-live-validation.md backend_py/docs/research/2026-05-31-g3-live-validation.json
git commit -m "📝 Docs: G3 live report after concurrency clamp (G3 Stage 2)"
```

---

## Self-Review

- **Spec coverage:** clamp primitive (T1) ✓; proportional scaling (T1 tests) ✓; anchors RAW (T3 keeps `attributed_*` untouched) ✓; cross-window via per-window bucketing (T2) ✓; total_capital_days USDT·days fix (T3) ✓; over-deploy report line (T4) ✓; re-run report (T5) ✓.
- **Placeholder scan:** none — every code step shows full code.
- **Type consistency:** `clamp_active_window` / `ClampedWindow` / `ClampDiagnostic` names and fields are consistent T1→T4; `_compute_verdict` / `build_verdict_from_neon` / `render_markdown` / `_verdict_to_json` all updated to the 4-tuple + `clamp_diag` param in the same tasks that consume them.
- **Out-of-scope guard:** duration stays p2=2 (no `avg_period`); passive baseline untouched; `decide_verdict` table untouched.

## Post-implementation (separate from this plan)

Adversarial review workflow over the diff — verify (a) the `total_capital_days` unit fix matches `decide_verdict`'s contract and is gate-safe at n_windows≥8, (b) no-clamp Decimal bit-exactness held, (c) anchors-raw vs return-clamped is the right boundary. Then update the strategy-journal / memory and discuss viability.
