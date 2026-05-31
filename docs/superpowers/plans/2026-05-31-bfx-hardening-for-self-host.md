# bfx Hardening for Self-Host Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Harden the bfx-funding-bot for self-managed hosting (Koyeb→Oracle VM) by adding a Postgres advisory single-writer lock, an alembic migration lock + timeouts, and re-deriving the canary loss limiter — all safe to merge to `main` while Koyeb still runs (single instance always acquires).

**Architecture:** A dedicated NullPool Postgres connection holds a session-scoped `pg_try_advisory_lock` acquired at daemon boot (live-only); a `WriterLockGuard` in the existing safety chain live-verifies lock ownership (`SELECT 1` on the dedicated conn) before every real-money submit and fails closed; a background liveness loop re-acquires after transient drops. Alembic migrations serialize on a distinct advisory lock with bounded `lock_timeout`/`statement_timeout`.

**Tech Stack:** Python 3.13 (uv), SQLAlchemy async + asyncpg, Postgres (Neon), Alembic, pytest (`asyncio_mode=auto`) + testcontainers, mypy --strict, ruff.

---

## Conventions (all tasks)

- **Every command runs from `backend_py/`** (repo-root pyenv 3.12 breaks sqlalchemy): `cd ~/second-brain/projects/startup/bfx-funding-bot/backend_py`.
- **Full gate before each commit:** `uv run ruff check && uv run mypy --strict src && uv run pytest -m "not integration and not gate" -q`
- **Integration tests** (real Postgres via testcontainers, needs Docker): `uv run pytest -m integration -q`
- **Commit prefixes** (CLAUDE.md): `✨ Feat:`, `✅ Test:`, `🔧 Chore:`, `🩹 Patch:`.
- Branch: `git checkout -b feat/hardening-self-host` before Task 1.

## Resolved design decisions (do not re-litigate)

1. **Acquire location:** inside `build_daemon`'s live block (`if not spec.is_simulated:`), right after building the dedicated conn — single-point fail-fast; runs before `boot_recovery.run()` (which is in `Daemon.run()`). 
2. **Fail-fast signal:** raise `WriterLockUnacquired(ExecutorFatalError)`; `main()` catches it → `sys.exit(EXIT_CODE_WRITER_LOCKED=75)` (EX_TEMPFAIL — "another writer holds it, retry later").
3. **Dedicated connection:** a NullPool `AsyncEngine` built from `_prepare_engine_kwargs(config.database_url)` + a held `AsyncConnection` for the daemon lifetime (NullPool ⇒ never recycled ⇒ session lock survives). `-pooler` is stripped by `_prepare_engine_kwargs`, giving a direct session (advisory locks don't work through a txn-mode pooler).
4. **Per-submit check = live verify, not a cached flag:** `WriterLockGuard.evaluate` calls `await lock.verify_held()` which runs `SELECT 1` on the dedicated conn (session lock held ⟺ session alive). Eliminates the stale-flag window that a background-only flag would leave open.
5. **Background liveness loop:** every 30s, `refresh()` (verify; on loss clear flag + attempt reconnect/re-acquire) + record heartbeat. Recovery + observability only; the guard's live verify is authoritative.
6. **Scope:** live-only — lock objects are None in paper/shadow (constructed only in the not-simulated block), so two shadow instances never contend.
7. **Lock-key:** `blake2b(f"bfx-writer:{account_id}:{env}", digest_size=8)` → signed int64 → `pg_try_advisory_lock($1)`. Builtin `hash()` is FORBIDDEN (PYTHONHASHSEED salt). A2's key uses a different namespace string so daemon and migration never false-share.
8. **A2 lock:** session-level `pg_try_advisory_lock` before `context.configure`, `pg_advisory_unlock` in `finally`; if not acquired → raise (another migration in progress). Plus `SET lock_timeout='5s'`, `SET statement_timeout='60s'`. Online path only (offline untouched).
9. **A2 compose file is OUT OF SCOPE here** (it belongs to Plan B / VM infra). This plan only changes `alembic/env.py`.

## File structure

| File | Responsibility | New? |
|---|---|---|
| `configs/safety.canary.yaml` | canary loss threshold 15→57 + comment fixes | modify |
| `tests/modules/execution/safety/test_safety_config.py` | pin threshold==57 | modify |
| `src/bfx_funding_bot/core/protocols.py` *(or execution/protocols.py — see Task 2)* | `WriterLockHandle` Protocol | modify |
| `src/bfx_funding_bot/modules/execution/safety/hard_guards.py` | `WriterLockGuard` | modify |
| `tests/modules/execution/safety/test_hard_guards.py` | guard unit test | modify |
| `src/bfx_funding_bot/core/errors.py` | `WriterLockUnacquired` + `EXIT_CODE_WRITER_LOCKED` | modify |
| `src/bfx_funding_bot/core/writer_lock.py` | `derive_lock_key` + `WriterLock` | **create** |
| `tests/integration/test_writer_lock.py` | acquire/contention/release/verify on real PG | **create** |
| `src/bfx_funding_bot/modules/marketfeed/daemon.py` | fields, build_daemon acquire, guard + liveness wiring | modify |
| `tests/modules/marketfeed/test_daemon_wiring.py` | sqlite construction assertions | modify |
| `tests/integration/test_writer_lock_daemon.py` | fail-closed reconciler + liveness on real PG | **create** |
| `alembic/env.py` | advisory lock + timeouts in `do_run_migrations` | modify |
| `tests/integration/test_alembic_migration_lock.py` | lock/timeout emitted | **create** |
| `scripts/deploy-koyeb.sh`, `docs/deploy/koyeb-paper.md`, `docs/deploy/koyeb-canary.md` | stale region/cap notes | modify |

---

### Task 1: A3 — canary `realized_loss_24h` threshold 15→57

**Files:**
- Modify: `configs/safety.canary.yaml:23-26` (+ comments at lines 9, 21, 26)
- Test: `tests/modules/execution/safety/test_safety_config.py` (append)

- [ ] **Step 1: Write the failing regression test** (append to the file; loads the REAL committed yaml)

```python
def test_canary_realized_loss_threshold_is_57() -> None:
    cfg = load_safety_config(Path(__file__).parents[3] / "configs" / "safety.canary.yaml")
    assert cfg.calibrated_guards.realized_loss_24h.enabled is True
    assert cfg.calibrated_guards.realized_loss_24h.threshold_usdt == 57
```
If `Path` / `load_safety_config` aren't already imported at the top of the file, add `from pathlib import Path` and confirm `from bfx_funding_bot.modules.execution.safety.config import load_safety_config` (match the existing import in this file).

- [ ] **Step 2: Run it — must FAIL for the right reason**

Run: `uv run pytest tests/modules/execution/safety/test_safety_config.py::test_canary_realized_loss_threshold_is_57 -q`
Expected: FAIL — `assert 15.0 == 57`.

- [ ] **Step 3: Edit the yaml value + stale comments**

`configs/safety.canary.yaml` line 26: `    threshold_usdt: 15.0     # halt if 24h realized loss exceeds ~10% of $150 cap` → 
```yaml
    threshold_usdt: 57       # halt if 24h realized loss exceeds ~10% of the 570 USDT cap
```
Also fix the two other now-stale comments (behavior-neutral, doc honesty for real-money config):
- line 9 header `...$150 canary profile (~10% loss / 15% drawdown).` → `...570 USDT canary profile (~10% loss / 15% drawdown).`
- line 21 `# set 450, matches funded wallet` → `# informational only — real cap comes from BFX_ALLOCATION_CAP_USDT env (570)`
Do NOT touch `drawdown_from_peak` (line 27-29) or `configs/safety.yaml`.

- [ ] **Step 4: Run it — must PASS; run the daemon-wiring tests that load this file**

Run: `uv run pytest tests/modules/execution/safety/test_safety_config.py -q && uv run pytest tests/modules/marketfeed/test_daemon_wiring.py tests/modules/marketfeed/test_daemon_pnl_wiring.py -q`
Expected: all PASS (those wiring tests only assert guards enabled, not the number, so they stay green).

- [ ] **Step 5: Full gate + commit**

Run: `uv run ruff check && uv run mypy --strict src && uv run pytest -m "not integration and not gate" -q`
```bash
git add configs/safety.canary.yaml tests/modules/execution/safety/test_safety_config.py
git commit -m "🩹 Patch: re-derive canary realized_loss_24h to 57 (10% of 570 cap)"
```

---

### Task 2: A1 — `WriterLockGuard` (fail-closed safety guard)

**Files:**
- Modify: `src/bfx_funding_bot/modules/execution/protocols.py` (add `WriterLockHandle` Protocol — confirm this is the module holding `GuardRule`/`GuardResult`; the kit cited `protocols.py:37-59`)
- Modify: `src/bfx_funding_bot/modules/execution/safety/hard_guards.py` (add `WriterLockGuard`)
- Test: `tests/modules/execution/safety/test_hard_guards.py`

- [ ] **Step 1: Add the `WriterLockHandle` Protocol** to `protocols.py` (near `GuardRule`)

```python
class WriterLockHandle(Protocol):
    """Anything that can live-verify whether this process holds the writer lock."""
    async def verify_held(self) -> bool: ...
```
Ensure `from typing import Protocol` is present (it already is — `GuardRule` is a Protocol).

- [ ] **Step 2: Write the failing guard test** (append to `test_hard_guards.py`, mirroring the `ManualKillGuard` tests already in the file — reuse their `_ctx()` / `_post()` helpers if present, else copy this)

```python
class _FakeLock:
    def __init__(self, held: bool) -> None:
        self._held = held
    async def verify_held(self) -> bool:
        return self._held


async def test_writer_lock_guard_blocks_when_lock_lost() -> None:
    guard = WriterLockGuard(lock=_FakeLock(held=False))
    r = await guard.evaluate(_post(), _ctx())
    assert r.allowed is False
    assert r.guard_name == "writer_lock"
    assert r.reason is not None


async def test_writer_lock_guard_allows_when_held() -> None:
    guard = WriterLockGuard(lock=_FakeLock(held=True))
    r = await guard.evaluate(_post(), _ctx())
    assert r.allowed is True
    assert r.guard_name == "writer_lock"
```
Add `from bfx_funding_bot.modules.execution.safety.hard_guards import WriterLockGuard` to the imports (next to the existing `ManualKillGuard` import). If `_ctx`/`_post` helpers don't already exist in this file, copy them verbatim from the ManualKillGuard test block in the same file (the kit confirms `_ctx()` → `AccountContext("default", Credentials("k","s"), Decimal("500"))`, `_post()` → `DecisionPayload(...)`).

- [ ] **Step 3: Run it — must FAIL**

Run: `uv run pytest tests/modules/execution/safety/test_hard_guards.py -k writer_lock -q`
Expected: FAIL — `ImportError: cannot import name 'WriterLockGuard'`.

- [ ] **Step 4: Implement `WriterLockGuard`** (append to `hard_guards.py`; reuse the file's existing imports of `DecisionPayload`, `AccountContext`, `GuardResult`, add `WriterLockHandle`)

```python
class WriterLockGuard:
    """Fail-closed single-writer guard. Refuses every real-money submit unless
    this process still holds the Postgres advisory writer lock, verified LIVE
    against the dedicated connection (no stale-flag window)."""

    name = "writer_lock"
    is_calibrated = False

    def __init__(self, *, lock: WriterLockHandle) -> None:
        self._lock = lock

    async def evaluate(self, decision: DecisionPayload, ctx: AccountContext) -> GuardResult:
        if await self._lock.verify_held():
            return GuardResult(allowed=True, guard_name=self.name)
        return GuardResult(
            allowed=False,
            guard_name=self.name,
            reason="writer advisory lock not held — failing closed (refusing real-money submit)",
        )
```
Add `from bfx_funding_bot.modules.execution.protocols import WriterLockHandle` (alongside the existing protocols import).

- [ ] **Step 5: Run it — must PASS**

Run: `uv run pytest tests/modules/execution/safety/test_hard_guards.py -k writer_lock -q`
Expected: PASS (2 tests).

- [ ] **Step 6: Full gate + commit**

Run: `uv run ruff check && uv run mypy --strict src && uv run pytest -m "not integration and not gate" -q`
```bash
git add src/bfx_funding_bot/modules/execution/protocols.py src/bfx_funding_bot/modules/execution/safety/hard_guards.py tests/modules/execution/safety/test_hard_guards.py
git commit -m "✨ Feat: add WriterLockGuard (fail-closed single-writer safety guard)"
```

---

### Task 3: A1 — `core/writer_lock.py` + errors + integration tests

**Files:**
- Modify: `src/bfx_funding_bot/core/errors.py` (add exception + exit code)
- Create: `src/bfx_funding_bot/core/writer_lock.py`
- Test: `tests/integration/test_writer_lock.py` (new, real Postgres)

- [ ] **Step 1: Add the error + exit code** to `core/errors.py`

```python
class WriterLockUnacquired(ExecutorFatalError):
    """Another live writer already holds the single-writer advisory lock."""


EXIT_CODE_WRITER_LOCKED = 75  # EX_TEMPFAIL — another writer holds the lock; retry later
```
(`ExecutorFatalError` already exists — `ExecutorAuthError` subclasses it at errors.py:27.)

- [ ] **Step 2: Write the failing integration test** `tests/integration/test_writer_lock.py`

```python
from __future__ import annotations

from uuid import uuid4

import pytest

from bfx_funding_bot.core.errors import WriterLockUnacquired
from bfx_funding_bot.core.writer_lock import WriterLock, derive_lock_key

pytestmark = pytest.mark.integration


def _url(pg_engine) -> str:
    return pg_engine.url.render_as_string(hide_password=False)


async def test_key_is_stable_and_namespaced() -> None:
    k1 = derive_lock_key("acct-A", "prod")
    k2 = derive_lock_key("acct-A", "prod")
    k3 = derive_lock_key("acct-A", "shadow")
    assert k1 == k2 and k1 != k3
    assert -(2**63) <= k1 < 2**63


async def test_acquire_then_second_contends_then_release(pg_engine) -> None:
    acct = f"wl-{uuid4().hex[:8]}"
    key = derive_lock_key(acct, "prod")
    a = WriterLock(database_url=_url(pg_engine), key=key)
    await a.acquire()
    assert await a.verify_held() is True

    b = WriterLock(database_url=_url(pg_engine), key=key)
    with pytest.raises(WriterLockUnacquired):
        await b.acquire()

    await a.release()
    # now b can take it
    await b.acquire()
    assert await b.verify_held() is True
    await b.release()


async def test_verify_held_false_after_release(pg_engine) -> None:
    acct = f"wl-{uuid4().hex[:8]}"
    lock = WriterLock(database_url=_url(pg_engine), key=derive_lock_key(acct, "prod"))
    await lock.acquire()
    await lock.release()
    assert await lock.verify_held() is False
```
(`pg_engine` fixture is in `tests/conftest.py:54-71`, `postgres:16-alpine` testcontainer, auto-skips without testcontainers.)

- [ ] **Step 3: Run it — must FAIL**

Run: `uv run pytest tests/integration/test_writer_lock.py -q`
Expected: FAIL — `ModuleNotFoundError: bfx_funding_bot.core.writer_lock`.

- [ ] **Step 4: Implement `core/writer_lock.py`**

```python
from __future__ import annotations

import hashlib
import logging

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

from bfx_funding_bot.core.db import _prepare_engine_kwargs
from bfx_funding_bot.core.errors import WriterLockUnacquired

log = logging.getLogger(__name__)

_NAMESPACE = "bfx-writer"


def derive_lock_key(account_id: str, env: str) -> int:
    """Deterministic, process-independent signed int64 advisory-lock key.
    NOTE: never use builtin hash() — it is salted per process (PYTHONHASHSEED)."""
    digest = hashlib.blake2b(f"{_NAMESPACE}:{account_id}:{env}".encode(), digest_size=8).digest()
    return int.from_bytes(digest, "big", signed=True)


class WriterLock:
    """Holds a session-scoped pg advisory lock on a DEDICATED NullPool connection
    (never recycled, so the session — and thus the lock — survives for the daemon
    lifetime). verify_held() is the authoritative per-submit check."""

    def __init__(self, *, database_url: str, key: int) -> None:
        self._database_url = database_url
        self._key = key
        self._engine: AsyncEngine | None = None
        self._conn: AsyncConnection | None = None

    async def acquire(self) -> None:
        kwargs = _prepare_engine_kwargs(self._database_url)
        url = kwargs["url"]
        connect_args = kwargs.get("connect_args", {})
        self._engine = create_async_engine(url, poolclass=NullPool, connect_args=connect_args)
        self._conn = await self._engine.connect()
        got = (await self._conn.execute(text("SELECT pg_try_advisory_lock(:k)"), {"k": self._key})).scalar()
        if not got:
            await self._close()
            raise WriterLockUnacquired(f"another live writer holds advisory lock key={self._key}")
        log.info("writer_lock_acquired key=%s", self._key)

    async def verify_held(self) -> bool:
        """Live check: a successful query proves the session (and its session-scoped
        advisory lock) is still alive. Any failure ⇒ not held ⇒ caller fails closed."""
        if self._conn is None:
            return False
        try:
            await self._conn.execute(text("SELECT 1"))
            return True
        except Exception:  # noqa: BLE001 — any connection error means lock lost
            log.error("writer_lock_verify_failed key=%s", self._key)
            return False

    async def refresh(self) -> bool:
        """Background recovery: if the lock was lost, drop the dead conn and try to
        re-acquire. Returns the resulting held state."""
        if await self.verify_held():
            return True
        await self._close()
        try:
            await self.acquire()
            log.warning("writer_lock_reacquired key=%s", self._key)
            return True
        except WriterLockUnacquired:
            return False
        except Exception:  # noqa: BLE001
            return False

    async def release(self) -> None:
        await self._close()

    async def _close(self) -> None:
        if self._conn is not None:
            try:
                await self._conn.close()
            except Exception:  # noqa: BLE001
                pass
            self._conn = None
        if self._engine is not None:
            try:
                await self._engine.dispose()
            except Exception:  # noqa: BLE001
                pass
            self._engine = None
```
If `_prepare_engine_kwargs` for a sqlite URL returns no `connect_args`, `.get(..., {})` covers it (this module is only ever live-wired on Postgres, but keep it safe).

- [ ] **Step 5: Run it — must PASS (needs Docker)**

Run: `uv run pytest tests/integration/test_writer_lock.py -q`
Expected: PASS (4 tests). If Docker isn't running, they SKIP — start Docker and re-run before commit.

- [ ] **Step 6: Full gate + commit**

Run: `uv run ruff check && uv run mypy --strict src && uv run pytest -m "not integration and not gate" -q`
```bash
git add src/bfx_funding_bot/core/errors.py src/bfx_funding_bot/core/writer_lock.py tests/integration/test_writer_lock.py
git commit -m "✨ Feat: WriterLock (dedicated-conn session advisory lock) + WriterLockUnacquired"
```

---

### Task 4: A1 — daemon wiring (acquire, guard, liveness, exit code)

**Files:**
- Modify: `src/bfx_funding_bot/modules/marketfeed/daemon.py` (fields ~187-192, build_daemon live block ~834, acquire, guards assembly ~763, TaskGroup ~212, new loop ~313, main() ~1188)
- Test: `tests/modules/marketfeed/test_daemon_wiring.py` (sqlite construction)
- Test: `tests/integration/test_writer_lock_daemon.py` (new — fail-closed + liveness on real PG)

> All edits are guarded so paper/shadow and the existing ~8 wiring + 3 integration tests stay green (defaults None/inert). Use the kit's verified anchors; re-grep context before editing (line numbers may drift).

- [ ] **Step 1: Write the failing sqlite wiring test** (append to `test_daemon_wiring.py`, reusing its verified live-env recipe at lines 101-156)

```python
async def test_canary_build_wires_writer_lock_and_guard(tmp_path, httpx_mock, monkeypatch) -> None:
    _set_canary_env(monkeypatch, tmp_path)  # the existing helper used by the canary wiring tests
    httpx_mock.add_response(
        url=re.compile(r"https://api-pub\.bitfinex\.com/.*"),
        method="GET", status_code=200, json=[], is_reusable=True, is_optional=True,
    )
    daemon = await build_daemon(cells_yaml_path=_write_cells_yaml(tmp_path), skip_ws=True)
    assert daemon.writer_lock is not None
    assert any(g.name == "writer_lock" for g in daemon.safety_chain.guards)
```
If the file's canary tests inline the env dict rather than using a `_set_canary_env` helper, copy that exact env block (kit lists every var: `BFX_PHASE`, `BFX_DEPLOYMENT_ENV`, `BFX_SAFETY_CONFIG=str(Path(__file__).parents[3]/"configs"/"safety.canary.yaml")`, `BFX_EXECUTOR=bitfinex_live`, `BFX_WS_CLIENT_ENABLED=true`, `BFX_API_KEY/SECRET="test_key"/"test_secret"`, `BFX_ALLOCATION_CAP_USDT=500`, `BFX_ACCOUNT_ID="default"`, `BFX_HEALTHZ_PORT="0"`, `BFX_SERVICE_VERSION="test-sha"`, `DATABASE_URL=sqlite+aiosqlite:///{tmp_path}/x.db`). Match the property names the daemon exposes (`daemon.writer_lock`, `daemon.safety_chain.guards`) — confirm `safety_chain` is the Daemon attr holding the `SafetyGuardChain` (kit: chain built at daemon.py:810-818).
> Note: on sqlite, `WriterLock.acquire()` is NOT called in the test path because acquire only runs for Postgres live wiring — but `build_daemon`'s live block DOES call acquire. To keep this sqlite test green, the acquire MUST be skipped when the URL is sqlite. See Step 4's guard: only construct+acquire the WriterLock when the engine is Postgres. Assert construction, not a live lock.

- [ ] **Step 2: Run it — must FAIL**

Run: `uv run pytest tests/modules/marketfeed/test_daemon_wiring.py -k writer_lock -q`
Expected: FAIL — `AttributeError: 'Daemon' object has no attribute 'writer_lock'`.

- [ ] **Step 3: Add Daemon fields** — after `daemon.py:191` (`admin_token: str | None = None`), before `_stop_event`:

```python
    writer_lock: WriterLock | None = None
```
Add the import at the top of daemon.py: `from bfx_funding_bot.core.writer_lock import WriterLock, derive_lock_key`.

- [ ] **Step 4: Construct + acquire in `build_daemon`'s live block** — inside `if not spec.is_simulated:` (anchor `daemon.py:834`), BEFORE `boot_recovery = BootRecovery(...)`. Only acquire on Postgres (keeps sqlite wiring tests green):

```python
        writer_lock: WriterLock | None = None
        if config.database_url.startswith(("postgres", "postgresql")):
            writer_lock = WriterLock(
                database_url=config.database_url,
                key=derive_lock_key(account_id, env_str),
            )
            await writer_lock.acquire()  # raises WriterLockUnacquired if another writer holds it
```
(`account_id` is the local at daemon.py:680; `env_str = config.deployment_environment.value` at daemon.py:697.) Then pass `writer_lock=writer_lock` into the `return Daemon(...)` kwargs (anchor daemon.py:1155-1185).

- [ ] **Step 5: Wire the guard** — in the guards assembly (anchor `daemon.py:763-765`, after the hard guards), append live-only:

```python
    if writer_lock is not None:
        guards.append(WriterLockGuard(lock=writer_lock))
```
Add import: `from bfx_funding_bot.modules.execution.safety.hard_guards import WriterLockGuard` (the file already imports `ManualKillGuard` from there — extend it). `writer_lock` is in scope here only if Step 4 ran in the same function before this block; confirm ordering (the live block at 834 is AFTER the guards block at 763 — so MOVE the `writer_lock` construction to just before the guards block at ~762, still inside `build_daemon`, and reference it in both the guards append and the live block / Daemon return). Simplest: construct `writer_lock` once near the top of the live-relevant section and reuse.

- [ ] **Step 6: Add the liveness loop method** — alongside `_db_keepalive_loop` (anchor `daemon.py:313-320`):

```python
    async def _writer_lock_liveness_loop(self) -> None:
        assert self.writer_lock is not None
        while not self._stop_event.is_set():
            if await self.writer_lock.refresh():
                self.probe.record_heartbeat("writer_lock")
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=30.0)
            except TimeoutError:
                pass
        log.info("sub_task_exit name=writer_lock")
```

- [ ] **Step 7: Register the loop in the TaskGroup** — after `daemon.py:217` (`db_keepalive` line):

```python
            if self.writer_lock is not None:
                tg.create_task(self._writer_lock_liveness_loop(), name="writer_lock")
```

- [ ] **Step 8: Release on shutdown + exit code in `main()`** — in the `_run()` `finally` cleanup (anchor daemon.py:1248-1256), add `if daemon.writer_lock is not None: await daemon.writer_lock.release()`. In `main()` (anchor daemon.py:1188-1197), add a catch BEFORE the `except ValueError`:

```python
    except WriterLockUnacquired:
        log.error("writer_lock_fatal — another live writer holds the lock; refusing to start")
        sys.exit(EXIT_CODE_WRITER_LOCKED)
```
Add imports: `from bfx_funding_bot.core.errors import WriterLockUnacquired, EXIT_CODE_WRITER_LOCKED`. (Note `WriterLockUnacquired` raised in `build_daemon` at `_run():1201` propagates out of `_run` to `main`'s `asyncio.run(_run())` — verify `main` wraps that call in try/except.)

- [ ] **Step 9: Run the sqlite wiring test — must PASS**

Run: `uv run pytest tests/modules/marketfeed/test_daemon_wiring.py -q`
Expected: all PASS (new test + existing ones; sqlite skips acquire so construction asserts hold).

- [ ] **Step 10: Write + run the real-PG fail-closed/liveness integration test** `tests/integration/test_writer_lock_daemon.py`

```python
from __future__ import annotations

import asyncio
from uuid import uuid4

import pytest

from bfx_funding_bot.core.writer_lock import WriterLock, derive_lock_key
from bfx_funding_bot.modules.execution.safety.hard_guards import WriterLockGuard

pytestmark = pytest.mark.integration


def _url(pg_engine) -> str:
    return pg_engine.url.render_as_string(hide_password=False)


async def test_guard_fails_closed_when_lock_lost(pg_engine) -> None:
    lock = WriterLock(database_url=_url(pg_engine), key=derive_lock_key(f"d-{uuid4().hex[:8]}", "prod"))
    await lock.acquire()
    guard = WriterLockGuard(lock=lock)
    # held -> allowed
    from tests.integration._guard_helpers import make_decision, make_ctx  # or inline minimal ctx/decision
    ok = await guard.evaluate(make_decision(), make_ctx())
    assert ok.allowed is True
    # lose the lock (release the underlying session) -> guard now fails closed
    await lock.release()
    blocked = await guard.evaluate(make_decision(), make_ctx())
    assert blocked.allowed is False
    assert blocked.guard_name == "writer_lock"


async def test_refresh_reacquires_after_transient_loss(pg_engine) -> None:
    key = derive_lock_key(f"d-{uuid4().hex[:8]}", "prod")
    lock = WriterLock(database_url=_url(pg_engine), key=key)
    await lock.acquire()
    await lock._close()           # simulate a dropped connection (lock released server-side)
    assert await lock.refresh() is True   # nobody else holds it -> re-acquire succeeds
    assert await lock.verify_held() is True
    await lock.release()
```
For `make_decision()`/`make_ctx()`, reuse the helpers from `tests/modules/execution/safety/test_hard_guards.py` (import them or copy the 2 tiny builders inline). Run: `uv run pytest tests/integration/test_writer_lock_daemon.py -q` → Expected: PASS.

- [ ] **Step 11: Full gate (unit) + integration + commit**

Run: `uv run ruff check && uv run mypy --strict src && uv run pytest -m "not integration and not gate" -q && uv run pytest -m integration -k "writer_lock" -q`
```bash
git add src/bfx_funding_bot/modules/marketfeed/daemon.py tests/modules/marketfeed/test_daemon_wiring.py tests/integration/test_writer_lock_daemon.py
git commit -m "✨ Feat: wire WriterLock into daemon (boot acquire, fail-closed guard, liveness, exit 75)"
```

---

### Task 5: A2 — alembic migration advisory lock + timeouts

**Files:**
- Modify: `alembic/env.py:43-52` (`do_run_migrations`)
- Test: `tests/integration/test_alembic_migration_lock.py` (new)

- [ ] **Step 1: Write the failing integration test** `tests/integration/test_alembic_migration_lock.py`

```python
from __future__ import annotations

import pytest
from sqlalchemy import text

pytestmark = pytest.mark.integration

_MIGRATE_KEY = -1  # placeholder; replaced by the real derived key in Step 3's module constant


async def test_do_run_migrations_sets_timeouts_and_takes_lock(pg_engine) -> None:
    # Drive do_run_migrations against a real connection and assert it SET the
    # timeouts and acquired+released the migration advisory lock.
    import alembic.env as aenv

    sync_url = pg_engine.url.render_as_string(hide_password=False).replace("+asyncpg", "+psycopg")
    from sqlalchemy import create_engine

    eng = create_engine(sync_url)
    try:
        with eng.connect() as conn:
            captured: list[str] = []
            real_exec = conn.exec_driver_sql

            def spy(sql, *a, **k):
                captured.append(str(sql))
                return real_exec(sql, *a, **k)

            conn.exec_driver_sql = spy  # type: ignore[method-assign]
            # minimal: just assert the lock+timeout statements are issued in order
            aenv.do_run_migrations(conn)
        joined = " | ".join(captured)
        assert "lock_timeout" in joined
        assert "statement_timeout" in joined
        assert "pg_try_advisory_lock" in joined
        assert "pg_advisory_unlock" in joined
    finally:
        eng.dispose()
```
> `alembic.env` instantiates `Settings()` at import (needs `DATABASE_URL`). Set it for the test session: add `monkeypatch.setenv("DATABASE_URL", sync_url)` BEFORE `import alembic.env as aenv`, or use the `pg_engine` URL. Adjust to the file's actual `do_run_migrations` signature (sync `Connection`). If driving the real `context.run_migrations()` is too heavy, the spy-on-`exec_driver_sql` approach above asserts the SQL emission without running every migration.

- [ ] **Step 2: Run it — must FAIL**

Run: `uv run pytest tests/integration/test_alembic_migration_lock.py -q`
Expected: FAIL — assertion (no `lock_timeout`/`pg_try_advisory_lock` emitted yet).

- [ ] **Step 3: Edit `alembic/env.py`** — add a module constant + wrap `do_run_migrations`

At module level (near the top, after imports), add the migration lock key (distinct namespace from the daemon writer lock):
```python
import hashlib

_MIGRATE_LOCK_KEY = int.from_bytes(
    hashlib.blake2b(b"bfx-migrate:alembic", digest_size=8).digest(), "big", signed=True
)
```
Replace `do_run_migrations` (lines 43-52) with:
```python
def do_run_migrations(connection: Connection) -> None:
    connection.exec_driver_sql("SET lock_timeout = '5s'")
    connection.exec_driver_sql("SET statement_timeout = '60s'")
    got = connection.exec_driver_sql(
        f"SELECT pg_try_advisory_lock({_MIGRATE_LOCK_KEY})"
    ).scalar()
    if not got:
        raise RuntimeError("another migration is already running (advisory lock held)")
    try:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            compare_server_default=True,
            include_object=include_object,
        )
        with context.begin_transaction():
            context.run_migrations()
    finally:
        connection.exec_driver_sql(f"SELECT pg_advisory_unlock({_MIGRATE_LOCK_KEY})")
```
Leave `run_migrations_offline` untouched (no live connection).

- [ ] **Step 4: Run it — must PASS; then prove migrations still apply end-to-end**

Run: `uv run pytest tests/integration/test_alembic_migration_lock.py -q`
Then verify a real upgrade still works against the testcontainer (sanity): `uv run pytest -m integration -k "alembic or migration" -q`
Expected: PASS.

- [ ] **Step 5: Full gate + commit**

Run: `uv run ruff check && uv run mypy --strict src && uv run pytest -m "not integration and not gate" -q`
```bash
git add alembic/env.py tests/integration/test_alembic_migration_lock.py
git commit -m "✨ Feat: serialize alembic migrations on advisory lock + lock/statement timeouts"
```

---

### Task 6: A4 — fix stale region/cap notes (housekeeping, no test)

**Files:**
- Modify: `scripts/deploy-koyeb.sh:52`, `docs/deploy/koyeb-paper.md:81`, `docs/deploy/koyeb-canary.md` (cap reference)

- [ ] **Step 1: Fix the script region comment** — `scripts/deploy-koyeb.sh:52` `REGION="sin"  # match Neon ap-southeast-1` → keep `REGION="sin"` (Koyeb stays Singapore until retired) but update the comment to note the VM successor runs Tokyo:
```bash
REGION="sin"                 # Koyeb co-located w/ Neon ap-southeast-1 (<5ms). NOTE: the Oracle VM successor runs Tokyo (~60-80ms, verified non-material — DB off the order-critical path).
```

- [ ] **Step 2: Fix the paper doc** — `docs/deploy/koyeb-paper.md:81` the "sin Singapore, same region as Neon ap-southeast-1, RTT < 5ms" note: append `(Koyeb only; the Oracle VM migration target is Tokyo — see migration spec 2026-05-31.)`

- [ ] **Step 3: Fix the canary doc cap** — in `docs/deploy/koyeb-canary.md`, update any `150`/`450` allocation-cap references to **570** (the live authoritative value), matching `safety.canary.yaml`'s re-derived 57 loss threshold.

- [ ] **Step 4: Commit**

```bash
git add scripts/deploy-koyeb.sh docs/deploy/koyeb-paper.md docs/deploy/koyeb-canary.md
git commit -m "🔧 Chore: correct stale region/cap notes for the Oracle VM migration"
```

---

## Self-Review

- **Spec coverage:** A1 advisory lock → Tasks 2,3,4 (guard / helper / wiring); A1 fail-closed per-submit → Task 2+4 (live verify in guard); A2 alembic lock+timeouts → Task 5; A3 loss limiter → Task 1; A4 docs → Task 6. All spec §A items mapped. (Compose split is Plan B, per decision 9.)
- **Placeholder scan:** no TBD/TODO; the one literal placeholder `_MIGRATE_KEY = -1` in Task 5 Step 1's test is replaced by the real constant in Step 3 and the test only asserts the emitted SQL strings — flagged inline.
- **Type consistency:** `WriterLock` (database_url, key) ctor + `acquire`/`verify_held`/`refresh`/`release` used identically across Tasks 3/4; `WriterLockGuard(lock=...)` + `.name=="writer_lock"` consistent Tasks 2/4; `derive_lock_key(account_id, env)` consistent; `WriterLockUnacquired`/`EXIT_CODE_WRITER_LOCKED` defined Task 3, used Task 4.
- **Known follow-ups for the implementer to verify against live code (line numbers may drift):** confirm `safety_chain` is the Daemon attribute name; confirm `main()` wraps `asyncio.run(_run())` in try/except; confirm the canary-env helper name in `test_daemon_wiring.py`. Re-grep the kit's anchors before each edit.
