# Resync on Reconnect / Seq-Gap — Design

**Date:** 2026-05-27
**Status:** Approved (brainstorming)
**Follow-up to:** `2026-05-27-live-venue-reconcile-backbone-design.md` (item *d*)

## Context

The reconcile backbone (prior spec) made the ledger converge to venue truth on a
fixed `BFX_RECONCILE_INTERVAL_S` timer (default 90 s) — correctness no longer
depends on the WS stream. That spec's best-practice table already named the
remaining gap:

> *Snapshot + incremental sync; re-sync on **seq gap / reconnect**, not only a timer.*

This spec implements that line: when the authenticated WS stream loses events,
fire an **immediate, off-interval reconcile** instead of waiting up to a full
interval. The timer remains the correctness backbone; triggers only cut latency.

System is pre-launch, so we build the industry-standard mechanism rather than the
minimal patch.

## Why both triggers (industry best practice)

Exchange connectivity / OMS drop-copy reconciliation treats the stream as a
latency optimization that is allowed to fail, and guarantees correctness through
**sequence integrity + snapshot reconcile**. Reconnect and seq-gap are not
alternatives — they are two detectors of the same thing (lost events) feeding one
recovery action (snapshot reconcile):

| Detector | Failure it catches | Without it |
|---|---|---|
| **seq-gap** (every packet carries a monotonic seq; a jump = dropped packet) | Silent in-connection message loss (queue overflow, dropped frame) — the **symptom class** of the 2026-05-26 incident | Only the timer notices, slowly |
| **reconnect** (a reconnect resets the seq baseline) | Events missed during a visible disconnect gap | After reconnect, no signal that the gap dropped events |

FIX drop-copy is the institutional analogue: an OMS never trusts its own fill
stream; a sequence break triggers reconciliation against the venue's
authoritative copy.

### Bitfinex sequencing — verified semantics

Enabling the sequencing config flag (`SEQ_ALL` = `65536`) via a `conf` event makes
Bitfinex append a **public sequence number to the end of every packet** (including
heartbeats), and an **auth sequence number** to channel-0 (authenticated) packets.
The public seq increments on every packet → strictly contiguous → the cleanest
"did I miss any packet" detector. The auth seq increments only on authenticated
actions.

- We track the **public seq** (contiguous, covers all loss). Auth-seq tracking is a
  non-goal.
- The seq lives in the **outer frame** (`msg[3]` public, `msg[4]` auth for
  channel-0), not inside the payload array `d` (which is `msg[2]`). So enabling
  `SEQ_ALL` does **not** shift the indices used by the `foc`/`fcn` payload parsers.
  `_parse_channel_msg` already reads `raw_seq = msg[3]`; it is `None` today only
  because the flag is never sent.
- **Open verification (planning):** the exact index of the public seq for
  channel-0 frames (`msg[3]` vs `msg[4]`) and whether heartbeat frames carry it
  must be pinned against a **real captured frame** via the gated-live harness,
  per this project's real-fixture methodology. The detector treats a missing /
  `None` seq as "no information" (never a false gap), so a wrong assumption
  degrades to "no seq-gap detection", not to false triggers.

Source: Bitfinex WS general docs; `bitfinexcom/bfx-api-node-plugin-seq-audit`.

## Architecture — multiple detectors → one resync → existing reconcile

```
auth_ws (transport integrity)            PeriodicReconcile (recovery coord)      BootRecovery (existing)
─────────────────────────────           ─────────────────────────────────      ──────────────────────
• send SEQ_ALL conf on connect           • request_resync(reason)  ◄──callback── (from auth_ws)
• track public seq per frame               └ set asyncio.Event
• gap OR (re)connect                      • run_loop waits {stop, trigger}, timeout=interval
   → on_resync_needed(reason) ──callback─►  └ wake: clear → debounce → _tick()
                                          • _tick() body UNCHANGED (fail-safe / divergence intact)
                                                   └► BootRecovery.run (authoritative snapshot reconcile)
```

The triggers reduce reconcile **latency** from "≤ interval" to "≈ debounce window".
The timer still runs; if every trigger path breaks, the system is still correct.

### How the three required constraints are met

| Constraint | Mechanism |
|---|---|
| **Dedup** | `asyncio.Event`: many `request_resync` calls before the next wake collapse into one; `set()` is idempotent. |
| **No race with the 90 s timer** | One loop, one `_tick()` at a time. A trigger only shortens the wait; timer-tick and trigger-tick can never run reconcile concurrently. |
| **Fail-safe unchanged** | `_tick()` body is byte-for-byte unchanged → consecutive-failure counter, `EXECUTOR DOWN`, and `RECONCILE DEGRADED` self-clear logic all preserved. |
| **Storm protection** (reconnect flapping / backoff) | Debounce: if the last tick was < `min_resync_interval_s` ago, wait out the remainder (stop-interruptible) before ticking → at most one reconcile per window. |

## Components & changes

| Component | File | Change |
|---|---|---|
| **SEQ_ALL conf** | `auth_ws.py::_connect_and_stream` | After sending the auth payload, send `{"event":"conf","flags":65536}`. Layout-safe (seq is outer-frame). |
| **Public-seq tracking + resync signal** | `auth_ws.py::BitfinexAuthWSClient` | New ctor param `on_resync_needed: Callable[[str], None] | None`. Per connection: reset the tracker on (re)connect; feed each frame's public seq to it; on a gap → `on_resync_needed("seq_gap")`; on a **non-first** connection becoming established → `on_resync_needed("reconnect")`. First connect fires nothing (boot reconcile already covered it). Callback wrapped in `suppress(Exception)`. |
| **Pure gap detector** | `auth_ws.py` (small helper, e.g. `SequenceTracker`) | `observe(seq: int | None) -> "ok" | "gap"` (None → "ok", no info); `reset()`. No socket, no domain types → unit-testable in isolation. |
| **Resync entry + debounce** | `periodic_reconcile.py::PeriodicReconcile` | New `request_resync(reason: str) -> None`: records reason, `set()`s an internal `asyncio.Event` (sync, safe from the WS callback — same loop). `run_loop` now waits on `{stop_event, trigger_event}` with `timeout=interval_s` instead of the pure timer. On a trigger wake: clear the event, apply the debounce window (stop-interruptible), log `periodic_reconcile_resync reason=… debounced=…`, then `_tick()`. New ctor params: `min_resync_interval_s: float`, and an injectable monotonic clock for tests. |
| **Daemon wiring** | `daemon.py` | When both `auth_ws` and `periodic_reconcile` are live (live executor path), set `auth_ws.on_resync_needed = periodic_reconcile.request_resync`. New env `BFX_RESYNC_MIN_INTERVAL_S` (default 10) → `min_resync_interval_s`. Paper/sim (either is `None`) → no wiring. |

Seq-gap detection lives in `auth_ws`, not the dispatcher, because (a) `auth_ws`
owns the reconnect boundary that is the seq-reset point, and (b) `auth_ws` sees
**every** frame including heartbeats (which carry the public seq), whereas the
dispatcher only receives typed domain events.

## Data flow

- **Normal:** seq contiguous → no trigger; timer ticks reconcile every interval.
- **Disconnect → reconnect:** `auth_ws` reconnects internally → new connection
  established → `resync("reconnect")` → loop wakes (debounced) → reconcile catches
  gap-period events → ledger converges in ≈ debounce window instead of ≤ interval.
- **Silent gap while connected:** public seq jumps → `SequenceTracker` → `gap` →
  `resync("seq_gap")` → same path. (This is the incident's symptom class.)
- **Storm** (flapping / backoff): many `request_resync` calls → Event collapse +
  debounce → ≤ 1 reconcile per `min_resync_interval_s`.
- **Trigger path broken:** timer still reconciles → correctness unaffected.

## Error handling / edge cases

- `on_resync_needed` wrapped in `suppress(Exception)` (mirrors `on_disconnect`) —
  never crashes the WS loop.
- `request_resync` is synchronous (only sets an Event) → safe to call from the
  `auth_ws` callback context (same event loop).
- **First connection fires no `reconnect` resync** — `BootRecovery` already
  reconciled at startup; avoids a redundant boot-time double reconcile.
- **(Re)connect resets the tracker** → the first frame of a new connection sets the
  baseline; no false gap from the seq restarting.
- **Missing / `None` seq** (flag not yet applied, non-seq frame) → tracker returns
  "ok" → no false gap. A wrong index assumption degrades to "no detection", never
  to false triggers.
- **Debounce wait is stop-interruptible** → never delays daemon shutdown.

## Testing

| Layer | Cases |
|---|---|
| unit — `SequenceTracker` (pure) | contiguous → ok; jump → gap; `reset()` → baseline, no false gap; `None` → ok |
| unit — `auth_ws` SEQ_ALL | `conf` frame sent after auth (fake socket); 2nd connection established → `on_resync_needed("reconnect")`; first connection → **no** resync; mid-stream seq jump → `("seq_gap")` |
| unit — `PeriodicReconcile` resync | `request_resync` wakes the loop before the interval (fake clock); dedup (N calls → 1 tick); debounce (rapid triggers → ≤ 1 tick / window); stop during debounce exits cleanly; trigger-path exception doesn't stop the timer; existing fail-safe / divergence tests stay green (proves `_tick` unchanged) |
| unit — daemon wiring | `auth_ws.on_resync_needed` bound to `periodic_reconcile.request_resync` when both live; None-safe on paper/sim |
| integration (extends incident repro) | reconnect mid-run → off-interval reconcile fires → ledger converges faster than the interval |
| gated-live (`@pytest.mark.integration`) | real auth frame carries the public seq at the assumed index; heartbeat seq behaviour — pins the planning verification |

Commit gate: `cd backend_py && uv run pytest -m "not integration"` green + `mypy src/` + `ruff check`. Live-contract tests excluded by default.

## Rollout

1. Land code; commit gate green.
2. (Optional but recommended) run the gated-live test with `BFX_API_KEY`/`SECRET`
   to confirm the public-seq index and capture a real seq-bearing fixture.
3. Deploy canary via the **manual** `scripts/deploy-koyeb.sh` (auto-deploy is
   disabled). Set `BFX_RESYNC_MIN_INTERVAL_S` if overriding the default.
4. Verify in logs: on the next WS reconnect, a `periodic_reconcile_resync
   reason=reconnect` line appears off the interval cadence; reconcile divergence
   counts behave as before.

## Non-goals / deferred (anti-gold-plating)

- **Force-reconnect on gap** — the snapshot reconcile is the authoritative recovery;
  cycling the connection is belt-and-suspenders. Add only if repeated gaps are
  observed in production.
- **Auth-seq (`msg[4]`) tracking** — public seq already covers "missed any packet".
- **Staleness-triggered resync** beyond the existing `hb_timeout_s` watchdog.
- **Per-currency / multi-symbol** reconcile — inherits `BootRecovery`'s current
  single-symbol scope; revisit with per-currency allocation.
