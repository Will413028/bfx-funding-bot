# Admin Smoke-Test Endpoint Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement `POST /admin/smoke-test` endpoint + boot-time auto smoke for Phase 4.3 executor middleware chain deploy-time verification.

**Architecture:** SmokeRunner core logic shared by boot-time path (in `_run()` pre-TaskGroup, runs L2 in-process only) and HTTP endpoint (mounted on daemon healthz uvicorn, runs L3 with Axiom round-trip). Synthetic events tagged with `account_id="smoke_test"` for full isolation from prod state; ledger handlers add account_id guard; bus gains `subscription()` async context manager for scoped subscriptions; new `AxiomEventQueryAdapter` implements APL query for L3 round-trip without touching the ledger replay stub.

**Tech Stack:** Python 3.13, FastAPI, asyncio, httpx, pytest + pytest-asyncio (auto mode).

**Spec:** `docs/superpowers/specs/2026-05-22-admin-smoke-test-design.md`

**All commands run from `backend_py/` directory** (per `bfx-funding-bot/CLAUDE.md`). Use `cd backend_py && uv run <cmd>`.

---

## File Structure

**New files:**
- `backend_py/src/bfx_funding_bot/modules/admin/__init__.py`
- `backend_py/src/bfx_funding_bot/modules/admin/smoke_runner.py` — `EventRecorder` + `SmokeRunner` + `SmokeResult` + `SmokeAssertionError` + `_SMOKE_LOCK`
- `backend_py/src/bfx_funding_bot/modules/admin/axiom_query.py` — `AxiomEventQueryAdapter`
- `backend_py/src/bfx_funding_bot/modules/admin/router.py` — FastAPI APIRouter for `POST /admin/smoke-test`
- `backend_py/tests/modules/admin/__init__.py`
- `backend_py/tests/modules/admin/test_event_recorder.py`
- `backend_py/tests/modules/admin/test_axiom_query.py`
- `backend_py/tests/modules/admin/test_smoke_runner.py`
- `backend_py/tests/modules/admin/test_router.py`
- `backend_py/tests/modules/admin/test_integration_full_chain.py`
- `backend_py/tests/modules/admin/test_integration_http.py`

**Modified files:**
- `backend_py/src/bfx_funding_bot/modules/execution/bus.py` — add `subscription()` context manager
- `backend_py/src/bfx_funding_bot/modules/execution/ledger.py` — add account_id guard to 3 handlers
- `backend_py/src/bfx_funding_bot/modules/marketfeed/healthz.py` — extend `make_app()` + `run_healthz_server()` signatures
- `backend_py/src/bfx_funding_bot/modules/marketfeed/daemon.py` — Daemon field, build_daemon wiring, _healthz_server_loop signature, _run boot smoke try/except
- `backend_py/tests/modules/execution/test_bus.py` — add subscription tests
- `backend_py/tests/modules/execution/test_ledger.py` — add account_id guard tests

---

## Task 1: Bus `subscription()` async context manager

**Files:**
- Modify: `backend_py/src/bfx_funding_bot/modules/execution/bus.py`
- Modify: `backend_py/tests/modules/execution/test_bus.py`

- [ ] **Step 1: Write failing tests**

Append to `backend_py/tests/modules/execution/test_bus.py`:

```python
async def test_subscription_context_subscribes_on_enter_unsubscribes_on_exit() -> None:
    bus = DomainEventBus()
    seen: list[ReservationClaimed] = []

    async def h(event: ReservationClaimed) -> None:
        seen.append(event)

    async with bus.subscription(ReservationClaimed, h):
        await bus.publish(_make_claim())
    # After context exit, handler must be removed
    await bus.publish(_make_claim())
    assert len(seen) == 1


async def test_subscription_unsubscribes_on_exception_in_body() -> None:
    bus = DomainEventBus()
    seen: list[ReservationClaimed] = []

    async def h(event: ReservationClaimed) -> None:
        seen.append(event)

    with pytest.raises(RuntimeError, match="boom"):
        async with bus.subscription(ReservationClaimed, h):
            await bus.publish(_make_claim())
            raise RuntimeError("boom")
    # Even on exception, unsubscribe ran
    await bus.publish(_make_claim())
    assert len(seen) == 1


async def test_subscription_allows_resubscribe_after_exit() -> None:
    bus = DomainEventBus()

    async def h(event: ReservationClaimed) -> None: ...

    async with bus.subscription(ReservationClaimed, h):
        pass
    # Should not raise — handler already removed
    async with bus.subscription(ReservationClaimed, h):
        pass


async def test_subscription_nested_with_existing_subscribe_coexists() -> None:
    bus = DomainEventBus()
    permanent: list[ReservationClaimed] = []
    scoped: list[ReservationClaimed] = []

    async def p_handler(e: ReservationClaimed) -> None:
        permanent.append(e)

    async def s_handler(e: ReservationClaimed) -> None:
        scoped.append(e)

    bus.subscribe(ReservationClaimed, p_handler)
    async with bus.subscription(ReservationClaimed, s_handler):
        await bus.publish(_make_claim())
    await bus.publish(_make_claim())
    assert len(permanent) == 2
    assert len(scoped) == 1
```

- [ ] **Step 2: Run tests, verify they fail**

Run: `cd backend_py && uv run pytest tests/modules/execution/test_bus.py -v -k subscription`
Expected: 4 FAIL with `AttributeError: 'DomainEventBus' object has no attribute 'subscription'`.

- [ ] **Step 3: Implement `subscription()`**

Edit `backend_py/src/bfx_funding_bot/modules/execution/bus.py`. Add imports + method:

```python
# Top of file (additions):
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

# Inside DomainEventBus class (append after publish method):
    @asynccontextmanager
    async def subscription(
        self, event_type: type, handler: EventHandler,
    ) -> AsyncIterator[None]:
        """Scoped subscription — auto-unsubscribe on exit (incl. exception path).

        Use for ephemeral subscribers (smoke recorder, test spies). Permanent
        wirings (ledger, axiom_sink) keep using subscribe().
        """
        self.subscribe(event_type, handler)
        try:
            yield
        finally:
            handlers = self._handlers.get(event_type, [])
            if handler in handlers:
                handlers.remove(handler)
```

- [ ] **Step 4: Run tests, verify pass + full bus test suite still green**

Run: `cd backend_py && uv run pytest tests/modules/execution/test_bus.py -v`
Expected: all PASS (existing 7 + new 4 = 11).

- [ ] **Step 5: Commit**

```bash
cd /Users/will/second-brain/projects/startup/bfx-funding-bot
git add backend_py/src/bfx_funding_bot/modules/execution/bus.py \
        backend_py/tests/modules/execution/test_bus.py
git commit -m "✨ Feat: bus subscription() async context manager for scoped subscribers"
```

---

## Task 2: Ledger account_id guard on 3 handlers

**Files:**
- Modify: `backend_py/src/bfx_funding_bot/modules/execution/ledger.py:62-86` (3 handler methods)
- Modify: `backend_py/tests/modules/execution/test_ledger.py`

- [ ] **Step 1: Write failing tests**

Append to `backend_py/tests/modules/execution/test_ledger.py`:

```python
from bfx_funding_bot.modules.execution.events import (
    OrderFilled,
    ReservationClaimed,
    ReservationReleased,
)


async def test_handler_skips_foreign_account_id_claim() -> None:
    ledger = PaperPositionLedger(account_id="default")
    foreign = ReservationClaimed(
        cid=1, venue_offer_id="x", size_usdt=Decimal("100"),
        signal_correlation_id=uuid4(), account_id="smoke_test", is_simulated=True,
    )
    await ledger.on_reservation_claimed(foreign)
    assert ledger.current_exposure() == Decimal("0")


async def test_handler_skips_foreign_account_id_fill() -> None:
    ledger = PaperPositionLedger(account_id="default")
    foreign = OrderFilled(
        cid=1, venue_offer_id="x", credit_id=None, size_usdt=Decimal("100"),
        fill_rate=0.0001, signal_correlation_id=uuid4(),
        account_id="smoke_test", is_simulated=True,
    )
    await ledger.on_order_filled(foreign)
    assert ledger.realized_exposure() == Decimal("0")
    assert ledger.current_exposure() == Decimal("0")
    assert ledger.replay_floor_hit_count == 0


async def test_handler_skips_foreign_account_id_release() -> None:
    ledger = PaperPositionLedger(account_id="default")
    foreign = ReservationReleased(
        cid=1, venue_offer_id="x", size_usdt=Decimal("100"),
        reason="venue_cancel", signal_correlation_id=uuid4(),
        account_id="smoke_test", is_simulated=True,
    )
    await ledger.on_reservation_released(foreign)
    assert ledger.current_exposure() == Decimal("0")
    assert ledger.replay_floor_hit_count == 0


async def test_handler_processes_matching_account_id_unchanged() -> None:
    """Regression: matching account_id still updates counters as before."""
    ledger = PaperPositionLedger(account_id="default")
    matching = ReservationClaimed(
        cid=1, venue_offer_id="x", size_usdt=Decimal("100"),
        signal_correlation_id=uuid4(), account_id="default", is_simulated=True,
    )
    await ledger.on_reservation_claimed(matching)
    assert ledger.current_exposure() == Decimal("100")
```

Note: existing imports at top of test file already include `Decimal` and `uuid4`.

- [ ] **Step 2: Run tests, verify failures**

Run: `cd backend_py && uv run pytest tests/modules/execution/test_ledger.py -v -k account_id`
Expected: 3 FAIL with AssertionError (current handlers update counters regardless of account_id); 1 PASS (matching-account regression).

- [ ] **Step 3: Add account_id guards**

Edit `backend_py/src/bfx_funding_bot/modules/execution/ledger.py`:

```python
# Replace lines 62-86 (the 3 handlers) with:
    async def on_reservation_claimed(self, event: ReservationClaimed) -> None:
        if event.account_id != self.account_id:
            return
        self._reserved += event.size_usdt

    async def on_order_filled(self, event: OrderFilled) -> None:
        if event.account_id != self.account_id:
            return
        delta = min(self._reserved, event.size_usdt)
        self._reserved -= delta
        if delta < event.size_usdt:
            self.replay_floor_hit_count += 1
            log.warning(
                "order_filled_without_claim cid=%d offer=%s expected=%.2f applied=%.2f",
                event.cid, event.venue_offer_id,
                float(event.size_usdt), float(delta),
            )
        self._realized += event.size_usdt

    async def on_reservation_released(self, event: ReservationReleased) -> None:
        if event.account_id != self.account_id:
            return
        delta = min(self._reserved, event.size_usdt)
        self._reserved -= delta
        if delta < event.size_usdt:
            self.replay_floor_hit_count += 1
            log.warning(
                "reservation_release_without_claim cid=%d offer=%s expected=%.2f applied=%.2f reason=%s",
                event.cid, event.venue_offer_id,
                float(event.size_usdt), float(delta), event.reason,
            )
```

- [ ] **Step 4: Run tests, verify pass + full ledger suite green**

Run: `cd backend_py && uv run pytest tests/modules/execution/test_ledger.py tests/modules/execution/test_ledger_reserved_realized.py tests/modules/execution/test_ledger_replay_event_dispatch.py -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
cd /Users/will/second-brain/projects/startup/bfx-funding-bot
git add backend_py/src/bfx_funding_bot/modules/execution/ledger.py \
        backend_py/tests/modules/execution/test_ledger.py
git commit -m "✨ Feat: ledger handlers add account_id guard for multi-tenant + smoke isolation"
```

---

## Task 3: EventRecorder + admin module scaffold

**Files:**
- Create: `backend_py/src/bfx_funding_bot/modules/admin/__init__.py`
- Create: `backend_py/src/bfx_funding_bot/modules/admin/smoke_runner.py`
- Create: `backend_py/tests/modules/admin/__init__.py`
- Create: `backend_py/tests/modules/admin/test_event_recorder.py`

- [ ] **Step 1: Write failing tests**

Create `backend_py/tests/modules/admin/__init__.py` (empty file).

Create `backend_py/tests/modules/admin/test_event_recorder.py`:

```python
"""EventRecorder — spy/recorder pattern for smoke verification."""
from __future__ import annotations

from dataclasses import dataclass

from bfx_funding_bot.modules.admin.smoke_runner import EventRecorder


@dataclass
class _FakeEvent:
    account_id: str
    payload: str


async def test_recorder_captures_matching_account_id() -> None:
    recorder = EventRecorder(account_id="smoke_test")
    e = _FakeEvent(account_id="smoke_test", payload="x")
    await recorder.record(e)
    assert recorder.events == [e]


async def test_recorder_skips_foreign_account_id() -> None:
    recorder = EventRecorder(account_id="smoke_test")
    await recorder.record(_FakeEvent(account_id="default", payload="x"))
    assert recorder.events == []


async def test_recorder_preserves_order() -> None:
    recorder = EventRecorder(account_id="smoke_test")
    events = [_FakeEvent(account_id="smoke_test", payload=str(i)) for i in range(5)]
    for e in events:
        await recorder.record(e)
    assert recorder.events == events


async def test_recorder_skips_event_without_account_id_attribute() -> None:
    """Defensive: a malformed event without account_id should be ignored, not crash."""
    recorder = EventRecorder(account_id="smoke_test")
    await recorder.record(object())  # no account_id attr
    assert recorder.events == []
```

- [ ] **Step 2: Run tests, verify ImportError fail**

Run: `cd backend_py && uv run pytest tests/modules/admin/test_event_recorder.py -v`
Expected: `ModuleNotFoundError: No module named 'bfx_funding_bot.modules.admin'`.

- [ ] **Step 3: Create admin module + EventRecorder**

Create `backend_py/src/bfx_funding_bot/modules/admin/__init__.py` (empty file).

Create `backend_py/src/bfx_funding_bot/modules/admin/smoke_runner.py`:

```python
"""Admin smoke-test runner — synthesize paper offer through prod chain.

Boot-time auto + admin endpoint share SmokeRunner core (this file). Synthetic
events tagged account_id="smoke_test"; prod ledger filters them out via
account_id guard (modules/execution/ledger.py).

Design: docs/superpowers/specs/2026-05-22-admin-smoke-test-design.md
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

# Module-level single-flight lock. Both boot smoke and endpoint share it.
# Router pre-checks .locked() to return 409; SmokeRunner uses async-with to
# serialize (no contention expected because router pre-check filters).
_SMOKE_LOCK = asyncio.Lock()


@dataclass
class EventRecorder:
    """Spy that captures bus events matching a given account_id."""
    account_id: str
    events: list[Any] = field(default_factory=list)

    async def record(self, event: Any) -> None:
        if getattr(event, "account_id", None) == self.account_id:
            self.events.append(event)
```

- [ ] **Step 4: Run tests, verify pass**

Run: `cd backend_py && uv run pytest tests/modules/admin/test_event_recorder.py -v`
Expected: 4 PASS.

- [ ] **Step 5: Commit**

```bash
cd /Users/will/second-brain/projects/startup/bfx-funding-bot
git add backend_py/src/bfx_funding_bot/modules/admin/__init__.py \
        backend_py/src/bfx_funding_bot/modules/admin/smoke_runner.py \
        backend_py/tests/modules/admin/__init__.py \
        backend_py/tests/modules/admin/test_event_recorder.py
git commit -m "✨ Feat: admin module scaffold + EventRecorder for smoke verification"
```

---

## Task 4: AxiomEventQueryAdapter

**Files:**
- Create: `backend_py/src/bfx_funding_bot/modules/admin/axiom_query.py`
- Create: `backend_py/tests/modules/admin/test_axiom_query.py`

- [ ] **Step 1: Write failing tests**

Create `backend_py/tests/modules/admin/test_axiom_query.py`:

```python
"""AxiomEventQueryAdapter — APL query for L3 round-trip verification."""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import httpx
import pytest

from bfx_funding_bot.modules.admin.axiom_query import AxiomEventQueryAdapter


def _tabular_response(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Build an Axiom APL tabular response from a list of row dicts."""
    if not rows:
        return {"tables": []}
    fields = list(rows[0].keys())
    columns = [[r[f] for r in rows] for f in fields]
    return {
        "tables": [
            {"fields": [{"name": f} for f in fields], "columns": columns},
        ],
    }


async def test_query_returns_parsed_rows() -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["body"] = request.read().decode()
        return httpx.Response(200, json=_tabular_response([
            {"_time": "2026-05-22T10:00:00Z", "event_type": "reservation_claimed",
             "account_id": "smoke_test", "payload": {"size_usdt": 1.0}},
            {"_time": "2026-05-22T10:00:01Z", "event_type": "order_fill",
             "account_id": "smoke_test", "payload": {"fill_size_usdt": 1.0}},
        ]))

    transport = httpx.MockTransport(handler)
    adapter = AxiomEventQueryAdapter(
        api_key="key", dataset="ds", base_url="https://api.axiom.co",
    )
    adapter._http = httpx.AsyncClient(transport=transport, base_url="https://api.axiom.co",
                                       headers={"Authorization": "Bearer key"})

    rows = await adapter.query_order_events(
        "smoke_test", datetime(2026, 5, 22, 9, 0, 0, tzinfo=UTC),
    )
    assert len(rows) == 2
    assert rows[0]["event_type"] == "reservation_claimed"
    assert rows[1]["event_type"] == "order_fill"
    # APL string check
    assert "smoke_test" in captured["body"]
    assert "reservation_claimed" in captured["body"]
    assert "order_fill" in captured["body"]
    await adapter.aclose()


async def test_query_empty_returns_empty_list() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"tables": []})

    transport = httpx.MockTransport(handler)
    adapter = AxiomEventQueryAdapter(api_key="key", dataset="ds")
    adapter._http = httpx.AsyncClient(transport=transport, base_url="https://api.axiom.co",
                                       headers={"Authorization": "Bearer key"})
    rows = await adapter.query_order_events("smoke_test", datetime.now(UTC))
    assert rows == []
    await adapter.aclose()


async def test_query_raises_on_auth_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "unauthorized"})

    transport = httpx.MockTransport(handler)
    adapter = AxiomEventQueryAdapter(api_key="bad", dataset="ds")
    adapter._http = httpx.AsyncClient(transport=transport, base_url="https://api.axiom.co",
                                       headers={"Authorization": "Bearer bad"})
    with pytest.raises(httpx.HTTPStatusError):
        await adapter.query_order_events("smoke_test", datetime.now(UTC))
    await adapter.aclose()
```

- [ ] **Step 2: Run tests, verify ImportError**

Run: `cd backend_py && uv run pytest tests/modules/admin/test_axiom_query.py -v`
Expected: `ModuleNotFoundError: No module named 'bfx_funding_bot.modules.admin.axiom_query'`.

- [ ] **Step 3: Implement AxiomEventQueryAdapter**

Create `backend_py/src/bfx_funding_bot/modules/admin/axiom_query.py`:

```python
"""AxiomEventQueryAdapter — APL query for smoke L3 round-trip verification.

Conforms to _AxiomQueryProtocol shape (modules/execution/ledger.py). Borrows
the httpx + APL pattern from smoke/g1.py:AxiomQueryClient. Stays separate
from daemon._AxiomQueryAdapter (4.3 stub used by ledger replay) — see spec
§9 Q1; merging happens when ledger replay switches to real query (future ADR).
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

import httpx


class AxiomEventQueryAdapter:
    def __init__(
        self,
        *,
        api_key: str,
        dataset: str,
        base_url: str = "https://api.axiom.co",
        timeout: float = 30.0,
    ) -> None:
        self._dataset = dataset
        self._http = httpx.AsyncClient(
            base_url=base_url,
            timeout=timeout,
            headers={"Authorization": f"Bearer {api_key}"},
        )

    async def query_order_events(
        self, account_id: str, since: datetime,
    ) -> list[dict[str, Any]]:
        """Return events with event_type ∈ {reservation_claimed, order_fill,
        reservation_released} for given account_id since timestamp.

        Raises httpx.HTTPStatusError on 4xx/5xx.
        """
        apl = self._build_apl(account_id, since)
        resp = await self._http.post(
            "/v1/datasets/_apl?format=tabular",
            json={"apl": apl},
        )
        resp.raise_for_status()
        return self._tabular_to_rows(resp.json())

    def _build_apl(self, account_id: str, since: datetime) -> str:
        # APL syntax: ['dataset'] | where ... | project ... | order by ...
        # _time filter uses datetime() literal; since is UTC-aware ISO format.
        return (
            f"['{self._dataset}']"
            f"\n| where ['event_type'] in ('reservation_claimed', 'order_fill', 'reservation_released')"
            f"\n  and ['account_id'] == '{account_id}'"
            f"\n  and _time > datetime({since.isoformat()})"
            f"\n| project _time, ['event_type'], ['account_id'], ['payload']"
            f"\n| order by _time asc"
        )

    @staticmethod
    def _tabular_to_rows(payload: dict[str, Any]) -> list[dict[str, Any]]:
        tables = payload.get("tables") or []
        if not tables:
            return []
        t = tables[0]
        fields = [f["name"] for f in t.get("fields", [])]
        cols = t.get("columns") or []
        if not fields or not cols:
            return []
        n_rows = len(cols[0]) if cols else 0
        return [
            {fields[ci]: cols[ci][ri] for ci in range(len(fields))}
            for ri in range(n_rows)
        ]

    async def aclose(self) -> None:
        await self._http.aclose()
```

- [ ] **Step 4: Run tests, verify pass**

Run: `cd backend_py && uv run pytest tests/modules/admin/test_axiom_query.py -v`
Expected: 3 PASS.

- [ ] **Step 5: Commit**

```bash
cd /Users/will/second-brain/projects/startup/bfx-funding-bot
git add backend_py/src/bfx_funding_bot/modules/admin/axiom_query.py \
        backend_py/tests/modules/admin/test_axiom_query.py
git commit -m "✨ Feat: AxiomEventQueryAdapter — APL query for smoke L3 round-trip"
```

---

## Task 5: SmokeRunner — L2 path (in-process chain verification)

**Files:**
- Modify: `backend_py/src/bfx_funding_bot/modules/admin/smoke_runner.py` (add SmokeRunner + SmokeResult + SmokeAssertionError + run_l2)
- Create: `backend_py/tests/modules/admin/test_smoke_runner.py`

- [ ] **Step 1: Write failing tests**

Create `backend_py/tests/modules/admin/test_smoke_runner.py`:

```python
"""SmokeRunner L2 (in-process verification) tests."""
from __future__ import annotations

import asyncio
from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import uuid4

import pytest

from bfx_funding_bot.modules.admin.smoke_runner import (
    SMOKE_ACCOUNT_ID,
    SMOKE_SIZE_USDT,
    SmokeAssertionError,
    SmokeResult,
    SmokeRunner,
)
from bfx_funding_bot.modules.execution.bus import DomainEventBus
from bfx_funding_bot.modules.execution.events import (
    OrderFilled,
    ReservationClaimed,
)
from bfx_funding_bot.modules.execution.middleware import (
    ReservationEmittingMiddleware,
)
from bfx_funding_bot.modules.execution.paper import EchoPaperExecutor
from bfx_funding_bot.modules.execution.protocols import (
    AccountContext,
    SubmittedOrder,
)
from bfx_funding_bot.modules.marketfeed.schemas import (
    DecisionPayload,
    Phase,
    StrategyName,
)


class _FakeAxiomClient:
    """Records all emit() calls in-memory. Conforms to _AxiomProtocol."""

    def __init__(self) -> None:
        self.emits: list[dict[str, Any]] = []

    async def emit(self, event: dict[str, Any]) -> None:
        self.emits.append(event)


class _FakeAxiomQuery:
    def __init__(self, events: list[dict[str, Any]]) -> None:
        self.events = events
        self.calls: list[tuple[str, datetime]] = []

    async def query_order_events(
        self, account_id: str, since: datetime,
    ) -> list[dict[str, Any]]:
        self.calls.append((account_id, since))
        return self.events


def _build_chain(bus: DomainEventBus, axiom: _FakeAxiomClient):
    """Build wrapped chain with paper executor + ReservationEmittingMiddleware."""
    paper = EchoPaperExecutor(
        axiom=axiom, phase=Phase.PAPER,
        strategy=StrategyName.RATE_PERCENTILE, cell="bfx_USDT",
    )
    return ReservationEmittingMiddleware(paper, bus=bus)


def _make_runner(executor=None, bus=None, axiom=None, axiom_query=None) -> SmokeRunner:
    bus = bus or DomainEventBus()
    axiom = axiom or _FakeAxiomClient()
    return SmokeRunner(
        executor=executor or _build_chain(bus, axiom),
        bus=bus,
        axiom_client=axiom,
        axiom_query=axiom_query or _FakeAxiomQuery([]),
        phase=Phase.PAPER,
        strategy=StrategyName.RATE_PERCENTILE,
        cell="bfx_USDT",
    )


async def test_run_l2_happy_path_returns_pass() -> None:
    bus = DomainEventBus()
    axiom = _FakeAxiomClient()
    runner = _make_runner(bus=bus, axiom=axiom)

    result = await runner.run_l2()

    assert result.status == "pass"
    assert result.level == "L2"
    assert result.checks["executor_returned_filled"] is True
    assert result.checks["events_count"] == 2
    assert result.duration_ms >= 0
    assert result.error is None


async def test_run_l2_with_failing_executor_returns_fail() -> None:
    class _RaisingExecutor:
        async def submit(self, decision, ctx):
            raise RuntimeError("boom")

    runner = _make_runner(executor=_RaisingExecutor())
    result = await runner.run_l2()

    assert result.status == "fail"
    assert result.level == "L2"
    assert "boom" in (result.error or "")


async def test_run_l2_with_non_filled_executor_returns_fail() -> None:
    class _SubmittedOnlyExecutor:
        async def submit(self, decision, ctx):
            return SubmittedOrder(cid=1, venue_offer_id="x", status="submitted", raw_response=None)

    runner = _make_runner(executor=_SubmittedOnlyExecutor())
    result = await runner.run_l2()

    assert result.status == "fail"
    assert result.checks["executor_returned_filled"] is False


async def test_run_l2_emits_smoke_account_id_events() -> None:
    """L2 should produce events tagged with smoke_test account_id."""
    bus = DomainEventBus()
    axiom = _FakeAxiomClient()
    captured: list[Any] = []

    async def spy(e):
        captured.append(e)

    bus.subscribe(ReservationClaimed, spy)
    bus.subscribe(OrderFilled, spy)

    runner = _make_runner(bus=bus, axiom=axiom)
    result = await runner.run_l2()

    assert result.status == "pass"
    assert len(captured) == 2
    assert all(e.account_id == SMOKE_ACCOUNT_ID for e in captured)
    assert all(e.is_simulated for e in captured)
    assert captured[0].size_usdt == Decimal(str(SMOKE_SIZE_USDT))


async def test_run_l2_single_flight_serializes() -> None:
    """Two concurrent run_l2 calls must serialize via _SMOKE_LOCK."""
    runner = _make_runner()
    r1, r2 = await asyncio.gather(runner.run_l2(), runner.run_l2())
    assert r1.status == "pass"
    assert r2.status == "pass"
```

- [ ] **Step 2: Run tests, verify failures**

Run: `cd backend_py && uv run pytest tests/modules/admin/test_smoke_runner.py -v`
Expected: ImportError on `SmokeRunner` / `SMOKE_ACCOUNT_ID` etc.

- [ ] **Step 3: Implement SmokeRunner + SmokeResult + run_l2**

Append to `backend_py/src/bfx_funding_bot/modules/admin/smoke_runner.py`:

```python
# ── New imports (add to existing top-of-file imports) ─────────────────
import logging
import time
from datetime import UTC, datetime
from decimal import Decimal
from typing import Literal, Protocol
from uuid import uuid4

from bfx_funding_bot.modules.execution.bus import DomainEventBus
from bfx_funding_bot.modules.execution.events import (
    OrderFilled,
    ReservationClaimed,
    ReservationReleased,
)
from bfx_funding_bot.modules.execution.protocols import (
    AccountContext,
    Credentials,
    ExecutorPort,
    SubmittedOrder,
)
from bfx_funding_bot.modules.marketfeed.schemas import (
    DecisionOutcome,
    DecisionPayload,
    Phase,
    StrategyName,
)

log = logging.getLogger(__name__)

SMOKE_ACCOUNT_ID = "smoke_test"
SMOKE_SIZE_USDT = 1.0
SMOKE_RATE = 0.0001
SMOKE_DURATION_DAYS = 2


class _AxiomProtocol(Protocol):
    async def emit(self, event: dict) -> None: ...


class _AxiomQueryProtocol(Protocol):
    async def query_order_events(
        self, account_id: str, since: datetime,
    ) -> list[dict]: ...


class SmokeAssertionError(Exception):
    """Raised when smoke chain produces unexpected state."""


@dataclass(frozen=True)
class SmokeResult:
    status: Literal["pass", "fail"]
    level: Literal["L2", "L3"]
    checks: dict[str, Any] = field(default_factory=dict)
    duration_ms: int = 0
    error: str | None = None


class SmokeRunner:
    def __init__(
        self,
        *,
        executor: ExecutorPort,
        bus: DomainEventBus,
        axiom_client: _AxiomProtocol,
        axiom_query: _AxiomQueryProtocol,
        phase: Phase,
        strategy: StrategyName,
        cell: str,
    ) -> None:
        self._executor = executor
        self._bus = bus
        self._axiom = axiom_client
        self._axiom_query = axiom_query
        self._phase = phase
        self._strategy = strategy
        self._cell = cell

    async def run_l2(self) -> SmokeResult:
        async with _SMOKE_LOCK:
            return await self._run_l2_unlocked()

    async def _run_l2_unlocked(self) -> SmokeResult:
        start = time.monotonic()
        checks: dict[str, Any] = {}
        recorder = EventRecorder(account_id=SMOKE_ACCOUNT_ID)
        try:
            before_ts, result = await self._run_chain(recorder)
            self._assert_l2(result, recorder, checks)
            duration_ms = int((time.monotonic() - start) * 1000)
            log.info("smoke_l2_passed duration_ms=%d", duration_ms)
            return SmokeResult(
                status="pass", level="L2", checks=checks, duration_ms=duration_ms,
            )
        except SmokeAssertionError as exc:
            duration_ms = int((time.monotonic() - start) * 1000)
            log.warning("smoke_l2_failed reason=%r duration_ms=%d", exc, duration_ms)
            return SmokeResult(
                status="fail", level="L2", checks=checks,
                duration_ms=duration_ms, error=str(exc),
            )
        except Exception as exc:
            duration_ms = int((time.monotonic() - start) * 1000)
            log.exception("smoke_l2_unexpected_error")
            return SmokeResult(
                status="fail", level="L2", checks=checks,
                duration_ms=duration_ms, error=f"unexpected: {exc!r}",
            )

    async def _run_chain(
        self, recorder: EventRecorder,
    ) -> tuple[datetime, SubmittedOrder]:
        """Execute the wrapped executor chain with smoke account_id.

        Returns (before_timestamp, submit_result). Bus subscriptions are
        scoped via async-with — handlers auto-unsubscribe on exit.
        """
        decision = DecisionPayload(
            decision_outcome=DecisionOutcome.POST,
            signal_correlation_id=uuid4(),
            offer_rate=SMOKE_RATE,
            offer_amount_usdt=SMOKE_SIZE_USDT,
            offer_duration_days=SMOKE_DURATION_DAYS,
        )
        smoke_ctx = AccountContext(
            account_id=SMOKE_ACCOUNT_ID,
            credentials=Credentials(api_key="smoke", api_secret="smoke"),
            allocation_cap_usdt=Decimal("1000"),
        )
        before_ts = datetime.now(UTC)
        async with (
            self._bus.subscription(ReservationClaimed, recorder.record),
            self._bus.subscription(OrderFilled, recorder.record),
            self._bus.subscription(ReservationReleased, recorder.record),
        ):
            result = await self._executor.submit(decision, smoke_ctx)
        return before_ts, result

    def _assert_l2(
        self,
        result: SubmittedOrder,
        recorder: EventRecorder,
        checks: dict[str, Any],
    ) -> None:
        checks["executor_returned_filled"] = result.status == "filled"
        checks["events_count"] = len(recorder.events)
        if not checks["executor_returned_filled"]:
            raise SmokeAssertionError(
                f"executor returned status={result.status!r}, expected 'filled'",
            )
        if len(recorder.events) != 2:
            raise SmokeAssertionError(
                f"expected 2 events, got {len(recorder.events)}",
            )
        e0, e1 = recorder.events
        if not isinstance(e0, ReservationClaimed):
            raise SmokeAssertionError(
                f"event[0] type={type(e0).__name__}, expected ReservationClaimed",
            )
        if not isinstance(e1, OrderFilled):
            raise SmokeAssertionError(
                f"event[1] type={type(e1).__name__}, expected OrderFilled",
            )
        expected_size = Decimal(str(SMOKE_SIZE_USDT))
        if e0.size_usdt != expected_size:
            raise SmokeAssertionError(
                f"event[0].size_usdt={e0.size_usdt}, expected {expected_size}",
            )
        if e1.size_usdt != expected_size:
            raise SmokeAssertionError(
                f"event[1].size_usdt={e1.size_usdt}, expected {expected_size}",
            )
        if not e0.is_simulated or not e1.is_simulated:
            raise SmokeAssertionError("expected is_simulated=True on both events")
        if e0.account_id != SMOKE_ACCOUNT_ID or e1.account_id != SMOKE_ACCOUNT_ID:
            raise SmokeAssertionError(
                f"events tagged with wrong account_id "
                f"(got {e0.account_id!r}/{e1.account_id!r}, expected {SMOKE_ACCOUNT_ID!r})",
            )
        checks["l2_passed"] = True
```

Also fix the `dataclass`/`field` imports at top of file (originally only `from dataclasses import dataclass, field` and `typing import Any` — but now we also need `frozen=True` SmokeResult). Final top-of-file imports should look like:

```python
from __future__ import annotations

import asyncio
import logging
import time
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Literal, Protocol
from uuid import uuid4
```

(Remove `AsyncExitStack` if linter complains about unused — keep only if needed; in this implementation we use `async with (a, b, c):` syntax which doesn't need ExitStack.)

- [ ] **Step 4: Run tests, verify pass**

Run: `cd backend_py && uv run pytest tests/modules/admin/test_smoke_runner.py -v`
Expected: 5 PASS.

- [ ] **Step 5: Commit**

```bash
cd /Users/will/second-brain/projects/startup/bfx-funding-bot
git add backend_py/src/bfx_funding_bot/modules/admin/smoke_runner.py \
        backend_py/tests/modules/admin/test_smoke_runner.py
git commit -m "✨ Feat: SmokeRunner.run_l2() — in-process chain verification w/ single-flight lock"
```

---

## Task 6: SmokeRunner — L3 path (Axiom round-trip)

**Files:**
- Modify: `backend_py/src/bfx_funding_bot/modules/admin/smoke_runner.py` (add run_l3)
- Modify: `backend_py/tests/modules/admin/test_smoke_runner.py` (add L3 tests)

- [ ] **Step 1: Write failing tests**

Append to `backend_py/tests/modules/admin/test_smoke_runner.py`:

```python
from bfx_funding_bot.modules.marketfeed.schemas import EventType


def _make_axiom_row(event_type: str) -> dict[str, Any]:
    return {
        "_time": "2026-05-22T10:00:00Z",
        "event_type": event_type,
        "account_id": SMOKE_ACCOUNT_ID,
        "payload": {"size_usdt": 1.0},
    }


async def test_run_l3_happy_returns_pass() -> None:
    axiom_query = _FakeAxiomQuery([
        _make_axiom_row(EventType.RESERVATION_CLAIMED.value),
        _make_axiom_row(EventType.ORDER_FILL.value),
    ])
    runner = _make_runner(axiom_query=axiom_query)

    result = await runner.run_l3()

    assert result.status == "pass"
    assert result.level == "L3"
    assert result.checks["l2_passed"] is True
    assert result.checks["axiom_events_seen"] == 2
    assert len(axiom_query.calls) >= 1
    assert axiom_query.calls[0][0] == SMOKE_ACCOUNT_ID


async def test_run_l3_axiom_returns_empty_fails_after_poll_timeout() -> None:
    """L2 passes but Axiom never returns events → L3 fail."""
    axiom_query = _FakeAxiomQuery([])  # always empty
    runner = _make_runner(axiom_query=axiom_query)

    # Override poll budget for speed (test does not wait 15s)
    result = await runner._run_l3_unlocked(
        poll_attempts=2, poll_interval_s=0.01,
    )

    assert result.status == "fail"
    assert result.level == "L3"
    assert result.checks["l2_passed"] is True
    assert "round-trip" in (result.error or "").lower() or \
           "axiom" in (result.error or "").lower()


async def test_run_l3_axiom_returns_only_one_event_type_fails() -> None:
    axiom_query = _FakeAxiomQuery([
        _make_axiom_row(EventType.RESERVATION_CLAIMED.value),
        # missing ORDER_FILL
    ])
    runner = _make_runner(axiom_query=axiom_query)
    result = await runner._run_l3_unlocked(poll_attempts=2, poll_interval_s=0.01)

    assert result.status == "fail"
    assert result.level == "L3"


async def test_run_l3_with_failing_l2_short_circuits() -> None:
    """If L2 fails, L3 should return immediately without polling Axiom."""
    class _RaisingExecutor:
        async def submit(self, decision, ctx):
            raise RuntimeError("boom")

    axiom_query = _FakeAxiomQuery([])
    runner = _make_runner(executor=_RaisingExecutor(), axiom_query=axiom_query)
    result = await runner.run_l3()

    assert result.status == "fail"
    assert result.level == "L2"  # L3 short-circuited; result reflects L2 failure
    assert axiom_query.calls == []  # no Axiom poll happened
```

- [ ] **Step 2: Run tests, verify failures**

Run: `cd backend_py && uv run pytest tests/modules/admin/test_smoke_runner.py -v -k l3`
Expected: 4 FAIL with AttributeError on `run_l3` / `_run_l3_unlocked`.

- [ ] **Step 3: Implement run_l3 + _run_l3_unlocked**

Append to `SmokeRunner` class in `smoke_runner.py`:

```python
    async def run_l3(self) -> SmokeResult:
        async with _SMOKE_LOCK:
            return await self._run_l3_unlocked()

    async def _run_l3_unlocked(
        self,
        *,
        poll_attempts: int = 5,
        poll_interval_s: float = 3.0,
    ) -> SmokeResult:
        """L2 + Axiom round-trip verification.

        After L2 passes (chain published to bus + axiom_sink emitted to Axiom),
        poll Axiom APL up to `poll_attempts × poll_interval_s` seconds for the
        events to appear. Tests override poll parameters for speed.
        """
        start = time.monotonic()
        checks: dict[str, Any] = {}
        recorder = EventRecorder(account_id=SMOKE_ACCOUNT_ID)
        # ── L2 phase ──
        try:
            before_ts, result = await self._run_chain(recorder)
            self._assert_l2(result, recorder, checks)
        except SmokeAssertionError as exc:
            duration_ms = int((time.monotonic() - start) * 1000)
            log.warning("smoke_l3_l2_failed reason=%r", exc)
            return SmokeResult(
                status="fail", level="L2", checks=checks,
                duration_ms=duration_ms, error=str(exc),
            )
        except Exception as exc:
            duration_ms = int((time.monotonic() - start) * 1000)
            log.exception("smoke_l3_l2_unexpected_error")
            return SmokeResult(
                status="fail", level="L2", checks=checks,
                duration_ms=duration_ms, error=f"unexpected: {exc!r}",
            )

        # ── L3 phase (round-trip Axiom query poll) ──
        try:
            await self._poll_axiom_round_trip(
                before_ts, checks,
                poll_attempts=poll_attempts, poll_interval_s=poll_interval_s,
            )
            duration_ms = int((time.monotonic() - start) * 1000)
            log.info("smoke_l3_passed duration_ms=%d", duration_ms)
            return SmokeResult(
                status="pass", level="L3", checks=checks, duration_ms=duration_ms,
            )
        except SmokeAssertionError as exc:
            duration_ms = int((time.monotonic() - start) * 1000)
            log.warning("smoke_l3_failed reason=%r", exc)
            return SmokeResult(
                status="fail", level="L3", checks=checks,
                duration_ms=duration_ms, error=str(exc),
            )
        except Exception as exc:
            duration_ms = int((time.monotonic() - start) * 1000)
            log.exception("smoke_l3_unexpected_error")
            return SmokeResult(
                status="fail", level="L3", checks=checks,
                duration_ms=duration_ms, error=f"unexpected: {exc!r}",
            )

    async def _poll_axiom_round_trip(
        self,
        since: datetime,
        checks: dict[str, Any],
        *,
        poll_attempts: int,
        poll_interval_s: float,
    ) -> None:
        from bfx_funding_bot.modules.marketfeed.schemas import EventType

        required_types = {
            EventType.RESERVATION_CLAIMED.value,
            EventType.ORDER_FILL.value,
        }
        last_seen = 0
        last_types: set[str] = set()
        for attempt in range(1, poll_attempts + 1):
            events = await self._axiom_query.query_order_events(
                SMOKE_ACCOUNT_ID, since,
            )
            last_seen = len(events)
            last_types = {e.get("event_type") for e in events if e.get("event_type")}
            if last_seen >= 2 and required_types.issubset(last_types):
                checks["axiom_events_seen"] = last_seen
                checks["axiom_event_types"] = sorted(last_types)
                return
            if attempt < poll_attempts:
                await asyncio.sleep(poll_interval_s)
        # Exhausted attempts
        checks["axiom_events_seen"] = last_seen
        checks["axiom_event_types"] = sorted(last_types)
        raise SmokeAssertionError(
            f"axiom round-trip timeout: seen={last_seen} types={sorted(last_types)} "
            f"required={sorted(required_types)}",
        )
```

- [ ] **Step 4: Run tests, verify all pass**

Run: `cd backend_py && uv run pytest tests/modules/admin/test_smoke_runner.py -v`
Expected: 9 PASS (5 L2 + 4 L3).

- [ ] **Step 5: Commit**

```bash
cd /Users/will/second-brain/projects/startup/bfx-funding-bot
git add backend_py/src/bfx_funding_bot/modules/admin/smoke_runner.py \
        backend_py/tests/modules/admin/test_smoke_runner.py
git commit -m "✨ Feat: SmokeRunner.run_l3() — Axiom round-trip verification w/ poll"
```

---

## Task 7: Admin router (POST /admin/smoke-test)

**Files:**
- Create: `backend_py/src/bfx_funding_bot/modules/admin/router.py`
- Create: `backend_py/tests/modules/admin/test_router.py`

- [ ] **Step 1: Write failing tests**

Create `backend_py/tests/modules/admin/test_router.py`:

```python
"""Admin router — POST /admin/smoke-test endpoint tests."""
from __future__ import annotations

from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from bfx_funding_bot.modules.admin.router import build_router
from bfx_funding_bot.modules.admin.smoke_runner import SmokeResult


class _FakeSmokeRunner:
    def __init__(
        self,
        l2_result: SmokeResult | None = None,
        l3_result: SmokeResult | None = None,
    ) -> None:
        self.l2_result = l2_result or SmokeResult(
            status="pass", level="L2", checks={}, duration_ms=10,
        )
        self.l3_result = l3_result or SmokeResult(
            status="pass", level="L3", checks={}, duration_ms=20,
        )
        self.l2_calls = 0
        self.l3_calls = 0

    async def run_l2(self) -> SmokeResult:
        self.l2_calls += 1
        return self.l2_result

    async def run_l3(self) -> SmokeResult:
        self.l3_calls += 1
        return self.l3_result


def _app(runner: Any, token: str) -> FastAPI:
    app = FastAPI()
    app.include_router(build_router(smoke_runner=runner, admin_token=token))
    return app


def test_missing_authorization_returns_401() -> None:
    runner = _FakeSmokeRunner()
    client = TestClient(_app(runner, "secret"))
    resp = client.post("/admin/smoke-test")
    assert resp.status_code == 401
    assert resp.json() == {"error": "missing_auth"}
    assert runner.l3_calls == 0


def test_wrong_token_returns_403() -> None:
    runner = _FakeSmokeRunner()
    client = TestClient(_app(runner, "secret"))
    resp = client.post(
        "/admin/smoke-test", headers={"Authorization": "Bearer wrong"},
    )
    assert resp.status_code == 403
    assert resp.json() == {"error": "unauthorized"}


def test_correct_token_runs_l3_by_default() -> None:
    runner = _FakeSmokeRunner()
    client = TestClient(_app(runner, "secret"))
    resp = client.post(
        "/admin/smoke-test", headers={"Authorization": "Bearer secret"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "pass"
    assert body["level"] == "L3"
    assert runner.l3_calls == 1
    assert runner.l2_calls == 0


def test_level_l2_query_param_runs_l2() -> None:
    runner = _FakeSmokeRunner()
    client = TestClient(_app(runner, "secret"))
    resp = client.post(
        "/admin/smoke-test?level=L2",
        headers={"Authorization": "Bearer secret"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["level"] == "L2"
    assert runner.l2_calls == 1
    assert runner.l3_calls == 0


def test_lock_held_returns_409() -> None:
    """Manually hold _SMOKE_LOCK to simulate concurrent smoke."""
    import asyncio

    from bfx_funding_bot.modules.admin import smoke_runner as sr_mod
    runner = _FakeSmokeRunner()
    client = TestClient(_app(runner, "secret"))

    async def hold_lock_briefly() -> None:
        async with sr_mod._SMOKE_LOCK:
            await asyncio.sleep(0.5)

    loop = asyncio.new_event_loop()
    task = loop.create_task(hold_lock_briefly())
    # Drive the loop just enough to acquire the lock
    loop.run_until_complete(asyncio.sleep(0.05))
    try:
        resp = client.post(
            "/admin/smoke-test", headers={"Authorization": "Bearer secret"},
        )
        assert resp.status_code == 409
        assert resp.json() == {"error": "smoke_already_running"}
    finally:
        loop.run_until_complete(task)
        loop.close()


def test_smoke_fail_returns_200_with_status_fail() -> None:
    fail_result = SmokeResult(
        status="fail", level="L2", checks={"events_count": 1},
        duration_ms=5, error="expected 2 events, got 1",
    )
    runner = _FakeSmokeRunner(l2_result=fail_result, l3_result=fail_result)
    client = TestClient(_app(runner, "secret"))
    resp = client.post(
        "/admin/smoke-test", headers={"Authorization": "Bearer secret"},
    )
    assert resp.status_code == 200  # endpoint worked; smoke result in body
    body = resp.json()
    assert body["status"] == "fail"
    assert body["error"]
```

- [ ] **Step 2: Run tests, verify ImportError**

Run: `cd backend_py && uv run pytest tests/modules/admin/test_router.py -v`
Expected: ImportError on `build_router`.

- [ ] **Step 3: Implement router**

Create `backend_py/src/bfx_funding_bot/modules/admin/router.py`:

```python
"""Admin FastAPI router — POST /admin/smoke-test.

Mounted by healthz.make_app() when both `smoke_runner` and `admin_token` are
provided. If either is None, the router is not built (caller skips mount).

Auth: static `Authorization: Bearer <BFX_ADMIN_TOKEN>` header check.
Single-flight: pre-check module-level _SMOKE_LOCK; 409 if held.
"""
from __future__ import annotations

import logging
from typing import Literal

from dataclasses import asdict
from fastapi import APIRouter, Header, Query, Request, status
from fastapi.responses import JSONResponse

from bfx_funding_bot.modules.admin import smoke_runner as sr_mod
from bfx_funding_bot.modules.admin.smoke_runner import SmokeRunner

log = logging.getLogger(__name__)


def build_router(*, smoke_runner: SmokeRunner, admin_token: str) -> APIRouter:
    router = APIRouter(prefix="/admin", tags=["admin"])

    @router.post("/smoke-test")
    async def smoke_test(
        request: Request,
        level: Literal["L2", "L3"] = Query(default="L3"),
        authorization: str | None = Header(default=None),
    ) -> JSONResponse:
        if not authorization:
            return JSONResponse(
                status_code=status.HTTP_401_UNAUTHORIZED,
                content={"error": "missing_auth"},
            )
        scheme, _, token = authorization.partition(" ")
        if scheme.lower() != "bearer" or token != admin_token:
            return JSONResponse(
                status_code=status.HTTP_403_FORBIDDEN,
                content={"error": "unauthorized"},
            )
        if sr_mod._SMOKE_LOCK.locked():
            return JSONResponse(
                status_code=status.HTTP_409_CONFLICT,
                content={"error": "smoke_already_running"},
            )
        result = await (
            smoke_runner.run_l2() if level == "L2"
            else smoke_runner.run_l3()
        )
        # SmokeResult is frozen dataclass — asdict() yields a JSON-friendly dict.
        return JSONResponse(status_code=200, content=asdict(result))

    return router
```

- [ ] **Step 4: Run tests, verify pass**

Run: `cd backend_py && uv run pytest tests/modules/admin/test_router.py -v`
Expected: 6 PASS.

- [ ] **Step 5: Commit**

```bash
cd /Users/will/second-brain/projects/startup/bfx-funding-bot
git add backend_py/src/bfx_funding_bot/modules/admin/router.py \
        backend_py/tests/modules/admin/test_router.py
git commit -m "✨ Feat: admin router — POST /admin/smoke-test w/ Bearer auth + single-flight"
```

---

## Task 8: Extend `healthz.make_app()` to mount admin router

**Files:**
- Modify: `backend_py/src/bfx_funding_bot/modules/marketfeed/healthz.py`

- [ ] **Step 1: Read current healthz.py** (already covered in design context; no new test file — Task 12 integration tests cover this end-to-end via HTTP)

Extend `make_app()` and `run_healthz_server()` signatures.

- [ ] **Step 2: Modify healthz.make_app()**

Edit `backend_py/src/bfx_funding_bot/modules/marketfeed/healthz.py`. Replace the existing `make_app` function:

```python
def make_app(
    probe: HealthProbe,
    *,
    smoke_runner: "SmokeRunner | None" = None,
    admin_token: str | None = None,
) -> FastAPI:
    """Build the FastAPI app bound to a given HealthProbe instance.

    If both `smoke_runner` and `admin_token` are provided (truthy), the admin
    router (POST /admin/smoke-test) is mounted alongside /healthz. If either
    is missing, the admin endpoint is not exposed (defaults preserve the
    pre-Phase-4.4 behaviour of healthz-only).
    """
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @app.get("/healthz")
    async def healthz() -> JSONResponse:
        # ... existing implementation unchanged ...
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

    if smoke_runner is not None and admin_token:
        from bfx_funding_bot.modules.admin.router import build_router
        app.include_router(build_router(
            smoke_runner=smoke_runner, admin_token=admin_token,
        ))
        log.info("admin_router_mounted endpoint=/admin/smoke-test")
    else:
        log.info(
            "admin_router_skipped smoke_runner=%s admin_token_set=%s",
            smoke_runner is not None, bool(admin_token),
        )

    return app
```

Then update `run_healthz_server()`:

```python
async def run_healthz_server(
    *,
    probe: HealthProbe,
    host: str,
    port: int,
    stop_event: asyncio.Event,
    smoke_runner: "SmokeRunner | None" = None,
    admin_token: str | None = None,
) -> None:
    """Run uvicorn until stop_event fires; cancellation safe."""
    app = make_app(probe, smoke_runner=smoke_runner, admin_token=admin_token)
    config = uvicorn.Config(
        app=app, host=host, port=port,
        log_level="warning", access_log=False,
        loop="asyncio",
        timeout_graceful_shutdown=5,
    )
    # ... rest unchanged ...
    server = uvicorn.Server(config)
    serve_task = asyncio.create_task(server.serve(), name="healthz_uvicorn")
    stop_task = asyncio.create_task(stop_event.wait(), name="healthz_stop_wait")
    try:
        await asyncio.wait(
            {serve_task, stop_task}, return_when=asyncio.FIRST_COMPLETED,
        )
    finally:
        server.should_exit = True
        for t in (serve_task, stop_task):
            if not t.done():
                t.cancel()
        try:
            await asyncio.wait_for(
                asyncio.gather(serve_task, stop_task, return_exceptions=True),
                timeout=10.0,
            )
        except TimeoutError:
            log.warning(
                "healthz_shutdown_timeout — uvicorn did not exit within 10s; "
                "daemon cleanup proceeding (serve_task may leak briefly)",
            )
        except (asyncio.CancelledError, Exception):
            pass
```

Add import at top of file (after existing imports):

```python
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from bfx_funding_bot.modules.admin.smoke_runner import SmokeRunner
```

- [ ] **Step 3: Verify existing healthz tests still pass**

Run: `cd backend_py && uv run pytest tests/modules/marketfeed -v -k healthz`
Expected: all existing PASS (signature is backward-compatible — added params have defaults).

- [ ] **Step 4: Verify type check + lint**

Run: `cd backend_py && uv run mypy src/bfx_funding_bot/modules/marketfeed/healthz.py && uv run ruff check src/bfx_funding_bot/modules/marketfeed/healthz.py`
Expected: clean.

- [ ] **Step 5: Commit**

```bash
cd /Users/will/second-brain/projects/startup/bfx-funding-bot
git add backend_py/src/bfx_funding_bot/modules/marketfeed/healthz.py
git commit -m "♻️ Refactor: healthz make_app/run_healthz_server accept optional smoke_runner + admin_token"
```

---

## Task 9: Daemon `build_daemon()` wiring

**Files:**
- Modify: `backend_py/src/bfx_funding_bot/modules/marketfeed/daemon.py`

- [ ] **Step 1: Add `smoke_runner` field to Daemon dataclass**

Edit `backend_py/src/bfx_funding_bot/modules/marketfeed/daemon.py:133-157` Daemon dataclass — add new field after `ledger`:

```python
@dataclass
class Daemon:
    # ... existing fields ...
    ledger: PaperPositionLedger
    smoke_runner: "SmokeRunner | None" = None       # NEW
    fill_tracker: RestPollingFillTracker | None = None
    healthz_host: str = "0.0.0.0"
    healthz_port: int = 8080
    admin_token: str | None = None                   # NEW
    _stop_event: asyncio.Event = field(default_factory=asyncio.Event)
```

Add forward import at top of file:

```python
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from bfx_funding_bot.modules.admin.smoke_runner import SmokeRunner
```

- [ ] **Step 2: Wire SmokeRunner in build_daemon()**

In `build_daemon()`, after `wrapped_executor = HeartbeatMiddleware(...)` block (around line 774-780), add SmokeRunner construction:

```python
    # ---- Phase 4.4 prework: SmokeRunner ----
    from bfx_funding_bot.modules.admin.axiom_query import AxiomEventQueryAdapter
    from bfx_funding_bot.modules.admin.smoke_runner import SmokeRunner

    smoke_axiom_query = AxiomEventQueryAdapter(
        api_key=config.axiom_api_key,
        dataset=config.axiom_dataset,
    )
    smoke_runner = SmokeRunner(
        executor=wrapped_executor,
        bus=bus,
        axiom_client=axiom,
        axiom_query=smoke_axiom_query,
        phase=config.phase,
        strategy=first_cell.strategy,
        cell=first_cell.cell_id,
    )
```

Add admin_token env read (alongside healthz_host / healthz_port read at line 936-938):

```python
    admin_token = os.environ.get("BFX_ADMIN_TOKEN", "").strip() or None
```

Pass to `Daemon(...)` return (line 940-962):

```python
    return Daemon(
        # ... existing fields ...
        ledger=ledger,
        smoke_runner=smoke_runner,                  # NEW
        fill_tracker=fill_tracker,
        healthz_host=healthz_host,
        healthz_port=healthz_port,
        admin_token=admin_token,                    # NEW
    )
```

- [ ] **Step 3: Pass through `_healthz_server_loop()`**

Edit `_healthz_server_loop()` (line 280-294) to pass smoke_runner + admin_token:

```python
    async def _healthz_server_loop(self) -> None:
        """Container-level liveness HTTP endpoint for Koyeb / k8s probes."""
        await run_healthz_server(
            probe=self.probe,
            host=self.healthz_host,
            port=self.healthz_port,
            stop_event=self._stop_event,
            smoke_runner=self.smoke_runner,
            admin_token=self.admin_token,
        )
        log.info("sub_task_exit name=healthz")
```

- [ ] **Step 4: Verify type check + boot smoke daemon test still pass**

Run: `cd backend_py && uv run mypy src/bfx_funding_bot/modules/marketfeed/daemon.py && uv run pytest tests/modules/marketfeed -v -k "not integration"`
Expected: mypy clean; existing daemon tests PASS (Daemon dataclass field additions are backward-compatible with defaults).

- [ ] **Step 5: Commit**

```bash
cd /Users/will/second-brain/projects/startup/bfx-funding-bot
git add backend_py/src/bfx_funding_bot/modules/marketfeed/daemon.py
git commit -m "✨ Feat: wire SmokeRunner + AxiomEventQueryAdapter into build_daemon()"
```

---

## Task 10: Boot-time smoke in `_run()`

**Files:**
- Modify: `backend_py/src/bfx_funding_bot/modules/marketfeed/daemon.py` (only `_run()` function)

- [ ] **Step 1: Insert boot smoke try/except block in `_run()`**

Edit `_run()` (line 980-1046) — insert boot smoke block after `daemon = await build_daemon()` (line 981) and before `stop = daemon._stop_event` (line 983):

```python
async def _run() -> None:
    daemon = await build_daemon()

    # ── Phase 4.4 prework: boot-time smoke L2 (deploy gate) ──
    # Runs BEFORE TaskGroup so exceptions don't trigger TaskGroup cancel.
    # Failure → log critical + SAFETY_TRIGGER event + daemon continues.
    if daemon.smoke_runner is not None:
        try:
            boot_smoke_result = await asyncio.wait_for(
                daemon.smoke_runner.run_l2(), timeout=30.0,
            )
            if boot_smoke_result.status == "pass":
                log.info(
                    "smoke_boot_passed duration_ms=%d",
                    boot_smoke_result.duration_ms,
                )
            else:
                raise RuntimeError(
                    f"smoke_boot_l2_fail: {boot_smoke_result.error!r} "
                    f"checks={boot_smoke_result.checks}",
                )
        except Exception as exc:
            log.critical("smoke_boot_failed err=%r — daemon continues", exc)
            try:
                await daemon.axiom.emit({
                    "timestamp": datetime.now(UTC).isoformat(),
                    "level": Level.CRITICAL.value,
                    "phase": daemon.config.phase.value,
                    "strategy": None,
                    "cell": None,
                    "event_type": EventType.SAFETY_TRIGGER.value,
                    "correlation_id": str(uuid4()),
                    "payload": {
                        "check_target": "smoke_boot",
                        "status": HealthStatus.DEGRADED.value,
                        "error_message": repr(exc),
                    },
                })
            except Exception:
                log.exception("smoke_boot_safety_trigger_emit_failed")
    # daemon unconditionally continues regardless of smoke outcome

    stop = daemon._stop_event  # share with signal handler
    # ... rest unchanged ...
```

Verify imports at top of daemon.py already include `Level`, `EventType`, `HealthStatus`, `uuid4`, `datetime`, `UTC`. (Yes — all present per earlier reads.)

- [ ] **Step 2: Add unit test for boot smoke wiring**

Append a new test file `backend_py/tests/modules/admin/test_boot_smoke_integration.py`:

```python
"""Boot-time smoke wiring — verify _run() invokes smoke_runner.run_l2()
and continues on failure."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from bfx_funding_bot.modules.admin.smoke_runner import SmokeResult


async def test_boot_smoke_passes_logs_info(monkeypatch, caplog) -> None:
    """A passing boot smoke should log smoke_boot_passed."""
    import logging
    caplog.set_level(logging.INFO)

    # Build a minimal fake Daemon-like object
    daemon_stub = MagicMock()
    daemon_stub.smoke_runner = MagicMock()
    daemon_stub.smoke_runner.run_l2 = AsyncMock(return_value=SmokeResult(
        status="pass", level="L2", checks={}, duration_ms=10,
    ))
    daemon_stub.axiom = MagicMock()
    daemon_stub.axiom.emit = AsyncMock()

    # Extract the boot smoke block as standalone logic for testability:
    # Test calls a helper that mirrors _run()'s smoke block.
    from bfx_funding_bot.modules.marketfeed.daemon_smoke_boot import run_boot_smoke
    await run_boot_smoke(daemon_stub)

    daemon_stub.smoke_runner.run_l2.assert_awaited_once()
    daemon_stub.axiom.emit.assert_not_awaited()
    assert any("smoke_boot_passed" in rec.message for rec in caplog.records)


async def test_boot_smoke_fails_emits_safety_trigger(caplog) -> None:
    import logging
    caplog.set_level(logging.CRITICAL)

    daemon_stub = MagicMock()
    daemon_stub.smoke_runner = MagicMock()
    daemon_stub.smoke_runner.run_l2 = AsyncMock(return_value=SmokeResult(
        status="fail", level="L2", checks={"events_count": 1},
        duration_ms=5, error="expected 2 events, got 1",
    ))
    daemon_stub.axiom = MagicMock()
    daemon_stub.axiom.emit = AsyncMock()
    daemon_stub.config.phase.value = "paper"

    from bfx_funding_bot.modules.marketfeed.daemon_smoke_boot import run_boot_smoke
    await run_boot_smoke(daemon_stub)

    daemon_stub.axiom.emit.assert_awaited_once()
    emit_call = daemon_stub.axiom.emit.call_args[0][0]
    assert emit_call["event_type"] == "safety_trigger"
    assert emit_call["level"] == "critical"
    assert "smoke_boot" in str(emit_call["payload"])


async def test_boot_smoke_none_runner_skips_silently(caplog) -> None:
    """If smoke_runner is None on Daemon (e.g. wiring opt-out), block is no-op."""
    daemon_stub = MagicMock()
    daemon_stub.smoke_runner = None
    daemon_stub.axiom = MagicMock()
    daemon_stub.axiom.emit = AsyncMock()

    from bfx_funding_bot.modules.marketfeed.daemon_smoke_boot import run_boot_smoke
    await run_boot_smoke(daemon_stub)

    daemon_stub.axiom.emit.assert_not_awaited()
```

- [ ] **Step 3: Extract `run_boot_smoke()` helper for testability**

To make the boot smoke block independently testable (and to keep `_run()` readable), extract a helper function. Create `backend_py/src/bfx_funding_bot/modules/marketfeed/daemon_smoke_boot.py`:

```python
"""Boot-time smoke L2 wrapper — called from daemon._run() between
build_daemon() and TaskGroup start.

Extracted for testability: _run() itself touches signal handlers + TaskGroup
and is hard to unit-test in isolation.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from uuid import uuid4

from bfx_funding_bot.modules.marketfeed.schemas import (
    EventType,
    HealthStatus,
    Level,
)

log = logging.getLogger(__name__)

BOOT_SMOKE_TIMEOUT_S = 30.0


async def run_boot_smoke(daemon) -> None:  # noqa: ANN001
    """Run boot smoke L2. Failure → log critical + emit SAFETY_TRIGGER.

    Daemon unconditionally continues — smoke crash never crashes daemon.
    `daemon` is typed loosely (Daemon | Mock) to keep this helper unit-testable.
    """
    if daemon.smoke_runner is None:
        log.info("smoke_boot_skipped reason=no_smoke_runner")
        return
    try:
        result = await asyncio.wait_for(
            daemon.smoke_runner.run_l2(), timeout=BOOT_SMOKE_TIMEOUT_S,
        )
        if result.status == "pass":
            log.info("smoke_boot_passed duration_ms=%d", result.duration_ms)
            return
        raise RuntimeError(
            f"smoke_boot_l2_fail: {result.error!r} checks={result.checks}",
        )
    except Exception as exc:
        log.critical("smoke_boot_failed err=%r — daemon continues", exc)
        try:
            await daemon.axiom.emit({
                "timestamp": datetime.now(UTC).isoformat(),
                "level": Level.CRITICAL.value,
                "phase": daemon.config.phase.value,
                "strategy": None,
                "cell": None,
                "event_type": EventType.SAFETY_TRIGGER.value,
                "correlation_id": str(uuid4()),
                "payload": {
                    "check_target": "smoke_boot",
                    "status": HealthStatus.DEGRADED.value,
                    "error_message": repr(exc),
                },
            })
        except Exception:
            log.exception("smoke_boot_safety_trigger_emit_failed")
```

Replace the inline block in `daemon._run()` (Step 1) with a single call:

```python
async def _run() -> None:
    daemon = await build_daemon()

    # Phase 4.4 prework: boot smoke (pre-TaskGroup)
    from bfx_funding_bot.modules.marketfeed.daemon_smoke_boot import run_boot_smoke
    await run_boot_smoke(daemon)

    stop = daemon._stop_event  # share with signal handler
    # ... rest unchanged ...
```

- [ ] **Step 4: Run tests, verify pass + daemon module tests still green**

Run: `cd backend_py && uv run pytest tests/modules/admin/test_boot_smoke_integration.py tests/modules/marketfeed/test_daemon_shutdown.py -v -m "not integration"`
Expected: 3 boot smoke tests PASS; daemon_shutdown PASS (no regression).

Run: `cd backend_py && uv run mypy src/bfx_funding_bot/modules/marketfeed/daemon_smoke_boot.py src/bfx_funding_bot/modules/marketfeed/daemon.py`
Expected: clean.

- [ ] **Step 5: Commit**

```bash
cd /Users/will/second-brain/projects/startup/bfx-funding-bot
git add backend_py/src/bfx_funding_bot/modules/marketfeed/daemon.py \
        backend_py/src/bfx_funding_bot/modules/marketfeed/daemon_smoke_boot.py \
        backend_py/tests/modules/admin/test_boot_smoke_integration.py
git commit -m "✨ Feat: boot-time smoke L2 in _run() — deploy gate w/o crashing daemon on smoke fail"
```

---

## Task 11: Full chain integration test

**Files:**
- Create: `backend_py/tests/modules/admin/test_integration_full_chain.py`

- [ ] **Step 1: Write integration test**

Create `backend_py/tests/modules/admin/test_integration_full_chain.py`:

```python
"""Full chain integration: SmokeRunner end-to-end with real bus + middleware
+ axiom_sink + prod_ledger. Verifies (1) prod ledger untouched, (2) recorder
captures both smoke events, (3) AxiomEventSink emits with smoke account_id.

Note: NOT marked pytest.mark.integration — runs without network (fake axiom
client). Lives in tests/modules/admin/ for default test run inclusion.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest

from bfx_funding_bot.modules.admin.smoke_runner import (
    SMOKE_ACCOUNT_ID,
    SmokeRunner,
)
from bfx_funding_bot.modules.execution.axiom_sink import AxiomEventSink
from bfx_funding_bot.modules.execution.bus import DomainEventBus
from bfx_funding_bot.modules.execution.events import (
    OrderFilled,
    ReservationClaimed,
    ReservationReleased,
)
from bfx_funding_bot.modules.execution.ledger import PaperPositionLedger
from bfx_funding_bot.modules.execution.middleware import (
    ReservationEmittingMiddleware,
)
from bfx_funding_bot.modules.execution.paper import EchoPaperExecutor
from bfx_funding_bot.modules.marketfeed.schemas import (
    EventType,
    Phase,
    StrategyName,
)


class _FakeAxiomClient:
    def __init__(self) -> None:
        self.emits: list[dict[str, Any]] = []

    async def emit(self, event: dict[str, Any]) -> None:
        self.emits.append(event)


class _FakeAxiomQuery:
    async def query_order_events(self, account_id, since):
        return []


async def test_smoke_does_not_touch_prod_ledger() -> None:
    """Invariant I1: smoke pollutes 0 prod state."""
    bus = DomainEventBus()
    axiom = _FakeAxiomClient()
    prod_ledger = PaperPositionLedger(account_id="default")
    sink = AxiomEventSink(
        axiom_client=axiom, phase=Phase.PAPER,
        strategy=StrategyName.RATE_PERCENTILE, cell="bfx_USDT",
    )
    bus.subscribe(ReservationClaimed, prod_ledger.on_reservation_claimed)
    bus.subscribe(OrderFilled, prod_ledger.on_order_filled)
    bus.subscribe(ReservationReleased, prod_ledger.on_reservation_released)
    bus.subscribe(ReservationClaimed, sink.on_reservation_claimed)
    bus.subscribe(OrderFilled, sink.on_order_filled)
    bus.subscribe(ReservationReleased, sink.on_reservation_released)

    paper = EchoPaperExecutor(
        axiom=axiom, phase=Phase.PAPER,
        strategy=StrategyName.RATE_PERCENTILE, cell="bfx_USDT",
    )
    wrapped = ReservationEmittingMiddleware(paper, bus=bus)
    runner = SmokeRunner(
        executor=wrapped, bus=bus, axiom_client=axiom,
        axiom_query=_FakeAxiomQuery(),
        phase=Phase.PAPER,
        strategy=StrategyName.RATE_PERCENTILE,
        cell="bfx_USDT",
    )

    before_reserved = prod_ledger.current_exposure()
    before_realized = prod_ledger.realized_exposure()

    result = await runner.run_l2()

    assert result.status == "pass"
    assert prod_ledger.current_exposure() == before_reserved
    assert prod_ledger.realized_exposure() == before_realized
    assert prod_ledger.replay_floor_hit_count == 0


async def test_smoke_axiom_sink_emits_with_smoke_account_id() -> None:
    """Invariant I5: axiom_sink emits 2 events with account_id=smoke_test."""
    bus = DomainEventBus()
    axiom = _FakeAxiomClient()
    sink = AxiomEventSink(
        axiom_client=axiom, phase=Phase.PAPER,
        strategy=StrategyName.RATE_PERCENTILE, cell="bfx_USDT",
    )
    bus.subscribe(ReservationClaimed, sink.on_reservation_claimed)
    bus.subscribe(OrderFilled, sink.on_order_filled)

    paper = EchoPaperExecutor(
        axiom=axiom, phase=Phase.PAPER,
        strategy=StrategyName.RATE_PERCENTILE, cell="bfx_USDT",
    )
    wrapped = ReservationEmittingMiddleware(paper, bus=bus)
    runner = SmokeRunner(
        executor=wrapped, bus=bus, axiom_client=axiom,
        axiom_query=_FakeAxiomQuery(),
        phase=Phase.PAPER,
        strategy=StrategyName.RATE_PERCENTILE,
        cell="bfx_USDT",
    )
    await runner.run_l2()

    # Two emit categories: (a) EchoPaperExecutor emits order_submit + order_fill
    # directly to axiom, (b) AxiomEventSink emits reservation_claimed + order_fill
    # via bus subscription. Filter to bus-emitted events (those from sink):
    sink_emits = [
        e for e in axiom.emits
        if e.get("event_type") in (
            EventType.RESERVATION_CLAIMED.value,
            EventType.ORDER_FILL.value,
            EventType.RESERVATION_RELEASED.value,
        )
        and e.get("account_id") == SMOKE_ACCOUNT_ID
    ]
    sink_event_types = {e["event_type"] for e in sink_emits}
    # AxiomEventSink should have emitted RESERVATION_CLAIMED + ORDER_FILL
    # (no RESERVATION_RELEASED for paper happy path).
    assert EventType.RESERVATION_CLAIMED.value in sink_event_types
    assert EventType.ORDER_FILL.value in sink_event_types
    # All bus-emitted events tagged smoke_test
    for e in sink_emits:
        assert e["account_id"] == SMOKE_ACCOUNT_ID
```

- [ ] **Step 2: Run tests**

Run: `cd backend_py && uv run pytest tests/modules/admin/test_integration_full_chain.py -v`
Expected: 2 PASS.

- [ ] **Step 3: Run full smoke suite to confirm no regression**

Run: `cd backend_py && uv run pytest tests/modules/admin/ tests/modules/execution/test_bus.py tests/modules/execution/test_ledger.py -v`
Expected: all PASS.

- [ ] **Step 4: Commit**

```bash
cd /Users/will/second-brain/projects/startup/bfx-funding-bot
git add backend_py/tests/modules/admin/test_integration_full_chain.py
git commit -m "✅ Test: smoke full chain integration — prod ledger untouched + axiom_sink emit verify"
```

---

## Task 12: HTTP integration test (FastAPI TestClient)

**Files:**
- Create: `backend_py/tests/modules/admin/test_integration_http.py`

- [ ] **Step 1: Write integration test**

Create `backend_py/tests/modules/admin/test_integration_http.py`:

```python
"""HTTP integration: build_router + healthz.make_app + real SmokeRunner."""
from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi.testclient import TestClient

from bfx_funding_bot.modules.admin.smoke_runner import SmokeRunner
from bfx_funding_bot.modules.execution.axiom_sink import AxiomEventSink
from bfx_funding_bot.modules.execution.bus import DomainEventBus
from bfx_funding_bot.modules.execution.events import (
    OrderFilled,
    ReservationClaimed,
)
from bfx_funding_bot.modules.execution.middleware import (
    ReservationEmittingMiddleware,
)
from bfx_funding_bot.modules.execution.paper import EchoPaperExecutor
from bfx_funding_bot.modules.marketfeed.health_monitor import HealthProbe
from bfx_funding_bot.modules.marketfeed.healthz import make_app
from bfx_funding_bot.modules.marketfeed.schemas import (
    EventType,
    Phase,
    StrategyName,
)


class _FakeAxiomClient:
    def __init__(self) -> None:
        self.emits: list[dict[str, Any]] = []

    async def emit(self, event: dict[str, Any]) -> None:
        self.emits.append(event)


class _FakeAxiomQuery:
    """Returns one ReservationClaimed + one ORDER_FILL row immediately."""

    async def query_order_events(self, account_id, since):
        return [
            {
                "_time": datetime.now().isoformat(),
                "event_type": EventType.RESERVATION_CLAIMED.value,
                "account_id": account_id,
                "payload": {"size_usdt": 1.0},
            },
            {
                "_time": datetime.now().isoformat(),
                "event_type": EventType.ORDER_FILL.value,
                "account_id": account_id,
                "payload": {"fill_size_usdt": 1.0},
            },
        ]


def _build_app(token: str = "secret"):
    bus = DomainEventBus()
    axiom = _FakeAxiomClient()
    sink = AxiomEventSink(
        axiom_client=axiom, phase=Phase.PAPER,
        strategy=StrategyName.RATE_PERCENTILE, cell="bfx_USDT",
    )
    bus.subscribe(ReservationClaimed, sink.on_reservation_claimed)
    bus.subscribe(OrderFilled, sink.on_order_filled)

    paper = EchoPaperExecutor(
        axiom=axiom, phase=Phase.PAPER,
        strategy=StrategyName.RATE_PERCENTILE, cell="bfx_USDT",
    )
    wrapped = ReservationEmittingMiddleware(paper, bus=bus)
    runner = SmokeRunner(
        executor=wrapped, bus=bus, axiom_client=axiom,
        axiom_query=_FakeAxiomQuery(),
        phase=Phase.PAPER,
        strategy=StrategyName.RATE_PERCENTILE,
        cell="bfx_USDT",
    )
    probe = HealthProbe()
    app = make_app(probe, smoke_runner=runner, admin_token=token)
    return app, runner


def test_http_smoke_l3_pass_end_to_end() -> None:
    app, _ = _build_app()
    client = TestClient(app)
    resp = client.post(
        "/admin/smoke-test", headers={"Authorization": "Bearer secret"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "pass"
    assert body["level"] == "L3"
    assert body["checks"]["l2_passed"] is True
    assert body["checks"]["axiom_events_seen"] >= 2


def test_http_smoke_l2_query_param() -> None:
    app, _ = _build_app()
    client = TestClient(app)
    resp = client.post(
        "/admin/smoke-test?level=L2",
        headers={"Authorization": "Bearer secret"},
    )
    assert resp.status_code == 200
    assert resp.json()["level"] == "L2"


def test_http_no_token_means_admin_router_not_mounted() -> None:
    """make_app(admin_token=None) → POST /admin/smoke-test should 404."""
    bus = DomainEventBus()
    axiom = _FakeAxiomClient()
    paper = EchoPaperExecutor(
        axiom=axiom, phase=Phase.PAPER,
        strategy=StrategyName.RATE_PERCENTILE, cell="bfx_USDT",
    )
    wrapped = ReservationEmittingMiddleware(paper, bus=bus)
    runner = SmokeRunner(
        executor=wrapped, bus=bus, axiom_client=axiom,
        axiom_query=_FakeAxiomQuery(),
        phase=Phase.PAPER,
        strategy=StrategyName.RATE_PERCENTILE,
        cell="bfx_USDT",
    )
    probe = HealthProbe()
    app = make_app(probe, smoke_runner=runner, admin_token=None)
    client = TestClient(app)
    resp = client.post(
        "/admin/smoke-test", headers={"Authorization": "Bearer anything"},
    )
    assert resp.status_code == 404


def test_http_healthz_still_works_alongside_admin() -> None:
    """Regression: mounting admin router doesn't break /healthz."""
    app, _ = _build_app()
    client = TestClient(app)
    # No sub-tasks registered → 503 starting
    resp = client.get("/healthz")
    assert resp.status_code == 503
    assert resp.json()["status"] == "starting"
```

- [ ] **Step 2: Run tests**

Run: `cd backend_py && uv run pytest tests/modules/admin/test_integration_http.py -v`
Expected: 4 PASS.

- [ ] **Step 3: Final full-repo smoke test pass**

Run: `cd backend_py && uv run pytest -m "not integration" -v 2>&1 | tail -30`
Expected: all tests PASS (existing ~492 + new tests).

Run: `cd backend_py && uv run mypy src/ && uv run ruff check src/`
Expected: mypy clean; ruff ≤ 4 (baseline).

- [ ] **Step 4: Commit**

```bash
cd /Users/will/second-brain/projects/startup/bfx-funding-bot
git add backend_py/tests/modules/admin/test_integration_http.py
git commit -m "✅ Test: smoke HTTP integration — end-to-end via FastAPI TestClient"
```

---

## Post-implementation verification

After all 12 tasks complete:

- [ ] **Verify full suite passes**
  ```bash
  cd backend_py && uv run pytest -m "not integration" -v
  ```
  Expected: all PASS, no regression.

- [ ] **Verify type check + lint**
  ```bash
  cd backend_py && uv run mypy src/ && uv run ruff check src/
  ```
  Expected: mypy clean; ruff ≤ 4 errors (Phase 4.3 baseline).

- [ ] **Verify deploy readiness**
  - `BFX_ADMIN_TOKEN` env var to be set in Koyeb dashboard before next deploy (else endpoint silently absent).
  - Document in `~/second-brain/wiki/projects/bfx-funding-bot/index.md` Runbook section if needed.

- [ ] **Test in production after deploy**
  - Watch boot logs for `smoke_boot_passed` line on container start.
  - From local: `curl -X POST https://<koyeb-url>/admin/smoke-test -H "Authorization: Bearer $BFX_ADMIN_TOKEN" | jq`.
  - Expected response: `{"status": "pass", "level": "L3", "checks": {...}, "duration_ms": ...}`.
  - Verify smoke events appear in Axiom: query `account_id=="smoke_test"` over last 1h, expect 2+ events per smoke call.

---

## Out of scope (deferred to future spec / ADR)

- 4.4 live smoke mode (`?mode=live` query param)
- 24/7 continuous synthetic monitoring (cron)
- Multi-tenant smoke (per-tenant)
- Replace daemon's `_AxiomQueryAdapter` stub with real query for ledger replay (separate ADR — affects boot behavior)
- DLQ / outbox for smoke event publish (Phase 5+)
