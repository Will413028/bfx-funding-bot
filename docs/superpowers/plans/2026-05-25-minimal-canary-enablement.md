# Minimal Canary Enablement Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `BFX_PHASE=canary` a valid real-money phase that refuses to start unless the full operational + loss-limit guard set is enabled.

**Architecture:** Two small changes. (1) `load_config` accepts `canary` alongside paper/shadow. (2) A pure module-level invariant `assert_canary_guard_invariant(phase, safety_cfg)` in `daemon.py`, called inside `build_daemon` before guard construction, raises a config-fatal `ValueError` when `phase==CANARY` and any required guard is disabled. Phase and executor stay orthogonal — no execution-path change.

**Tech Stack:** Python 3.13, pytest (`asyncio_mode = "auto"`), Pydantic v2. No new dependencies.

**Spec:** `docs/superpowers/specs/2026-05-25-minimal-canary-enablement-design.md`

**Working dir for all commands:** `backend_py/` (`cd backend_py` first — see repo CLAUDE.md).

---

## File Structure

- **Modify** `src/bfx_funding_bot/modules/marketfeed/config.py` — `load_config` accepts `canary`; update `phase` field description.
- **Modify** `tests/modules/marketfeed/test_config.py` — replace the canary-rejection test with an acceptance test; add an unknown-phase rejection test.
- **Modify** `src/bfx_funding_bot/modules/marketfeed/daemon.py` — add `Phase` + `SafetyConfig` imports, the `assert_canary_guard_invariant` function, and its call in `build_daemon`.
- **Create** `tests/modules/marketfeed/test_canary_invariant.py` — unit tests for the pure invariant.

---

## Task 1: Allow `canary` phase in `load_config`

**Files:**
- Modify: `src/bfx_funding_bot/modules/marketfeed/config.py` (lines 88, 110-113)
- Test: `tests/modules/marketfeed/test_config.py` (replace lines 58-65)

- [ ] **Step 1: Update the tests (write the new behavior first)**

In `tests/modules/marketfeed/test_config.py`, replace the existing
`test_load_config_rejects_canary_phase` function (the whole function, currently):

```python
def test_load_config_rejects_canary_phase(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("BFX_PHASE", "canary")
    monkeypatch.setenv("DATABASE_URL", "x")
    monkeypatch.setenv("BFX_DEPLOYMENT_ENV", "ci")
    yaml_path = _write_yaml(tmp_path, _valid_yaml())

    with pytest.raises(ValueError, match="canary"):
        load_config(cells_yaml_path=yaml_path)
```

with:

```python
def test_load_config_accepts_canary_phase(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("BFX_PHASE", "canary")
    monkeypatch.setenv("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
    monkeypatch.setenv("BFX_DEPLOYMENT_ENV", "ci")
    monkeypatch.delenv("BFX_CELLS", raising=False)
    monkeypatch.delenv("BFX_RUN_DURATION_HOURS", raising=False)
    yaml_path = _write_yaml(tmp_path, _valid_yaml())

    cfg = load_config(cells_yaml_path=yaml_path)

    assert cfg.phase == "canary"


def test_load_config_rejects_unknown_phase(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("BFX_PHASE", "bogus")
    monkeypatch.setenv("DATABASE_URL", "x")
    monkeypatch.setenv("BFX_DEPLOYMENT_ENV", "ci")
    yaml_path = _write_yaml(tmp_path, _valid_yaml())

    with pytest.raises(ValueError, match="must be paper"):
        load_config(cells_yaml_path=yaml_path)
```

- [ ] **Step 2: Run tests to verify the acceptance test fails**

Run: `cd backend_py && uv run pytest tests/modules/marketfeed/test_config.py -q`
Expected: `test_load_config_accepts_canary_phase` FAILS — `load_config` currently
raises `ValueError("BFX_PHASE=canary not allowed in 4.1 daemon ...")`.
(`test_load_config_rejects_unknown_phase` already passes — unknown is rejected by the existing branch; it is a regression guard.)

- [ ] **Step 3: Allow canary in `config.py`**

In `src/bfx_funding_bot/modules/marketfeed/config.py`, replace (lines 110-113):

```python
    if phase_str == "canary":
        raise ValueError("BFX_PHASE=canary not allowed in 4.1 daemon -- canary is 4.4 sub-spec")
    if phase_str not in {"paper", "shadow"}:
        raise ValueError(f"BFX_PHASE must be paper or shadow, got {phase_str!r}")
```

with:

```python
    if phase_str not in {"paper", "shadow", "canary"}:
        raise ValueError(f"BFX_PHASE must be paper, shadow, or canary, got {phase_str!r}")
```

And update the `phase` field description (line 88):

```python
    phase: Annotated[Phase, Field(description="paper / shadow only -- canary rejected by daemon")]
```

to:

```python
    phase: Annotated[Phase, Field(description="paper / shadow / canary")]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd backend_py && uv run pytest tests/modules/marketfeed/test_config.py -q`
Expected: PASS (acceptance + unknown-rejection + existing happy/shadow tests).

- [ ] **Step 5: Type-check + lint**

Run: `cd backend_py && uv run mypy src/ && uv run ruff check`
Expected: clean.

- [ ] **Step 6: Commit**

```bash
cd backend_py && git add src/bfx_funding_bot/modules/marketfeed/config.py tests/modules/marketfeed/test_config.py
git commit -m "✨ Feat: allow BFX_PHASE=canary in load_config

canary is now a valid phase alongside paper/shadow (unknown values still
rejected). The real-money safety gate is enforced separately by the canary
guard invariant (next task), not by blocking the phase outright.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 2: Canary all-guards invariant

**Files:**
- Modify: `src/bfx_funding_bot/modules/marketfeed/daemon.py` (imports at lines 85 + 107-112; new function before `async def build_daemon` at line 582; call before `hg = safety_cfg.hard_guards` ~line 702)
- Test: `tests/modules/marketfeed/test_canary_invariant.py` (new)

- [ ] **Step 1: Write the failing test**

Create `tests/modules/marketfeed/test_canary_invariant.py`:

```python
"""Canary phase requires the full operational + loss-limit guard set enabled."""
from __future__ import annotations

from pathlib import Path

import pytest

from bfx_funding_bot.modules.execution.safety.config import load_safety_config
from bfx_funding_bot.modules.marketfeed.daemon import assert_canary_guard_invariant
from bfx_funding_bot.modules.marketfeed.schemas import Phase


def _safety_yaml(*, disable: str | None = None) -> str:
    on = dict.fromkeys(
        ("manual_kill", "auth_health", "heartbeat",
         "allocation_cap", "realized_loss_24h", "drawdown_from_peak"),
        True,
    )
    if disable is not None:
        on[disable] = False

    def b(name: str) -> str:
        return "true" if on[name] else "false"

    return f"""
hard_guards:
  manual_kill:
    enabled: {b("manual_kill")}
  auth_health:
    enabled: {b("auth_health")}
  heartbeat:
    enabled: {b("heartbeat")}
    sub_task_stale_threshold_seconds: 300
  allocation_cap:
    enabled: {b("allocation_cap")}
calibrated_guards:
  realized_loss_24h:
    enabled: {b("realized_loss_24h")}
    threshold_usdt: {15 if on["realized_loss_24h"] else "null"}
  drawdown_from_peak:
    enabled: {b("drawdown_from_peak")}
    threshold_pct: {15 if on["drawdown_from_peak"] else "null"}
  divergence_rate:
    enabled: false
    threshold_pct: null
    window_minutes: null
"""


def _load(tmp_path: Path, disable: str | None = None):
    p = tmp_path / "safety.yaml"
    p.write_text(_safety_yaml(disable=disable))
    return load_safety_config(p)


def test_canary_ok_when_all_required_enabled(tmp_path: Path) -> None:
    cfg = _load(tmp_path)
    assert_canary_guard_invariant(Phase.CANARY, cfg)  # must not raise


@pytest.mark.parametrize("guard", [
    "manual_kill", "auth_health", "heartbeat",
    "allocation_cap", "realized_loss_24h", "drawdown_from_peak",
])
def test_canary_raises_when_required_guard_disabled(tmp_path: Path, guard: str) -> None:
    cfg = _load(tmp_path, disable=guard)
    with pytest.raises(ValueError, match=guard):
        assert_canary_guard_invariant(Phase.CANARY, cfg)


def test_shadow_allows_disabled_guard(tmp_path: Path) -> None:
    cfg = _load(tmp_path, disable="allocation_cap")
    assert_canary_guard_invariant(Phase.SHADOW, cfg)  # invariant is canary-only
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend_py && uv run pytest tests/modules/marketfeed/test_canary_invariant.py -q`
Expected: FAIL at import — `ImportError: cannot import name 'assert_canary_guard_invariant' from ...daemon`.

- [ ] **Step 3: Add imports to `daemon.py`**

Replace (line 85):

```python
from bfx_funding_bot.modules.execution.safety.config import load_safety_config
```

with:

```python
from bfx_funding_bot.modules.execution.safety.config import SafetyConfig, load_safety_config
```

Replace the schemas import block (lines 107-112):

```python
from bfx_funding_bot.modules.marketfeed.schemas import (
    EventType,
    HealthStatus,
    HealthTarget,
    Level,
)
```

with:

```python
from bfx_funding_bot.modules.marketfeed.schemas import (
    EventType,
    HealthStatus,
    HealthTarget,
    Level,
    Phase,
)
```

- [ ] **Step 4: Add the invariant function**

In `src/bfx_funding_bot/modules/marketfeed/daemon.py`, immediately before the line
`async def build_daemon(` (line 582), insert:

```python
_CANARY_REQUIRED_HARD = ("manual_kill", "auth_health", "heartbeat", "allocation_cap")
_CANARY_REQUIRED_CALIBRATED = ("realized_loss_24h", "drawdown_from_peak")


def assert_canary_guard_invariant(phase: Phase, safety_cfg: SafetyConfig) -> None:
    """Canary (real money) must not run with a safety guard silently off.

    Requires the 4 hard guards + the 2 loss-limiters enabled; raises a
    config-fatal ValueError (propagates to non-zero startup exit) otherwise.
    No-op for paper/shadow. divergence_rate stays optional.
    """
    if phase != Phase.CANARY:
        return
    missing = [
        name for name in _CANARY_REQUIRED_HARD
        if not getattr(safety_cfg.hard_guards, name).enabled
    ]
    missing += [
        name for name in _CANARY_REQUIRED_CALIBRATED
        if not getattr(safety_cfg.calibrated_guards, name).enabled
    ]
    if missing:
        raise ValueError(
            f"BFX_PHASE=canary requires all safety guards enabled; disabled: {missing}"
        )


```

- [ ] **Step 5: Call the invariant in `build_daemon`**

In `build_daemon`, replace (line ~702):

```python
    hg = safety_cfg.hard_guards
```

with:

```python
    # Canary (real money) must not boot with a safety guard silently off.
    assert_canary_guard_invariant(config.phase, safety_cfg)
    hg = safety_cfg.hard_guards
```

- [ ] **Step 6: Run the test to verify it passes**

Run: `cd backend_py && uv run pytest tests/modules/marketfeed/test_canary_invariant.py -v`
Expected: PASS — `test_canary_ok_when_all_required_enabled`, 6 parametrized
`test_canary_raises_when_required_guard_disabled`, `test_shadow_allows_disabled_guard`.

- [ ] **Step 7: Type-check + lint**

Run: `cd backend_py && uv run mypy src/ && uv run ruff check`
Expected: clean.

- [ ] **Step 8: Commit**

```bash
cd backend_py && git add src/bfx_funding_bot/modules/marketfeed/daemon.py tests/modules/marketfeed/test_canary_invariant.py
git commit -m "✨ Feat: enforce canary all-guards invariant at daemon build

phase==canary requires the 4 hard guards + realized_loss_24h + drawdown_from_peak
enabled, else config-fatal ValueError (refuse to start). Prevents real money from
running with a safety guard silently disabled. paper/shadow unaffected.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Final verification

- [ ] **Run the full default gate**

Run: `cd backend_py && uv run pytest -m "not integration" -q`
Expected: all pass (previous 634 + new canary tests), 0 failed.

- [ ] **Confirm mypy + ruff clean**

Run: `cd backend_py && uv run mypy src/ && uv run ruff check`
Expected: clean.

---

## Out of scope (per spec — do NOT implement here)

- G3 P&L tracking-error auto-halt / real `PnLLedger` aggregation.
- Capital ramp automation; `divergence_rate` as a mandatory canary gate.
- Operational config (env vars, cells.yaml, Koyeb secrets) — these are deploy-time
  settings, not code; see the spec's "Operational canary profile" section.
