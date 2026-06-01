# Per-Currency Allocation Phase 2 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make allocation caps, balance buffers, order routing, and the ledger read path per-currency (native units), so fUSD/fADA can ship dark at `cap=0` and the live fUST canary behaves byte-identically — closing the executor mis-route + reconciler-global-cap + divergent-default blind spots.

**Architecture:** Single-component, key-partitioned (Paradigm A). Phase 1 already made every stateful surface `dict[symbol]`-keyed; Phase 2 moves the remaining VALUES (caps/buffers), one route (executor), and removes the cross-symbol-sum backdoors. No per-currency object graph. Borrowed structural safety: an executor-boundary fail-fast invariant.

**Tech Stack:** Python 3.13 (uv-managed; `cd backend_py` for ALL commands), FastAPI daemon, SQLAlchemy 2.0 async, Alembic, pydantic v2, pytest + pytest-asyncio (`asyncio_mode="auto"`), Decimal money. Spec: `docs/superpowers/specs/2026-06-01-per-currency-allocation-phase-2-design.md`.

**Conventions (apply to every task):**
- Run the gate from `backend_py/`: `cd backend_py && uv run pytest -m "not integration"`, then `uv run mypy src/` + `uv run ruff check`.
- Money is `Decimal` (idiom `D = Decimal`). Guards return a frozen `GuardResult(allowed, guard_name, reason)` — they NEVER raise; assert blocked via `r.allowed is False`. The ONLY raise-based money path is the executor fail-fast (Task 2).
- Async tests need no `@pytest.mark.asyncio` (auto-mode) but copy whatever the neighbour file does for a clean diff.
- Commit after every task with the repo's emoji prefix.

---

## File Structure

| File | Responsibility | Tasks |
|---|---|---|
| `src/.../marketfeed/config.py` | host `configured_symbols()` helper (moved here to break the daemon→reconciler circular import) | 1 |
| `src/.../external/bitfinex/live_executor.py` | route submit by `decision.symbol` + fail-fast | 2 |
| `src/.../modules/execution/registry.py` | `build_executor` drops `symbol`, threads configured-symbol set | 3 |
| `src/.../marketfeed/daemon.py` | wire configured-symbol set, caps/buffers maps, boot assert | 3,5,6,7,8 |
| `src/.../modules/execution/safety/config.py` | caps/buffers config schema + `buying_power` block | 4 |
| `configs/safety.yaml` + `configs/safety.canary.yaml` | per-symbol caps/buffers maps | 4 |
| `src/.../modules/execution/safety/hard_guards.py` | guards read `caps[symbol]`/`buffers[symbol]` | 6,7 |
| `src/.../modules/execution/deployment/reconciler.py` | per-symbol sizing loop | 8 |
| `src/.../modules/execution/deployment/tracker.py` | per-symbol rescale partition | 9 |
| `src/.../modules/execution/event_store/store.py` + `serialization.py` | `size_usdt`→`amount` payload + fold symbol filter | 10 |
| `src/.../modules/execution/events.py` + `marketfeed/schemas.py` | mandatory `symbol` (drop `'fUSD'` default) | 11 |
| `src/.../modules/execution/ledger.py` | drop `symbol=None` cross-symbol-sum read backdoor | 12 |
| `alembic/versions/b7c1d2e3f4a5_*.py` | already authored — APPLY only | 13 |

---

## SLICE 1 — Money-route fix (highest priority; latent real-money mis-route)

### Task 1: Move `configured_symbols` helper to break the circular import

**Files:**
- Modify: `src/bfx_funding_bot/modules/marketfeed/config.py` (add helper near `CellConfig`)
- Modify: `src/bfx_funding_bot/modules/marketfeed/daemon.py:154-164` (delete helper, import it)
- Test: `tests/modules/marketfeed/test_config.py` (add a co-location test)

The reconciler (Task 8) must call `configured_symbols(cells)`, but it lives in `daemon.py`, which imports `reconciler.py` → importing it back is a circular import. Move it next to `CellConfig`.

- [ ] **Step 1: Write the failing test**

```python
# tests/modules/marketfeed/test_config.py
from bfx_funding_bot.modules.marketfeed.config import CellConfig, configured_symbols

def test_configured_symbols_distinct_order_preserving():
    cells = [
        CellConfig(symbol="fUST", period_agg="a30", strategy="mean_reversion"),
        CellConfig(symbol="fUST", period_agg="p2", strategy="mean_reversion"),
        CellConfig(symbol="fUSD", period_agg="a30", strategy="mean_reversion"),
    ]
    assert configured_symbols(cells) == ["fUST", "fUSD"]
```
(Match the real `CellConfig` required fields — read `config.py:40-65` and fill any other required kwargs.)

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend_py && uv run pytest tests/modules/marketfeed/test_config.py::test_configured_symbols_distinct_order_preserving -v`
Expected: FAIL — `ImportError: cannot import name 'configured_symbols' from ...config`

- [ ] **Step 3: Move the helper**

Cut the function from `daemon.py:154-164` and paste it into `config.py` (below `CellConfig`):
```python
def configured_symbols(cells: list[CellConfig]) -> list[str]:
    """Distinct cell symbols, order-preserving. Single-currency cells.yaml → 1-element list."""
    seen: dict[str, None] = {}
    for c in cells:
        seen.setdefault(c.symbol, None)
    return list(seen)
```
In `daemon.py`, replace the definition with an import: `from bfx_funding_bot.modules.marketfeed.config import CellConfig, configured_symbols` (keep existing call sites at `:911`/`:932` working).

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd backend_py && uv run pytest tests/modules/marketfeed/ -v`
Expected: PASS (new test + all existing daemon/config tests green — the call sites still resolve).

- [ ] **Step 5: Commit**

```bash
git add backend_py/src/bfx_funding_bot/modules/marketfeed/config.py backend_py/src/bfx_funding_bot/modules/marketfeed/daemon.py backend_py/tests/modules/marketfeed/test_config.py
git commit -m "♻️ Refactor: move configured_symbols to marketfeed/config.py (break daemon↔reconciler circular import)"
```

### Task 2: Executor routes by `decision.symbol` + fail-fast invariant

**Files:**
- Modify: `src/bfx_funding_bot/external/bitfinex/live_executor.py:184,195,201-250`
- Test: `tests/external/bitfinex/test_live_executor_integration.py` (invert the old lock + add routing test)

The old test `test_submit_uses_configured_symbol_and_fixed_point_rate` (`:94-127`) LOCKS the buggy behaviour (executor `symbol` wins). It must be inverted: the POST body symbol must equal `decision.symbol`, not a constructor value.

- [ ] **Step 1: Write the failing test** (and delete/invert the old locking test)

```python
# tests/external/bitfinex/test_live_executor_integration.py
import json, httpx
from datetime import date
from bfx_funding_bot.modules.execution.errors import InvariantViolation
from bfx_funding_bot.modules.execution.bus import DomainEventBus
# ... reuse existing imports/SUCCESS response/_EventCapture/_make_decision/_make_ctx in this file

async def test_submit_routes_by_decision_symbol_not_constructor():
    captured = []
    def handler(req): captured.append(json.loads(req.content)); return httpx.Response(200, json=SUCCESS)
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    ex = BitfinexLiveExecutor(
        http=http, event_sink=_EventCapture(), bus=DomainEventBus(),
        phase=Phase.PAPER, strategy=StrategyName.MEAN_REVERSION,
        cell="fUST_a30", configured_symbols=frozenset({"fUST"}),  # NO symbol= param
        nonce_provider=lambda: 1000, date_provider=lambda: date(2026, 5, 22),
    )
    decision = _make_decision(symbol="fUST")   # route MUST follow the decision
    result = await ex.submit(decision, _make_ctx())
    assert captured[0]["symbol"] == "fUST" == decision.symbol
    assert result.status == "submitted"

async def test_submit_rejects_unconfigured_symbol():
    http = httpx.AsyncClient(transport=httpx.MockTransport(lambda req: httpx.Response(200, json=SUCCESS)))
    ex = BitfinexLiveExecutor(http=http, event_sink=_EventCapture(), bus=DomainEventBus(),
        phase=Phase.PAPER, strategy=StrategyName.MEAN_REVERSION, cell="fUST_a30",
        configured_symbols=frozenset({"fUST"}), nonce_provider=lambda: 1, date_provider=lambda: date(2026,5,22))
    with pytest.raises(InvariantViolation):
        await ex.submit(_make_decision(symbol="fUSD"), _make_ctx())  # fUSD not configured
```
Update `_make_decision` to accept `symbol="fUST"`. DELETE the old `test_submit_uses_configured_symbol_and_fixed_point_rate` body that asserts the executor's own symbol wins (keep its fixed-point-rate assertions in a separate test that builds the decision WITH `symbol="fUST"`).

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend_py && uv run pytest tests/external/bitfinex/test_live_executor_integration.py -v`
Expected: FAIL — `TypeError: __init__ got unexpected keyword 'configured_symbols'` (param doesn't exist yet).

- [ ] **Step 3: Implement the route fix**

In `live_executor.py` `__init__` (`:176-199`): delete the `symbol: str` param and `self._symbol = symbol`; add `configured_symbols: frozenset[str]` param and `self._configured_symbols = configured_symbols`. Keep `cell`.
In `submit()` (`:201`), at the top before `cid`/payload:
```python
if not decision.symbol or decision.symbol not in self._configured_symbols:
    raise InvariantViolation(
        f"submit rejected: decision.symbol={decision.symbol!r} not in "
        f"configured set {sorted(self._configured_symbols)}"
    )
```
Line `:209`: `symbol=self._symbol` → `symbol=decision.symbol`. Line `:246` network-error log: `self._symbol` → `decision.symbol`. (Line `:238` already logs `payload['symbol']` — no change.) Verify `grep self._symbol live_executor.py` returns 0 hits.

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd backend_py && uv run pytest tests/external/bitfinex/test_live_executor_integration.py -v`
Expected: FAIL still — the 5 other constructor sites in this file pass `symbol=` (fixed in Task 3). Run just the two new tests: `... -k "routes_by_decision or rejects_unconfigured"` → PASS.

- [ ] **Step 5: Commit**

```bash
git add backend_py/src/bfx_funding_bot/external/bitfinex/live_executor.py backend_py/tests/external/bitfinex/test_live_executor_integration.py
git commit -m "🐛 Fix: executor routes by decision.symbol + fail-fast on unconfigured symbol (close §8 mis-route blind spot)"
```

### Task 3: Drop `symbol` from `build_executor`; thread the configured-symbol set

**Files:**
- Modify: `src/bfx_funding_bot/modules/execution/registry.py:52-61,108-111`
- Modify: `src/bfx_funding_bot/modules/marketfeed/daemon.py:799-807` (the ONLY prod call site)
- Test: `tests/modules/execution/test_registry.py`, `test_registry_factory.py` (10 calls), `tests/external/bitfinex/test_live_executor_integration.py` + `test_live_executor_cancel.py` (5 ctor sites)

> Spec §4 A2 line numbers (881/944/1031) were wrong — `grep -n 'build_executor('` shows exactly ONE prod call at `daemon.py:799`. The other sites are TESTS.

- [ ] **Step 1: Update the failing tests to the new signature**

In `test_registry.py`/`test_registry_factory.py`, change every `build_executor(..., symbol="fUSD", ...)` to drop `symbol=` and, for the `bitfinex_live` branch calls, add `configured_symbols=frozenset({"fUST"})`. In `test_live_executor_integration.py` (`:62,:82,:112,:138,:169`) and `test_live_executor_cancel.py:121`, drop `symbol="..."` and add `configured_symbols=frozenset({"fUST"})`.

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd backend_py && uv run pytest tests/modules/execution/test_registry.py tests/modules/execution/test_registry_factory.py -v`
Expected: FAIL — `build_executor() got unexpected keyword 'configured_symbols'`.

- [ ] **Step 3: Implement**

`registry.py:52-61`: delete `symbol: str` param; add `configured_symbols: frozenset[str]`. At `:108-111`, the `BitfinexLiveExecutor(...)` construction drops `symbol=symbol`, adds `configured_symbols=configured_symbols`. The `EchoPaperExecutor` branch (`:82-84`) is unchanged (it never took symbol).
`daemon.py:799-807`: drop `symbol=first_cell.symbol`; add `configured_symbols=frozenset(configured_symbols(config.cells))`.

- [ ] **Step 4: Run the full gate**

Run: `cd backend_py && uv run pytest -m "not integration" && uv run mypy src/ && uv run ruff check`
Expected: PASS (executor + registry + daemon-wiring tests all green).

- [ ] **Step 5: Commit**

```bash
git add backend_py/src/bfx_funding_bot/modules/execution/registry.py backend_py/src/bfx_funding_bot/modules/marketfeed/daemon.py backend_py/tests/
git commit -m "♻️ Refactor: build_executor drops symbol, threads configured-symbol set (currency-agnostic executor)"
```

---

## SLICE 2 — Per-symbol caps/buffers config + ALL consumers

### Task 4: Config schema — caps/buffers maps + `buying_power` block

**Files:**
- Modify: `src/bfx_funding_bot/modules/execution/safety/config.py:32-42`
- Modify: `configs/safety.yaml`, `configs/safety.canary.yaml`
- Test: `tests/modules/execution/safety/test_safety_config.py`

- [ ] **Step 1: Write the failing test**

```python
# test_safety_config.py
from decimal import Decimal
def test_caps_and_buffers_maps_parse(tmp_path):
    p = tmp_path / "safety.yaml"
    p.write_text("""
hard_guards:
  manual_kill: {enabled: true}
  auth_health: {enabled: true}
  heartbeat: {enabled: true, sub_task_stale_threshold_seconds: 300}
  allocation_cap:
    enabled: true
    caps: {fUST: 3000, fUSD: 0, fADA: 0}
    default_cap: 0
  buying_power:
    enabled: true
    buffers: {fUST: 3, fUSD: 3, fADA: 0}
    default_buffer: 0
calibrated_guards:
  realized_loss_24h: {enabled: false}
  drawdown_from_peak: {enabled: false}
  divergence_rate: {enabled: false}
""")
    cfg = load_safety_config(p)
    assert cfg.hard_guards.allocation_cap.caps["fUST"] == Decimal("3000")
    assert cfg.hard_guards.buying_power.buffers["fADA"] == Decimal("0")
    assert cfg.hard_guards.allocation_cap.default_cap == Decimal("0")
```
(Copy the exact `calibrated_guards`/heartbeat shape from an existing `_safety_yaml` helper in this file so the model validates.)

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend_py && uv run pytest tests/modules/execution/safety/test_safety_config.py::test_caps_and_buffers_maps_parse -v`
Expected: FAIL — `ValidationError: extra fields not permitted (caps)` and `buying_power`.

- [ ] **Step 3: Implement schema + both YAMLs**

`config.py` (add `from decimal import Decimal` at top):
```python
class _AllocationCapCfg(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool
    caps: dict[str, Decimal] = {}
    default_cap: Decimal = Decimal("0")

class _BuyingPowerCfg(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool
    buffers: dict[str, Decimal] = {}
    default_buffer: Decimal = Decimal("0")

class HardGuardsCfg(BaseModel):
    model_config = ConfigDict(extra="forbid")
    manual_kill: _ManualKillCfg
    auth_health: _AuthHealthCfg
    heartbeat: _HeartbeatCfg
    allocation_cap: _AllocationCapCfg
    buying_power: _BuyingPowerCfg   # NEW required field
```
`configs/safety.yaml` AND `configs/safety.canary.yaml` — under `hard_guards:`:
```yaml
  allocation_cap:
    enabled: true
    caps: {fUSD: 0, fUST: 3000, fADA: 0}
    default_cap: 0
  buying_power:                      # NEW block
    enabled: true
    buffers: {fUSD: 3, fUST: 3, fADA: 0}
    default_buffer: 0
```
Because `buying_power` is required, ALSO add the block to every test-fixture YAML in `test_safety_config.py` and `test_canary_invariant.py` (search `_safety_yaml`).

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd backend_py && uv run pytest tests/modules/execution/safety/test_safety_config.py tests/modules/marketfeed/test_canary_invariant.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add backend_py/src/bfx_funding_bot/modules/execution/safety/config.py backend_py/configs/safety.yaml backend_py/configs/safety.canary.yaml backend_py/tests/
git commit -m "✨ Feat: per-symbol caps/buffers config maps + buying_power block (both safety yamls)"
```

### Task 5: Boot assert `assert_caps_invariant` (explicit entry; `>0` under canary)

**Files:**
- Modify: `src/bfx_funding_bot/modules/marketfeed/daemon.py` (near `assert_canary_guard_invariant:637`)
- Test: `tests/modules/marketfeed/test_canary_invariant.py`

- [ ] **Step 1: Write the failing test**

```python
# test_canary_invariant.py
import pytest
from bfx_funding_bot.modules.marketfeed.daemon import assert_caps_invariant
from bfx_funding_bot.modules.marketfeed.config import CellConfig
from bfx_funding_bot.core.phase import Phase   # match the real Phase import

def _alloc_cfg(caps): 
    from bfx_funding_bot.modules.execution.safety.config import _AllocationCapCfg
    return _AllocationCapCfg(enabled=True, caps=caps, default_cap=0)

def test_caps_invariant_raises_when_configured_symbol_missing():
    cells = [CellConfig(symbol="fUST", period_agg="a30", strategy="mean_reversion")]
    with pytest.raises(ValueError, match="fUST"):
        assert_caps_invariant(Phase.CANARY, cells, _alloc_cfg({"fUSD": 0}))

def test_caps_invariant_raises_when_canary_cap_zero():
    cells = [CellConfig(symbol="fUST", period_agg="a30", strategy="mean_reversion")]
    with pytest.raises(ValueError, match="cap.*0|> 0"):
        assert_caps_invariant(Phase.CANARY, cells, _alloc_cfg({"fUST": 0}))

def test_caps_invariant_ok_for_funded_canary():
    cells = [CellConfig(symbol="fUST", period_agg="a30", strategy="mean_reversion")]
    assert_caps_invariant(Phase.CANARY, cells, _alloc_cfg({"fUST": 3000})) is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend_py && uv run pytest tests/modules/marketfeed/test_canary_invariant.py -k caps_invariant -v`
Expected: FAIL — `ImportError: cannot import name 'assert_caps_invariant'`.

- [ ] **Step 3: Implement** (mirror `assert_canary_guard_invariant:637-657`)

```python
def assert_caps_invariant(phase: Phase, cells: list[CellConfig], alloc_cfg: _AllocationCapCfg) -> None:
    """Every configured-cell symbol needs an explicit caps entry; >0 under canary (config-fatal)."""
    for symbol in configured_symbols(cells):
        if symbol not in alloc_cfg.caps:
            raise ValueError(f"caps invariant: configured symbol {symbol!r} has no explicit caps entry")
        if phase == Phase.CANARY and alloc_cfg.caps[symbol] <= 0:
            raise ValueError(f"caps invariant: canary symbol {symbol!r} cap must be > 0, got {alloc_cfg.caps[symbol]}")
```
Call it in `build_daemon` right after `assert_canary_guard_invariant(...)`, and log the effective cap per symbol: `log.info("effective_cap_per_symbol %s", {s: alloc_cfg.caps.get(s, alloc_cfg.default_cap) for s in configured_symbols(config.cells)})`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd backend_py && uv run pytest tests/modules/marketfeed/test_canary_invariant.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add backend_py/src/bfx_funding_bot/modules/marketfeed/daemon.py backend_py/tests/modules/marketfeed/test_canary_invariant.py
git commit -m "✨ Feat: assert_caps_invariant boot gate (explicit entry + canary cap>0) + effective-cap log"
```

### Task 6: `AllocationCapGuard` reads `caps[symbol]`

**Files:**
- Modify: `src/bfx_funding_bot/modules/execution/safety/hard_guards.py` (`AllocationCapGuard`)
- Modify: `src/bfx_funding_bot/modules/marketfeed/daemon.py` (guard wiring)
- Test: `tests/modules/execution/safety/test_hard_guards.py`

- [ ] **Step 1: Write the failing test** (mirror `test_allocation_cap_isolates_buckets_per_symbol:193`)

```python
async def test_allocation_cap_reads_per_symbol_cap():
    ledger = _FakeLedger({"fUST": Decimal("100"), "fUSD": Decimal("0")})
    g = AllocationCapGuard(ledger=ledger, caps={"fUST": Decimal("3000"), "fUSD": Decimal("0")}, default_cap=Decimal("0"))
    # fUSD cap=0 → any POST blocked
    r = await g.evaluate(_post(symbol="fUSD", amount=Decimal("10")), _ctx())
    assert r.allowed is False and "fUSD" in (r.reason or "")
    # fUST has room
    assert (await g.evaluate(_post(symbol="fUST", amount=Decimal("10")), _ctx())).allowed is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend_py && uv run pytest tests/modules/execution/safety/test_hard_guards.py::test_allocation_cap_reads_per_symbol_cap -v`
Expected: FAIL — `__init__() got unexpected keyword 'caps'`.

- [ ] **Step 3: Implement**

```python
class AllocationCapGuard:
    def __init__(self, *, ledger, caps: dict[str, Decimal], default_cap: Decimal,
                 env_fallback_cap: Decimal | None = None) -> None:
        self.ledger = ledger
        self._caps = caps
        self._default_cap = default_cap
        self._env_fallback = env_fallback_cap

    async def evaluate(self, decision, ctx) -> GuardResult:
        cap = self._caps.get(decision.symbol)
        if cap is None:
            cap = self._env_fallback if self._env_fallback is not None else self._default_cap
        exposure = self.ledger.current_exposure(decision.symbol)
        offer = Decimal(str(decision.offer_amount_usdt))
        if exposure + offer > cap:
            return GuardResult(allowed=False, guard_name="allocation_cap",
                               reason=f"{decision.symbol} exposure {exposure}+{offer} > cap {cap}")
        return GuardResult(allowed=True, guard_name="allocation_cap")
```
`daemon.py` wiring: `AllocationCapGuard(ledger=ledger, caps=hg.allocation_cap.caps, default_cap=hg.allocation_cap.default_cap, env_fallback_cap=Decimal(os.environ.get("BFX_ALLOCATION_CAP_USDT", "0")))`. Stop reading `ctx.allocation_cap_usdt` in the guard. (`ctx.allocation_cap_usdt` stays on `AccountContext` as the env-fallback source — do NOT remove the field.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd backend_py && uv run pytest tests/modules/execution/safety/test_hard_guards.py -v`
Expected: PASS (new + existing per-symbol independence tests).

- [ ] **Step 5: Commit**

```bash
git add backend_py/src/bfx_funding_bot/modules/execution/safety/hard_guards.py backend_py/src/bfx_funding_bot/modules/marketfeed/daemon.py backend_py/tests/modules/execution/safety/test_hard_guards.py
git commit -m "✨ Feat: AllocationCapGuard reads caps[symbol] (cap=0 disables a currency)"
```

### Task 7: `BuyingPowerGuard` reads `buffers[symbol]`

**Files:**
- Modify: `hard_guards.py` (`BuyingPowerGuard`), `daemon.py` (live-only wiring at `:850-851`)
- Test: `tests/modules/execution/safety/test_hard_guards.py`

- [ ] **Step 1: Write the failing test** (mirror `test_buying_power_isolates_buckets_per_symbol:336`)

```python
async def test_buying_power_reads_per_symbol_buffer():
    ledger = _FakeBalanceLedger({"fUST": Decimal("100"), "fUSD": Decimal("2")})
    g = BuyingPowerGuard(ledger=ledger, buffers={"fUST": Decimal("3"), "fUSD": Decimal("3")}, default_buffer=Decimal("0"))
    # fUSD: available 2 - buffer 3 = -1 → any positive offer blocked
    assert (await g.evaluate(_post(symbol="fUSD", amount=Decimal("1")), _ctx())).allowed is False
    # fUST: available 100 - buffer 3 = 97 room
    assert (await g.evaluate(_post(symbol="fUST", amount=Decimal("10")), _ctx())).allowed is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend_py && uv run pytest tests/modules/execution/safety/test_hard_guards.py::test_buying_power_reads_per_symbol_buffer -v`
Expected: FAIL — `__init__() got unexpected keyword 'buffers'`.

- [ ] **Step 3: Implement**

```python
class BuyingPowerGuard:
    def __init__(self, *, ledger, buffers: dict[str, Decimal], default_buffer: Decimal,
                 env_fallback_buffer: Decimal | None = None) -> None:
        self.ledger = ledger
        self._buffers = buffers
        self._default_buffer = default_buffer
        self._env_fallback = env_fallback_buffer

    async def evaluate(self, decision, ctx) -> GuardResult:
        buffer = self._buffers.get(decision.symbol)
        if buffer is None:
            buffer = self._env_fallback if self._env_fallback is not None else self._default_buffer
        available = self.ledger.available_balance(decision.symbol)
        offer = Decimal(str(decision.offer_amount_usdt))
        if offer > available - buffer:
            return GuardResult(allowed=False, guard_name="buying_power",
                               reason=f"{decision.symbol} offer {offer} > available {available} - buffer {buffer}")
        return GuardResult(allowed=True, guard_name="buying_power")
```
Keep the existing live-only / `model_construct` missing-amount defensive branches. `daemon.py:850-851` (inside `if not spec.is_simulated:`): `BuyingPowerGuard(ledger=ledger, buffers=hg.buying_power.buffers, default_buffer=hg.buying_power.default_buffer, env_fallback_buffer=Decimal(os.environ.get("BFX_BALANCE_BUFFER_USDT", "3")))`.

- [ ] **Step 4: Run tests** → `cd backend_py && uv run pytest tests/modules/execution/safety/test_hard_guards.py -v` → PASS (incl. `test_buying_power_skip_bypasses`, `test_buying_power_missing_amount_blocks`).

- [ ] **Step 5: Commit**

```bash
git add backend_py/src/bfx_funding_bot/modules/execution/safety/hard_guards.py backend_py/src/bfx_funding_bot/modules/marketfeed/daemon.py backend_py/tests/modules/execution/safety/test_hard_guards.py
git commit -m "✨ Feat: BuyingPowerGuard reads buffers[symbol] (live-only wiring preserved)"
```

### Task 8: Reconciler per-symbol sizing loop

**Files:**
- Modify: `src/bfx_funding_bot/modules/execution/deployment/reconciler.py:56-73,96-211`
- Modify: `src/bfx_funding_bot/modules/marketfeed/daemon.py` (reconciler wiring at `:987`)
- Test: `tests/modules/execution/deployment/test_reconciler.py`

- [ ] **Step 1: Write the failing test** (extend `_build`/`_FakeLedger` to two symbols; mirror `test_two_active_cells_split_when_gap_exceeds_cap:196`)

```python
async def test_independent_per_symbol_gap_pools():
    cells = [_cell("fUST", "a30"), _cell("fUSD", "a30")]   # TWO symbols
    rec = _build_multi(
        cells=cells,
        exposures={"fUST": D("0"), "fUSD": D("0")},
        available_by_symbol={"fUST": D("5000"), "fUSD": D("5000")},
        caps={"fUST": D("3000"), "fUSD": D("0")},  # fUSD disabled
        buffers={"fUST": D("3"), "fUSD": D("3")},
    )
    await rec.deploy()
    # fUST sized against its 3000 cap; fUSD cap=0 → skipped, zero offers
    submitted = rec._executor.submitted  # adapt to the test's capture mechanism
    assert all(s.symbol == "fUST" for s in submitted)
    assert any(s.symbol == "fUST" for s in submitted)
```
Add `_build_multi` next to the existing `_build`; extend `_FakeLedger` to key `exposure`/`available` by symbol (it already has `available_by_symbol`).

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend_py && uv run pytest tests/modules/execution/deployment/test_reconciler.py::test_independent_per_symbol_gap_pools -v`
Expected: FAIL — `DeploymentReconciler.__init__() got unexpected keyword 'caps'`.

- [ ] **Step 3: Implement**

`__init__`: add `caps: dict[str, Decimal]`, `default_cap: Decimal`, `buffers: dict[str, Decimal]`, `default_buffer: Decimal`. Keep `account_ctx`/`balance_buffer_usdt` as the env-fallback source (don't remove). `deploy()`:
```python
async def deploy(self) -> None:
    now = self._clock()
    for symbol in configured_symbols(self._cells):
        cap = self._caps.get(symbol, self._default_cap)
        if cap == 0:
            continue  # cap=0 ships dark; guard also blocks (defense)
        buffer = self._buffers.get(symbol, self._default_buffer)
        e_total = self._ledger.current_exposure(symbol)
        headroom = max(Decimal("0"), self._ledger.available_balance(symbol) - buffer)
        cap_per_cell = self._concentration_pct * cap
        symbol_cells = [c for c in self._cells if self._cell_symbol[c.cell_id] == symbol]
        fills = allocate_gap(target=cap, current_exposure=e_total,
                             active_cells=symbol_cells, available_headroom=headroom,
                             cap_per_cell=cap_per_cell)
        self._tracker.reconcile_to_total(reserved_total=e_total, cells=[c.cell_id for c in symbol_cells], cap_per_cell=cap_per_cell)
        # ... existing per-cell submit loop, scoped to symbol_cells ...
```
`from bfx_funding_bot.modules.marketfeed.config import configured_symbols`. `daemon.py:987` reconciler construction gains the four map kwargs (same `hg.allocation_cap.*`/`hg.buying_power.*` + env fallbacks).

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd backend_py && uv run pytest tests/modules/execution/deployment/test_reconciler.py -v`
Expected: PASS — new 2-symbol test AND the ~30 existing single-symbol tests stay green (single-active-symbol path byte-identical: one loop iteration == old behaviour).

- [ ] **Step 5: Commit**

```bash
git add backend_py/src/bfx_funding_bot/modules/execution/deployment/reconciler.py backend_py/src/bfx_funding_bot/modules/marketfeed/daemon.py backend_py/tests/modules/execution/deployment/test_reconciler.py
git commit -m "🐛 Fix: reconciler sizing per-symbol caps[symbol]/buffers[symbol] (close global-cap blind spot, cap=0 skip)"
```

### Task 9: Tracker per-symbol rescale partition

**Files:**
- Modify: `src/bfx_funding_bot/modules/execution/deployment/tracker.py:30-74`
- Test: `tests/modules/execution/deployment/test_tracker.py`

- [ ] **Step 1: Write the failing test** (mirror `test_reconcile_to_total_scales_proportionally:24`)

```python
def test_rescale_is_independent_per_symbol():
    t = CellDeploymentTracker()
    t.record_deploy("fUST_a30", D("100")); t.record_deploy("fUST_p2", D("100"))
    t.record_deploy("fUSD_a30", D("100")); t.record_deploy("fUSD_p2", D("100"))
    t.reconcile_to_total(D("50"), cells=["fUST_a30", "fUST_p2"])  # only fUST sub-pool → 25 each
    snap = t.snapshot()
    assert snap["fUST_a30"] == D("25") and snap["fUST_p2"] == D("25")
    assert snap["fUSD_a30"] == D("100") and snap["fUSD_p2"] == D("100")  # untouched
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend_py && uv run pytest tests/modules/execution/deployment/test_tracker.py::test_rescale_is_independent_per_symbol -v`
Expected: FAIL — `reconcile_to_total() got unexpected keyword 'cells'`.

- [ ] **Step 3: Implement** (option b — cell subset, back-compat `cells=None`)

```python
def reconcile_to_total(self, reserved_total: Decimal, *, cells: list[str] | None = None,
                       cap_per_cell: Decimal | None = None) -> None:
    keys = cells if cells is not None else list(self._deployed.keys())
    s = sum((self._deployed.get(c, Decimal("0")) for c in keys), Decimal("0"))
    if s <= 0:
        return
    factor = reserved_total / s
    for c in keys:
        rescaled = self._deployed.get(c, Decimal("0")) * factor
        if cap_per_cell is not None:
            rescaled = min(rescaled, cap_per_cell)
        self._deployed[c] = rescaled
```
(`cells=None` preserves the existing all-cells single-symbol behaviour exactly.)

- [ ] **Step 4: Run tests** → `cd backend_py && uv run pytest tests/modules/execution/deployment/test_tracker.py -v` → PASS (new + existing).

- [ ] **Step 5: Commit**

```bash
git add backend_py/src/bfx_funding_bot/modules/execution/deployment/tracker.py backend_py/tests/modules/execution/deployment/test_tracker.py
git commit -m "🐛 Fix: tracker reconcile_to_total partitions rescale by symbol's cell subset (no cross-currency contamination)"
```

---

## SLICE 3 — Correctness prerequisites

### Task 10: Atomic `size_usdt`→`amount` payload rename + fold symbol filter (D1/D3)

**Files:**
- Modify: `src/bfx_funding_bot/modules/execution/event_store/store.py:99-103,150,393-416`, `serialization.py:30`
- Test: `tests/modules/execution/event_store/test_rebuild_from_checkpoint.py` (fold), `test_store_append_unit.py`

> The EVENT field `size_usdt` (events.py) is the rename target → `amount`. DB columns `OfferClaimRow.size_usdt` / `ReconcileObservationRow.reserved_usdt` are NOT renamed — only the data SOURCE switches.

- [ ] **Step 1: Write the failing test** (mirror `_engine_sm()` from `test_position_state_symbol.py`)

```python
async def test_rebuild_folds_two_symbols_into_two_rows():
    engine, sm = await _engine_sm()
    store = PostgresEventStore(deployment_environment="ci")
    async with sm() as session:
        await _append_claimed(session, store, symbol="fUST", amount=D("100"))
        await _append_claimed(session, store, symbol="fUSD", amount=D("40"))
        await store.rebuild_snapshot_from_log(session, account_id="default", symbol="fUST")
        await store.rebuild_snapshot_from_log(session, account_id="default", symbol="fUSD")
        rows = await _load_position_state(session, "default", "ci")
    assert rows["fUST"].reserved == D("100")   # NOT 140 — fold filtered by symbol
    assert rows["fUSD"].reserved == D("40")
```
`_append_claimed` writes an `EventLogRow` whose payload carries `{"symbol": ..., "amount": str(amount), ...}` — copy the payload-crafting from `test_rebuild_from_checkpoint.py` but use `"amount"` not `"size_usdt"`.

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend_py && uv run pytest tests/modules/execution/event_store/test_rebuild_from_checkpoint.py::test_rebuild_folds_two_symbols_into_two_rows -v`
Expected: FAIL — fold reads `payload['size_usdt']` (0 for these rows) and has no symbol filter → both rows fold into one / amounts wrong.

- [ ] **Step 3: Implement** (one atomic change)

`store.py:393-416` tail-fold:
```python
for r in rows:
    if r.event_seq <= fence:
        continue
    if (r.payload or {}).get("symbol") != symbol:
        continue
    size = Decimal(str((r.payload or {}).get("amount", 0) or 0))
```
`store.py:99-103` append projection: `getattr(_ev, "size_usdt", None)` → `getattr(_ev, "amount", None)`. `store.py:150` offer-claims upsert: `_ev.size_usdt` → `_ev.amount` (the `size_usdt=` DB-column KWARG stays). `serialization.py:30`: `_DECIMAL_FIELDS = {"amount"}` (drop `"size_usdt"` — only valid once the event `size_usdt` alias is dropped in Task 11; if keeping the alias this step, leave `{"size_usdt", "amount"}` and finish in Task 11).

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd backend_py && uv run pytest tests/modules/execution/event_store/ -v`
Expected: PASS (fold test + existing store round-trip tests).

- [ ] **Step 5: Commit**

```bash
git add backend_py/src/bfx_funding_bot/modules/execution/event_store/store.py backend_py/src/bfx_funding_bot/modules/execution/event_store/serialization.py backend_py/tests/
git commit -m "🐛 Fix: store fold reads payload['amount'] + filters by symbol (atomic size_usdt→amount, 2-symbol rebuild safe)"
```

### Task 11: Mandatory `symbol` on events + `DecisionPayload` (D2 write side)

**Files:**
- Modify: `src/bfx_funding_bot/modules/execution/events.py:119-249` (4 events), `marketfeed/schemas.py:114`, `event_store/store.py:45,210,258,328`
- Test: `tests/modules/execution/test_events.py` (DELETE/invert the default tests), all builders that omit `symbol=`

- [ ] **Step 1: Invert the failing tests**

DELETE (or convert to mandatory-symbol assertions): `test_events.py::test_reservation_claimed_symbol_defaults_to_fusd`, `test_position_reconciled_symbol_defaults_to_fusd`. Add:
```python
def test_reservation_claimed_requires_symbol():
    with pytest.raises(TypeError):  # frozen dataclass: symbol now positional-required
        ReservationClaimed(cid=1, venue_offer_id="x", amount=Decimal("10"))  # no symbol
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend_py && uv run pytest tests/modules/execution/test_events.py::test_reservation_claimed_requires_symbol -v`
Expected: FAIL — symbol still defaults to `"fUSD"`, no `TypeError`.

- [ ] **Step 3: Implement**

Each of `ReservationClaimed`/`OrderFilled`/`ReservationReleased`/`PositionReconciled`: move `symbol: str` (no default) ABOVE the first defaulted field (frozen+slots raises "non-default follows default" otherwise):
```python
@dataclass(frozen=True, slots=True)
class ReservationClaimed:
    symbol: str          # mandatory, FIRST
    cid: int
    venue_offer_id: str
    amount: Decimal | None = None
    size_usdt: Decimal | None = None  # transitional alias (drop when producers stop sending it)
    ...
```
`schemas.py:114`: `symbol: str = "fUSD"` → `symbol: str` (pydantic required). `store.py`: remove `DEFAULT_RECONCILE_SYMBOL = "fUST"` (`:45`) and the `symbol: str = DEFAULT_RECONCILE_SYMBOL` kwarg defaults at `:210/:258/:328` → `symbol: str`. Then fix EVERY caller/test builder that omits `symbol=` (producers `ws_dispatcher.py:92/123`, `fill_tracker.py`, `reservation_emitting.py` already pass it; the integration `test_pg_event_store.py:177` `rebuild_snapshot_from_log` call must add `symbol=`; all DecisionPayload test builders across slices 1-2 add `symbol="fUST"`).

- [ ] **Step 4: Run the full gate**

Run: `cd backend_py && uv run pytest -m "not integration" && uv run mypy src/ && uv run ruff check`
Expected: PASS (this is the slice where omitted-symbol builders surface — fix each until green).

- [ ] **Step 5: Commit**

```bash
git add backend_py/src/ backend_py/tests/
git commit -m "🐛 Fix: symbol mandatory on events + DecisionPayload + store (remove fUSD/fUST divergent defaults)"
```

### Task 12: Remove ledger `symbol=None` cross-symbol-sum backdoor (D2 read side)

**Files:**
- Modify: `src/bfx_funding_bot/modules/execution/ledger.py:158,169,184,194`
- Test: `tests/modules/execution/test_ledger_per_symbol.py`

- [ ] **Step 1: Invert the failing test**

DELETE/convert `test_no_arg_getters_return_cross_symbol_sum` (`:135-143`) which ENCODES the backdoor. Add:
```python
def test_getters_require_symbol():
    ledger = PaperPositionLedger()   # match the real constructor
    with pytest.raises(TypeError):
        ledger.current_exposure()    # type: ignore[call-arg]

def test_total_helper_is_explicit_non_hot_path():
    ledger = PaperPositionLedger()
    ledger.on_reservation_claimed(_claimed(symbol="fUST", amount=D("10")))
    ledger.on_reservation_claimed(_claimed(symbol="fUSD", amount=D("5")))
    assert ledger.total_exposure_all_symbols() == D("15")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend_py && uv run pytest tests/modules/execution/test_ledger_per_symbol.py -k "require_symbol or total_helper" -v`
Expected: FAIL — `current_exposure()` returns the sum (no TypeError); `total_exposure_all_symbols` undefined.

- [ ] **Step 3: Implement**

For `current_exposure`/`reserved_exposure`/`realized_exposure`/`available_balance` (`:158/169/184/194`): drop the `symbol: str | None = None` default + the `if symbol is None: return sum(...)` branch → `def current_exposure(self, symbol: str) -> Decimal: return self._reserved.get(symbol, Decimal("0")) + self._realized.get(symbol, Decimal("0"))`. Add ONE explicit non-hot-path helper that no guard/reconciler calls:
```python
def total_exposure_all_symbols(self) -> Decimal:
    return sum(self._reserved.values(), Decimal("0")) + sum(self._realized.values(), Decimal("0"))
```
(Prod callers `hard_guards.py:136/181`, `reconciler.py:102/106/114` already pass `symbol` — verify no other call omits it.)

- [ ] **Step 4: Run the full gate**

Run: `cd backend_py && uv run pytest -m "not integration" && uv run mypy src/ && uv run ruff check`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add backend_py/src/bfx_funding_bot/modules/execution/ledger.py backend_py/tests/modules/execution/test_ledger_per_symbol.py
git commit -m "🐛 Fix: ledger read getters require symbol; cross-symbol sum moved to explicit non-hot-path helper"
```

---

## SLICE 4 — Cutover (apply migration; write-gated runbook)

### Task 13: Migration apply test (integration-marked) + alembic check

**Files:**
- Test: `tests/modules/execution/event_store/test_migration_per_symbol_pk.py` (NEW, `@pytest.mark.integration`)

> `b7c1d2e3f4a5` is ALREADY authored (Phase 1) and is the current alembic head. D4 = APPLY only. sqlite unit tests are migration-agnostic (`Base.metadata.create_all` already reflects the per-symbol ORM); the PK DROP/CREATE needs real Postgres.

- [ ] **Step 1: Write the integration test** (copy scaffolding from `test_alembic_migration_lock.py`)

```python
import pytest
pytestmark = pytest.mark.integration

async def test_position_state_per_symbol_pk_after_upgrade(pg_engine, monkeypatch):
    sync_url = pg_engine.url.render_as_string(hide_password=False).replace("+asyncpg", "+psycopg")
    monkeypatch.setenv("DATABASE_URL", sync_url)
    from alembic import command
    from alembic.config import Config
    cfg = Config("alembic.ini")
    command.upgrade(cfg, "head")
    # assert composite PK (account_id, deployment_environment, symbol) on position_state
    eng = create_engine(sync_url)
    with eng.connect() as conn:
        pk = inspect(conn).get_pk_constraint("position_state")["constrained_columns"]
    assert set(pk) == {"account_id", "deployment_environment", "symbol"}
```

- [ ] **Step 2: Run it (needs Postgres)**

Run: `cd backend_py && uv run pytest tests/modules/execution/event_store/test_migration_per_symbol_pk.py -m integration -v`
Expected: PASS against the test Postgres container (or SKIP if no container — note it in the PR).

- [ ] **Step 3: alembic check (no-drift) after a Neon-cred refresh**

Refresh `.env` via Neon MCP `get_connection_string` first (the symlink `backend_py/.env` → repo `.env`). Then:
Run: `cd backend_py && uv run alembic check`
Expected: "No new upgrade operations detected" (metadata == head).

- [ ] **Step 4: Commit**

```bash
git add backend_py/tests/modules/execution/event_store/test_migration_per_symbol_pk.py
git commit -m "✅ Test: integration test asserts position_state per-symbol PK after alembic upgrade"
```

### Task 14: Live canary cutover runbook (ops — execute with operator)

> NOT a unit test — a write-gated operational checklist. Execute on the Oracle VM (`ssh -i ~/.ssh/id_ed25519_will413028 ubuntu@150.230.105.94`). fUST cap unchanged at 3000; fUSD/fADA stay `cap=0` (dark).

- [ ] **Step 1:** Merge Slices 1-3 to main; full gate green (`pytest -m "not integration"` + mypy + ruff).
- [ ] **Step 2:** Refresh the stale Neon credential in `.env` (Neon MCP `get_connection_string`) — known landmine before any alembic command.
- [ ] **Step 3:** Deploy WRITE-GATED: set `BFX_KILL_SWITCH=true` in the VM `bot.env` (reconciler places no offers on first boot), then `./scripts/deploy-vm.sh canary` (builds the new sha, runs the migrate one-shot `alembic upgrade head` transactionally).
- [ ] **Step 4:** Verify on the gated process (logs): `position_state` rebuilt the live fUST credit row (~273 fUST); the boot `effective_cap_per_symbol` log shows `fUST=3000` (map won over env `BFX_ALLOCATION_CAP_USDT`); no `InvariantViolation` from the executor; `/healthz` 200; `caps_invariant` passed. Confirm the `safety.canary.yaml` caps/buffers maps loaded.
- [ ] **Step 5:** Release writes: set `BFX_KILL_SWITCH=false`, recreate the bot container. Confirm fUST offers resume at cap 3000, fUSD/fADA produce zero offers (cap=0, dark), and the %-of-NAV loss limiter (`0b90763`) is unperturbed. Rollback path if anything mis-routes: `BFX_KILL_SWITCH=true` + recreate; DB via Neon PITR; Koyeb stays paused.

---

## Self-Review (completed)

**Spec coverage:** A1/A2→Tasks 2-3; B1→Task 4; boot assert (§3)→Task 5; B2→Tasks 6-7; C1→Task 8; C2→Task 9; D1/D3→Task 10; D2 write→Task 11; D2 read→Task 12; D4+§7→Tasks 13-14. NAV split / reconcile_observation / offer_claims / fADA cell → spec §8 deferred (NOT in this plan, hard-gated before a 2nd currency trades).

**Type consistency:** `configured_symbols(cells)` (config.py) used by Tasks 1/3/5/8; `caps: dict[str,Decimal]`/`default_cap`/`buffers: dict[str,Decimal]`/`default_buffer` consistent across Tasks 4/6/7/8; `GuardResult(allowed,guard_name,reason)` verdict (never raise) in Tasks 6/7; executor fail-fast is the only `InvariantViolation` (Task 2); `symbol` mandatory contract (Task 11) means every builder added in Tasks 2/6/7/8/10/12 must pass `symbol=` — Task 11's gate run is where omissions surface.

**Deferred safety:** fUSD/fADA at `cap=0` are blocked by AllocationCapGuard AND skipped by the reconciler AND (if ever a stray offer) rejected by the executor fail-fast — triple defense; no NAV-split needed until a 2nd currency actually trades.
