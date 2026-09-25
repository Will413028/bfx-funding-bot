"""Automatic protections: tripping, the supervised kill, and what never trips.

The replay section feeds the five production divergences of 2026-08-27 ..
2026-09-24 (all ``released=0 claimed=0 failed=0 reserved_drift=0``) through the
same classifier the recovery uses; none may halt.
"""
from __future__ import annotations

import asyncio
from decimal import Decimal

import pytest

from bfx_funding_bot.modules.execution.event_store.store import SymbolLedgerDelta
from bfx_funding_bot.modules.execution.events import PositionReconciled
from bfx_funding_bot.modules.execution.safety.kill_switch import CancelAllOutcome, KillResult
from bfx_funding_bot.modules.execution.safety.nav_pnl_source import ReconcileNavTracker
from bfx_funding_bot.modules.execution.safety.protection import (
    LOSS_LIMITER,
    SUBMIT_OUTCOME_UNKNOWN,
    WRITER_LOCK_LOST,
    AutomaticProtection,
    LedgerConservation,
    LossLimitMonitor,
    WriterLockWatch,
)
from bfx_funding_bot.modules.execution.safety.trading_state import TradingState

D = Decimal


class FakeKill:
    def __init__(self, failures: int = 0) -> None:
        self.calls: list[tuple[str, str, str]] = []
        self.failures = failures

    async def engage(self, *, cause: str, actor: str, reason: str) -> KillResult:
        self.calls.append((cause, actor, reason))
        if self.failures:
            self.failures -= 1
            raise RuntimeError("database unavailable")
        state = TradingState(id=9, state="HALTED", cause=cause, actor=actor, reason=reason,
                             created_at_ms=1)
        return KillResult(state=state, state_changed=True,
                          cancel_all=(CancelAllOutcome("UST", "acknowledged"),))


class Recorder:
    def __init__(self) -> None:
        self.trips: list[tuple[str, str]] = []

    def trip(self, trigger: str, detail: str) -> None:
        self.trips.append((trigger, detail))


# ------------------------------------------------------------------ tripping


async def test_trip_stops_at_once_and_the_supervised_task_kills_with_cause_auto() -> None:
    protection = AutomaticProtection(clock=lambda: 7)
    kill = FakeKill()
    protection.bind(kill)
    assert protection.pending_reason() is None
    protection.trip(SUBMIT_OUTCOME_UNKNOWN, "cid=1 symbol=fUST")
    assert protection.pending_reason() == "submit_outcome_unknown: cid=1 symbol=fUST"
    stop = asyncio.Event()
    task = asyncio.create_task(protection.run(stop))
    for _ in range(100):
        if kill.calls:
            break
        await asyncio.sleep(0.01)
    stop.set()
    await asyncio.wait_for(task, timeout=5)
    assert kill.calls == [("auto", "auto:submit_outcome_unknown", "submit_outcome_unknown: cid=1 symbol=fUST")]
    assert protection.pending_reason() is None


async def test_trips_queued_before_the_task_runs_become_one_kill() -> None:
    protection = AutomaticProtection()
    kill = FakeKill()
    protection.bind(kill)
    protection.trip(SUBMIT_OUTCOME_UNKNOWN, "a")
    protection.trip(LOSS_LIMITER, "b")
    await protection.run_pending()
    assert len(kill.calls) == 1
    cause, actor, reason = kill.calls[0]
    assert (cause, actor) == ("auto", "auto:submit_outcome_unknown")
    assert "loss_limiter: b" in reason


async def test_a_failed_kill_keeps_new_offers_stopped_and_is_retried() -> None:
    protection = AutomaticProtection(retry_s=0.01)
    kill = FakeKill(failures=2)
    protection.bind(kill)
    protection.trip(WRITER_LOCK_LOST, "lost")
    stop = asyncio.Event()
    task = asyncio.create_task(protection.run(stop))
    for _ in range(300):
        if len(kill.calls) == 3:
            break
        assert protection.pending_reason() is not None
        await asyncio.sleep(0.01)
    stop.set()
    await asyncio.wait_for(task, timeout=5)
    assert len(kill.calls) == 3
    assert protection.pending_reason() is None


async def test_an_unbound_protection_still_stops_new_offers() -> None:
    protection = AutomaticProtection()
    protection.trip("not_a_known_trigger", "programming error")
    await protection.run_pending()
    assert protection.pending_reason() is not None


# ------------------------------------------------------ ledger conservation


def _delta(symbol: str, prior_offered: str, prior_lent: str, observed_offered: str,
           observed_lent: str, baseline: bool = True) -> SymbolLedgerDelta:
    return SymbolLedgerDelta(symbol, D(prior_offered), D(prior_lent), D(observed_offered),
                             D(observed_lent), baseline)


def test_replay_2026_09_24_canary_loan_end_does_not_halt() -> None:
    """realized_drift=150.77638588 reserved_drift=0: the canary's 150.78 loan ended."""
    conservation = LedgerConservation()
    assert conservation.observe([_delta("fUST", "0", "150.77638588", "0", "0")],
                                confirmed=True) == []


@pytest.mark.parametrize("when", ["2026-08-27 12:14", "2026-08-27 14:51",
                                  "2026-08-28 14:32", "2026-08-30 16:14"])
@pytest.mark.parametrize("reading", ["returned", "first_observation"])
def test_replay_2026_08_migration_catch_ups_do_not_halt(when: str, reading: str) -> None:
    """realized_drift=392.x reserved_drift=0 during the account migration.

    The logged drift is an absolute value, so both readings of "the whole
    balance caught up" are replayed: lent returning to the wallet, and the
    first venue observation of a ledger the migration re-keyed (no baseline).
    """
    conservation = LedgerConservation()
    delta = (_delta("fUST", "0", "392.4", "0", "0") if reading == "returned"
             else _delta("fUST", "0", "0", "0", "392.4", baseline=False))
    assert conservation.observe([delta], confirmed=True) == []


@pytest.mark.parametrize(("delta", "why"), [
    (_delta("fUST", "150", "0", "0", "150"), "fill caught by reconcile instead of WS"),
    (_delta("fUST", "150", "0", "50", "100"), "partial fill caught by reconcile"),
    (_delta("fUST", "150", "0", "0", "0"), "cancel or expiry caught by reconcile"),
    (_delta("fUST", "150", "300", "0", "250"), "fill and a loan end in one interval"),
    (_delta("fUST", "0", "100", "0", "100.005"), "within epsilon"),
])
def test_expected_catch_ups_never_trip(delta: SymbolLedgerDelta, why: str) -> None:
    assert LedgerConservation().observe([delta], confirmed=True) == [], why


def test_lent_growth_our_offers_cannot_explain_trips() -> None:
    anomalies = LedgerConservation().observe(
        [_delta("fUST", "100", "0", "0", "492.4"), _delta("fUSD", "0", "0", "0", "0")],
        confirmed=True)
    assert len(anomalies) == 1
    assert "symbol=fUST" in anomalies[0] and "unexplained=392.4" in anomalies[0]


def test_a_double_counted_fill_in_an_unconfirmed_snapshot_cancels_out() -> None:
    """Offers fetched before a fill and credits after it count the money twice;
    such a snapshot is never confirmed, and carrying its delta to the next
    confirmed one nets the artefact out."""
    conservation = LedgerConservation()
    assert conservation.observe([_delta("fUST", "150", "0", "150", "150")], confirmed=False) == []
    assert conservation.observe([_delta("fUST", "150", "150", "0", "150")], confirmed=True) == []


def test_an_anomaly_absorbed_by_an_unconfirmed_snapshot_is_still_caught() -> None:
    conservation = LedgerConservation()
    assert conservation.observe([_delta("fUST", "0", "0", "0", "392.4")], confirmed=False) == []
    # The unconfirmed snapshot already reset the ledger to 392.4.
    anomalies = conservation.observe([_delta("fUST", "0", "392.4", "0", "392.4")], confirmed=True)
    assert anomalies and "unexplained=392.4" in anomalies[0]


def test_an_unconfirmed_snapshot_alone_never_trips() -> None:
    assert LedgerConservation().observe([_delta("fUST", "0", "0", "0", "392.4")],
                                        confirmed=False) == []


# ------------------------------------------------------------ loss limiter


def _reconciled(symbol: str, nav: str, at: int) -> PositionReconciled:
    return PositionReconciled(account_id="acct", symbol=symbol, reserved=D("0"), realized=D("0"),
                              available=D(nav), n_offers=0, n_credits=0, occurred_at_ms=at)


async def test_loss_limiter_trips_once_per_breach_and_isolates_currencies() -> None:
    recorder = Recorder()
    monitor = LossLimitMonitor(source=ReconcileNavTracker("acct"), protection=recorder,
                               realized_loss_threshold_pct=5.0, drawdown_threshold_pct=10.0)
    await monitor.on_position_reconciled(_reconciled("fUST", "1000", 1))
    await monitor.on_position_reconciled(_reconciled("fUSD", "1000", 1))
    await monitor.on_position_reconciled(_reconciled("fUST", "960", 2))  # 4% < 5%
    assert recorder.trips == []
    await monitor.on_position_reconciled(_reconciled("fUST", "940", 3))  # 6% > 5%
    # Tripped on the very event that crossed: the tracker updated first.
    assert [trigger for trigger, _ in recorder.trips] == [LOSS_LIMITER]
    await monitor.on_position_reconciled(_reconciled("fUST", "939", 4))  # same breach
    assert [trigger for trigger, _ in recorder.trips] == [LOSS_LIMITER]
    assert "realized_loss_pct_24h[fUST]" in recorder.trips[0][1]
    await monitor.on_position_reconciled(_reconciled("fUSD", "999", 5))  # unrelated currency
    assert len(recorder.trips) == 1


async def test_loss_limiter_disabled_thresholds_never_trip() -> None:
    recorder = Recorder()
    monitor = LossLimitMonitor(source=ReconcileNavTracker("acct"), protection=recorder,
                               realized_loss_threshold_pct=None, drawdown_threshold_pct=None)
    await monitor.on_position_reconciled(_reconciled("fUST", "1000", 1))
    await monitor.on_position_reconciled(_reconciled("fUST", "1", 2))
    assert recorder.trips == []


# -------------------------------------------------------------- writer lock


class FakeLock:
    def __init__(self, results: list[bool]) -> None:
        self.results = results

    async def refresh(self) -> bool:
        return self.results.pop(0)


async def test_writer_lock_trips_only_when_not_held_after_refresh() -> None:
    recorder = Recorder()
    watch = WriterLockWatch(lock=FakeLock([True, False, False, True, False]), protection=recorder)
    assert await watch.check() is True
    assert await watch.check() is False
    assert await watch.check() is False  # still lost: one trip per loss
    assert await watch.check() is True
    assert await watch.check() is False
    assert [trigger for trigger, _ in recorder.trips] == [WRITER_LOCK_LOST, WRITER_LOCK_LOST]
