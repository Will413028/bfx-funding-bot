# Periodic Reconcile Backbone Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make periodic REST snapshot reconcile the runtime correctness backbone so the live ledger always converges to venue truth, ending the stuck-at-cap failure even when the WS stream is dead.

**Architecture:** Reuse the existing pure reconcile (`compute_recovery_actions`) and `BootRecovery` fetch/persist/publish machinery, but run them on an interval as a daemon sub-task. Add a grace window so runtime reconcile never races an in-flight placement, return a result so divergence is observable, and a fail-safe that blocks new offers when the venue is unreachable.

**Tech Stack:** Python 3.13, asyncio TaskGroup, SQLAlchemy 2.0 async, pytest (`-m "not integration"` commit gate), pydantic config.

**Scope note:** This is Plan 1 of 2 from `docs/superpowers/specs/2026-05-27-live-venue-reconcile-backbone-design.md`. Plan 2 (WS `foc` parser fix + real-fixture rebuild + dispatcher `EXECUTED→OrderFilled` + WS-reconnect/seq-gap-triggered reconcile) is a separate, later plan — the WS path is a latency optimization and is not required for correctness. This plan ships and deploys independently and self-unsticks the currently-idle canary capital.

**Single-symbol constraint (intentional, current canary):** `BootRecovery` loads *all* of the account's local claims and compares them against venue offers for *one* `symbol`. This is correct only while every claim is that symbol (canary is fUST-only). Multi-symbol reconcile is deferred until per-currency allocation lands; this plan keeps the single-symbol reconcile (`first_cell.symbol`).

---

## File Structure

- **Modify** `backend_py/src/bfx_funding_bot/modules/execution/boot_recovery.py`
  — add `action_grace_ms` to `compute_recovery_actions` (gate orphan + missing); thread it through `BootRecovery`; make `BootRecovery.run()` return a `ReconcileResult`.
- **Create** `backend_py/src/bfx_funding_bot/modules/execution/periodic_reconcile.py`
  — `PeriodicReconcile` sub-task: interval loop around a `BootRecovery`, divergence logging, venue-unreachable fail-safe.
- **Modify** `backend_py/src/bfx_funding_bot/modules/marketfeed/daemon.py`
  — `Daemon` field, construction in the live-only block, TaskGroup sub-task, `BFX_RECONCILE_INTERVAL_S` env.
- **Modify** `backend_py/tests/modules/execution/test_boot_recovery.py` — grace cases.
- **Create** `backend_py/tests/modules/execution/test_periodic_reconcile.py` — loop / divergence / fail-safe unit tests.
- **Create** `backend_py/tests/integration/test_reconcile_converges_without_ws.py` — reproduces the incident.

All commands run from `backend_py/`. Commit gate: `cd backend_py && uv run pytest -m "not integration" && uv run mypy src/ && uv run ruff check`.

---

### Task 1: Add `action_grace_ms` grace window to `compute_recovery_actions`

Gate the `orphan→claim` and `missing→release` directions on age, so periodic runtime reconcile cannot act on an offer/claim that is still mid-placement. Default `0` preserves boot behaviour (immediate full reconcile).

**Files:**
- Modify: `backend_py/src/bfx_funding_bot/modules/execution/boot_recovery.py:87-133`
- Test: `backend_py/tests/modules/execution/test_boot_recovery.py`

- [ ] **Step 1: Write the failing tests**

Add to `tests/modules/execution/test_boot_recovery.py` (reuses existing `_offer`, `_claim`, `_actions`, `_ACC`, `_NOW`, `RegistryState`):

```python
def test_action_grace_skips_recent_orphan():
    # offer created 50s before now; action_grace_ms=120s → too fresh to claim
    offer = _offer(voi="555", amount="100")
    offer = ActiveFundingOffer(
        venue_offer_id="555", symbol="fUSD", amount=Decimal("100"),
        rate=0.0003, period_days=2, mts_created=_NOW - 50_000, status="ACTIVE",
    )
    acts = compute_recovery_actions(
        venue_offers=[offer], local_claims=[], account_id=_ACC,
        is_simulated=False, now_ms=_NOW, grace_ms=120_000, action_grace_ms=120_000,
    )
    assert acts == []


def test_action_grace_skips_recent_missing_claim():
    # local CLAIMED occurred 50s before now, venue empty; action_grace too fresh
    claim = _claim(cid=1, voi="555", state=RegistryState.CLAIMED, occurred=_NOW - 50_000)
    acts = compute_recovery_actions(
        venue_offers=[], local_claims=[claim], account_id=_ACC,
        is_simulated=False, now_ms=_NOW, grace_ms=120_000, action_grace_ms=120_000,
    )
    assert acts == []


def test_action_grace_releases_stale_missing_claim():
    # claim occurred 5 min ago, venue empty → past grace → released
    claim = _claim(cid=1, voi="555", state=RegistryState.CLAIMED, occurred=_NOW - 300_000)
    acts = compute_recovery_actions(
        venue_offers=[], local_claims=[claim], account_id=_ACC,
        is_simulated=False, now_ms=_NOW, grace_ms=120_000, action_grace_ms=120_000,
    )
    assert len(acts) == 1
    assert isinstance(acts[0], ReservationReleased)
    assert acts[0].venue_offer_id == "555"


def test_action_grace_zero_preserves_boot_behaviour():
    # default action_grace_ms=0 → recent offer still claimed (boot path unchanged)
    offer = ActiveFundingOffer(
        venue_offer_id="555", symbol="fUSD", amount=Decimal("100"),
        rate=0.0003, period_days=2, mts_created=_NOW - 1, status="ACTIVE",
    )
    acts = compute_recovery_actions(
        venue_offers=[offer], local_claims=[], account_id=_ACC,
        is_simulated=False, now_ms=_NOW, grace_ms=120_000,
    )
    assert len(acts) == 1 and isinstance(acts[0], ReservationClaimed)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd backend_py && uv run pytest tests/modules/execution/test_boot_recovery.py -k action_grace -v`
Expected: FAIL — `compute_recovery_actions() got an unexpected keyword argument 'action_grace_ms'`.

- [ ] **Step 3: Implement the grace gating**

In `boot_recovery.py`, change the `compute_recovery_actions` signature and the two loops:

```python
def compute_recovery_actions(
    *,
    venue_offers: list[ActiveFundingOffer],
    local_claims: list[LocalClaim],
    account_id: str,
    is_simulated: bool,
    now_ms: int,
    grace_ms: int,
    action_grace_ms: int = 0,
) -> list[RecoveryAction]:
    """Pure reconciliation: produce the ordered list of domain events to append.

    action_grace_ms gates the orphan/missing directions so a runtime reconcile
    never acts on an offer/claim still mid-placement (boot passes 0 = immediate).
    """
    venue_by_voi = {o.venue_offer_id: o for o in venue_offers}
    claimed_by_voi = {
        c.venue_offer_id: c
        for c in local_claims
        if c.state == RegistryState.CLAIMED and c.venue_offer_id is not None
    }
    actions: list[RecoveryAction] = []

    # orphan: venue has it, local CLAIMED set doesn't -> claim (reserved += size)
    for voi, offer in venue_by_voi.items():
        if voi in claimed_by_voi:
            continue
        if (now_ms - offer.mts_created) < action_grace_ms:
            continue  # too fresh — local claim may still be committing
        actions.append(ReservationClaimed(
            cid=synth_orphan_cid(voi), venue_offer_id=voi,
            size_usdt=offer.amount, signal_correlation_id=synth_orphan_scid(voi),
            account_id=account_id, is_simulated=is_simulated, occurred_at_ms=now_ms,
        ))

    # missing: local CLAIMED, venue gone -> release (reserved -= size)
    for voi, claim in claimed_by_voi.items():
        if voi in venue_by_voi:
            continue
        if (now_ms - claim.occurred_at_ms) < action_grace_ms:
            continue  # too fresh — venue snapshot may lag the just-placed offer
        actions.append(ReservationReleased(
            cid=claim.cid, venue_offer_id=voi, size_usdt=claim.size_usdt,
            reason="missing_from_venue", signal_correlation_id=claim.signal_correlation_id,
            account_id=account_id, is_simulated=is_simulated, occurred_at_ms=now_ms,
        ))

    # stale PENDING (crash-mid-flight, unmatchable) -> FAILED (capital-neutral)
    for c in local_claims:
        if c.state == RegistryState.PENDING and (now_ms - c.occurred_at_ms) >= grace_ms:
            actions.append(ReservationFailed(
                cid=c.cid, size_usdt=c.size_usdt,
                signal_correlation_id=c.signal_correlation_id,
                account_id=account_id, is_simulated=is_simulated,
                reason="unresolved_at_boot", occurred_at_ms=now_ms,
            ))

    return actions
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd backend_py && uv run pytest tests/modules/execution/test_boot_recovery.py -v`
Expected: PASS (new grace tests + all existing boot_recovery tests, which use `action_grace_ms` default 0).

- [ ] **Step 5: Commit**

```bash
git add backend_py/src/bfx_funding_bot/modules/execution/boot_recovery.py backend_py/tests/modules/execution/test_boot_recovery.py
git commit -m "♻️ Refactor: add action_grace_ms to compute_recovery_actions (runtime-safe reconcile)"
```

---

### Task 2: Make `BootRecovery.run()` return a `ReconcileResult` and accept `action_grace_ms`

So a caller (the periodic loop) can detect divergence and configure the grace.

**Files:**
- Modify: `backend_py/src/bfx_funding_bot/modules/execution/boot_recovery.py:152-217`
- Test: `backend_py/tests/modules/execution/test_boot_recovery.py`

- [ ] **Step 1: Write the failing test**

Add to `test_boot_recovery.py`. This needs the existing async harness — match the existing `BootRecovery.run()` integration test in this file for fakes (stubbed `auth_rest`, in-memory `store`, `session_factory`, `bus`). If the file already has a `BootRecovery.run()` test, copy its fixtures; otherwise add this minimal one using the existing stubs:

```python
@pytest.mark.asyncio
async def test_run_returns_result_with_release_count(boot_recovery_harness):
    # harness seeds one local CLAIMED voi=555 and an empty venue snapshot
    h = boot_recovery_harness(local_claimed=["555"], venue_offers=[])
    result = await h.recovery.run()
    assert result.n_released == 1
    assert result.n_claimed == 0
    assert result.n_failed == 0
```

If no reusable harness exists, instead assert against the return type using the file's existing `BootRecovery.run()` test by capturing its return value: change that test to `result = await recovery.run()` and add `assert result.n_released == <expected>`. Use whichever matches the file.

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend_py && uv run pytest tests/modules/execution/test_boot_recovery.py -k returns_result -v`
Expected: FAIL — `run()` returns `None` / `AttributeError: 'NoneType' object has no attribute 'n_released'`.

- [ ] **Step 3: Implement `ReconcileResult` + return + `action_grace_ms`**

In `boot_recovery.py`, add the dataclass near `RecoveryAction` (after line 47):

```python
@dataclass(frozen=True, slots=True)
class ReconcileResult:
    n_claimed: int
    n_released: int
    n_failed: int
```

Add `action_grace_ms: int = 0` to `BootRecovery.__init__` params (store as `self._action_grace_ms = action_grace_ms`). Then change `run()` to pass it through and return the result:

```python
    async def run(self) -> ReconcileResult:
        venue_offers = await self._fetch_offers()  # may raise -> caller decides (boot: fail-safe startup; periodic: catch + degrade)
        async with session_scope(self._session_factory) as session:
            local_claims = await self._load_local_claims(session)
            actions = compute_recovery_actions(
                venue_offers=venue_offers, local_claims=local_claims,
                account_id=self._ctx.account_id, is_simulated=self._is_simulated,
                now_ms=self._clock(), grace_ms=self._grace_ms,
                action_grace_ms=self._action_grace_ms,
            )
            for ev in actions:
                await self._store.append(session, ev)
        n_claim = n_release = n_fail = 0
        for ev in actions:
            if isinstance(ev, ReservationClaimed):
                n_claim += 1
                await self._safe_publish(ev)
            elif isinstance(ev, ReservationReleased):
                n_release += 1
                await self._safe_publish(ev)
            elif isinstance(ev, ReservationFailed):
                n_fail += 1
        log.info(
            "reconcile_complete venue_offers=%d orphans_claimed=%d released=%d pending_failed=%d",
            len(venue_offers), n_claim, n_release, n_fail,
        )
        return ReconcileResult(n_claimed=n_claim, n_released=n_release, n_failed=n_fail)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd backend_py && uv run pytest tests/modules/execution/test_boot_recovery.py -v`
Expected: PASS (existing boot tests ignore the new return value; new test asserts counts).

- [ ] **Step 5: Commit**

```bash
git add backend_py/src/bfx_funding_bot/modules/execution/boot_recovery.py backend_py/tests/modules/execution/test_boot_recovery.py
git commit -m "♻️ Refactor: BootRecovery.run returns ReconcileResult + accepts action_grace_ms"
```

---

### Task 3: Create `PeriodicReconcile` sub-task (interval loop + divergence + fail-safe)

**Files:**
- Create: `backend_py/src/bfx_funding_bot/modules/execution/periodic_reconcile.py`
- Test: `backend_py/tests/modules/execution/test_periodic_reconcile.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/modules/execution/test_periodic_reconcile.py`:

```python
import asyncio
from dataclasses import dataclass

import pytest

from bfx_funding_bot.modules.execution.boot_recovery import ReconcileResult
from bfx_funding_bot.modules.execution.periodic_reconcile import PeriodicReconcile
from bfx_funding_bot.modules.marketfeed.schemas import HealthStatus, HealthTarget


class _FakeProbe:
    def __init__(self):
        self.beats: list[str] = []
        self.updates: list[tuple] = []

    def record_heartbeat(self, sub_task: str) -> None:
        self.beats.append(sub_task)

    def update(self, target, status, **fields) -> None:
        self.updates.append((target, status, fields))


@dataclass
class _FakeRecovery:
    results: list  # each item: ReconcileResult OR an Exception to raise
    _i: int = 0

    async def run(self) -> ReconcileResult:
        item = self.results[min(self._i, len(self.results) - 1)]
        self._i += 1
        if isinstance(item, Exception):
            raise item
        return item


@pytest.mark.asyncio
async def test_loop_runs_reconcile_each_interval_and_heartbeats():
    probe = _FakeProbe()
    recovery = _FakeRecovery(results=[ReconcileResult(0, 0, 0)])
    pr = PeriodicReconcile(
        recovery=recovery, probe=probe, interval_s=0.01, max_consecutive_failures=3,
    )
    stop = asyncio.Event()

    async def _stop_soon():
        await asyncio.sleep(0.035)
        stop.set()

    await asyncio.gather(pr.run_loop(stop), _stop_soon())
    assert recovery._i >= 2  # ran multiple times
    assert "periodic_reconcile" in probe.beats


@pytest.mark.asyncio
async def test_divergence_on_periodic_release_sets_degraded():
    probe = _FakeProbe()
    recovery = _FakeRecovery(results=[ReconcileResult(0, 2, 0)])  # 2 releases = drift
    pr = PeriodicReconcile(
        recovery=recovery, probe=probe, interval_s=0.01, max_consecutive_failures=3,
    )
    stop = asyncio.Event()

    async def _stop_soon():
        await asyncio.sleep(0.02)
        stop.set()

    await asyncio.gather(pr.run_loop(stop), _stop_soon())
    assert any(
        t == HealthTarget.BITFINEX_REST and s == HealthStatus.DEGRADED
        for (t, s, _f) in probe.updates
    )


@pytest.mark.asyncio
async def test_consecutive_fetch_failures_trip_executor_down_failsafe():
    probe = _FakeProbe()
    recovery = _FakeRecovery(results=[RuntimeError("venue unreachable")])
    pr = PeriodicReconcile(
        recovery=recovery, probe=probe, interval_s=0.005, max_consecutive_failures=3,
    )
    stop = asyncio.Event()

    async def _stop_soon():
        await asyncio.sleep(0.05)
        stop.set()

    await asyncio.gather(pr.run_loop(stop), _stop_soon())
    assert any(
        t == HealthTarget.EXECUTOR and s == HealthStatus.DOWN
        for (t, s, _f) in probe.updates
    )


@pytest.mark.asyncio
async def test_recovery_after_failure_clears_failsafe():
    probe = _FakeProbe()
    # 3 failures (trip DOWN) then a success (clear)
    recovery = _FakeRecovery(results=[
        RuntimeError("x"), RuntimeError("x"), RuntimeError("x"), ReconcileResult(0, 0, 0),
    ])
    pr = PeriodicReconcile(
        recovery=recovery, probe=probe, interval_s=0.005, max_consecutive_failures=3,
    )
    stop = asyncio.Event()

    async def _stop_soon():
        await asyncio.sleep(0.06)
        stop.set()

    await asyncio.gather(pr.run_loop(stop), _stop_soon())
    # last EXECUTOR update should be HEALTHY (cleared after recovery)
    exec_updates = [(s) for (t, s, _f) in probe.updates if t == HealthTarget.EXECUTOR]
    assert exec_updates and exec_updates[-1] == HealthStatus.HEALTHY
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd backend_py && uv run pytest tests/modules/execution/test_periodic_reconcile.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'bfx_funding_bot.modules.execution.periodic_reconcile'`.

- [ ] **Step 3: Implement `PeriodicReconcile`**

Create `periodic_reconcile.py`:

```python
"""PeriodicReconcile — runtime correctness backbone (spec 2026-05-27).

Runs the venue snapshot reconcile (BootRecovery.run) on an interval. The WS
stream is a latency optimization; this loop is what GUARANTEES the ledger
converges to venue truth, so the bot can never get permanently stuck at the
allocation cap when the stream silently breaks.

- Divergence: a release on a periodic (non-boot) run means the stream missed an
  event → BITFINEX_REST DEGRADED + WARN so the silent breakage surfaces.
- Fail-safe: consecutive venue-fetch failures → EXECUTOR DOWN so AuthHealthGuard
  blocks new offers (never trade on a stale ledger). Cleared on recovery.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Protocol

from bfx_funding_bot.modules.execution.boot_recovery import ReconcileResult
from bfx_funding_bot.modules.marketfeed.schemas import HealthStatus, HealthTarget

log = logging.getLogger(__name__)


class _Recovery(Protocol):
    async def run(self) -> ReconcileResult: ...


class _Probe(Protocol):
    def record_heartbeat(self, sub_task: str) -> None: ...
    def update(self, target: HealthTarget, status: HealthStatus, **fields: object) -> None: ...


class PeriodicReconcile:
    SUB_TASK = "periodic_reconcile"

    def __init__(
        self,
        *,
        recovery: _Recovery,
        probe: _Probe,
        interval_s: float,
        max_consecutive_failures: int = 3,
    ) -> None:
        self._recovery = recovery
        self._probe = probe
        self._interval_s = interval_s
        self._max_failures = max_consecutive_failures
        self._consecutive_failures = 0
        self._tripped_down = False  # this loop owns the EXECUTOR DOWN it sets

    async def run_loop(self, stop_event: asyncio.Event) -> None:
        while not stop_event.is_set():
            await self._tick()
            self._probe.record_heartbeat(self.SUB_TASK)
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=self._interval_s)
            except TimeoutError:
                continue

    async def _tick(self) -> None:
        try:
            result = await self._recovery.run()
        except Exception as exc:  # noqa: BLE001 — never let the backbone crash the daemon
            self._consecutive_failures += 1
            log.warning(
                "periodic_reconcile_failed attempt=%d/%d err=%r",
                self._consecutive_failures, self._max_failures, exc,
            )
            if self._consecutive_failures >= self._max_failures and not self._tripped_down:
                self._tripped_down = True
                self._probe.update(
                    HealthTarget.EXECUTOR, HealthStatus.DOWN,
                    error_message=f"venue reconcile unreachable: {exc!r}",
                )
            return

        # success
        self._consecutive_failures = 0
        if self._tripped_down:
            self._tripped_down = False
            self._probe.update(
                HealthTarget.EXECUTOR, HealthStatus.HEALTHY,
                error_message="venue reconcile recovered",
            )
        if result.n_released > 0 or result.n_claimed > 0:
            # drift on a steady-state periodic run = the WS stream missed events
            log.warning(
                "periodic_reconcile_divergence released=%d claimed=%d failed=%d "
                "— WS lifecycle path missed events",
                result.n_released, result.n_claimed, result.n_failed,
            )
            self._probe.update(
                HealthTarget.BITFINEX_REST, HealthStatus.DEGRADED,
                error_message=(
                    f"reconcile drift released={result.n_released} claimed={result.n_claimed}"
                ),
            )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd backend_py && uv run pytest tests/modules/execution/test_periodic_reconcile.py -v`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add backend_py/src/bfx_funding_bot/modules/execution/periodic_reconcile.py backend_py/tests/modules/execution/test_periodic_reconcile.py
git commit -m "✨ Feat: PeriodicReconcile sub-task — interval reconcile + divergence + fail-safe"
```

---

### Task 4: Wire `PeriodicReconcile` into the daemon (construction + TaskGroup + env)

**Files:**
- Modify: `backend_py/src/bfx_funding_bot/modules/marketfeed/daemon.py:141-171` (dataclass field)
- Modify: `backend_py/src/bfx_funding_bot/modules/marketfeed/daemon.py:185-215` (TaskGroup)
- Modify: `backend_py/src/bfx_funding_bot/modules/marketfeed/daemon.py:800-815` (construction)

- [ ] **Step 1: Add the import and dataclass field**

Near the existing `from bfx_funding_bot.modules.execution.boot_recovery import BootRecovery` (daemon.py:53), add:

```python
from bfx_funding_bot.modules.execution.periodic_reconcile import PeriodicReconcile
```

In the `Daemon` dataclass (after `boot_recovery: BootRecovery | None = None`, line 167):

```python
    periodic_reconcile: PeriodicReconcile | None = None
```

- [ ] **Step 2: Register the TaskGroup sub-task**

In `Daemon.run()`, inside the `async with asyncio.TaskGroup() as tg:` block, after the `ws_dispatcher` registration (daemon.py:211-215), add:

```python
            # Spec 2026-05-27: periodic venue reconcile is the correctness
            # backbone — runs live-only, converges the ledger every interval.
            if self.periodic_reconcile is not None:
                tg.create_task(
                    self.periodic_reconcile.run_loop(self._stop_event),
                    name="periodic_reconcile",
                )
```

- [ ] **Step 3: Construct it in the live-only build block**

In the build function, right after the `BootRecovery` construction block (daemon.py:800-815, inside `if not spec.is_simulated:`), add a SECOND `BootRecovery` configured with the runtime grace + wrap it:

```python
        reconcile_interval_s = float(os.environ.get("BFX_RECONCILE_INTERVAL_S", "90"))
        runtime_recovery = BootRecovery(
            store=event_store,
            session_factory=session_factory,
            auth_rest=auth_rest,
            account_ctx=account_ctx,
            deployment_environment=env_str,
            bus=bus,
            is_simulated=spec.is_simulated,
            symbol=first_cell.symbol,
            action_grace_ms=120_000,  # never release/claim an offer < 2 min old (in-flight guard)
        )
        periodic_reconcile = PeriodicReconcile(
            recovery=runtime_recovery,
            probe=probe,
            interval_s=reconcile_interval_s,
        )
```

Then pass `periodic_reconcile=periodic_reconcile` into the `Daemon(...)` constructor call (find the `Daemon(` instantiation near daemon.py:1073 where `boot_recovery=boot_recovery` is passed, and add the new kwarg next to it). If `boot_recovery` stays `None` (simulated), `periodic_reconcile` must also be `None` — declare `periodic_reconcile: PeriodicReconcile | None = None` before the `if not spec.is_simulated:` block and assign inside it, mirroring how `boot_recovery` is declared at line 803.

- [ ] **Step 4: Run the full unit suite + types + lint**

Run: `cd backend_py && uv run pytest -m "not integration" && uv run mypy src/ && uv run ruff check`
Expected: PASS, no type/lint errors. (`probe`, `event_store`, `session_factory`, `auth_rest`, `account_ctx`, `env_str`, `bus`, `first_cell` are all already in scope in this block — confirm names against the surrounding `BootRecovery` construction; reuse the exact identifiers used there.)

- [ ] **Step 5: Commit**

```bash
git add backend_py/src/bfx_funding_bot/modules/marketfeed/daemon.py
git commit -m "✨ Feat: wire PeriodicReconcile into daemon (live-only, BFX_RECONCILE_INTERVAL_S=90)"
```

---

### Task 5: Integration test — reconcile converges the ledger with the WS stream dead (reproduces the incident)

Proves the end-to-end fix: a stuck `CLAIMED` reservation whose offer is gone from the venue gets released by the periodic reconcile alone (no WS events), dropping `reserved` below the cap so new offers can resume.

**Files:**
- Create: `backend_py/tests/integration/test_reconcile_converges_without_ws.py`

- [ ] **Step 1: Write the failing test**

Model it on the existing `BootRecovery.run()` integration setup in `tests/modules/execution/test_boot_recovery.py` (reuse its in-memory `store` / `session_factory` / `bus` / stubbed `auth_rest` fixtures — import or replicate them). The test seeds the ledger via the bus exactly like `test_cancel_path.py` does (`await bus.publish(ReservationClaimed(...))`), then runs one reconcile tick with an empty venue snapshot:

```python
import pytest
from decimal import Decimal

from bfx_funding_bot.modules.execution.boot_recovery import BootRecovery
from bfx_funding_bot.modules.execution.events import ReservationClaimed
from bfx_funding_bot.modules.execution.periodic_reconcile import PeriodicReconcile


@pytest.mark.integration
@pytest.mark.asyncio
async def test_reconcile_releases_stuck_claim_without_any_ws_event(reconcile_harness):
    """Incident reproduction: offer CLAIMED locally, gone from venue, no WS event.
    Periodic reconcile alone must release it and free the reserved capital."""
    h = reconcile_harness  # provides bus, ledger, store, session_factory, account_ctx, env

    # Seed: ledger reserved=150 for a claim placed > grace ago, voi 4960024777
    await h.bus.publish(ReservationClaimed(
        cid=1001, venue_offer_id="4960024777", size_usdt=Decimal("150"),
        signal_correlation_id=h.scid, account_id="default", is_simulated=False,
        occurred_at_ms=h.now_ms - 600_000,  # 10 min old → past action grace
    ))
    await h.persist_claim(cid=1001, voi="4960024777", size="150",
                          occurred_ms=h.now_ms - 600_000)  # write CLAIMED row to PG
    assert h.ledger.current_exposure() == Decimal("150")

    # Venue snapshot is EMPTY (offer matched/closed) and NO WS events fire.
    recovery = BootRecovery(
        store=h.store, session_factory=h.session_factory,
        auth_rest=h.empty_venue_auth_rest, account_ctx=h.account_ctx,
        deployment_environment=h.env, bus=h.bus, is_simulated=False,
        symbol="fUST", action_grace_ms=120_000, clock=lambda: h.now_ms,
    )
    pr = PeriodicReconcile(recovery=recovery, probe=h.probe, interval_s=0.01)

    import asyncio
    stop = asyncio.Event()

    async def _one_tick():
        await asyncio.sleep(0.03)
        stop.set()

    await asyncio.gather(pr.run_loop(stop), _one_tick())

    # Ledger converged: reservation released, exposure back to 0 → cap freed.
    assert h.ledger.current_exposure() == Decimal("0")
```

Add the `reconcile_harness` fixture to `tests/integration/conftest.py` (or a local `conftest.py`), composing the same in-memory pieces the boot_recovery test uses: a real `DomainEventBus` with the ledger subscribed, a `PostgresEventStore` over an in-memory/sqlite test session, a `_FakeProbe`, an `auth_rest` stub whose `get_active_funding_offers` returns `[]`, and a `persist_claim` helper that writes an `OfferClaimRow` with `state="CLAIMED"`. Mirror the construction already proven in `test_boot_recovery.py`'s `BootRecovery.run()` test — do not invent new infrastructure.

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend_py && uv run pytest tests/integration/test_reconcile_converges_without_ws.py -v`
Expected: FAIL initially — either the fixture/harness isn't wired yet, or (once wired) it demonstrates the release path. Build the harness until the test runs and asserts the exposure drop.

- [ ] **Step 3: Make it pass**

No new production code should be required (Tasks 1-3 implement the behaviour). Finish wiring the `reconcile_harness` fixture so the test exercises the real `BootRecovery` + `PeriodicReconcile` + ledger projection. If the ledger doesn't reach 0, verify the `ReservationReleased` is being published to the bus and the ledger subscribes to it (it does — `LedgerProjection.on_reservation_released`).

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend_py && uv run pytest tests/integration/test_reconcile_converges_without_ws.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add backend_py/tests/integration/test_reconcile_converges_without_ws.py backend_py/tests/integration/conftest.py
git commit -m "✅ Test: integration — periodic reconcile converges ledger with WS dead (incident repro)"
```

---

### Task 6: Deploy to canary and verify self-unstick

Manual rollout. Auto-deploy is disabled (`no_deploy_on_push=true`); deploy via the script.

- [ ] **Step 1: Full pre-deploy gate**

Run: `cd backend_py && uv run pytest -m "not integration" && uv run mypy src/ && uv run ruff check`
Expected: all green.

- [ ] **Step 2: Deploy canary**

Run: `./scripts/deploy-koyeb.sh` (the canary path sets `BFX_EXECUTOR=bitfinex_live`, `BFX_WS_CLIENT_ENABLED=true`, `BFX_ALLOCATION_CAP_USDT=450`). Confirm the deploy adds/keeps `BFX_RECONCILE_INTERVAL_S` (default 90 is applied in code if unset — no env change strictly required).

- [ ] **Step 3: Watch the first reconcile converge**

Run: `koyeb service logs <service-id> --type runtime | grep -E "reconcile_complete|periodic_reconcile_divergence"`
Expected: within ~90s of boot, a `reconcile_complete ... released=2` (the two stuck phantom offers) and a `periodic_reconcile_divergence released=2` WARN.

- [ ] **Step 4: Verify the ledger and event log via Neon**

Confirm (project `lingering-resonance-64910611`):
```sql
SELECT reserved_usdt, realized_usdt FROM position_state
WHERE deployment_environment='prod' AND account_id='default';
-- expect reserved_usdt = 150 (the one still-open offer), down from 450

SELECT state, count(*) FROM event_log
WHERE deployment_environment='prod' AND event_type='RESERVATION_RELEASED' GROUP BY state;
-- expect new RESERVATION_RELEASED rows (reason missing_from_venue)
```
Expected: `reserved_usdt` dropped to 150; subsequent strategy ticks place new offers again (no longer blocked by `AllocationCapGuard`).

---

## Self-Review

**Spec coverage:**
- Periodic snapshot reconcile backbone → Tasks 2-4. ✓
- Grace window (in-flight race safety) → Task 1. ✓
- Fail-safe (venue unreachable → block new offers) → Task 3 (EXECUTOR DOWN via existing `AuthHealthGuard`). ✓
- Divergence observability → Task 3 (`BITFINEX_REST` DEGRADED + WARN). ✓
- Reuse `compute_recovery_actions` / `BootRecovery`, no logic duplication → Tasks 1-4. ✓
- Self-unsticking rollout → Task 6. ✓
- Incident reproduction test → Task 5. ✓
- **Deferred (correctly, to Plan 2):** WS `foc` parser fix, `fcn` demotion, dispatcher `EXECUTED→OrderFilled`, real-fixture rebuild, WS-reconnect/seq-gap-triggered reconcile. Stated in scope note. ✓

**Placeholder scan:** No TBD/TODO. Task 2 Step 1 and Task 5 reference reusing the *existing* `BootRecovery.run()` test harness rather than printing it verbatim, because that harness already exists in `test_boot_recovery.py` and must be matched exactly — the engineer reads the real fixtures there. This is intentional (matching existing infra), not a placeholder.

**Type consistency:** `ReconcileResult(n_claimed, n_released, n_failed)` defined in Task 2, consumed in Task 3 (`result.n_released`, `result.n_claimed`). `PeriodicReconcile(recovery, probe, interval_s, max_consecutive_failures)` defined in Task 3, constructed identically in Task 4 and Task 5. `action_grace_ms` added in Task 1, threaded in Task 2, supplied in Task 4 (`120_000`). `HealthTarget.EXECUTOR` / `HealthTarget.BITFINEX_REST` / `HealthStatus.DOWN` / `DEGRADED` / `HEALTHY` match `schemas.py:70-86`. `AuthHealthGuard` blocks on `EXECUTOR` `DOWN` per `hard_guards.py:44-66`. ✓

**Risk flagged for executor:** In Task 3 the fail-safe reuses `HealthTarget.EXECUTOR` `DOWN`, which is also written by the executor heartbeat path. The `_tripped_down` ownership flag ensures this loop only clears the DOWN *it* set, but if both writers are active the last write wins. Acceptable for canary; revisit if it conflicts with the separately-tracked executor-heartbeat anti-pattern work.
