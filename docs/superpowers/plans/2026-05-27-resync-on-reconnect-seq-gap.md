# Resync on Reconnect / Seq-Gap Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** When the authenticated Bitfinex WS stream loses events (visible reconnect, or a silent in-connection sequence gap), fire an immediate off-interval venue reconcile instead of waiting up to a full `BFX_RECONCILE_INTERVAL_S` (default 90 s).

**Architecture:** Multiple stream-integrity detectors in `auth_ws` (reconnect boundary + public-seq gap) collapse into one deduped, debounced `request_resync` call on the existing `PeriodicReconcile` loop, which runs the existing `BootRecovery.run` snapshot reconcile. One loop, one `_tick()` at a time → triggers only shorten the wait and never race the timer; the `_tick()` body (fail-safe / divergence health) is unchanged. The timer remains the correctness backbone; triggers only cut latency.

**Tech Stack:** Python 3.13, asyncio, `websockets`, pytest + pytest-asyncio. All commands run from `backend_py/` via `uv`.

**Spec:** `docs/superpowers/specs/2026-05-27-resync-on-reconnect-seq-gap-design.md`

---

## File Structure

- **Modify** `backend_py/src/bfx_funding_bot/external/bitfinex/auth_ws.py`
  - Add `SEQ_ALL_FLAG` constant + `_public_seq(msg)` extractor (pure).
  - Add `SequenceTracker` (pure gap detector).
  - `Heartbeat` and `Unknown` gain a `raw_seq` field so every channel frame carries the public seq.
  - `_parse_channel_msg` routes all channel frames' seq through `_public_seq`.
  - `BitfinexAuthWSClient`: new `on_resync_needed` ctor param; send `conf` SEQ_ALL on connect; reset tracker + fire `"reconnect"` on each non-first connection; observe seq + fire `"seq_gap"` per frame.
- **Modify** `backend_py/src/bfx_funding_bot/modules/execution/periodic_reconcile.py`
  - `request_resync(reason)` (sync, sets an `asyncio.Event`); `run_loop` waits on `{stop, trigger}`; debounce window; `_tick()` untouched.
- **Modify** `backend_py/src/bfx_funding_bot/modules/marketfeed/daemon.py`
  - `BFX_RESYNC_MIN_INTERVAL_S` env → `PeriodicReconcile.min_resync_interval_s`; wire `auth_ws.on_resync_needed = periodic_reconcile.request_resync` when both are live.
- **Create** `backend_py/tests/external/bitfinex/test_sequence_tracker.py`
- **Create** `backend_py/tests/external/bitfinex/test_auth_ws_seq_resync.py`
- **Create** `backend_py/tests/external/bitfinex/test_auth_ws_public_seq.py`
- **Create** `backend_py/tests/integration/test_resync_trigger_off_interval.py`
- **Create** `backend_py/tests/external/bitfinex/test_auth_ws_seq_live.py` (gated-live, `@pytest.mark.integration`)
- **Modify** `backend_py/tests/modules/execution/test_periodic_reconcile.py` (add resync/debounce cases)
- **Modify** `backend_py/tests/modules/marketfeed/test_daemon_wiring.py` (add binding case)

Commit gate (every task): `cd backend_py && uv run pytest -m "not integration"` green + `uv run mypy src/` + `uv run ruff check`.

---

### Task 1: SequenceTracker (pure gap detector)

**Files:**
- Modify: `backend_py/src/bfx_funding_bot/external/bitfinex/auth_ws.py`
- Test: `backend_py/tests/external/bitfinex/test_sequence_tracker.py`

- [ ] **Step 1: Write the failing test**

Create `backend_py/tests/external/bitfinex/test_sequence_tracker.py`:

```python
from bfx_funding_bot.external.bitfinex.auth_ws import SequenceTracker


def test_first_observation_sets_baseline_no_gap():
    t = SequenceTracker()
    assert t.observe(10) == "ok"


def test_contiguous_is_ok():
    t = SequenceTracker()
    t.observe(10)
    assert t.observe(11) == "ok"
    assert t.observe(12) == "ok"


def test_forward_jump_is_gap():
    t = SequenceTracker()
    t.observe(10)
    assert t.observe(13) == "gap"  # 11, 12 dropped


def test_resync_after_gap_then_contiguous_is_ok():
    t = SequenceTracker()
    t.observe(10)
    t.observe(13)  # gap → baseline becomes 14
    assert t.observe(14) == "ok"


def test_none_seq_is_no_info_ok():
    t = SequenceTracker()
    t.observe(10)
    assert t.observe(None) == "ok"
    assert t.observe(11) == "ok"  # None did not disturb the baseline


def test_reset_clears_baseline_no_false_gap():
    t = SequenceTracker()
    t.observe(99)
    t.reset()  # new connection: seq restarts low
    assert t.observe(1) == "ok"
    assert t.observe(2) == "ok"


def test_backwards_seq_is_ok_and_rebaselines():
    t = SequenceTracker()
    t.observe(10)
    assert t.observe(8) == "ok"  # duplicate / reorder, not forward loss
    assert t.observe(9) == "ok"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend_py && uv run pytest tests/external/bitfinex/test_sequence_tracker.py -q`
Expected: FAIL — `ImportError: cannot import name 'SequenceTracker'`.

- [ ] **Step 3: Write minimal implementation**

In `auth_ws.py`, after the `log = logging.getLogger(__name__)` line (around line 35), add:

```python
SEQ_ALL_FLAG = 65536  # Bitfinex `conf` flag: append a sequence number to every packet


class SequenceTracker:
    """Detects dropped packets via Bitfinex's monotonic public sequence number.

    Pure logic — no socket, no domain types. `reset()` at each (re)connect, since
    the sequence restarts per connection. A forward jump means packets were lost.
    A `None` seq carries no information (flag not applied / non-seq frame).
    """

    def __init__(self) -> None:
        self._expected: int | None = None

    def reset(self) -> None:
        self._expected = None

    def observe(self, seq: int | None) -> str:
        if seq is None:
            return "ok"
        if self._expected is not None and seq > self._expected:
            self._expected = seq + 1
            return "gap"
        self._expected = seq + 1
        return "ok"
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend_py && uv run pytest tests/external/bitfinex/test_sequence_tracker.py -q`
Expected: PASS (7 passed).

- [ ] **Step 5: Commit**

```bash
cd /Users/will/second-brain/projects/startup/bfx-funding-bot
git add backend_py/src/bfx_funding_bot/external/bitfinex/auth_ws.py \
        backend_py/tests/external/bitfinex/test_sequence_tracker.py
git commit -m "✅ Test: SequenceTracker — pure public-seq gap detector"
```

---

### Task 2: PeriodicReconcile.request_resync + debounce

**Files:**
- Modify: `backend_py/src/bfx_funding_bot/modules/execution/periodic_reconcile.py`
- Test: `backend_py/tests/modules/execution/test_periodic_reconcile.py` (append cases)

- [ ] **Step 1: Write the failing tests**

Append to `backend_py/tests/modules/execution/test_periodic_reconcile.py`:

```python
@pytest.mark.asyncio
async def test_request_resync_wakes_loop_before_interval():
    """A resync request triggers an off-interval tick well before interval_s."""
    probe = _FakeProbe()
    recovery = _FakeRecovery(results=[ReconcileResult(0, 0, 0)])
    pr = PeriodicReconcile(
        recovery=recovery, probe=probe, interval_s=10.0,  # long: only a trigger can cause tick 2
        max_consecutive_failures=3, min_resync_interval_s=0.0,
    )
    stop = asyncio.Event()

    async def _drive():
        await asyncio.sleep(0.02)
        pr.request_resync("reconnect")
        await asyncio.sleep(0.05)
        stop.set()

    await asyncio.gather(pr.run_loop(stop), _drive())
    assert recovery._i >= 2  # tick 1 at loop start + tick 2 from the resync


@pytest.mark.asyncio
async def test_repeated_requests_dedup_into_bounded_ticks():
    """Many request_resync calls before a wake collapse into one extra tick."""
    probe = _FakeProbe()
    recovery = _FakeRecovery(results=[ReconcileResult(0, 0, 0)])
    pr = PeriodicReconcile(
        recovery=recovery, probe=probe, interval_s=10.0,
        max_consecutive_failures=3, min_resync_interval_s=0.05,
    )
    stop = asyncio.Event()

    async def _drive():
        await asyncio.sleep(0.01)
        for _ in range(20):
            pr.request_resync("seq_gap")  # storm
        await asyncio.sleep(0.03)  # < debounce window → no second resync tick yet
        stop.set()

    await asyncio.gather(pr.run_loop(stop), _drive())
    # tick 1 (loop start) + at most one debounced resync tick within the window
    assert recovery._i <= 2


@pytest.mark.asyncio
async def test_stop_during_debounce_exits_promptly():
    """Stopping while a resync is in its debounce wait exits without hanging."""
    probe = _FakeProbe()
    recovery = _FakeRecovery(results=[ReconcileResult(0, 0, 0)])
    pr = PeriodicReconcile(
        recovery=recovery, probe=probe, interval_s=10.0,
        max_consecutive_failures=3, min_resync_interval_s=100.0,  # long debounce
    )
    stop = asyncio.Event()

    async def _drive():
        await asyncio.sleep(0.02)
        pr.request_resync("reconnect")  # enters a 100s debounce wait
        await asyncio.sleep(0.02)
        stop.set()  # must break the debounce wait

    await asyncio.wait_for(
        asyncio.gather(pr.run_loop(stop), _drive()), timeout=2.0,
    )
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd backend_py && uv run pytest tests/modules/execution/test_periodic_reconcile.py -q`
Expected: FAIL — `TypeError: __init__() got an unexpected keyword argument 'min_resync_interval_s'` (and `AttributeError: 'PeriodicReconcile' object has no attribute 'request_resync'`).

- [ ] **Step 3: Write the implementation**

In `periodic_reconcile.py`, add to the existing stdlib imports at the top of the file (keep the existing `from bfx_funding_bot...` domain imports below them untouched): add `import contextlib`, `import time`, and `from collections.abc import Callable`. The stdlib/typing import section should read:

```python
from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Callable
from typing import Protocol
```

Replace the `__init__` (currently lines ~39-53) with:

```python
    def __init__(
        self,
        *,
        recovery: _Recovery,
        probe: _Probe,
        interval_s: float,
        max_consecutive_failures: int = 3,
        min_resync_interval_s: float = 10.0,
        monotonic: Callable[[], float] | None = None,
    ) -> None:
        self._recovery = recovery
        self._probe = probe
        self._interval_s = interval_s
        self._max_failures = max_consecutive_failures
        self._min_resync_interval_s = min_resync_interval_s
        self._monotonic = monotonic or time.monotonic
        self._consecutive_failures = 0
        self._tripped_down = False  # this loop owns the EXECUTOR DOWN it sets
        self._divergence_flagged = False  # this loop owns HealthTarget.RECONCILE
        self._resync_event = asyncio.Event()
        self._resync_reason = ""
        self._last_tick_mono = 0.0
```

Replace `run_loop` (currently lines ~55-62) with:

```python
    def request_resync(self, reason: str) -> None:
        """Request one off-interval reconcile. Synchronous and safe to call from a
        WS callback (same event loop). Multiple calls before the next wake collapse
        into a single reconcile (the Event is idempotent)."""
        self._resync_reason = reason
        self._resync_event.set()

    async def run_loop(self, stop_event: asyncio.Event) -> None:
        while not stop_event.is_set():
            await self._tick()
            self._last_tick_mono = self._monotonic()
            self._probe.record_heartbeat(self.SUB_TASK)
            if await self._wait_next(stop_event):
                reason = self._resync_reason
                self._resync_event.clear()
                await self._debounce(stop_event)
                log.info("periodic_reconcile_resync reason=%s", reason)

    async def _wait_next(self, stop_event: asyncio.Event) -> bool:
        """Sleep up to interval_s, waking early on stop or a resync request.
        Returns True iff a resync was requested (not on timeout/stop)."""
        stop_task = asyncio.create_task(stop_event.wait())
        trig_task = asyncio.create_task(self._resync_event.wait())
        try:
            done, _pending = await asyncio.wait(
                {stop_task, trig_task},
                timeout=self._interval_s,
                return_when=asyncio.FIRST_COMPLETED,
            )
        finally:
            for t in (stop_task, trig_task):
                t.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await t
        return trig_task in done and not stop_event.is_set()

    async def _debounce(self, stop_event: asyncio.Event) -> None:
        """Enforce >= min_resync_interval_s between ticks; stop-interruptible so a
        storm of triggers can never reconcile faster than the window."""
        remaining = self._min_resync_interval_s - (self._monotonic() - self._last_tick_mono)
        if remaining > 0:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop_event.wait(), timeout=remaining)
```

Leave `_tick` (lines ~64-107) **unchanged**.

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd backend_py && uv run pytest tests/modules/execution/test_periodic_reconcile.py -q`
Expected: PASS — the 5 existing tests stay green (proving `_tick` is unchanged) + 3 new tests pass.

- [ ] **Step 5: mypy + ruff**

Run: `cd backend_py && uv run mypy src/bfx_funding_bot/modules/execution/periodic_reconcile.py && uv run ruff check src/bfx_funding_bot/modules/execution/periodic_reconcile.py`
Expected: clean.

- [ ] **Step 6: Commit**

```bash
cd /Users/will/second-brain/projects/startup/bfx-funding-bot
git add backend_py/src/bfx_funding_bot/modules/execution/periodic_reconcile.py \
        backend_py/tests/modules/execution/test_periodic_reconcile.py
git commit -m "✨ Feat: PeriodicReconcile.request_resync — deduped, debounced off-interval reconcile"
```

---

### Task 3: Public-seq extraction for every channel frame

**Files:**
- Modify: `backend_py/src/bfx_funding_bot/external/bitfinex/auth_ws.py`
- Test: `backend_py/tests/external/bitfinex/test_auth_ws_public_seq.py`

**Why a heuristic, not a fixed index:** with SEQ_ALL the public seq sits at the *end* of the frame, but channel-0 *data* frames also append an auth seq (`[…, MSG_SEQ, AUTH_SEQ]`) while heartbeats do not (`[…, MSG_SEQ]`). Tracking only typed domain events would miss the seq that heartbeats and snapshot/`Unknown` frames consume, so a sparse domain event would always look like a gap. We therefore extract the public seq from **every** channel frame uniformly. For data frames this returns the same value the code already used (`msg[3]`). The exact layout is confirmed against a real frame in Task 7.

- [ ] **Step 1: Write the failing test**

Create `backend_py/tests/external/bitfinex/test_auth_ws_public_seq.py`:

```python
from bfx_funding_bot.external.bitfinex.auth_ws import (
    Heartbeat,
    Unknown,
    _public_seq,
    parse_frame,
)


def test_public_seq_channel0_data_frame_takes_msg_seq_not_auth_seq():
    # [chan, type, payload, MSG_SEQ, AUTH_SEQ]
    msg = [0, "foc", [123, None, None], 77, 5]
    assert _public_seq(msg) == 77


def test_public_seq_heartbeat_frame_takes_last_int():
    # [chan, "hb", MSG_SEQ]
    assert _public_seq([0, "hb", 78]) == 78


def test_public_seq_data_frame_without_auth_seq_takes_last_int():
    assert _public_seq([0, "foc", [1, 2], 79]) == 79


def test_public_seq_no_seq_present_is_none():
    assert _public_seq([0, "foc", [1, 2]]) is None
    assert _public_seq([0, "hb"]) is None


def test_heartbeat_event_carries_public_seq():
    ev = parse_frame("[0,\"hb\",78]")
    assert isinstance(ev, Heartbeat)
    assert ev.raw_seq == 78


def test_unknown_channel_frame_carries_public_seq():
    # an unmodelled channel-0 frame (e.g. wallet update) still consumes a seq
    ev = parse_frame("[0,\"wu\",[\"funding\",\"USD\",100],90,6]")
    assert isinstance(ev, Unknown)
    assert ev.raw_seq == 90
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend_py && uv run pytest tests/external/bitfinex/test_auth_ws_public_seq.py -q`
Expected: FAIL — `ImportError: cannot import name '_public_seq'` and `Heartbeat()`/`Unknown` lack `raw_seq`.

- [ ] **Step 3: Write the implementation**

In `auth_ws.py`, add `raw_seq` to the `Heartbeat` dataclass (currently lines ~93-95):

```python
@dataclass(frozen=True, slots=True)
class Heartbeat(BfxWSEvent):
    raw_seq: int | None = None
```

Add `raw_seq` to the `Unknown` dataclass (currently lines ~105-107):

```python
@dataclass(frozen=True, slots=True)
class Unknown(BfxWSEvent):
    raw: Any
    raw_seq: int | None = None
```

Add the extractor just above `_parse_channel_msg` (around line 146):

```python
def _public_seq(msg: list[Any]) -> int | None:
    """Public sequence number from a SEQ_ALL channel frame.

    Channel-0 data frames end with [..., MSG_SEQ, AUTH_SEQ] → public = 2nd-to-last;
    heartbeats / frames without an auth seq end with [..., MSG_SEQ] → public = last.
    Returns None when no trailing int seq is present (flag not applied).
    """
    if len(msg) >= 2 and isinstance(msg[-1], int) and isinstance(msg[-2], int):
        return msg[-2]
    if msg and isinstance(msg[-1], int):
        return msg[-1]
    return None
```

Replace the body of `_parse_channel_msg` (currently lines ~147-174) so all frames route their seq through `_public_seq`:

```python
def _parse_channel_msg(msg: list[Any]) -> BfxWSEvent | None:
    if len(msg) < 2:
        return None
    raw_seq = _public_seq(msg)
    payload = msg[1]
    if payload == "hb":
        return Heartbeat(raw_seq=raw_seq)
    if not isinstance(payload, str) or len(msg) < 3:
        return None

    msg_type = payload
    data = msg[2]

    if not isinstance(data, list):
        return None

    try:
        if msg_type == "fcn":
            return _parse_fcn(data, raw_seq)
        if msg_type == "fcu":
            return _parse_fcu(data, raw_seq)
        if msg_type == "foc":
            return _parse_foc(data, raw_seq)
    except (IndexError, TypeError, ValueError, BitfinexShapeError) as e:
        log.warning("bfx_ws_parse_failed type=%s err=%r", msg_type, e)
        return None

    return Unknown(raw=msg, raw_seq=raw_seq)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd backend_py && uv run pytest tests/external/bitfinex/test_auth_ws_public_seq.py tests/external/bitfinex/test_auth_ws_parse_frame.py -q`
Expected: PASS — new seq tests pass AND the existing `parse_frame` tests stay green (existing fixtures have no trailing int seq → `_public_seq` returns `None`, matching prior `msg[3]`-absent behaviour).

- [ ] **Step 5: mypy + ruff**

Run: `cd backend_py && uv run mypy src/bfx_funding_bot/external/bitfinex/auth_ws.py && uv run ruff check src/bfx_funding_bot/external/bitfinex/auth_ws.py`
Expected: clean.

- [ ] **Step 6: Commit**

```bash
cd /Users/will/second-brain/projects/startup/bfx-funding-bot
git add backend_py/src/bfx_funding_bot/external/bitfinex/auth_ws.py \
        backend_py/tests/external/bitfinex/test_auth_ws_public_seq.py
git commit -m "✨ Feat: extract Bitfinex public seq on every channel frame (hb + Unknown carry raw_seq)"
```

---

### Task 4: auth_ws — SEQ_ALL conf + reconnect & seq-gap resync

**Files:**
- Modify: `backend_py/src/bfx_funding_bot/external/bitfinex/auth_ws.py`
- Test: `backend_py/tests/external/bitfinex/test_auth_ws_seq_resync.py`

- [ ] **Step 1: Write the failing tests**

Create `backend_py/tests/external/bitfinex/test_auth_ws_seq_resync.py`:

```python
"""auth_ws fires on_resync_needed on reconnect and on a public-seq gap, and sends
the SEQ_ALL conf frame on connect."""
import asyncio
import contextlib
import json
from typing import Any

import pytest
import websockets
from websockets.asyncio.server import serve as ws_serve

from bfx_funding_bot.external.bitfinex.auth_ws import (
    SEQ_ALL_FLAG,
    BitfinexAuthWSClient,
)
from bfx_funding_bot.modules.execution.protocols import Credentials


class _SeqServer:
    """After auth, optionally push a scripted list of channel frames."""

    def __init__(self, frames: list[list] | None = None) -> None:
        self.received: list[dict] = []
        self.connections: list = []
        self._frames = frames or []

    async def handler(self, websocket: Any) -> None:
        self.connections.append(websocket)
        try:
            async for msg in websocket:
                data = json.loads(msg)
                self.received.append(data)
                if data.get("event") == "auth":
                    await websocket.send(json.dumps({
                        "event": "auth", "status": "OK", "chanId": 0, "userId": 1,
                    }))
                    for fr in self._frames:
                        await websocket.send(json.dumps(fr))
        except websockets.ConnectionClosed:
            pass


async def _serve(server: _SeqServer):
    s = await ws_serve(server.handler, "127.0.0.1", 0)
    port = s.sockets[0].getsockname()[1]
    return s, f"ws://127.0.0.1:{port}"


@pytest.mark.asyncio
async def test_conf_seq_all_sent_on_connect():
    server = _SeqServer()
    s, url = await _serve(server)
    client = BitfinexAuthWSClient(creds=Credentials(api_key="k", api_secret="s"),
                                  url=url, nonce_provider=lambda: 1)

    async def run():
        async for _ in client.events():
            break

    task = asyncio.create_task(run())
    await asyncio.sleep(0.3)
    await client.close()
    with contextlib.suppress(asyncio.TimeoutError, asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=2.0)
    s.close(); await s.wait_closed()

    assert any(
        m.get("event") == "conf" and m.get("flags") == SEQ_ALL_FLAG
        for m in server.received
    )


@pytest.mark.asyncio
async def test_reconnect_fires_resync_but_first_connect_does_not():
    server = _SeqServer()
    s, url = await _serve(server)
    reasons: list[str] = []
    client = BitfinexAuthWSClient(
        creds=Credentials(api_key="k", api_secret="s"), url=url,
        nonce_provider=lambda: 1, on_resync_needed=reasons.append,
    )

    async def run():
        with contextlib.suppress(Exception):
            async for _ in client.events():
                await asyncio.sleep(0.02)

    task = asyncio.create_task(run())
    await asyncio.sleep(0.3)
    assert "reconnect" not in reasons  # first connection: no resync

    for ws in list(server.connections):
        await ws.close()  # force a client reconnect
    await asyncio.sleep(1.5)

    await client.close()
    with contextlib.suppress(asyncio.TimeoutError, asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=2.0)
    s.close(); await s.wait_closed()

    assert "reconnect" in reasons


@pytest.mark.asyncio
async def test_public_seq_gap_fires_seq_gap_resync():
    # Two heartbeats with a gap: seq 10 then 13 → "seq_gap".
    server = _SeqServer(frames=[[0, "hb", 10], [0, "hb", 13]])
    s, url = await _serve(server)
    reasons: list[str] = []
    client = BitfinexAuthWSClient(
        creds=Credentials(api_key="k", api_secret="s"), url=url,
        nonce_provider=lambda: 1, on_resync_needed=reasons.append,
    )

    async def run():
        with contextlib.suppress(Exception):
            n = 0
            async for _ in client.events():
                n += 1
                if n >= 2:
                    break

    task = asyncio.create_task(run())
    await asyncio.wait_for(task, timeout=3.0)
    await client.close()
    s.close(); await s.wait_closed()

    assert "seq_gap" in reasons
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd backend_py && uv run pytest tests/external/bitfinex/test_auth_ws_seq_resync.py -q`
Expected: FAIL — `TypeError: __init__() got an unexpected keyword argument 'on_resync_needed'`.

- [ ] **Step 3: Write the implementation**

In `auth_ws.py`, extend the `BitfinexAuthWSClient.__init__` signature (currently lines ~280-298) to add the `on_resync_needed` param and the integrity state:

```python
    def __init__(
        self,
        *,
        creds: Credentials,
        url: str = BITFINEX_AUTH_WS_URL,
        hb_timeout_s: float = 30.0,
        nonce_provider: Callable[[], int] | None = None,
        on_disconnect: Callable[[str], None] | None = None,
        on_resync_needed: Callable[[str], None] | None = None,
    ) -> None:
        self._creds = creds
        self._url = url
        self._hb_timeout_s = hb_timeout_s
        self._nonce_provider = nonce_provider or (lambda: int(time.time() * 1000))
        self._on_disconnect = on_disconnect
        self._on_resync_needed = on_resync_needed
        self._ws: ClientConnection | None = None
        self._stop = False
        self.reconnect_attempts = 0
        self._reconnect_history: deque[float] = deque(maxlen=1000)
        self._last_msg_ts: float = time.monotonic()
        self._seq = SequenceTracker()
        self._connection_count = 0
```

Replace `_connect_and_stream` (currently lines ~343-358) with:

```python
    def _fire_resync(self, reason: str) -> None:
        if self._on_resync_needed is not None:
            with contextlib.suppress(Exception):
                self._on_resync_needed(reason)

    async def _connect_and_stream(self) -> AsyncIterator[BfxWSEvent]:
        async with websockets.connect(self._url, max_size=2**20) as ws:
            self._ws = ws
            self._seq.reset()
            self._connection_count += 1
            if self._connection_count > 1:
                # A reconnect: events during the gap were lost → resync the ledger.
                # (First connection is covered by boot reconcile, so it fires nothing.)
                self._fire_resync("reconnect")
            auth = build_auth_payload(
                api_key=self._creds.api_key,
                api_secret=self._creds.api_secret,
                nonce_ms=self._nonce_provider(),
            )
            await ws.send(json.dumps(auth))
            await ws.send(json.dumps({"event": "conf", "flags": SEQ_ALL_FLAG}))

            async for raw in ws:
                event = parse_frame(raw)
                if event is not None:
                    if self._seq.observe(_public_seq_of(event)) == "gap":
                        self._fire_resync("seq_gap")
                    yield event
                if self._stop:
                    return
```

Add the seq accessor helper near `_public_seq` (module level, around line 146):

```python
def _public_seq_of(event: BfxWSEvent) -> int | None:
    return getattr(event, "raw_seq", None)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd backend_py && uv run pytest tests/external/bitfinex/test_auth_ws_seq_resync.py tests/external/bitfinex/test_auth_ws_lifecycle.py -q`
Expected: PASS — new resync tests pass AND existing lifecycle tests stay green (the extra `conf` send does not change the events they collect).

- [ ] **Step 5: mypy + ruff (whole module)**

Run: `cd backend_py && uv run mypy src/bfx_funding_bot/external/bitfinex/auth_ws.py && uv run ruff check src/bfx_funding_bot/external/bitfinex/auth_ws.py`
Expected: clean.

- [ ] **Step 6: Commit**

```bash
cd /Users/will/second-brain/projects/startup/bfx-funding-bot
git add backend_py/src/bfx_funding_bot/external/bitfinex/auth_ws.py \
        backend_py/tests/external/bitfinex/test_auth_ws_seq_resync.py
git commit -m "✨ Feat: auth_ws fires resync on reconnect + public-seq gap; enables SEQ_ALL"
```

---

### Task 5: Daemon wiring + BFX_RESYNC_MIN_INTERVAL_S

**Files:**
- Modify: `backend_py/src/bfx_funding_bot/modules/marketfeed/daemon.py`
- Test: `backend_py/tests/modules/marketfeed/test_daemon_wiring.py` (append a case)

- [ ] **Step 1: Write the failing test**

Append to `backend_py/tests/modules/marketfeed/test_daemon_wiring.py` (reuses the module's `_write_cells_yaml` helper):

```python
@pytest.mark.asyncio
async def test_auth_ws_resync_wired_to_periodic_reconcile(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    httpx_mock: HTTPXMock,
) -> None:
    """Live executor + WS client: auth_ws.on_resync_needed is bound to
    periodic_reconcile.request_resync so a stream break triggers an off-interval
    reconcile."""
    from bfx_funding_bot.modules.marketfeed.daemon import build_daemon

    safety_canary = Path(__file__).parents[3] / "configs" / "safety.canary.yaml"
    monkeypatch.setenv("BFX_PHASE", "canary")
    monkeypatch.setenv("BFX_DEPLOYMENT_ENV", "prod")
    monkeypatch.setenv("BFX_SAFETY_CONFIG", str(safety_canary))
    monkeypatch.setenv("BFX_EXECUTOR", "bitfinex_live")
    monkeypatch.setenv("BFX_WS_CLIENT_ENABLED", "true")
    monkeypatch.setenv("BFX_SERVICE_VERSION", "test-sha")
    db_path = tmp_path / "resync_wiring.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{db_path}")
    monkeypatch.setenv("BFX_HEALTHZ_PORT", "0")
    monkeypatch.setenv("BFX_ACCOUNT_ID", "default")
    monkeypatch.setenv("BFX_API_KEY", "test_key")
    monkeypatch.setenv("BFX_API_SECRET", "test_secret")
    monkeypatch.setenv("BFX_ALLOCATION_CAP_USDT", "500")
    monkeypatch.setenv("BFX_RESYNC_MIN_INTERVAL_S", "7")

    daemon = await build_daemon(cells_yaml_path=_write_cells_yaml(tmp_path))

    assert daemon.auth_ws is not None
    assert daemon.periodic_reconcile is not None
    # bound method equality: same __self__ + __func__
    assert daemon.auth_ws._on_resync_needed == daemon.periodic_reconcile.request_resync
    assert daemon.periodic_reconcile._min_resync_interval_s == 7.0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend_py && uv run pytest tests/modules/marketfeed/test_daemon_wiring.py::test_auth_ws_resync_wired_to_periodic_reconcile -q`
Expected: FAIL — `assert None == <bound method ...>` (auth_ws built with `on_resync_needed=None`), or `min_resync_interval_s` mismatch.

> If the build raises before the assertion (a `bitfinex_live` boot prerequisite this env recipe misses), add the missing env to the test from the failure message — the assertions above are the contract; the env scaffolding mirrors the existing `test_build_daemon_emit_and_query_env_symmetric` canary case.

- [ ] **Step 3: Write the implementation**

In `daemon.py`, in the `if not spec.is_simulated:` block, after the existing `reconcile_interval_s` validation (around line 826-830), add:

```python
        resync_min_interval_s = float(os.environ.get("BFX_RESYNC_MIN_INTERVAL_S", "10"))
        if resync_min_interval_s < 0:
            raise ValueError(
                f"BFX_RESYNC_MIN_INTERVAL_S must be >= 0, got {resync_min_interval_s}"
            )
```

Update the `PeriodicReconcile(...)` construction (around line 842-846) to pass the new arg:

```python
        periodic_reconcile = PeriodicReconcile(
            recovery=runtime_recovery,
            probe=probe,
            interval_s=reconcile_interval_s,
            min_resync_interval_s=resync_min_interval_s,
        )
```

Update the `auth_ws = BitfinexAuthWSClient(creds=creds)` construction (around line 1070) to wire the callback:

```python
        auth_ws = BitfinexAuthWSClient(
            creds=creds,
            on_resync_needed=(
                periodic_reconcile.request_resync
                if periodic_reconcile is not None
                else None
            ),
        )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend_py && uv run pytest tests/modules/marketfeed/test_daemon_wiring.py -q`
Expected: PASS (existing wiring tests + the new binding test).

- [ ] **Step 5: mypy + ruff**

Run: `cd backend_py && uv run mypy src/bfx_funding_bot/modules/marketfeed/daemon.py && uv run ruff check src/bfx_funding_bot/modules/marketfeed/daemon.py`
Expected: clean.

- [ ] **Step 6: Commit**

```bash
cd /Users/will/second-brain/projects/startup/bfx-funding-bot
git add backend_py/src/bfx_funding_bot/modules/marketfeed/daemon.py \
        backend_py/tests/modules/marketfeed/test_daemon_wiring.py
git commit -m "✨ Feat: wire auth_ws resync → PeriodicReconcile; add BFX_RESYNC_MIN_INTERVAL_S"
```

---

### Task 6: Integration — reconnect triggers an off-interval reconcile

**Files:**
- Create: `backend_py/tests/integration/test_resync_trigger_off_interval.py`

Proves end-to-end with real objects (real `DomainEventBus` + `PaperPositionLedger` + `OfferRegistry` + `BootRecovery` + `PeriodicReconcile`) that a `request_resync` causes a reconcile **off the interval**: tick 1 (loop start) converges the seeded drift, then with a 3600 s interval the trigger forces a *second* `BootRecovery.run` within milliseconds — work the timer would not have done for an hour. A counting wrapper records each run, so the assertion does not depend on the second run releasing anything again (that would be deduped); it asserts the off-interval reconcile *ran*.

- [ ] **Step 1: Write the failing test**

Create `backend_py/tests/integration/test_resync_trigger_off_interval.py`:

```python
"""Integration: a resync trigger (as auth_ws fires on reconnect / seq-gap) makes
PeriodicReconcile run a reconcile OFF the interval — far sooner than the timer.
Real DomainEventBus + ledger + OfferRegistry + BootRecovery + PeriodicReconcile.
"""
from __future__ import annotations

import asyncio
from decimal import Decimal
from typing import Any
from uuid import uuid4

import pytest

from bfx_funding_bot.modules.execution.boot_recovery import BootRecovery, ReconcileResult
from bfx_funding_bot.modules.execution.event_store.tables import OfferClaimRow
from bfx_funding_bot.modules.execution.events import ReservationClaimed
from bfx_funding_bot.modules.execution.periodic_reconcile import PeriodicReconcile
from bfx_funding_bot.modules.execution.protocols import AccountContext, Credentials
from bfx_funding_bot.modules.execution.registry_offers import RegistryState

# Reuse the stub session/store/auth-rest shapes from the WS-dead integration test.
from tests.integration.test_reconcile_converges_without_ws import (
    _EmptyAuthRest,
    _FakeProbe,
    _OneClaimSessionFactory,
    _StubStore,
)

_NOW = 2_000_000
_ACCOUNT = "default"
_ENV = "ci"
_VOI = "777"
_CID = 777
_SIZE = Decimal("150")
_ACTION_GRACE_MS = 120_000
_CLAIM_OCCURRED_MS = _NOW - 600_000


class _CountingRecovery:
    """Wraps a real BootRecovery, recording each run() so we can assert the
    trigger produced an off-interval reconcile (independent of dedup)."""

    def __init__(self, inner: BootRecovery) -> None:
        self._inner = inner
        self.calls = 0

    async def run(self) -> ReconcileResult:
        self.calls += 1
        return await self._inner.run()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_resync_request_reconciles_off_interval(
    domain_chain: dict[str, Any],
) -> None:
    bus = domain_chain["bus"]
    ledger = domain_chain["ledger"]
    scid = uuid4()

    await bus.publish(ReservationClaimed(
        cid=_CID, venue_offer_id=_VOI, size_usdt=_SIZE,
        signal_correlation_id=scid, account_id=_ACCOUNT, is_simulated=False,
        occurred_at_ms=_CLAIM_OCCURRED_MS,
    ))
    assert ledger.current_exposure() == Decimal("150")

    claim_row = OfferClaimRow(
        cid=_CID, account_id=_ACCOUNT, deployment_environment=_ENV,
        state=RegistryState.CLAIMED.value, venue_offer_id=_VOI, size_usdt=_SIZE,
        signal_correlation_id=str(scid), occurred_at_ms=_CLAIM_OCCURRED_MS,
        last_updated_ms=_CLAIM_OCCURRED_MS, last_event_seq=1,
    )
    inner = BootRecovery(
        store=_StubStore(),  # type: ignore[arg-type]
        session_factory=_OneClaimSessionFactory(claim_row),  # type: ignore[arg-type]
        auth_rest=_EmptyAuthRest(),
        account_ctx=AccountContext(
            account_id=_ACCOUNT,
            credentials=Credentials(api_key="k", api_secret="s"),
            allocation_cap_usdt=Decimal("450"),
        ),
        deployment_environment=_ENV, bus=bus, is_simulated=False,
        action_grace_ms=_ACTION_GRACE_MS, max_attempts=1, backoff_base_s=0,
        clock=lambda: _NOW,
    )
    recovery = _CountingRecovery(inner)

    # Huge interval: only an off-interval resync can produce a second reconcile.
    pr = PeriodicReconcile(
        recovery=recovery, probe=_FakeProbe(), interval_s=3600.0,
        max_consecutive_failures=3, min_resync_interval_s=0.0,
    )
    stop = asyncio.Event()

    async def _drive() -> None:
        await asyncio.sleep(0.05)
        assert recovery.calls == 1                       # tick 1 (loop start) only
        assert ledger.current_exposure() == Decimal("0")  # ...and it really converged
        pr.request_resync("reconnect")                   # the trigger under test
        await asyncio.sleep(0.1)
        assert recovery.calls >= 2                        # off-interval reconcile ran
        stop.set()

    await asyncio.wait_for(asyncio.gather(pr.run_loop(stop), _drive()), timeout=5.0)
```

- [ ] **Step 2: Run test to verify it passes (and is not vacuous)**

Run: `cd backend_py && uv run pytest -m integration tests/integration/test_resync_trigger_off_interval.py -q`
Expected: PASS. (Not vacuous: the `recovery.calls >= 2` assertion can only hold because the trigger fired a second reconcile — the 3600 s interval cannot. Drop the `request_resync` line and it fails.)

- [ ] **Step 3: Commit**

```bash
cd /Users/will/second-brain/projects/startup/bfx-funding-bot
git add backend_py/tests/integration/test_resync_trigger_off_interval.py
git commit -m "✅ Test: integration — resync trigger reconciles off-interval"
```

---

### Task 7: Gated-live — verify the public-seq layout against a real frame

**Files:**
- Create: `backend_py/tests/external/bitfinex/test_auth_ws_seq_live.py`

This is the empirical check the spec defers to planning: confirm the SEQ_ALL frame layout `_public_seq` assumes (and that heartbeats carry a contiguous public seq). Excluded from the commit gate (`@pytest.mark.integration`); run manually with real credentials.

- [ ] **Step 1: Write the gated-live test**

Create `backend_py/tests/external/bitfinex/test_auth_ws_seq_live.py`:

```python
"""Gated-live contract: real Bitfinex auth stream with SEQ_ALL on.

Run manually:  BFX_API_KEY=... BFX_API_SECRET=... \
  uv run pytest -m integration tests/external/bitfinex/test_auth_ws_seq_live.py -q

Verifies the public-seq layout `_public_seq` assumes is real, and that the public
seq increments contiguously across consecutive frames (incl. heartbeats). If this
fails, `_public_seq` needs to match the captured layout before seq-gap is trusted.
"""
from __future__ import annotations

import asyncio
import os

import pytest

from bfx_funding_bot.external.bitfinex.auth_ws import (
    BitfinexAuthWSClient,
    _public_seq_of,
)
from bfx_funding_bot.modules.execution.protocols import Credentials

_KEY = os.environ.get("BFX_API_KEY")
_SECRET = os.environ.get("BFX_API_SECRET")


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.skipif(not (_KEY and _SECRET), reason="needs BFX_API_KEY/SECRET")
async def test_real_auth_stream_public_seq_is_contiguous() -> None:
    client = BitfinexAuthWSClient(creds=Credentials(api_key=_KEY, api_secret=_SECRET))
    seqs: list[int] = []

    async def collect() -> None:
        async for ev in client.events():
            s = _public_seq_of(ev)
            if s is not None:
                seqs.append(s)
            if len(seqs) >= 5:  # auth ack + a few heartbeats
                break

    await asyncio.wait_for(collect(), timeout=60.0)
    await client.close()

    assert len(seqs) >= 2, f"no public seq observed — SEQ_ALL layout wrong? {seqs}"
    # consecutive observed frames must be strictly contiguous (no real loss in 60s)
    assert all(b == a + 1 for a, b in zip(seqs, seqs[1:])), f"non-contiguous: {seqs}"
```

- [ ] **Step 2: (Manual, optional) Run against the live venue**

Run: `cd backend_py && BFX_API_KEY=… BFX_API_SECRET=… uv run pytest -m integration tests/external/bitfinex/test_auth_ws_seq_live.py -q`
Expected: PASS — confirms `_public_seq` reads the right element and heartbeats keep the seq contiguous. If it fails, adjust `_public_seq` to the observed layout and re-run Task 3/4 unit tests.

- [ ] **Step 3: Verify it is skipped in the default gate**

Run: `cd backend_py && uv run pytest -m "not integration" tests/external/bitfinex/test_auth_ws_seq_live.py -q`
Expected: deselected/skipped (0 run) — it never blocks the commit gate.

- [ ] **Step 4: Commit**

```bash
cd /Users/will/second-brain/projects/startup/bfx-funding-bot
git add backend_py/tests/external/bitfinex/test_auth_ws_seq_live.py
git commit -m "✅ Test: gated-live contract for SEQ_ALL public-seq layout"
```

---

## Final verification (after all tasks)

- [ ] **Full commit gate:**

```bash
cd backend_py && uv run pytest -m "not integration" -q && uv run mypy src/ && uv run ruff check
```
Expected: all green.

- [ ] **Integration suite:**

```bash
cd backend_py && uv run pytest -m integration -q
```
Expected: green (excluding the credential-gated live test, which skips without keys).

## Rollout (from the spec)

1. Commit gate green (above).
2. Run the gated-live test (Task 7) with real keys to confirm the seq layout.
3. Deploy canary via the **manual** `scripts/deploy-koyeb.sh` (auto-deploy is disabled). Optionally set `BFX_RESYNC_MIN_INTERVAL_S` (default 10).
4. Verify in logs: on the next WS reconnect, a `periodic_reconcile_resync reason=reconnect` line appears off the normal interval cadence; divergence counts behave as before.
