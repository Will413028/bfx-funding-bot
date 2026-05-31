# G3 Stage 3 — bot-vs-idle reframe Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Reframe the G3 live-validation gate so the primary PASS/FAIL is **bot-vs-idle** (the bot's absolute realized return on the allocated budget vs an idle balance earning 0), demoting active-vs-passive (MR timing alpha) to a reported, non-gating secondary diagnostic.

**Architecture:** Add an explicit zero-return `attribute_idle` arm. The bot-vs-idle paired difference (active − idle) equals the active arm's absolute per-window return; its bootstrap CI drives the verdict. MR-alpha (active − passive) is still computed and carried on `G3Verdict` for the report but never changes the state. The market-rate coverage guard and band guard are decoupled from the primary verdict — they now only mark the MR-alpha diagnostic unavailable (idle ≡ 0 needs no market-rate data). Anchor-divergence still forces UNRELIABLE.

**Tech Stack:** Python 3.13, `Decimal`, pytest (`uv run` from `backend_py/`), frozen dataclasses, existing `oos_profitability` bootstrap/paired-return helpers.

**Commands (run from `backend_py/`):**
- Test one: `uv run pytest <path>::<test> -v`
- Gate: `uv run pytest -m "not integration"` + `uv run mypy src/` + `uv run ruff check`

---

### Task 1: `attribute_idle` zero-return arm

**Files:**
- Modify: `src/bfx_funding_bot/modules/live_validation/live_attribution.py` (add after `attribute_passive`, ~line 295)
- Test: `tests/modules/live_validation/test_live_attribution.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/modules/live_validation/test_live_attribution.py` (after the `attribute_passive` tests block, before the deployment-anchor block). Also add `attribute_idle` to the import block at the top of the file (alongside `attribute_passive`).

```python
# ---------------------------------------------------------------------------
# attribute_idle (AlwaysIdle arm — capital sits idle, earns 0 by construction)
# ---------------------------------------------------------------------------


def test_attribute_idle_zero_return_per_window():
    out = attribute_idle(window_bounds=[(0, WEEK), (WEEK, 2 * WEEK)])
    assert len(out) == 2
    assert [o.month_mts for o in out] == [0, WEEK]
    assert all(o.net_monthly == Decimal("0") for o in out)
    assert all(o.n_trades == 0 for o in out)
    assert all(o.fill_rate == Decimal("0") for o in out)


def test_attribute_idle_aligns_with_active_month_mts():
    # bot-vs-idle relies on attribute_idle aligning 1:1 by month_mts with
    # attribute_active so paired_active_returns can subtract arm-by-arm.
    bounds = [(0, WEEK), (WEEK, 2 * WEEK)]
    active = attribute_active([], capital=C, window_bounds=bounds)
    idle = attribute_idle(window_bounds=bounds)
    assert [o.month_mts for o in active] == [o.month_mts for o in idle]


def test_attribute_idle_empty_bounds():
    assert attribute_idle(window_bounds=[]) == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/modules/live_validation/test_live_attribution.py::test_attribute_idle_zero_return_per_window -v`
Expected: FAIL — `ImportError: cannot import name 'attribute_idle'`

- [ ] **Step 3: Write minimal implementation**

In `live_attribution.py`, add directly after `attribute_passive` (after its closing `return out`, ~line 295):

```python
def attribute_idle(*, window_bounds: list[tuple[int, int]]) -> list[WindowOutcome]:
    """AlwaysIdle arm: capital sits idle, earning 0 by construction.

    One zero WindowOutcome per window. The bot-vs-idle paired difference
    (attribute_active − attribute_idle) therefore equals the active arm's
    absolute per-window return — the product's primary success metric (earn the
    market rate vs leave the balance idle). Mirrors attribute_passive's shape so
    paired_active_returns aligns the two arms 1:1 by month_mts.
    """
    return [
        WindowOutcome(
            month_mts=lo,
            net_monthly=Decimal("0"),
            n_trades=0,
            fill_rate=Decimal("0"),
        )
        for lo, _hi in window_bounds
    ]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/modules/live_validation/test_live_attribution.py -k attribute_idle -v`
Expected: 3 PASS

- [ ] **Step 5: Commit**

```bash
git add src/bfx_funding_bot/modules/live_validation/live_attribution.py tests/modules/live_validation/test_live_attribution.py
git commit -m "✨ Feat: attribute_idle zero-return arm for bot-vs-idle (G3 Stage 3)"
```

---

### Task 2: `G3Verdict` + `decide_verdict` reframe (rename headline, add MR-alpha diagnostic fields)

**Files:**
- Modify: `src/bfx_funding_bot/modules/live_validation/live_attribution.py:369-434` (`G3Verdict`, `decide_verdict`)
- Test: `tests/modules/live_validation/test_live_attribution.py:344-423` (`_kw`, verdict tests)

**Interface locked here (used by Tasks 3 & 4):**
- `G3Verdict` fields: `state`, `headline_bot_vs_idle`, `n_windows`, `ci_lo`, `ci_hi`, `reasons`, `mr_alpha_spread`, `mr_alpha_ci_lo`, `mr_alpha_ci_hi`, `mr_alpha_available`.
- `decide_verdict` keyword params: `headline_bot_vs_idle`, `n_windows`, `total_capital_days`, `ci_lo`, `ci_hi`, `deployment_anchor`, `nav_anchor`, `min_windows`, `min_capital_days`, `mr_alpha_spread`, `mr_alpha_ci_lo`, `mr_alpha_ci_hi`, `mr_alpha_available`.
- `ci_lo`/`ci_hi` now carry the **bot-vs-idle** CI (primary gate). MR-alpha fields are carried onto the result but never change `state`.

- [ ] **Step 1: Update the failing tests**

In `tests/modules/live_validation/test_live_attribution.py`, replace the `_kw` helper (currently lines ~344-363) with the version below (renames `headline_active_spread` → `headline_bot_vs_idle`, adds MR-alpha defaults):

```python
def _kw(**over):
    base = {
        "headline_bot_vs_idle": Decimal("0.06"),
        "n_windows": 10,
        "total_capital_days": Decimal("4000"),
        "ci_lo": Decimal("0.01"),
        "ci_hi": Decimal("0.10"),
        "deployment_anchor": check_deployment_anchor(
            attributed_deployed=Decimal("300"),
            observed_realized=Decimal("300"),
            tol=Decimal("0.05"),
        ),
        "nav_anchor": check_nav_anchor(
            nav_delta=None, attributed_interest=Decimal("0"), tol=Decimal("0.1")
        ),
        "min_windows": 8,
        "min_capital_days": Decimal("3990"),
        "mr_alpha_spread": Decimal("0.0"),
        "mr_alpha_ci_lo": Decimal("-0.01"),
        "mr_alpha_ci_hi": Decimal("0.02"),
        "mr_alpha_available": True,
    }
    base.update(over)
    return base
```

Update `test_verdict_insufficient_when_ci_straddles_zero` to match the new reason text:

```python
def test_verdict_insufficient_when_ci_straddles_zero():
    v = decide_verdict(**_kw(ci_lo=Decimal("-0.02"), ci_hi=Decimal("0.05")))
    assert v.state is VerdictState.INSUFFICIENT_DATA
    assert "straddles" in " ".join(v.reasons).lower()
```

Add new tests pinning the reframe semantics (MR-alpha never gates; headline field renamed):

```python
def test_verdict_carries_mr_alpha_without_gating():
    # A negative MR-alpha CI (MR loses to AlwaysMarketRate) must NOT change a
    # bot-vs-idle PASS — alpha is diagnostic only.
    v = decide_verdict(
        **_kw(mr_alpha_spread=Decimal("-0.5"), mr_alpha_ci_lo=Decimal("-0.9"), mr_alpha_ci_hi=Decimal("-0.1"))
    )
    assert v.state is VerdictState.PASS
    assert v.mr_alpha_spread == Decimal("-0.5")
    assert v.mr_alpha_ci_lo == Decimal("-0.9")
    assert v.mr_alpha_ci_hi == Decimal("-0.1")
    assert v.mr_alpha_available is True


def test_verdict_exposes_headline_bot_vs_idle():
    v = decide_verdict(**_kw(headline_bot_vs_idle=Decimal("1.23")))
    assert v.headline_bot_vs_idle == Decimal("1.23")


def test_verdict_mr_alpha_unavailable_flag_carried():
    v = decide_verdict(**_kw(mr_alpha_available=False))
    assert v.state is VerdictState.PASS  # unavailability does not gate
    assert v.mr_alpha_available is False
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/modules/live_validation/test_live_attribution.py -k verdict -v`
Expected: FAIL — `decide_verdict() got an unexpected keyword argument 'headline_bot_vs_idle'` / missing attribute `mr_alpha_spread`.

- [ ] **Step 3: Write the implementation**

In `live_attribution.py`, replace the `G3Verdict` dataclass (lines ~369-376) with:

```python
@dataclass(frozen=True)
class G3Verdict:
    state: VerdictState
    headline_bot_vs_idle: Decimal  # absolute active return on budget; bot-vs-idle (idle ≡ 0)
    n_windows: int
    ci_lo: Decimal  # bot-vs-idle 95% CI (primary gate)
    ci_hi: Decimal
    reasons: list[str]
    # Secondary MR-timing-alpha diagnostic (active − AlwaysMarketRate). Reported,
    # never gating. mr_alpha_available is False when market-rate coverage/band
    # makes the passive arm untrustworthy → render as "unavailable".
    mr_alpha_spread: Decimal
    mr_alpha_ci_lo: Decimal
    mr_alpha_ci_hi: Decimal
    mr_alpha_available: bool
```

Replace `decide_verdict` (lines ~379-434) with:

```python
def decide_verdict(
    *,
    headline_bot_vs_idle: Decimal,
    n_windows: int,
    total_capital_days: Decimal,
    ci_lo: Decimal,
    ci_hi: Decimal,
    deployment_anchor: DeploymentAnchorResult,
    nav_anchor: NavAnchorResult,
    min_windows: int,
    min_capital_days: Decimal,
    mr_alpha_spread: Decimal,
    mr_alpha_ci_lo: Decimal,
    mr_alpha_ci_hi: Decimal,
    mr_alpha_available: bool,
) -> G3Verdict:
    """Pure four-state decision. Primary gate = bot-vs-idle CI (ci_lo/ci_hi).

    MR-alpha fields are stamped onto the result for the report but never change
    the state — the product's success criterion is absolute return vs idle, not
    timing alpha vs AlwaysMarketRate. Anchor divergence (attribution-vs-venue
    truth) still forces UNRELIABLE regardless of the primary metric.
    """
    reasons: list[str] = []

    def verdict(state: VerdictState) -> G3Verdict:
        return G3Verdict(
            state=state,
            headline_bot_vs_idle=headline_bot_vs_idle,
            n_windows=n_windows,
            ci_lo=ci_lo,
            ci_hi=ci_hi,
            reasons=reasons,
            mr_alpha_spread=mr_alpha_spread,
            mr_alpha_ci_lo=mr_alpha_ci_lo,
            mr_alpha_ci_hi=mr_alpha_ci_hi,
            mr_alpha_available=mr_alpha_available,
        )

    if not deployment_anchor.within_tolerance:
        reasons.append(
            f"deployment anchor diverged: attributed {deployment_anchor.attributed_deployed} "
            f"vs observed {deployment_anchor.observed_realized}"
        )
        return verdict(VerdictState.UNRELIABLE)
    if not nav_anchor.within_tolerance:
        reasons.append(
            f"NAV anchor diverged: ΔNAV {nav_anchor.nav_delta} "
            f"vs attributed interest {nav_anchor.attributed_interest}"
        )
        return verdict(VerdictState.UNRELIABLE)

    if n_windows < min_windows:
        reasons.append(f"only {n_windows} weekly windows (need >= {min_windows})")
        return verdict(VerdictState.INSUFFICIENT_DATA)
    if total_capital_days < min_capital_days:
        reasons.append(
            f"deployed {total_capital_days} capital-days (need >= {min_capital_days})"
        )
        return verdict(VerdictState.INSUFFICIENT_DATA)

    if ci_hi < 0:
        reasons.append(f"bot-vs-idle CI [{ci_lo}, {ci_hi}] entirely below 0")
        return verdict(VerdictState.FAIL)
    if ci_lo > 0:
        reasons.append(f"bot-vs-idle CI [{ci_lo}, {ci_hi}] entirely above 0")
        return verdict(VerdictState.PASS)

    reasons.append(f"bot-vs-idle CI [{ci_lo}, {ci_hi}] straddles 0 — inconclusive")
    return verdict(VerdictState.INSUFFICIENT_DATA)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/modules/live_validation/test_live_attribution.py -k verdict -v`
Expected: all PASS (existing + 3 new).

- [ ] **Step 5: Commit**

```bash
git add src/bfx_funding_bot/modules/live_validation/live_attribution.py tests/modules/live_validation/test_live_attribution.py
git commit -m "✨ Feat: G3Verdict bot-vs-idle headline + non-gating MR-alpha diagnostic (G3 Stage 3)"
```

---

### Task 3: `_compute_verdict` reframe — primary bot-vs-idle CI, decoupled guards

**Files:**
- Modify: `scripts/_g3_loaders.py` (imports ~28-45; `_compute_verdict` body ~202-374)
- Test: `tests/scripts/test_g3_loaders.py`

**Behavior changes:**
- Primary CI = bootstrap of `paired_active_returns(strat, idle)` (= active absolute returns). Headline = single-span active `net_monthly` (no passive subtraction).
- Secondary MR-alpha = `paired_active_returns(strat, passive)` mean + bootstrap CI.
- Band guard / no-coverage guard NO LONGER change the primary state. They set `mr_alpha_available=False` and prepend an informational caveat to `reasons`. Anchor divergence still → UNRELIABLE (unchanged, lives in `decide_verdict`).

- [ ] **Step 1: Update the failing tests**

In `tests/scripts/test_g3_loaders.py`, replace `test_compute_verdict_frr_scale_points_degrade_to_unreliable` (lines ~74-85) with the reframed expectation — band no longer forces UNRELIABLE; it marks MR-alpha unavailable while the primary stays data-driven (1 window, idle-ish → INSUFFICIENT_DATA):

```python
def test_compute_verdict_frr_scale_points_mark_mr_alpha_unavailable_not_unreliable():
    # frr-scale (~1e-6) passive series is the wrong source for the MR-alpha
    # diagnostic, but bot-vs-idle (idle ≡ 0) needs no market-rate data. The band
    # guard now ONLY marks MR-alpha unavailable + leaves a caveat; the primary
    # verdict stays data-driven (1 window → INSUFFICIENT_DATA), never UNRELIABLE.
    pts = [MarketRatePoint(mts=1000 + i, rate=Decimal("1.1e-06")) for i in range(5)]
    fills = [_fill(1000, "570", "0.0003")]
    verdict, _window, n_fills, _clamp = _compute_verdict(
        fills=fills, market_rate_points=pts, observed_realized=Decimal("570"), capital=C
    )
    assert verdict.state is VerdictState.INSUFFICIENT_DATA
    assert verdict.mr_alpha_available is False
    assert any("plausible per-day band" in r for r in verdict.reasons)
    assert n_fills == 1
```

Update `test_compute_verdict_legit_points_no_band_override` (lines ~88-98) — legit points → MR-alpha available, no band caveat, still INSUFFICIENT (1 window):

```python
def test_compute_verdict_legit_points_mr_alpha_available():
    pts = [MarketRatePoint(mts=1000 + i, rate=Decimal("0.0002")) for i in range(5)]
    fills = [_fill(1000, "570", "0.0003")]
    verdict, _window, n_fills, _clamp = _compute_verdict(
        fills=fills, market_rate_points=pts, observed_realized=Decimal("570"), capital=C
    )
    assert not any("plausible per-day band" in r for r in verdict.reasons)
    assert verdict.mr_alpha_available is True
    assert verdict.state is VerdictState.INSUFFICIENT_DATA
    assert n_fills == 1
```

Add a test pinning the headline is the absolute active return (bot-vs-idle), not a spread, and that no-coverage marks MR-alpha unavailable without blocking the primary:

```python
def test_compute_verdict_headline_is_absolute_active_return():
    # One full-budget 2-day fill at rate 3e-4, cap 570: bot-vs-idle headline =
    # active net_monthly = 570*3e-4*2 / 570 * 100 = 0.06 (idle subtracts 0).
    pts = [MarketRatePoint(mts=1000 + i, rate=Decimal("0.0002")) for i in range(5)]
    fills = [_fill(1000, "570", "0.0003")]
    verdict, _window, _n, _clamp = _compute_verdict(
        fills=fills, market_rate_points=pts, observed_realized=Decimal("570"), capital=C
    )
    assert verdict.headline_bot_vs_idle == Decimal("570") * Decimal("0.0003") * Decimal("2") / C * Decimal("100")


def test_compute_verdict_no_coverage_marks_mr_alpha_unavailable_primary_unblocked():
    # Fills present but ZERO market-rate points → MR-alpha cannot be computed, but
    # bot-vs-idle is unaffected. Primary stays data-driven (INSUFFICIENT: 1 window),
    # mr_alpha_available False, no band reason (band guard no-ops on empty).
    fills = [_fill(1000, "570", "0.0003")]
    verdict, _window, _n, _clamp = _compute_verdict(
        fills=fills, market_rate_points=[], observed_realized=Decimal("570"), capital=C
    )
    assert verdict.mr_alpha_available is False
    assert verdict.state is VerdictState.INSUFFICIENT_DATA
    assert not any("plausible per-day band" in r for r in verdict.reasons)
```

`test_compute_verdict_empty_is_insufficient_no_crash`, `test_compute_verdict_capital_days_is_usdt_days_not_divided`, and `test_compute_verdict_over_deploy_populates_diagnostic` stay as-is (they don't reference the removed UNRELIABLE-by-band behavior).

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/scripts/test_g3_loaders.py -k compute_verdict -v`
Expected: FAIL — `AttributeError: 'G3Verdict' object has no attribute 'mr_alpha_available'` (loader still builds the old verdict) and the band test asserting the new state.

- [ ] **Step 3: Write the implementation**

In `scripts/_g3_loaders.py`, add `attribute_idle` to the `live_attribution` import block (alongside `attribute_passive`).

Replace the body of `_compute_verdict` from the `# ── Compute windows + attribution` section through the end of the function (lines ~224-374) with:

```python
    n_fills = len(fills)

    # ── Compute windows + attribution ────────────────────────────────────────
    if fills or market_rate_points:
        all_mts = [f.fill_ts_ms for f in fills] + [p.mts for p in market_rate_points]
        min_ts = min(all_mts)
        max_ts = max(all_mts)
    else:
        # Truly empty — no data at all; produce a zero-data verdict.
        now_ms = int(datetime.now(UTC).timestamp() * 1000)
        min_ts = now_ms
        max_ts = now_ms

    bounds = weekly_window_bounds(min_ts, max_ts)

    mean_fn = lambda xs: sum(xs, Decimal("0")) / Decimal(len(xs))  # noqa: E731

    # window-coverage of the passive arm decides the MR-alpha diagnostic only.
    window_rate_points = (
        [p for p in market_rate_points if min_ts <= p.mts < max_ts]
        if (fills or market_rate_points)
        else []
    )

    if fills and bounds:
        strat_outcomes = attribute_active(fills, capital=capital, window_bounds=bounds)
        idle_outcomes = attribute_idle(window_bounds=bounds)
        base_outcomes = attribute_passive(market_rate_points, window_bounds=bounds)

        # Primary: bot-vs-idle = active − idle (idle ≡ 0) → absolute active return.
        bot_vs_idle = paired_active_returns(strat_outcomes, idle_outcomes)
        if len(bot_vs_idle) >= 2:
            ci_lo, ci_hi = bootstrap_ci(bot_vs_idle, mean_fn)
        else:
            ci_lo, ci_hi = Decimal("0"), Decimal("0")

        # Headline: single-window absolute active return over the full span.
        single_strat = attribute_active(fills, capital=capital, window_bounds=[(min_ts, max_ts)])
        single_base = attribute_passive(market_rate_points, window_bounds=[(min_ts, max_ts)])
        headline_bot_vs_idle = single_strat[0].net_monthly

        # Secondary diagnostic: MR alpha = active − AlwaysMarketRate.
        mr_actives = paired_active_returns(strat_outcomes, base_outcomes)
        if len(mr_actives) >= 2:
            mr_alpha_ci_lo, mr_alpha_ci_hi = bootstrap_ci(mr_actives, mean_fn)
        else:
            mr_alpha_ci_lo, mr_alpha_ci_hi = Decimal("0"), Decimal("0")
        mr_alpha_spread = single_strat[0].net_monthly - single_base[0].net_monthly
        # MR-alpha is trustworthy only with real market-rate coverage and an
        # in-band series. The band check is deferred to band_reason below.
        mr_alpha_available = len(window_rate_points) > 0

        full_clamp = clamp_active_window(fills, cap=capital)
        total_capital_days = full_clamp.capital_days
        clamp_diag = ClampDiagnostic(
            cap=capital,
            peak_concurrent=full_clamp.peak_concurrent,
            raw_interest=full_clamp.raw_interest,
            clamped_interest=full_clamp.interest,
        )

        # attributed_deployed = open principal at the end of the data window
        # (point-in-time, comparable to the position_state realized snapshot).
        attributed_deployed = open_principal_at(fills, max_ts)
        attributed_interest = sum(
            (f.size_usdt * f.rate * _fill_duration_days(f) for f in fills), Decimal("0")
        )
    else:
        # No fills (idle canary): no active arm → no bot-vs-idle, no MR-alpha.
        ci_lo, ci_hi = Decimal("0"), Decimal("0")
        headline_bot_vs_idle = Decimal("0")
        mr_alpha_spread = Decimal("0")
        mr_alpha_ci_lo, mr_alpha_ci_hi = Decimal("0"), Decimal("0")
        mr_alpha_available = False
        total_capital_days = Decimal("0")
        attributed_deployed = Decimal("0")
        attributed_interest = Decimal("0")
        clamp_diag = ClampDiagnostic(
            cap=capital,
            peak_concurrent=Decimal("0"),
            raw_interest=Decimal("0"),
            clamped_interest=Decimal("0"),
        )

    # Band guard: a wrong-scale market series corrupts the MR-alpha diagnostic
    # only — bot-vs-idle (idle ≡ 0) needs no market-rate data, so the primary
    # verdict is unaffected. Mark MR-alpha unavailable and surface the message.
    band_reason: str | None = None
    try:
        assert_market_rate_band([p.rate for p in window_rate_points])
    except ValueError as exc:
        band_reason = str(exc)
        mr_alpha_available = False

    n_windows = len(bounds)
    min_capital_days = capital * Decimal("7")

    deployment_anchor = check_deployment_anchor(
        attributed_deployed=attributed_deployed,
        observed_realized=observed_realized,
        tol=_DEPLOY_TOL,
    )
    nav_anchor = check_nav_anchor(
        nav_delta=None,  # NAV unavailable in v1
        attributed_interest=attributed_interest,
        tol=_NAV_TOL,
    )

    verdict = decide_verdict(
        headline_bot_vs_idle=headline_bot_vs_idle,
        n_windows=n_windows,
        total_capital_days=total_capital_days,
        ci_lo=ci_lo,
        ci_hi=ci_hi,
        deployment_anchor=deployment_anchor,
        nav_anchor=nav_anchor,
        min_windows=_MIN_WINDOWS,
        min_capital_days=min_capital_days,
        mr_alpha_spread=mr_alpha_spread,
        mr_alpha_ci_lo=mr_alpha_ci_lo,
        mr_alpha_ci_hi=mr_alpha_ci_hi,
        mr_alpha_available=mr_alpha_available,
    )

    # MR-alpha unavailability caveats are informational — they do NOT change the
    # primary bot-vs-idle state (decoupled from passive-arm data quality). Prepend
    # so the operator sees why the secondary diagnostic is missing.
    caveats: list[str] = []
    if band_reason is not None:
        caveats.append(band_reason)
    elif len(window_rate_points) == 0 and (fills or market_rate_points):
        caveats.append(
            "MR-alpha diagnostic unavailable: no market-rate coverage in window — "
            "bot-vs-idle (idle ≡ 0) is unaffected"
        )
    if caveats:
        verdict = G3Verdict(
            state=verdict.state,
            headline_bot_vs_idle=verdict.headline_bot_vs_idle,
            n_windows=verdict.n_windows,
            ci_lo=verdict.ci_lo,
            ci_hi=verdict.ci_hi,
            reasons=[*caveats, *verdict.reasons],
            mr_alpha_spread=verdict.mr_alpha_spread,
            mr_alpha_ci_lo=verdict.mr_alpha_ci_lo,
            mr_alpha_ci_hi=verdict.mr_alpha_ci_hi,
            mr_alpha_available=verdict.mr_alpha_available,
        )

    # Human-readable data window
    if fills or market_rate_points:
        min_dt = datetime.fromtimestamp(min_ts / 1000, UTC).strftime("%Y-%m-%d")
        max_dt = datetime.fromtimestamp(max_ts / 1000, UTC).strftime("%Y-%m-%d")
        data_window = f"{min_dt}..{max_dt}"
    else:
        data_window = "n/a"

    return verdict, data_window, n_fills, clamp_diag
```

Also add the `G3Verdict` import to the `live_attribution` import block in `_g3_loaders.py` if not already present (it is, line ~31 — keep). Remove the now-unused references to the old override blocks (this replacement deletes them). Note `assert_market_rate_band` import stays (still used).

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/scripts/test_g3_loaders.py -v`
Expected: all PASS (including the two seeded `build_verdict_from_neon` tests — but those assert UNRELIABLE; see Step 5).

- [ ] **Step 5: Fix the two seeded build_verdict tests (band no longer → UNRELIABLE)**

The two `build_verdict_from_neon` tests assert UNRELIABLE/INSUFFICIENT via the band trip. Update `test_build_verdict_queries_only_fust_p2_1h_cell` (lines ~162-185): a correct query reads the frr-scale target cell → band caveat present + `mr_alpha_available False`; the state is now INSUFFICIENT_DATA (idle, 0 fills). Replace its assertions:

```python
    verdict, _window, n_fills, _clamp = await build_verdict_from_neon(
        capital=C, session_factory=g3_factory
    )
    # A correct fUST/p2/1h query reads the frr-scale target → band caveat fires
    # (proving cell targeting). Band no longer forces UNRELIABLE: idle canary
    # (0 fills) → INSUFFICIENT_DATA with mr_alpha unavailable.
    assert verdict.state is VerdictState.INSUFFICIENT_DATA
    assert verdict.mr_alpha_available is False
    assert any("plausible per-day band" in r for r in verdict.reasons)
    assert n_fills == 0
```

`test_build_verdict_legit_idle_cell_is_insufficient_not_crash` (lines ~188-205) already expects INSUFFICIENT_DATA with no band reason — keep, and add `assert verdict.mr_alpha_available is False` (idle, 0 fills → no active arm) after the existing asserts.

Run: `uv run pytest tests/scripts/test_g3_loaders.py -v`
Expected: all PASS.

- [ ] **Step 6: Commit**

```bash
git add scripts/_g3_loaders.py tests/scripts/test_g3_loaders.py
git commit -m "✨ Feat: _compute_verdict primary bot-vs-idle CI + decoupled MR-alpha guards (G3 Stage 3)"
```

---

### Task 4: `render_markdown` + `_verdict_to_json` reframe

**Files:**
- Modify: `scripts/run_g3_live_validation.py:22-79` (`render_markdown`, `_verdict_to_json`)
- Test: `tests/scripts/test_run_g3_live_validation.py`

- [ ] **Step 1: Update the failing tests**

In `tests/scripts/test_run_g3_live_validation.py`, replace the `_verdict` helper's `base` dict (lines ~22-38) to use the new field names + MR-alpha defaults:

```python
    base: dict = {
        "headline_bot_vs_idle": Decimal("0.06"),
        "n_windows": 10,
        "total_capital_days": Decimal("4000"),
        "ci_lo": Decimal("0.01"),
        "ci_hi": Decimal("0.10"),
        "deployment_anchor": check_deployment_anchor(
            attributed_deployed=Decimal("300"),
            observed_realized=Decimal("300"),
            tol=Decimal("0.05"),
        ),
        "nav_anchor": check_nav_anchor(
            nav_delta=None, attributed_interest=Decimal("0"), tol=Decimal("0.1")
        ),
        "min_windows": 8,
        "min_capital_days": Decimal("3990"),
        "mr_alpha_spread": Decimal("0.0"),
        "mr_alpha_ci_lo": Decimal("-0.01"),
        "mr_alpha_ci_hi": Decimal("0.02"),
        "mr_alpha_available": True,
    }
```

Update `test_render_markdown_contains_verdict_and_headline` to assert the bot-vs-idle framing, and add MR-alpha section tests:

```python
def test_render_markdown_contains_verdict_and_headline():
    from scripts.run_g3_live_validation import render_markdown

    v = _verdict({})
    md = render_markdown(
        verdict=v, data_window="2026-05-30..2026-06-30", n_fills=12, clamp_diag=_diag("400")
    )
    assert "PASS" in md
    assert "0.06" in md
    assert "bot-vs-idle" in md
    assert "G3 Live Validation" in md


def test_render_markdown_mr_alpha_section_available():
    from scripts.run_g3_live_validation import render_markdown

    v = _verdict({})  # mr_alpha_available True by default
    md = render_markdown(verdict=v, data_window="x", n_fills=12, clamp_diag=_diag("400"))
    assert "MR timing alpha" in md
    assert "secondary diagnostic" in md
    assert "0% by construction" in md
    # the diagnostic numbers render when available
    assert "0.02" in md  # mr_alpha_ci_hi


def test_render_markdown_mr_alpha_section_unavailable():
    from scripts.run_g3_live_validation import render_markdown

    v = _verdict({"mr_alpha_available": False})
    md = render_markdown(verdict=v, data_window="x", n_fills=12, clamp_diag=_diag("400"))
    assert "MR timing alpha" in md
    assert "unavailable" in md
```

Update `test_verdict_to_json_includes_over_deploy_block` to also pin the new JSON shape (renamed headline key + `mr_alpha` block):

```python
def test_verdict_to_json_includes_over_deploy_block():
    from scripts.run_g3_live_validation import _verdict_to_json

    v = _verdict({"n_windows": 1})
    j = _verdict_to_json(v, _diag("863"))
    assert "over_deploy" in j
    od = j["over_deploy"]
    assert od["cap"] == "570"
    assert od["peak_concurrent"] == "863"
    assert od["detected"] is True
    j2 = _verdict_to_json(v, _diag("400"))
    assert j2["over_deploy"]["detected"] is False
    # reframed keys
    assert j["headline_bot_vs_idle"] == "0.06"
    assert "headline_active_spread" not in j
    assert j["mr_alpha"]["available"] is True
    assert j["mr_alpha"]["spread"] == "0.0"
    assert j["mr_alpha"]["ci_lo"] == "-0.01"
    assert j["mr_alpha"]["ci_hi"] == "0.02"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/scripts/test_run_g3_live_validation.py -v`
Expected: FAIL — `decide_verdict()` keyword mismatch in `_verdict` and missing `bot-vs-idle` / `mr_alpha` strings.

- [ ] **Step 3: Write the implementation**

In `scripts/run_g3_live_validation.py`, replace `render_markdown` (lines ~22-61) with:

```python
def render_markdown(
    *, verdict: G3Verdict, data_window: str, n_fills: int, clamp_diag: ClampDiagnostic
) -> str:
    """Pure renderer — unit-testable without a DB."""
    honesty = [
        "## Honesty caveats",
        "- bot-vs-idle PASS asserts the bot beats an idle balance (earns the market"
        " rate), NOT that MR timing beats always-lending — see the MR alpha diagnostic.",
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

    if verdict.mr_alpha_available:
        mr_alpha = [
            "## MR timing alpha (secondary diagnostic)",
            f"- MR alpha spread (active − AlwaysMarketRate): {verdict.mr_alpha_spread}%",
            f"- MR alpha 95% CI: [{verdict.mr_alpha_ci_lo}, {verdict.mr_alpha_ci_hi}]",
            "- Diagnostic only — does NOT gate the verdict. Near 0 means MR timing adds"
            " little over always-lending; the product value is bot-vs-idle.",
            "- idle arm = 0% by construction; the headline is the bot's own realized"
            " return on the allocated budget.",
        ]
    else:
        mr_alpha = [
            "## MR timing alpha (secondary diagnostic)",
            "- unavailable — no in-band market-rate coverage in window (see reasons)."
            " bot-vs-idle (idle ≡ 0) is unaffected.",
            "- idle arm = 0% by construction; the headline is the bot's own realized"
            " return on the allocated budget.",
        ]

    lines = [
        "# G3 Live Validation — fUST MeanReversion (a30, p2), deployed ema_span=24/thr=0.5",
        "",
        f"Data window: {data_window} | fills: {n_fills} | weekly windows: {verdict.n_windows}",
        "",
        "## TL;DR",
        f"- **Verdict: {verdict.state.value}**",
        f"- Headline bot-vs-idle (absolute return on budget since inception): {verdict.headline_bot_vs_idle}%",
        f"- bot-vs-idle 95% CI: [{verdict.ci_lo}, {verdict.ci_hi}]",
        "",
        "### Reasons",
        *[f"- {r}" for r in verdict.reasons],
        "",
        *mr_alpha,
        "",
        *honesty,
        "",
        "## Recommendation",
        "- PASS: bot reliably beats idle (earns the market rate on the budget); scale-up is the operator's call.",
        "- INSUFFICIENT_DATA: keep accruing fills/windows; re-run after more weekly windows.",
        "- FAIL: bot does not beat idle (negative-rate regime or realized loss) — investigate before scaling.",
        "- UNRELIABLE: yield model diverges from venue truth — fix attribution before trusting.",
    ]
    return "\n".join(lines)
```

Replace `_verdict_to_json` (lines ~64-79) with:

```python
def _verdict_to_json(v: G3Verdict, clamp_diag: ClampDiagnostic) -> dict[str, object]:
    return {
        "state": v.state.value,
        "headline_bot_vs_idle": str(v.headline_bot_vs_idle),
        "n_windows": v.n_windows,
        "ci_lo": str(v.ci_lo),
        "ci_hi": str(v.ci_hi),
        "reasons": v.reasons,
        "mr_alpha": {
            "spread": str(v.mr_alpha_spread),
            "ci_lo": str(v.mr_alpha_ci_lo),
            "ci_hi": str(v.mr_alpha_ci_hi),
            "available": v.mr_alpha_available,
        },
        "over_deploy": {
            "cap": str(clamp_diag.cap),
            "peak_concurrent": str(clamp_diag.peak_concurrent),
            "raw_interest": str(clamp_diag.raw_interest),
            "clamped_interest": str(clamp_diag.clamped_interest),
            "detected": clamp_diag.over_deployed,
        },
    }
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/scripts/test_run_g3_live_validation.py -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add scripts/run_g3_live_validation.py tests/scripts/test_run_g3_live_validation.py
git commit -m "✨ Feat: G3 report leads bot-vs-idle headline + MR-alpha secondary section (G3 Stage 3)"
```

---

### Task 5: Full gate + final verification

**Files:** none (verification only)

- [ ] **Step 1: Run the full commit gate**

Run (from `backend_py/`):
```bash
uv run pytest -m "not integration" -q && uv run mypy src/ && uv run ruff check
```
Expected: pytest all green (906+ tests), mypy clean on `src/`, ruff clean. Note: `scripts/` is NOT in the mypy gate; the two pre-existing `scripts/bfx_offer_spike.py` / `scripts/explore_mr_param_sensitivity.py` mypy errors are known tech debt — do NOT touch them.

- [ ] **Step 2: If any failure, debug with systematic-debugging skill**

Do not paper over failures. If a test outside the four touched files broke, it is a real regression from the rename — fix the reference, re-run.

- [ ] **Step 3: Sanity-check the verdict module imports cleanly**

Run: `uv run python -c "from scripts._g3_loaders import _compute_verdict; from scripts.run_g3_live_validation import render_markdown, _verdict_to_json; print('imports ok')"`
Expected: `imports ok`

- [ ] **Step 4: Final commit (only if Steps 1-3 produced uncommitted fixes)**

```bash
git add -A && git commit -m "✅ Test: G3 Stage 3 bot-vs-idle gate green (pytest+mypy+ruff)"
```

---

## Self-review notes

- **Spec coverage:** `attribute_idle` (T1) ✓; `G3Verdict` rename + MR-alpha fields + `decide_verdict` (T2) ✓; `_compute_verdict` primary CI + decoupled guards (T3) ✓; `render_markdown`/`_verdict_to_json` reframe (T4) ✓; tests for each ✓; gate (T5) ✓.
- **Type consistency:** field/param names `headline_bot_vs_idle`, `mr_alpha_spread`, `mr_alpha_ci_lo`, `mr_alpha_ci_hi`, `mr_alpha_available` used identically across T2 (definition), T3 (loader construction via `decide_verdict`), T4 (render/JSON). `attribute_idle(*, window_bounds=...)` keyword-only, matching `attribute_passive`'s shape and T3's call site.
- **Out of scope (per spec):** backtest OOS report reframe; G3 ≥8-window threshold unchanged.
