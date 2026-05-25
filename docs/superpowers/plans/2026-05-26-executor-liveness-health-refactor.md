# Executor Liveness Health-Check Refactor Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop the canary daemon from restarting in quiet markets by removing the reactive `executor`/`safety_chain` heartbeats from the liveness (restart-driving) path, and re-pointing `HeartbeatGuard` at market-data liveness instead.

**Architecture:** Separate **liveness** (own-loop, event-loop-driven sub-tasks → drive `/healthz` 503 + `scan_staleness` FatalError → restart) from **activity** (reactive middleware bumped only on POST decisions → observability WARN only, never restart/fatal/block). This is the k8s/SRE best practice: a liveness probe must never depend on business activity or downstream availability. `HeartbeatGuard` (a readiness gate, "don't trade on a stale market view") is re-pointed from the reactive `["safety_chain","executor"]` to market-data liveness `["ws"]`.

**Tech Stack:** Python 3.13, pytest (`asyncio_mode="auto"`), FastAPI TestClient, pytest-httpx. No new dependencies.

**Root cause (investigated 2026-05-26):** `record_heartbeat("executor")` only fires in `execution/middleware/heartbeat.py:36` (`HeartbeatMiddleware.submit`) — reactive. `safety_chain` likewise (`signal_engine.py:290`: `safety_chain.evaluate` only on POST). Both are bumped once at boot (smoke L2), then never in a quiet market. But `SUB_TASK_THRESHOLDS` lumps them with own-loop tasks and `healthz.py:73` returns 503 if any is stale → Koyeb restart. Observed on canary deployment `d824ee7a` (idle, no deposit): `executor degraded heartbeat stale >360s` → `/healthz` 503 → restart at 22:39. Not deposit-specific — any quiet market (MeanReversion >360s without a POST decision) reproduces it.

**Working reference in this codebase:** `daemon.py:312 _ws_heartbeat_poll_loop` already solves the same "quiet market, connection alive" problem for the `ws` sub-task by polling liveness independent of business data. `ws` therefore stays fresh in quiet markets (Bitfinex public WS sends `hb` frames ~15s), which is exactly why it is the correct thing for `HeartbeatGuard` to watch.

---

## File Structure

- **Modify** `src/bfx_funding_bot/modules/marketfeed/health_monitor.py` — split `SUB_TASK_THRESHOLDS` into `LIVENESS_THRESHOLDS` + `ACTIVITY_THRESHOLDS` (merged view kept for threshold lookup); `scan_staleness` never escalates an activity-class sub-task to `FatalError`.
- **Modify** `src/bfx_funding_bot/modules/marketfeed/healthz.py` — `/healthz` only considers `LIVENESS_THRESHOLDS` members; reactive/unknown sub-tasks are ignored (whitelist).
- **Modify** `src/bfx_funding_bot/modules/marketfeed/daemon.py:743` — `HeartbeatGuard` `watched_sub_tasks=["ws"]`.
- **Modify** `tests/modules/marketfeed/test_health_monitor.py` — add activity-no-fatal + liveness-still-fatal tests.
- **Modify** `tests/modules/marketfeed/test_healthz.py` — update axiom-based cases to liveness tasks; add reactive-ignored regression test.
- **Modify** `tests/modules/marketfeed/test_daemon_phase42.py` — add HeartbeatGuard-watches-ws wiring test.

---

## Task 1: Split liveness vs activity thresholds; activity never fatal

**Files:**
- Modify: `src/bfx_funding_bot/modules/marketfeed/health_monitor.py` (lines 25-43 threshold dict; line ~251 fatal escalation)
- Test: `tests/modules/marketfeed/test_health_monitor.py` (add to `TestStalenessScan`)

- [ ] **Step 1: Write the failing tests**

Add these two tests to the `TestStalenessScan` class in `tests/modules/marketfeed/test_health_monitor.py` (after `test_unknown_subtask_uses_default_threshold`):

```python
    async def test_activity_subtask_emits_but_never_fatal(self, monitor, fake_sink):
        """executor/safety_chain are reactive activity (not liveness): a stale
        heartbeat means 'no trades flowed', NOT a stuck process. Must emit a
        WARN/down observability event but NEVER raise FatalError, even past 3×."""
        # executor threshold 360s; 30min=1800s > 3× (1080s) → fatal for a
        # liveness task, but executor is activity-class → must not raise.
        monitor.probe.last_active_ts["executor"] = (
            datetime.now(UTC) - timedelta(minutes=30)
        )
        result = await monitor.scan_staleness()  # must NOT raise
        assert len(result) == 1
        assert result[0]["sub_task"] == "executor"
        assert result[0]["severity"] == "down"  # >2× threshold
        assert len(fake_sink.emitted) == 1
        assert fake_sink.emitted[0]["payload"]["check_target"] == "executor"
        assert fake_sink.emitted[0]["payload"]["status"] == "down"

    async def test_liveness_subtask_still_escalates_fatal(self, monitor):
        """A liveness sub-task (ws, threshold 90s) past 3× (270s) still raises."""
        monitor.probe.last_active_ts["ws"] = (
            datetime.now(UTC) - timedelta(seconds=300)
        )
        with pytest.raises(FatalError):
            await monitor.scan_staleness()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd backend_py && uv run pytest tests/modules/marketfeed/test_health_monitor.py::TestStalenessScan::test_activity_subtask_emits_but_never_fatal -v`
Expected: FAIL — currently `executor` at 1800s > 3×360s raises `FatalError` (no activity carve-out yet).

- [ ] **Step 3: Split the threshold table**

In `src/bfx_funding_bot/modules/marketfeed/health_monitor.py`, replace the block (lines 25-43):

```python
# Per spec D4 heartbeat threshold table — staleness threshold in seconds.
SUB_TASK_THRESHOLDS: dict[str, int] = {
    "ws": 90,                    # Phase 4.2.0 lesson v2: poll ws_client.last_msg_age_ms()
                                 # every 15s; threshold 90s covers 4-5 missed Bitfinex
                                 # `hb` frames (which arrive ~15s on subscribed channels).
    "candle_writer": 65 * 60,    # Phase 4.2.0 d90363fa lesson v1: 1h funding cells
                                 # publish candle only on tick — can be silent
                                 # >5min in quiet markets. ws heartbeat poller is
                                 # the fast-zombie detector now; candle_writer
                                 # only catches truly stuck queues (>3hr).
    "scheduler": 65 * 60,        # hourly boundary + buffer
    "health_check": 6 * 60,      # 5min hb + buffer
    "db_keepalive": 7 * 60,      # 5min interval + 2min buffer
    # Phase 4.2 Task 19: execution-pipeline sub-tasks.
    "safety_chain": 6 * 60,      # 5min watchdog + 1min buffer
    "executor": 6 * 60,          # 5min watchdog + 1min buffer
    "fill_tracker": 90,          # 30s poll cadence x 3 missed
}
_DEFAULT_THRESHOLD_S = 60
```

with:

```python
# ── Liveness sub-tasks (own-loop, event-loop-driven) ──────────────────────────
# A stale heartbeat means the loop is stuck or the event loop is deadlocked →
# restarting the process can recover. These DRIVE /healthz 503 (Koyeb restart)
# and scan_staleness FatalError. Per spec D4 heartbeat threshold table.
LIVENESS_THRESHOLDS: dict[str, int] = {
    "ws": 90,                    # Phase 4.2.0 lesson v2: poll ws_client.last_msg_age_ms()
                                 # every 15s; threshold 90s covers 4-5 missed Bitfinex
                                 # `hb` frames (which arrive ~15s on subscribed channels).
    "candle_writer": 65 * 60,    # Phase 4.2.0 d90363fa lesson v1: 1h funding cells
                                 # publish candle only on tick — can be silent
                                 # >5min in quiet markets. ws heartbeat poller is
                                 # the fast-zombie detector now; candle_writer
                                 # only catches truly stuck queues (>3hr).
    "scheduler": 65 * 60,        # hourly boundary + buffer
    "health_check": 6 * 60,      # 5min hb + buffer
    "db_keepalive": 7 * 60,      # 5min interval + 2min buffer
    "fill_tracker": 90,          # 30s poll cadence x 3 missed
}

# ── Activity sub-tasks (reactive middleware) ──────────────────────────────────
# executor/safety_chain are bumped ONLY when a POST decision flows through the
# chain (execution/middleware/heartbeat.py, signal_engine.py:290). A stale
# heartbeat means "no trading activity", NOT a failure. Tying these to liveness
# caused the 2026-05-26 canary restart loop (idle market → stale → /healthz 503
# → restart) — a textbook k8s anti-pattern (liveness must not depend on business
# activity). These are emitted as WARN for observability but NEVER drive a
# restart, FatalError, or trade block.
ACTIVITY_THRESHOLDS: dict[str, int] = {
    "safety_chain": 6 * 60,
    "executor": 6 * 60,
}

# Merged view: scan_staleness needs a threshold for both classes to emit. The
# liveness/fatal gating is keyed on LIVENESS_THRESHOLDS membership, not on this.
SUB_TASK_THRESHOLDS: dict[str, int] = {**LIVENESS_THRESHOLDS, **ACTIVITY_THRESHOLDS}
_DEFAULT_THRESHOLD_S = 60
```

- [ ] **Step 4: Gate fatal escalation on liveness membership**

In `scan_staleness`, replace the fatal-escalation block (currently lines 250-255):

```python
            # Escalate to fatal AFTER emit, so the event is persisted before raise
            if age_s > 3 * threshold:
                raise FatalError(
                    f"sub_task={sub_task} stale {age_s:.0f}s > "
                    f"3× threshold ({3 * threshold}s) — escalating fatal"
                )
```

with:

```python
            # Escalate to fatal AFTER emit, so the event is persisted before raise.
            # Activity-class sub-tasks (reactive: executor / safety_chain) never
            # escalate — a stale executor means "no trades flowed", not a stuck
            # process. Liveness sub-tasks and unknown keys (default threshold)
            # still escalate so a genuinely hung own-loop task triggers restart.
            if age_s > 3 * threshold and sub_task not in ACTIVITY_THRESHOLDS:
                raise FatalError(
                    f"sub_task={sub_task} stale {age_s:.0f}s > "
                    f"3× threshold ({3 * threshold}s) — escalating fatal"
                )
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd backend_py && uv run pytest tests/modules/marketfeed/test_health_monitor.py -v`
Expected: PASS — new activity-no-fatal + liveness-still-fatal tests pass; existing `test_fatal_escalation_at_3x_threshold` (uses unknown key "axiom") still passes (unknown keys keep escalating).

- [ ] **Step 6: Commit**

```bash
cd backend_py && git add src/bfx_funding_bot/modules/marketfeed/health_monitor.py tests/modules/marketfeed/test_health_monitor.py
git commit -m "♻️ Refactor: split liveness vs activity heartbeats; activity never fatal

executor/safety_chain are reactive (bumped only on POST decisions), not
own-loop liveness. Splitting SUB_TASK_THRESHOLDS into LIVENESS_THRESHOLDS +
ACTIVITY_THRESHOLDS; scan_staleness no longer escalates an activity-class
sub-task to FatalError (still emits WARN for observability). Root cause of the
2026-05-26 canary idle-restart loop.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 2: `/healthz` only considers liveness sub-tasks

**Files:**
- Modify: `src/bfx_funding_bot/modules/marketfeed/healthz.py` (imports line 33-37; route lines 60-80)
- Test: `tests/modules/marketfeed/test_healthz.py`

- [ ] **Step 1: Update + add tests (write the new behaviour first)**

In `tests/modules/marketfeed/test_healthz.py`, replace `test_healthz_returns_200_when_all_sub_tasks_fresh`, `test_healthz_returns_503_when_any_sub_task_stale`, `test_healthz_returns_503_when_multiple_sub_tasks_stale`, and `test_healthz_uses_per_task_threshold_from_sub_task_thresholds` with the versions below, and add the new regression test. (The old tests used "axiom" — a removed sub-task that fell back to the default threshold; the whitelist refactor ignores non-liveness keys, so they must use real liveness tasks.)

```python
def test_healthz_returns_200_when_all_sub_tasks_fresh() -> None:
    probe = _probe_with(fresh=["ws", "scheduler", "candle_writer", "fill_tracker"])
    client = TestClient(make_app(probe))
    resp = client.get("/healthz")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["tasks"] == 4


def test_healthz_returns_503_when_any_sub_task_stale() -> None:
    probe = _probe_with(
        fresh=["scheduler", "candle_writer"],
        stale=["ws"],
    )
    client = TestClient(make_app(probe))
    resp = client.get("/healthz")
    assert resp.status_code == 503
    body = resp.json()
    assert body["status"] == "degraded"
    assert len(body["stale"]) == 1
    assert body["stale"][0]["task"] == "ws"
    assert body["stale"][0]["age_s"] > body["stale"][0]["threshold_s"]


def test_healthz_returns_503_when_multiple_sub_tasks_stale() -> None:
    probe = _probe_with(
        fresh=["scheduler"],
        stale=["ws", "fill_tracker"],
    )
    client = TestClient(make_app(probe))
    resp = client.get("/healthz")
    assert resp.status_code == 503
    body = resp.json()
    stale_tasks = sorted(s["task"] for s in body["stale"])
    assert stale_tasks == ["fill_tracker", "ws"]


def test_healthz_uses_per_task_threshold() -> None:
    """candle_writer has 65*60s threshold (1h+); 30min stale is FRESH for it
    but a 90s-threshold task (ws) at 30min is stale."""
    probe = HealthProbe()
    now = datetime.now(UTC)
    probe.last_active_ts["candle_writer"] = now - timedelta(minutes=30)  # ok for 1h+ threshold
    probe.last_active_ts["ws"] = now - timedelta(minutes=30)             # stale for 90s threshold
    probe.last_active_ts["scheduler"] = now                              # fresh
    client = TestClient(make_app(probe))
    resp = client.get("/healthz")
    assert resp.status_code == 503
    stale_tasks = [s["task"] for s in resp.json()["stale"]]
    assert "ws" in stale_tasks
    assert "candle_writer" not in stale_tasks


def test_healthz_ignores_reactive_executor_and_safety_chain() -> None:
    """Regression for the 2026-05-26 canary idle-restart loop: executor/
    safety_chain are reactive activity, not liveness. Even 10h stale they must
    NOT cause /healthz 503 as long as a liveness task is fresh."""
    probe = HealthProbe()
    now = datetime.now(UTC)
    probe.last_active_ts["ws"] = now                               # fresh liveness
    probe.last_active_ts["scheduler"] = now                        # fresh liveness
    probe.last_active_ts["executor"] = now - timedelta(hours=10)   # stale activity
    probe.last_active_ts["safety_chain"] = now - timedelta(hours=10)
    client = TestClient(make_app(probe))
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json()["tasks"] == 2  # only liveness tasks counted
```

Also replace `test_healthz_returns_503_when_no_sub_tasks_registered` with one that proves activity-only registration is still "starting":

```python
def test_healthz_returns_503_when_no_liveness_sub_tasks_registered() -> None:
    """Startup: only reactive activity recorded (e.g. boot-smoke bumped
    executor) but no liveness loop ticked yet → not ready, do not mark healthy."""
    probe = HealthProbe()
    probe.last_active_ts["executor"] = datetime.now(UTC)  # activity only
    client = TestClient(make_app(probe))
    resp = client.get("/healthz")
    assert resp.status_code == 503
    body = resp.json()
    assert body["status"] == "starting"
    assert body["reason"] == "no_liveness_sub_tasks_registered_yet"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd backend_py && uv run pytest tests/modules/marketfeed/test_healthz.py::test_healthz_ignores_reactive_executor_and_safety_chain -v`
Expected: FAIL — current `/healthz` counts every key in `last_active_ts` (including stale executor/safety_chain) → returns 503, not 200.

- [ ] **Step 3: Rewrite the `/healthz` route to a liveness whitelist**

In `src/bfx_funding_bot/modules/marketfeed/healthz.py`, replace the import (lines 33-37):

```python
from bfx_funding_bot.modules.marketfeed.health_monitor import (
    _DEFAULT_THRESHOLD_S,
    SUB_TASK_THRESHOLDS,
    HealthProbe,
)
```

with:

```python
from bfx_funding_bot.modules.marketfeed.health_monitor import (
    LIVENESS_THRESHOLDS,
    HealthProbe,
)
```

Then replace the route body (lines 60-80):

```python
    @app.get("/healthz")
    async def healthz() -> JSONResponse:
        last_active = dict(probe.last_active_ts)  # snapshot
        if not last_active:
            return JSONResponse(
                status_code=503,
                content={"status": "starting", "reason": "no_sub_tasks_registered_yet"},
            )
        now = datetime.now(UTC)
        stale: list[dict[str, float | int | str]] = []
        for task, last_ts in last_active.items():
            threshold = SUB_TASK_THRESHOLDS.get(task, _DEFAULT_THRESHOLD_S)
            age_s = (now - last_ts).total_seconds()
            if age_s > threshold:
                stale.append({"task": task, "age_s": age_s, "threshold_s": threshold})
        if stale:
            return JSONResponse(status_code=503, content={"status": "degraded", "stale": stale})
        return JSONResponse(
            status_code=200,
            content={"status": "ok", "tasks": len(last_active)},
        )
```

with:

```python
    @app.get("/healthz")
    async def healthz() -> JSONResponse:
        # Liveness probe: ONLY own-loop, event-loop-driven sub-tasks count.
        # Reactive activity (executor / safety_chain) and unknown keys are
        # excluded — "no trading activity" must never trigger a restart
        # (k8s liveness anti-pattern). See health_monitor.LIVENESS_THRESHOLDS.
        now = datetime.now(UTC)
        liveness = {
            t: ts for t, ts in probe.last_active_ts.items()
            if t in LIVENESS_THRESHOLDS
        }
        if not liveness:
            return JSONResponse(
                status_code=503,
                content={"status": "starting", "reason": "no_liveness_sub_tasks_registered_yet"},
            )
        stale: list[dict[str, float | int | str]] = []
        for task, last_ts in liveness.items():
            threshold = LIVENESS_THRESHOLDS[task]
            age_s = (now - last_ts).total_seconds()
            if age_s > threshold:
                stale.append({"task": task, "age_s": age_s, "threshold_s": threshold})
        if stale:
            return JSONResponse(status_code=503, content={"status": "degraded", "stale": stale})
        return JSONResponse(
            status_code=200,
            content={"status": "ok", "tasks": len(liveness)},
        )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd backend_py && uv run pytest tests/modules/marketfeed/test_healthz.py -v`
Expected: PASS (all updated + new tests).

- [ ] **Step 5: Commit**

```bash
cd backend_py && git add src/bfx_funding_bot/modules/marketfeed/healthz.py tests/modules/marketfeed/test_healthz.py
git commit -m "♻️ Refactor: /healthz liveness whitelist (ignore reactive executor/safety_chain)

/healthz now only considers LIVENESS_THRESHOLDS members (own-loop tasks).
Reactive activity sub-tasks and unknown keys no longer drive the Koyeb 503 →
restart. Closes the canary idle-restart loop at the liveness boundary.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 3: `HeartbeatGuard` watches market-data liveness, not reactive activity

**Files:**
- Modify: `src/bfx_funding_bot/modules/marketfeed/daemon.py:743`
- Test: `tests/modules/marketfeed/test_daemon_phase42.py` (add wiring test)

- [ ] **Step 1: Write the failing test**

Add to `tests/modules/marketfeed/test_daemon_phase42.py` (after `test_build_daemon_filters_disabled_hard_guards`):

```python
@pytest.mark.asyncio
async def test_build_daemon_heartbeat_guard_watches_market_data_not_executor(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, httpx_mock: HTTPXMock,
) -> None:
    """HeartbeatGuard is a readiness gate ('don't trade on a stale market
    view'), so it watches market-data own-loop liveness (ws), NOT the reactive
    executor/safety_chain — watching those self-suppresses trading in quiet
    markets (the 2026-05-26 canary restart bug) and is a reactive mismatch."""
    await _base_env(monkeypatch, tmp_path)
    monkeypatch.setenv("BFX_ACCOUNT_ID", "default")
    monkeypatch.setenv("BFX_API_KEY", "test_key")
    monkeypatch.setenv("BFX_API_SECRET", "test_secret")
    monkeypatch.setenv("BFX_ALLOCATION_CAP_USDT", "500")
    monkeypatch.delenv("BFX_EXECUTOR", raising=False)
    monkeypatch.delenv("BFX_FILL_TRACKER_ENABLED", raising=False)

    # heartbeat guard enabled (disable nothing).
    safety_yaml = _write_safety_yaml(tmp_path, disable=set())
    monkeypatch.setenv("BFX_SAFETY_CONFIG", str(safety_yaml))
    _add_bitfinex_mock(httpx_mock)

    from bfx_funding_bot.modules.execution.safety.hard_guards import HeartbeatGuard
    from bfx_funding_bot.modules.marketfeed.daemon import build_daemon
    daemon = await build_daemon(
        cells_yaml_path=_write_cells_yaml(tmp_path), skip_ws=True,
    )
    hbg = next(
        g for g in daemon.safety_chain.guards if isinstance(g, HeartbeatGuard)
    )
    assert hbg.watched == ["ws"]
    assert "executor" not in hbg.watched
    assert "safety_chain" not in hbg.watched
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend_py && uv run pytest "tests/modules/marketfeed/test_daemon_phase42.py::test_build_daemon_heartbeat_guard_watches_market_data_not_executor" -v`
Expected: FAIL — `hbg.watched == ["safety_chain", "executor"]`, not `["ws"]`.

- [ ] **Step 3: Re-point the guard in `daemon.py`**

In `src/bfx_funding_bot/modules/marketfeed/daemon.py`, replace (lines 739-744):

```python
    if hg.heartbeat.enabled:
        guards.append(HeartbeatGuard(
            probe=probe,
            threshold_seconds=hg.heartbeat.sub_task_stale_threshold_seconds,
            watched_sub_tasks=["safety_chain", "executor"],
        ))
```

with:

```python
    if hg.heartbeat.enabled:
        guards.append(HeartbeatGuard(
            probe=probe,
            threshold_seconds=hg.heartbeat.sub_task_stale_threshold_seconds,
            # Readiness gate: block POST only when our MARKET VIEW is stale.
            # Watch market-data own-loop liveness ("ws"), not the reactive
            # executor/safety_chain — those are bumped only by trading itself,
            # so watching them self-suppresses trades in quiet markets and was
            # part of the 2026-05-26 canary restart loop. ws stays fresh in
            # quiet markets via _ws_heartbeat_poll_loop (Bitfinex hb ~15s).
            watched_sub_tasks=["ws"],
        ))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend_py && uv run pytest "tests/modules/marketfeed/test_daemon_phase42.py::test_build_daemon_heartbeat_guard_watches_market_data_not_executor" -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
cd backend_py && git add src/bfx_funding_bot/modules/marketfeed/daemon.py tests/modules/marketfeed/test_daemon_phase42.py
git commit -m "♻️ Refactor: HeartbeatGuard watches market-data liveness (ws), not reactive activity

Re-point HeartbeatGuard from [safety_chain, executor] (reactive, self-
suppressing in quiet markets) to [ws] (market-data own-loop liveness). The
guard's intent is 'don't trade on a stale market view' — ws is the right
signal and stays fresh in quiet markets via the ws heartbeat poll loop.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Final verification

- [ ] **Run the full default gate**

Run: `cd backend_py && uv run pytest -m "not integration" -q`
Expected: all pass, 0 failed (existing suite + new tests). Watch especially `test_health_monitor.py`, `test_healthz.py`, `test_daemon_phase42.py`.

- [ ] **Confirm mypy + ruff clean**

Run: `cd backend_py && uv run mypy src/ && uv run ruff check`
Expected: clean.

- [ ] **Update the canary runbook known-issue note**

In `docs/deploy/koyeb-canary.md`, the troubleshooting row "空跑（無成交）時 instance 反覆 unhealthy / daemon 重啟" — append to its 處置 cell: `已修（2026-05-26）：executor/safety_chain 移出 liveness，HeartbeatGuard 改 watch ws。`. Commit with `📝 Docs:`.

---

## Out of scope (do NOT implement here)

- Adding a brand-new dedicated event-loop liveness ticker (the existing own-loop tasks — health_check 30s / ws 15s — already prove event-loop liveness; no new ticker needed).
- Removing the `record_heartbeat("executor")` / `record_heartbeat("safety_chain")` calls (they stay for observability WARN emission; only their liveness/fatal/guard wiring changes).
- Re-tuning `safety.canary.yaml` `heartbeat.sub_task_stale_threshold_seconds` (300s is fine as a readiness threshold for `ws`, whose liveness threshold is 90s).
- G3 P&L tracking-error / capital ramp (separate scale-up specs).

---

## Self-Review

- **Coverage:** Root cause (executor/safety_chain reactive in liveness path) addressed by Task 1 (no fatal) + Task 2 (no 503) + Task 3 (no guard block). All three vectors that turn "idle" into "restart/block" are closed.
- **Placeholders:** none — every step has concrete code + exact commands.
- **Type consistency:** `LIVENESS_THRESHOLDS` / `ACTIVITY_THRESHOLDS` / `SUB_TASK_THRESHOLDS` used consistently across Task 1 (defined) → Task 2 (imported). `HeartbeatGuard.watched` attribute name matches `hard_guards.py:85` (`self.watched = watched_sub_tasks`). `_write_safety_yaml(tmp_path, disable=...)` signature matches existing usage at test_daemon_phase42.py:158.
- **Existing-test impact:** `test_healthz.py` axiom-based cases rewritten to liveness tasks (axiom is a removed sub-task; whitelist now ignores it). `test_health_monitor.py` unknown-key ("axiom"/"custom_task") fatal tests still valid — unknown keys keep escalating; only ACTIVITY_THRESHOLDS members are exempted.
