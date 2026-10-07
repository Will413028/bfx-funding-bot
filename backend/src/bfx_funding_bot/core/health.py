"""Shared health state, heartbeat thresholds, and pure assessment."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from bfx_funding_bot.core.telemetry import HealthStatus, HealthTarget

# ── Liveness sub-tasks (own-loop, event-loop-driven) ──────────────────────────
# A stale heartbeat means the loop is stuck or the event loop is deadlocked →
# restarting the process can recover. These DRIVE /healthz 503 and
# scan_staleness FatalError. Per spec D4 heartbeat threshold table.
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
    "periodic_reconcile": 3 * 90,  # BFX_RECONCILE_INTERVAL_S default 90s x 3 missed
                                    # (proactive: beats unconditionally each interval, so a
                                    # stale beat means the reconcile backbone is stuck → restart)
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
    # writer_lock liveness loop (daemon._writer_lock_liveness_loop) refreshes the
    # single-writer advisory lock every 30s and beats only on a SUCCESSFUL
    # refresh. This beat is OBSERVABILITY + RECOVERY ONLY — it must NEVER drive a
    # fatal escalation / daemon restart:
    #   - The authoritative fail-closed gate is the per-submit
    #     WriterLockGuard.verify_held(): if the lock isn't held, every real-money
    #     submit is blocked, so safety is preserved without any restart.
    #   - refresh() already attempts recovery (reconnect/re-acquire) each 30s.
    #   - A reactive restart loop (liveness keyed on a recovery task) is the
    #     documented 2026-05-26 canary anti-pattern.
    # Declared here (activity-class, NOT liveness) so a lost lock emits a WARN/down
    # observability event but is gated out of the fatal branch — never the silent
    # _DEFAULT_THRESHOLD_S=60 fallback. 90s comfortably exceeds the 30s loop cadence
    # so a healthy loop never trips (matches the "ws" 90s = 3-missed-beats budget).
    "writer_lock": 90,
}

# Merged view: scan_staleness needs a threshold for both classes to emit. The
# liveness/fatal gating is keyed on LIVENESS_THRESHOLDS membership, not on this.
SUB_TASK_THRESHOLDS: dict[str, int] = {**LIVENESS_THRESHOLDS, **ACTIVITY_THRESHOLDS}
_DEFAULT_THRESHOLD_S = 60

# A sub-task is EITHER liveness OR activity, never both. Overlap would make the
# merged dict silently take the ACTIVITY value and the scan_staleness gate would
# suppress fatal escalation for a task meant to be liveness — enforce at import.
assert not (LIVENESS_THRESHOLDS.keys() & ACTIVITY_THRESHOLDS.keys()), (
    "sub-task threshold keys must not overlap between LIVENESS and ACTIVITY"
)


# auth WS reconnects/hour at/above which we call it "flapping" (still authing
# sometimes, but unstable). ~6 consecutive failures already hits the 60s backoff cap.
_AUTH_WS_FLAP_THRESHOLD = 6


def assess_auth_ws_health(
    *, connection_count: int, auth_ok_count: int, reconnect_count_last_hour: int,
) -> tuple[HealthStatus, str | None]:
    """Observe-only health for the authenticated Bitfinex WS.

    Catches the failure a disconnect callback can't: an ABSENCE of success —
    the socket opens but never authenticates (the 2026-07 µs/ms nonce bug, dead
    for months because nothing looked). Returns a status for the HEALTH_CHECK
    emit path only; the caller MUST NOT record a liveness heartbeat off this, so
    a DOWN never drives /healthz 503 or autoheal restart — a systemic auth/nonce
    fault won't heal on restart, it would just flap-restart.
    """
    if connection_count == 0:
        return HealthStatus.HEALTHY, None  # boot grace: not connected yet
    if auth_ok_count == 0:
        return (
            HealthStatus.DOWN,
            f"auth WS connected ({connection_count}x) but never authenticated "
            "— check API key nonce scope (shared REST/WS per-key nonce)",
        )
    if reconnect_count_last_hour >= _AUTH_WS_FLAP_THRESHOLD:
        return (
            HealthStatus.DEGRADED,
            f"auth WS flapping (reconnects_last_hour={reconnect_count_last_hour})",
        )
    return HealthStatus.HEALTHY, None


@dataclass
class _TargetState:
    status: HealthStatus = HealthStatus.HEALTHY
    last_msg_age_ms: int | None = None
    reconnect_count_last_hour: int | None = None
    latency_ms: int | None = None
    error_message: str | None = None
    dirty: bool = False


class HealthProbe:
    """In-process pub-sub for per-target state + heartbeat registry.

    Existing API: update() for HealthTarget state (WS / DB / Redis status).
    Added (D4): record_heartbeat() for sub-task progress timestamps and
    last_active_ts dict keyed by sub-task name.
    Added (Phase 4.3): cell_pipeline_status registry for per-cell
    SIGNAL_PIPELINE state — readable by scan_staleness carve-out (Task 5).
    """

    def __init__(self) -> None:
        self._state: dict[HealthTarget, _TargetState] = {}
        self.last_active_ts: dict[str, datetime] = {}
        # Per-cell SIGNAL_PIPELINE health state for sticky transition logic.
        # Keyed by pair_id (strategy:cell_id) so two strategies on the same
        # cell are independent state machines.
        self.cell_pipeline_status: dict[str, HealthStatus] = {}

    def get_cell_pipeline_status(self, pair_id: str) -> HealthStatus | None:
        """Return current SIGNAL_PIPELINE status for a cell x strategy pair.

        Used by scan_staleness carve-out (Task 5) — SIGNAL_PIPELINE DEGRADED
        with reason=stale_exceeded never escalates to fatal. Keyed by pair_id
        (strategy:cell_id format) since two strategies on same cell are
        independent state machines.
        """
        return self.cell_pipeline_status.get(pair_id)

    def set_cell_pipeline_status(self, pair_id: str, status: HealthStatus) -> None:
        """Update per-cell pipeline status. Called by daemon LOCF emit path."""
        self.cell_pipeline_status[pair_id] = status

    def record_heartbeat(self, sub_task: str) -> None:
        """Sub-task calls this each time it completes a progress unit."""
        self.last_active_ts[sub_task] = datetime.now(UTC)

    def current_status(self, target: HealthTarget) -> HealthStatus | None:
        """Return last-set status for a target, or None if never set.

        Used by daemon code to detect transitions (avoid update spam when
        target stays HEALTHY across many polling ticks).
        """
        state = self._state.get(target)
        return state.status if state is not None else None

    def update(
        self, target: HealthTarget, status: HealthStatus, **fields: Any,
    ) -> None:
        cur = self._state.setdefault(target, _TargetState())
        changed = cur.status != status or any(
            getattr(cur, k, None) != v for k, v in fields.items()
        )
        cur.status = status
        for k, v in fields.items():
            setattr(cur, k, v)
        cur.dirty = changed

    def snapshot(self) -> dict[HealthTarget, _TargetState]:
        return self._state

    def drain_dirty(self) -> dict[HealthTarget, _TargetState]:
        out = {t: s for t, s in self._state.items() if s.dirty}
        for s in out.values():
            s.dirty = False
        return out
