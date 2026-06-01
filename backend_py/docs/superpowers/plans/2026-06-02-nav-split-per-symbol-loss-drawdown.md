# NAV-split — per-symbol loss/drawdown limiters — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Split the L2 `RealizedLossGuard` / `DrawdownGuard` from a single global-summed NAV metric to per-symbol metrics, so a profitable currency can no longer mask a losing one (the D4 hard gate before fUSD trades).

**Architecture:** `ReconcileNavTracker` already buckets NAV per symbol but collapses all buckets into one global 24h window + one global peak. We replace those with per-symbol windows + per-symbol peaks, change the two metric methods to take a `symbol`, have both guards pass `decision.symbol`, and delete the global series. Threshold stays a single shared scalar (no config change). The fUST-only path must remain byte-identical.

**Tech Stack:** Python 3.13, async, `decimal.Decimal`, `collections.deque`, pytest (`pytest -m "not integration"`), mypy, ruff.

**Spec:** `docs/superpowers/specs/2026-06-02-nav-split-per-symbol-loss-drawdown-design.md`

**Working directories (per repo CLAUDE.md):**
- `pytest` / `mypy` / `ruff` run from `backend_py/` (e.g. `cd backend_py && uv run pytest ...`).
- `git` runs from the repo root (`bfx-funding-bot/`); file paths below are relative to `backend_py/`, so prefix `backend_py/` in `git add`.

**No daemon change:** `ReconcileNavTracker(account_id=...)` construction, the single shared instance feeding both guards, the bus subscription, and the scalar-threshold guard construction in `daemon.py` are all UNCHANGED. The change lives entirely inside the tracker, the protocol/guards, and tests/docs.

---

### Task 1: Make the two NAV metrics per-symbol (signature + tracker internals + lockstep stubs)

This is one atomic task: a `Protocol` signature change forces every implementer + caller + test to move together (mypy checks the whole `src/`). After it, the fUST-only path is byte-identical and the old global-sum behaviour is gone.

**Files:**
- Modify: `src/bfx_funding_bot/modules/execution/safety/calibrated_guards.py` (protocol L22-24; guard call sites L51, L80; reason strings L55, L84)
- Modify: `src/bfx_funding_bot/modules/execution/safety/nav_pnl_source.py` (per-symbol internals + signatures + docstring)
- Modify (test stub): `tests/modules/execution/safety/test_calibrated_guards.py:37-49` (`_FakePnLSource`)
- Modify (test stub): `tests/modules/execution/test_integration_path_a.py:52-57` (`_StubPnL`)
- Test: `tests/modules/execution/safety/test_nav_pnl_source.py` (pass `"fUST"` everywhere; replace the two-symbol global test; add two isolation tests)

- [ ] **Step 1: Rewrite the tracker's unit tests to the per-symbol API (failing test)**

In `tests/modules/execution/safety/test_nav_pnl_source.py`, update EVERY metric call to pass `"fUST"`. The calls to change (each `t.realized_loss_pct_24h()` → `t.realized_loss_pct_24h("fUST")`, each `t.drawdown_pct()` → `t.drawdown_pct("fUST")`) are in: `test_cold_start_returns_zero_before_any_reconcile` (L65-66), `test_nav_drop_yields_drawdown_and_loss` (L76-77), `test_nav_rise_is_not_a_loss_or_drawdown` (L87-88), `test_nav_is_available_plus_reserved_plus_realized` (L105-106), `test_24h_window_evicts_old_high_for_loss_but_peak_is_all_time` (L122-124), `test_recovery_from_trough_tracks_latest_not_trough` (L137-138), `test_new_peak_after_recovery_resets_drawdown` (L148, L152), `test_ignores_other_account` (L162-163), `test_single_symbol_global_api_identical_to_pre_per_symbol` (L179-180).

Then **replace** `test_two_symbols_global_nav_is_sum_of_latest_per_symbol` (L183-202) entirely with these two tests:

```python
@pytest.mark.asyncio
async def test_two_symbols_isolated_no_cross_masking() -> None:
    """Per-symbol: a fUST drop is measured against fUST's OWN peak/window only;
    fUSD (unchanged) reads 0%. No cross-symbol summing — a profitable fUSD can
    never mask a losing fUST, nor vice versa (the D4 risk-isolation invariant)."""
    t = ReconcileNavTracker(account_id=_ACC)
    await t.on_position_reconciled(
        _reconciled(available="100", ts=_T0, symbol="fUST")
    )
    await t.on_position_reconciled(
        _reconciled(available="100", ts=_T0 + _HOUR_MS, symbol="fUSD")
    )
    # fUST drops 100 → 70 in its OWN bucket; fUSD untouched.
    await t.on_position_reconciled(
        _reconciled(available="70", ts=_T0 + 2 * _HOUR_MS, symbol="fUST")
    )
    # fUST vs fUST's own peak/window: 30% loss + 30% drawdown.
    assert t.realized_loss_pct_24h("fUST") == pytest.approx(30.0)
    assert t.drawdown_pct("fUST") == pytest.approx(30.0)
    # fUSD never dropped from its own peak: 0% (NOT the old summed 15%).
    assert t.realized_loss_pct_24h("fUSD") == 0.0
    assert t.drawdown_pct("fUSD") == 0.0


@pytest.mark.asyncio
async def test_unseen_symbol_is_permissive_while_another_has_history() -> None:
    """Cold-start is PER-BUCKET: a never-reconciled symbol returns 0.0 even when
    another symbol already has a drawdown (must not gate a freshly-funded 2nd
    currency on the first currency's history)."""
    t = ReconcileNavTracker(account_id=_ACC)
    await t.on_position_reconciled(
        _reconciled(available="100", ts=_T0, symbol="fUST")
    )
    await t.on_position_reconciled(
        _reconciled(available="60", ts=_T0 + _HOUR_MS, symbol="fUST")
    )
    assert t.realized_loss_pct_24h("fUST") == pytest.approx(40.0)
    # fUSD never seen → permissive, NOT gated on fUST's 40% drop.
    assert t.realized_loss_pct_24h("fUSD") == 0.0
    assert t.drawdown_pct("fUSD") == 0.0
```

- [ ] **Step 2: Run the tracker tests to verify they fail**

Run: `cd backend_py && uv run pytest tests/modules/execution/safety/test_nav_pnl_source.py -q`
Expected: FAIL — `TypeError: realized_loss_pct_24h() takes 1 positional argument but 2 were given` (the methods don't take `symbol` yet).

- [ ] **Step 3: Change the protocol signature and have both guards pass `decision.symbol`**

In `src/bfx_funding_bot/modules/execution/safety/calibrated_guards.py`, change the protocol (L22-24):

```python
class _PnLSourceProtocol(Protocol):
    def realized_loss_pct_24h(self, symbol: str) -> float: ...
    def drawdown_pct(self, symbol: str) -> float: ...
```

In `RealizedLossGuard.evaluate`, change L51 + the reason string (L52-56) to:

```python
        loss_pct = self.source.realized_loss_pct_24h(decision.symbol)
        if loss_pct > self.threshold_pct:
            return GuardResult(
                allowed=False, guard_name=self.name,
                reason=f"realized_loss_pct_24h[{decision.symbol}]={loss_pct} > threshold_pct={self.threshold_pct}",
            )
```

In `DrawdownGuard.evaluate`, change L80 + the reason string (L81-85) to:

```python
        dd = self.source.drawdown_pct(decision.symbol)
        if dd > self.threshold_pct:
            return GuardResult(
                allowed=False, guard_name=self.name,
                reason=f"drawdown[{decision.symbol}]={dd:.4f} > threshold={self.threshold_pct}",
            )
```

- [ ] **Step 4: Rewrite `ReconcileNavTracker` to per-symbol peak + window**

Replace the body of `ReconcileNavTracker` in `src/bfx_funding_bot/modules/execution/safety/nav_pnl_source.py` (the `__init__`, `on_position_reconciled`, and both metric methods — currently L51-98) with:

```python
class ReconcileNavTracker:
    def __init__(self, account_id: str) -> None:
        self.account_id = account_id
        # Per-symbol all-time peak NAV (high-water mark). Keyed by symbol so a
        # profitable currency never lifts another currency's peak.
        self._peak_by_symbol: dict[str, Decimal] = {}
        # Per-symbol 24h window of (occurred_at_ms, nav), oldest first, each
        # trimmed against its own latest occurred_at_ms.
        self._samples_by_symbol: dict[str, deque[tuple[int, Decimal]]] = {}

    async def on_position_reconciled(self, event: PositionReconciled) -> None:
        if event.account_id != self.account_id:
            return
        # _resolve_position_fields guarantees these are non-None at runtime.
        available = event.available
        reserved = event.reserved
        realized = event.realized
        assert available is not None and reserved is not None and realized is not None
        symbol = event.symbol
        nav = available + reserved + realized  # native units; never cross-summed
        samples = self._samples_by_symbol.setdefault(symbol, deque())
        samples.append((event.occurred_at_ms, nav))
        peak = self._peak_by_symbol.get(symbol)
        if peak is None or nav > peak:
            self._peak_by_symbol[symbol] = nav
        cutoff = event.occurred_at_ms - _WINDOW_MS
        while samples and samples[0][0] < cutoff:
            samples.popleft()

    # ---- _PnLSourceProtocol (calibrated_guards) ----

    def realized_loss_pct_24h(self, symbol: str) -> float:
        samples = self._samples_by_symbol.get(symbol)
        if not samples:
            return 0.0
        latest_nav = samples[-1][1]
        window_high = max(nav for _, nav in samples)
        if window_high <= 0:
            return 0.0
        loss = max(Decimal("0"), window_high - latest_nav)
        return float(loss / window_high * 100)

    def drawdown_pct(self, symbol: str) -> float:
        peak = self._peak_by_symbol.get(symbol)
        samples = self._samples_by_symbol.get(symbol)
        if peak is None or peak <= 0 or not samples:
            return 0.0
        latest_nav = samples[-1][1]
        return float((peak - latest_nav) / peak * 100)
```

Then replace the module docstring (L1-40) with the per-symbol version:

```python
"""ReconcileNavTracker — per-symbol NAV source for the L2 loss-limiter guards.

NAV (account equity) for a currency is sampled from each per-symbol
PositionReconciled venue snapshot:

    NAV = available + reserved + realized

i.e. total funding-wallet capital for that currency — idle funds + open offers +
lent principal. A maturing credit returns principal to `available` so NAV is
unchanged; interest paid raises `available` → NAV up; capital lost for ANY reason
(a bug burning funds, a platform socialised loss, a withdrawal) → NAV down.

Metrics are PER SYMBOL — each currency is measured against its OWN history and
NEVER summed across currencies (native units differ, and a profitable currency
must never mask a losing one — the per-currency risk-isolation invariant). For
each symbol:

  * realized_loss_pct_24h(symbol) = (highest NAV in last 24h − latest NAV) / that
      high × 100 — catches fast recent bleeding in that currency.
  * drawdown_pct(symbol)          = (all-time peak NAV − latest NAV) / all-time
      peak × 100 — catches slow sustained decline from that currency's peak.

Both are PERCENTAGES so the guards auto-scale with funded capital — no manual
re-anchoring on deposit/withdrawal. With a single active symbol (fUST today) a
symbol's metric is identical to the pre-per-symbol scalar tracker.

Each symbol's 24h window is trimmed against that symbol's latest occurred_at_ms
(the reconcile clock), so the source needs no wall-clock injection and is fully
deterministic from the event stream.

All in-memory (mirrors PaperPositionLedger.available_balance's deliberate
no-persistence design): every symbol's peak and 24h window reset on restart and
rebuild within ~90s of that symbol's first reconcile. Before a symbol's first
reconcile its metrics return 0.0 (permissive). Limitation: a drawdown developing
across a restart is forgotten; persisting per-symbol peaks is a separate
follow-up.

Caveat: a manual withdrawal lowers a currency's NAV and so reads as a drawdown
for that currency — for a single-operator canary, halting that currency's trading
on an unexplained equity drop is the desired behaviour.
"""
```

(The `from collections import deque` and `from decimal import Decimal` imports at the top of the file are already present and stay.)

- [ ] **Step 5: Update the two test stubs to the new signature**

In `tests/modules/execution/safety/test_calibrated_guards.py`, change `_FakePnLSource`'s two methods (L43-49) to take `symbol`:

```python
    def realized_loss_pct_24h(self, symbol: str) -> float:
        return self.loss_pct

    def drawdown_pct(self, symbol: str) -> float:
        if self.peak == 0:
            return 0.0
        return float((self.peak - self.current) / self.peak)
```

In `tests/modules/execution/test_integration_path_a.py`, change `_StubPnL`'s two methods (L53-57) to:

```python
    def realized_loss_pct_24h(self, symbol: str) -> float:
        return 0.0

    def drawdown_pct(self, symbol: str) -> float:
        return 0.0
```

- [ ] **Step 6: Run the full unit gate + mypy + ruff to verify green**

Run: `cd backend_py && uv run pytest -m "not integration" -q && uv run mypy src/ && uv run ruff check`
Expected: PASS — all unit tests green (incl. the new per-symbol tests and the byte-identical single-symbol tests), mypy clean (protocol + all 4 implementers aligned), ruff clean.

Note: `test_daemon_pnl_wiring.py` needs NO change — it calls `guard.evaluate(_post(), ctx)` where `_post()` already sets `symbol="fUST"` and `_reconciled(...)` emits `symbol="fUST"`, so the fUST 100→40 drop still trips both guards through the per-symbol path.

- [ ] **Step 7: Commit**

```bash
git add backend_py/src/bfx_funding_bot/modules/execution/safety/nav_pnl_source.py \
        backend_py/src/bfx_funding_bot/modules/execution/safety/calibrated_guards.py \
        backend_py/tests/modules/execution/safety/test_nav_pnl_source.py \
        backend_py/tests/modules/execution/safety/test_calibrated_guards.py \
        backend_py/tests/modules/execution/test_integration_path_a.py
git commit -m "✨ Feat: per-symbol NAV loss/drawdown metrics (D4 NAV-split)

ReconcileNavTracker keeps per-symbol 24h window + peak (drop the global Σ series);
RealizedLossGuard/DrawdownGuard pass decision.symbol. fUST-only byte-identical;
two-symbol test rewritten from global-sum to isolation."
```

---

### Task 2: Guard-level per-symbol isolation tests (the headline guarantee)

Task 1 proved the tracker isolates per symbol. This task proves the *guards* route `decision.symbol` correctly end-to-end — a drawdown in symbol A must not block a POST for symbol B. There is no such end-to-end test today.

**Files:**
- Modify: `tests/modules/execution/safety/test_calibrated_guards.py` (add a per-symbol fake + a per-symbol `_post` helper + two tests)

- [ ] **Step 1: Add a per-symbol fake source and helper, and the two failing tests**

In `tests/modules/execution/safety/test_calibrated_guards.py`, add after the existing `_FakePnLSource` class (after L49):

```python
class _PerSymbolFakePnLSource:
    """Returns DIFFERENT loss/drawdown per symbol, so a guard that ignored
    decision.symbol (or hard-coded a bucket) would fail these tests."""
    def __init__(
        self,
        loss_by_symbol: dict[str, float] | None = None,
        dd_by_symbol: dict[str, float] | None = None,
    ) -> None:
        self.loss_by_symbol = loss_by_symbol or {}
        self.dd_by_symbol = dd_by_symbol or {}

    def realized_loss_pct_24h(self, symbol: str) -> float:
        return self.loss_by_symbol.get(symbol, 0.0)

    def drawdown_pct(self, symbol: str) -> float:
        return self.dd_by_symbol.get(symbol, 0.0)
```

Add a per-symbol POST helper next to `_post` (after L34):

```python
def _post_for(symbol: str) -> DecisionPayload:
    return DecisionPayload(
        decision_outcome=DecisionOutcome.POST,
        signal_correlation_id=uuid4(),
        offer_rate=0.0001, offer_amount_usdt=100.0, offer_duration_days=2,
        symbol=symbol)
```

Add the two tests at the end of the file:

```python
@pytest.mark.asyncio
async def test_realized_loss_blocks_only_the_breaching_symbol() -> None:
    """fUSD over its own 24h loss threshold must NOT block fUST, and vice versa."""
    src = _PerSymbolFakePnLSource(loss_by_symbol={"fUSD": 7.0, "fUST": 0.0})
    g = RealizedLossGuard(enabled=True, threshold_pct=5.0, source=src)
    assert (await g.evaluate(_post_for("fUSD"), _ctx())).allowed is False
    assert (await g.evaluate(_post_for("fUST"), _ctx())).allowed is True


@pytest.mark.asyncio
async def test_drawdown_blocks_only_the_breaching_symbol() -> None:
    """fUSD over its own drawdown threshold must NOT block fUST, and vice versa."""
    src = _PerSymbolFakePnLSource(dd_by_symbol={"fUSD": 12.0, "fUST": 0.0})
    g = DrawdownGuard(enabled=True, threshold_pct=10.0, source=src)
    assert (await g.evaluate(_post_for("fUSD"), _ctx())).allowed is False
    assert (await g.evaluate(_post_for("fUST"), _ctx())).allowed is True
```

- [ ] **Step 2: Run the two new tests to verify they pass (and that they pin routing)**

Run: `cd backend_py && uv run pytest tests/modules/execution/safety/test_calibrated_guards.py -q`
Expected: PASS — both new tests green. (They pass against Task 1's code; they would FAIL against the pre-split guards that ignored `decision.symbol`. They are the regression lock for per-symbol routing.)

- [ ] **Step 3: Run mypy + ruff on the changed test file**

Run: `cd backend_py && uv run mypy src/ && uv run ruff check`
Expected: PASS.

- [ ] **Step 4: Commit**

```bash
git add backend_py/tests/modules/execution/safety/test_calibrated_guards.py
git commit -m "✅ Test: guard-level per-symbol isolation (A breaches, B still allowed)"
```

---

### Task 3: Docs + stale-comment cleanup + DivergenceRateGuard per-symbol TODO

**Files:**
- Modify: `ARCHITECTURE.md:299` (drop stale "stub source 回 0" claim; state per-symbol)
- Modify: `src/bfx_funding_bot/modules/execution/safety/calibrated_guards.py` (TODO above `_DivergenceSourceProtocol`, L27)
- Modify: `tests/modules/marketfeed/test_daemon_pnl_wiring.py:136-137` (stale threshold comment)

- [ ] **Step 1: Fix the stale ARCHITECTURE.md L2 description**

In `ARCHITECTURE.md`, replace line 299:

```markdown
**L2 calibrated guards**：`RealizedLossGuard`（24h realized 虧損 > threshold）、`DrawdownGuard`（peak-to-trough drawdown_pct > threshold）、`DivergenceRateGuard`（無已驗證 threshold，目前 disabled）。canary（`safety.canary.yaml`）開 realized_loss + drawdown（目前接 stub source 回 0，真 PnLLedger 待接）。`enabled=True` 但 threshold 為 None 時 loader 直接 `ValueError`。
```

with:

```markdown
**L2 calibrated guards**：`RealizedLossGuard`（24h NAV 虧損 % > threshold）、`DrawdownGuard`（peak-to-trough NAV drawdown_pct > threshold）、`DivergenceRateGuard`（無已驗證 threshold，目前 disabled）。metric 由 `ReconcileNavTracker` 提供，**per-symbol**（每幣別對自己的 24h window-high / all-time peak 計算，絕不跨幣加總——賺錢幣別不會掩蓋虧損幣別）；guard 讀 `decision.symbol` 取對應幣別 metric。canary（`safety.canary.yaml`）開 realized_loss(5%) + drawdown(10%)，單一 active 幣別（fUST）時與 pre-per-symbol 純量值相同。`enabled=True` 但 threshold 為 None 時 loader 直接 `ValueError`。
```

- [ ] **Step 2: Add the DivergenceRateGuard per-symbol TODO**

In `src/bfx_funding_bot/modules/execution/safety/calibrated_guards.py`, add a comment directly above `class _DivergenceSourceProtocol` (L27):

```python
# TODO(per-symbol): _DivergenceSourceProtocol is symbol-blind (a constant-0 stub
# today, so no cross-symbol summing exists to fix). When a real divergence source
# lands it will need the same per-symbol treatment as the NAV metrics above
# (take `symbol`, guard passes decision.symbol).
class _DivergenceSourceProtocol(Protocol):
    def divergence_rate_pct(self, window_minutes: int) -> float: ...
```

- [ ] **Step 3: Fix the stale threshold comment in the wiring test**

In `tests/modules/marketfeed/test_daemon_pnl_wiring.py`, replace the comment at L136-137:

```python
    # (B) subscribed to the daemon bus → (C) a NAV drop makes both guards block.
    #     100 → 40 = $60 loss (> 57 threshold) and 60% drawdown (> 15% threshold).
```

with:

```python
    # (B) subscribed to the daemon bus → (C) a fUST NAV drop makes both guards
    #     block. 100 → 40 = 60% 24h loss (> 5%) and 60% drawdown (> 10%), both
    #     measured against fUST's own per-symbol window/peak.
```

- [ ] **Step 4: Verify docs/comment-only change keeps everything green**

Run: `cd backend_py && uv run pytest -m "not integration" -q && uv run mypy src/ && uv run ruff check`
Expected: PASS (no behaviour change; the wiring test still trips on fUST 100→40).

- [ ] **Step 5: Commit**

```bash
git add backend_py/ARCHITECTURE.md \
        backend_py/src/bfx_funding_bot/modules/execution/safety/calibrated_guards.py \
        backend_py/tests/modules/marketfeed/test_daemon_pnl_wiring.py
git commit -m "📝 Docs: ARCHITECTURE L2 guards now per-symbol; DivergenceGuard per-symbol TODO; fix stale wiring-test threshold comment"
```

---

## Final verification (after all tasks)

- [ ] Run the integration suite too, to catch the `_StubPnL` signature change under the integration marker:

Run: `cd backend_py && uv run pytest tests/modules/execution/test_integration_path_a.py -q`
Expected: PASS (or the same pre-existing skip/xfail state as before this change — confirm no NEW failure from the `_StubPnL` signature).

- [ ] Full gate one more time: `cd backend_py && uv run pytest -m "not integration" -q && uv run mypy src/ && uv run ruff check` — all green.

## Out of scope (do NOT do in this plan)

- Per-symbol threshold *values* (a `dict[str,float]` map): threshold stays a shared scalar; `config.py`, `safety.canary.yaml`, `safety.yaml` are UNCHANGED.
- Any account/portfolio-level backstop guard.
- Peak high-water-mark persistence across restart (separate item — touches DB/migration).
- Any change to `daemon.py` (tracker construction, subscription, guard wiring all unchanged).
- Wiring a real `DivergenceRateGuard` source (left as a TODO only).
